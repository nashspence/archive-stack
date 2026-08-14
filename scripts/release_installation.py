"""Generate Riverhog's authoritative independent-client installation artifacts."""

from __future__ import annotations

import gzip
import hashlib
import html
import io
import json
import re
import shlex
import subprocess
import tarfile
import tempfile
import tomllib
import urllib.parse
from collections.abc import Sequence
from pathlib import Path, PurePosixPath
from typing import Any, Protocol, cast

from gogurt.listener import listener_release_contract
from packaging.markers import Marker, default_environment
from packaging.tags import Tag, compatible_tags, cpython_tags, mac_platforms
from packaging.utils import InvalidWheelFilename, parse_wheel_filename

INSTALLATION_SCHEMA = "riverhog-installation/v1"
END_USER_ROOTS = (
    "gogurt",
    "jeb-client",
    "munchy-client",
    "riverhog-client",
    "riverhog-recover",
)
SUPPORTED_PLATFORMS = ("linux-x64", "macos-arm64", "windows-x64")
INSTALLATION_POLICY = {
    "method": "uv-tool",
    "lock_format": "pylock.toml",
    "managed_python": True,
    "wheel_only": True,
    "simple_index_path": "artifacts/v{version}/simple/",
    "listener": {
        "root": "gogurt",
        "scope": "current-user",
        "resume": "next-login",
        "autorun": "explicit-required",
        "service_managers": ["systemd-user", "launchd-user", "task-scheduler-user"],
    },
}


class InstallationError(RuntimeError):
    """The generated installation projection is incomplete or inconsistent."""


class ProjectLike(Protocol):
    @property
    def name(self) -> str: ...

    @property
    def path(self) -> str: ...

    @property
    def role(self) -> str: ...

    @property
    def version(self) -> str: ...


def normalize_name(value: str) -> str:
    return re.sub(r"[-_.]+", "-", value).lower()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _tool_version(root: Path, name: str) -> str:
    lock = tomllib.loads((root / "mise.lock").read_text(encoding="utf-8"))
    entries = lock.get("tools", {}).get(name)
    if not isinstance(entries, list) or len(entries) != 1:
        raise InstallationError(f"mise.lock must contain one {name} tool")
    entry = entries[0]
    version = entry.get("version")
    if not isinstance(version, str) or not version:
        raise InstallationError(f"mise.lock has no exact {name} version")
    for platform in SUPPORTED_PLATFORMS:
        artifact = entry.get(f"platforms.{platform}")
        if not isinstance(artifact, dict):
            raise InstallationError(f"mise.lock has no {name} artifact for {platform}")
        if re.fullmatch(r"sha256:[0-9a-f]{64}", str(artifact.get("checksum", ""))) is None:
            raise InstallationError(f"mise.lock has no verified {name} checksum for {platform}")
    return version


def qualified_tool_versions(root: Path) -> dict[str, str]:
    """Return versions only from the cross-platform mise lock authority."""

    versions = {name: _tool_version(root, name) for name in ("python", "uv")}
    mise = tomllib.loads((root / "mise.toml").read_text(encoding="utf-8"))["tools"]
    if any(str(mise[name]) != version for name, version in versions.items()):
        raise InstallationError("mise.toml and mise.lock differ for an installation tool")
    return versions


def installation_roots(projects: Sequence[ProjectLike]) -> list[ProjectLike]:
    roots = sorted(
        (project for project in projects if project.role == "end_user_artifact"),
        key=lambda project: project.name,
    )
    if tuple(project.name for project in roots) != END_USER_ROOTS:
        raise InstallationError("end-user release roots differ from the v1 installation contract")
    return roots


def project_dependency_graph(root: Path, projects: Sequence[ProjectLike]) -> dict[str, set[str]]:
    names = {project.name for project in projects}
    graph: dict[str, set[str]] = {}
    for project in projects:
        metadata = tomllib.loads(
            (root / project.path / "pyproject.toml").read_text(encoding="utf-8")
        )["project"]
        dependencies: set[str] = set()
        for value in metadata.get("dependencies", []):
            match = re.match(r"[A-Za-z0-9_.-]+", str(value))
            if match is None:
                raise InstallationError(f"{project.name} has an invalid dependency")
            dependencies.add(normalize_name(match.group()))
        graph[project.name] = dependencies & names
    return graph


def dependency_closure(graph: dict[str, set[str]], root: str) -> set[str]:
    pending = [root]
    result: set[str] = set()
    while pending:
        name = pending.pop()
        if name in result:
            continue
        result.add(name)
        pending.extend(graph[name] - result)
    return result


def _entry_points(root: Path, project: ProjectLike) -> list[str]:
    metadata = tomllib.loads((root / project.path / "pyproject.toml").read_text(encoding="utf-8"))[
        "project"
    ]
    scripts = metadata.get("scripts")
    if not isinstance(scripts, dict) or not scripts:
        raise InstallationError(f"{project.name} exposes no project.scripts entry point")
    return sorted(str(name) for name in scripts)


def _split_lock(text: str) -> tuple[str, dict[str, str]]:
    marker = "[[packages]]"
    header, separator, remainder = text.partition(marker)
    if not separator:
        return text.rstrip() + "\n", {}
    blocks: dict[str, str] = {}
    for raw in (marker + remainder).split(marker):
        if not raw.strip():
            continue
        block = marker + raw
        match = re.search(r'(?m)^name = "([^"]+)"$', block)
        if match is None:
            raise InstallationError("uv emitted a PEP 751 package without a name")
        name = normalize_name(match.group(1))
        if name in blocks:
            raise InstallationError(f"uv emitted a repeated PEP 751 package: {name}")
        blocks[name] = block.strip() + "\n"
    return header.rstrip() + "\n", blocks


def _internal_lock_block(
    *,
    name: str,
    version: str,
    index_url: str,
    wheel_url: str,
    wheel_size: int,
    wheel_sha256: str,
) -> str:
    return (
        "[[packages]]\n"
        f"name = {json.dumps(name)}\n"
        f"version = {json.dumps(version)}\n"
        f"index = {json.dumps(index_url)}\n"
        "wheels = [{ "
        f"url = {json.dumps(wheel_url)}, size = {wheel_size}, "
        f"hashes = {{ sha256 = {json.dumps(wheel_sha256)} }}"
        " }]\n"
    )


def _export_external_lock(root: Path, package: str, uv_version: str) -> tuple[str, dict[str, str]]:
    with tempfile.TemporaryDirectory(prefix="riverhog-pylock.") as temporary:
        path = Path(temporary) / f"pylock.{package}.toml"
        subprocess.run(
            [
                "uv",
                "export",
                "--frozen",
                "--package",
                package,
                "--no-dev",
                "--no-editable",
                "--no-emit-local",
                "--no-header",
                "--format",
                "pylock.toml",
                "--output-file",
                str(path),
            ],
            cwd=root,
            check=True,
            stdout=subprocess.DEVNULL,
        )
        header, blocks = _split_lock(path.read_text(encoding="utf-8"))
    header = re.sub(
        r'(?m)^created-by = "[^"]+"$',
        f'created-by = "Riverhog release.py with uv {uv_version}"',
        header,
    )
    return header, blocks


def _write_component_lock(
    root: Path,
    destination: Path,
    *,
    package: str,
    closure: set[str],
    project_versions: dict[str, str],
    wheels: dict[str, dict[str, Any]],
    index_url: str,
    asset_base_url: str,
    uv_version: str,
) -> list[dict[str, Any]]:
    header, blocks = _export_external_lock(root, package, uv_version)
    overlap = set(blocks) & set(project_versions)
    if overlap:
        raise InstallationError(f"workspace identities escaped uv export: {sorted(overlap)}")
    components: list[dict[str, Any]] = []
    for name in sorted(closure):
        wheel = wheels[name]
        blocks[name] = _internal_lock_block(
            name=name,
            version=project_versions[name],
            index_url=index_url,
            wheel_url=asset_base_url + str(wheel["asset"]),
            wheel_size=int(wheel["size"]),
            wheel_sha256=str(wheel["sha256"]),
        )
    for name, block in blocks.items():
        parsed = tomllib.loads(block)
        package_item = parsed["packages"][0]
        components.append(
            {
                "name": name,
                "version": str(package_item["version"]),
                "first_party": name in closure,
            }
        )
    destination.parent.mkdir(parents=True, exist_ok=True)
    rendered_blocks = "\n".join(blocks[name].rstrip() for name in sorted(blocks))
    destination.write_text(
        header.rstrip() + "\n\n" + rendered_blocks + "\n",
        encoding="utf-8",
    )
    parsed_lock = tomllib.loads(destination.read_text(encoding="utf-8"))
    if parsed_lock.get("lock-version") != "1.0":
        raise InstallationError(f"generated lock is not PEP 751 v1: {destination.name}")
    return sorted(components, key=lambda item: str(item["name"]))


def _marker_environment(platform: str, python_version: str) -> dict[str, str]:
    environment = cast(dict[str, str], dict(default_environment()))
    environment.update(
        {
            "implementation_name": "cpython",
            "implementation_version": python_version,
            "python_full_version": python_version,
            "python_version": ".".join(python_version.split(".")[:2]),
        }
    )
    if platform == "linux-x64":
        environment.update(
            {
                "os_name": "posix",
                "platform_machine": "x86_64",
                "platform_system": "Linux",
                "sys_platform": "linux",
            }
        )
    elif platform == "macos-arm64":
        environment.update(
            {
                "os_name": "posix",
                "platform_machine": "arm64",
                "platform_system": "Darwin",
                "sys_platform": "darwin",
            }
        )
    elif platform == "windows-x64":
        environment.update(
            {
                "os_name": "nt",
                "platform_machine": "AMD64",
                "platform_system": "Windows",
                "sys_platform": "win32",
            }
        )
    else:  # pragma: no cover - guarded by the release platform contract
        raise InstallationError(f"unsupported installation platform: {platform}")
    return environment


def _platform_tags(platform: str) -> list[Tag]:
    if platform == "linux-x64":
        platform_tags = [
            *(f"manylinux_2_{minor}_x86_64" for minor in range(39, 16, -1)),
            "manylinux2014_x86_64",
            "manylinux2010_x86_64",
            "manylinux1_x86_64",
            "linux_x86_64",
        ]
    elif platform == "macos-arm64":
        platform_tags = list(mac_platforms(version=(15, 0), arch="arm64"))
    elif platform == "windows-x64":
        platform_tags = ["win_amd64"]
    else:  # pragma: no cover - guarded by the release platform contract
        raise InstallationError(f"unsupported installation platform: {platform}")
    return [
        *cpython_tags(
            python_version=(3, 12),
            abis=["cp312", "abi3", "none"],
            platforms=platform_tags,
        ),
        *compatible_tags(
            python_version=(3, 12),
            interpreter="cp312",
            platforms=platform_tags,
        ),
    ]


def _platform_requirements(
    lock_path: Path,
    *,
    platform: str,
    python_version: str,
) -> list[dict[str, str]]:
    lock = tomllib.loads(lock_path.read_text(encoding="utf-8"))
    environment = _marker_environment(platform, python_version)
    supported = {tag: rank for rank, tag in enumerate(_platform_tags(platform))}
    requirements: list[dict[str, str]] = []
    for package in lock["packages"]:
        marker = package.get("marker")
        if marker is not None and not Marker(str(marker)).evaluate(environment=environment):
            continue
        candidates: list[tuple[int, dict[str, Any]]] = []
        for wheel in package.get("wheels", []):
            url = str(wheel["url"])
            filename = PurePosixPath(urllib.parse.urlsplit(url).path).name
            try:
                _name, _version, _build, tags = parse_wheel_filename(filename)
            except InvalidWheelFilename as exc:
                raise InstallationError(
                    f"PEP 751 lock contains an invalid wheel: {filename}"
                ) from exc
            ranks = [supported[tag] for tag in tags if tag in supported]
            if ranks:
                candidates.append((min(ranks), wheel))
        if not candidates:
            raise InstallationError(
                f"PEP 751 lock has no {platform} CPython 3.12 wheel for {package['name']}"
            )
        wheel = min(candidates, key=lambda item: item[0])[1]
        digest = str(wheel.get("hashes", {}).get("sha256", ""))
        if re.fullmatch(r"[0-9a-f]{64}", digest) is None:
            raise InstallationError(f"PEP 751 wheel has no SHA-256: {package['name']}")
        url = str(wheel["url"])
        requirements.append(
            {
                "name": normalize_name(str(package["name"])),
                "version": str(package["version"]),
                "url": url,
                "sha256": digest,
                "requirement": (f"{normalize_name(str(package['name']))} @ {url}#sha256={digest}"),
            }
        )
    return sorted(requirements, key=lambda item: item["name"])


def _simple_project_page(name: str, wheel: dict[str, Any], asset_base_url: str) -> bytes:
    href = html.escape(
        asset_base_url + str(wheel["asset"]) + "#sha256=" + str(wheel["sha256"]),
        quote=True,
    )
    filename = html.escape(str(wheel["asset"]))
    return (
        '<!DOCTYPE html>\n<html><head><meta name="pypi:repository-version" '
        'content="1.0"></head><body>\n'
        f'<a href="{href}" data-requires-python="&gt;=3.12">{filename}</a>\n'
        "</body></html>\n"
    ).encode()


def _simple_root_page(names: set[str]) -> bytes:
    links = "".join(
        f'<a href="{html.escape(normalize_name(name))}/">{html.escape(name)}</a>\n'
        for name in sorted(names)
    )
    return (
        '<!DOCTYPE html>\n<html><head><meta name="pypi:repository-version" '
        f'content="1.0"></head><body>\n{links}</body></html>\n'
    ).encode()


def _tar_info(
    name: str, *, size: int, source_epoch: int, directory: bool = False
) -> tarfile.TarInfo:
    info = tarfile.TarInfo(name.rstrip("/") + ("/" if directory else ""))
    info.type = tarfile.DIRTYPE if directory else tarfile.REGTYPE
    info.size = 0 if directory else size
    info.mode = 0o755 if directory else 0o644
    info.uid = info.gid = 0
    info.uname = info.gname = "root"
    info.mtime = source_epoch
    return info


def write_index_snapshot(
    destination: Path,
    *,
    simple_index_path: str,
    names: set[str],
    wheels: dict[str, dict[str, Any]],
    asset_base_url: str,
    source_epoch: int,
) -> dict[str, bytes]:
    prefix = PurePosixPath(simple_index_path)
    files = {str(prefix / "index.html"): _simple_root_page(names)}
    for name in sorted(names):
        files[str(prefix / normalize_name(name) / "index.html")] = _simple_project_page(
            name, wheels[name], asset_base_url
        )
    directories = {str(PurePosixPath(path).parent) for path in files}
    directories |= {
        str(parent)
        for path in list(directories)
        for parent in PurePosixPath(path).parents
        if str(parent) != "."
    }
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open("wb") as raw:
        with gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=source_epoch) as compressed:
            with tarfile.open(fileobj=compressed, mode="w", format=tarfile.PAX_FORMAT) as archive:
                for directory in sorted(directories, key=lambda value: (value.count("/"), value)):
                    archive.addfile(
                        _tar_info(
                            directory,
                            size=0,
                            source_epoch=source_epoch,
                            directory=True,
                        )
                    )
                for path, content in sorted(files.items()):
                    archive.addfile(
                        _tar_info(path, size=len(content), source_epoch=source_epoch),
                        io.BytesIO(content),
                    )
    return files


def _posix_commands(
    *,
    package: str,
    version: str,
    lock_name: str,
    lock_url: str,
    index_url: str,
    python: str,
    requirements: list[str],
) -> list[str]:
    install = [
        "uv",
        "--no-config",
        "tool",
        "install",
        f"{package}=={version}",
        "--index",
        index_url,
        "--default-index",
        "https://pypi.org/simple",
        "--index-strategy",
        "first-index",
        "--python",
        python,
        "--managed-python",
        "--no-build",
    ]
    for requirement in requirements:
        install.extend(("--with", requirement))
    download = (
        "curl --fail --location --proto '=https' --tlsv1.2 "
        f"--output {shlex.quote(lock_name)} {shlex.quote(lock_url)}"
    )
    tool_python = f'"$(uv --no-config tool dir)/{package}/bin/python"'
    verify = shlex.join(
        [
            "uv",
            "--no-config",
            "pip",
            "sync",
            lock_name,
            "--python",
        ]
    )
    verify += f" {tool_python} --dry-run --strict --no-build"
    return [
        download,
        shlex.join(install),
        verify,
        shlex.join([*install, "--reinstall"]),
        shlex.join(["uv", "--no-config", "tool", "uninstall", package]),
    ]


def _powershell_quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _windows_commands(
    *,
    package: str,
    version: str,
    lock_name: str,
    lock_url: str,
    index_url: str,
    python: str,
    requirements: list[str],
) -> list[str]:
    install = [
        "uv",
        "--no-config",
        "tool",
        "install",
        f"{package}=={version}",
        "--index",
        index_url,
        "--default-index",
        "https://pypi.org/simple",
        "--index-strategy",
        "first-index",
        "--python",
        python,
        "--managed-python",
        "--no-build",
    ]
    for requirement in requirements:
        install.extend(("--with", requirement))

    def render(values: list[str]) -> str:
        return " ".join(_powershell_quote(value) for value in values)

    download = (
        f"Invoke-WebRequest -Uri {_powershell_quote(lock_url)} "
        f"-OutFile {_powershell_quote(lock_name)}"
    )
    tool_relative = package + "\\Scripts\\python.exe"
    verify = (
        f"$toolPython = Join-Path (uv --no-config tool dir) "
        f"{_powershell_quote(tool_relative)}; "
        f"uv --no-config pip sync {_powershell_quote(lock_name)} "
        "--python $toolPython --dry-run --strict --no-build"
    )
    return [
        download,
        render(install),
        verify,
        render([*install, "--reinstall"]),
        render(["uv", "--no-config", "tool", "uninstall", package]),
    ]


def build_installation_artifacts(
    root: Path,
    output: Path,
    projects: Sequence[ProjectLike],
    wheel_records: list[dict[str, Any]],
    *,
    version: str,
    source_sha: str,
    source_epoch: int,
    repository: str,
    simple_index_path: str,
    asset_base_url: str | None = None,
    index_base_url: str | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Write locks, index snapshot, and manifest; return manifest and evidence subjects."""

    roots = installation_roots(projects)
    graph = project_dependency_graph(root, projects)
    project_by_name = {project.name: project for project in projects}
    project_versions = {project.name: project.version for project in projects}
    tools = qualified_tool_versions(root)
    tag = f"v{version}"
    release_base = asset_base_url or (f"https://github.com/{repository}/releases/download/{tag}/")
    normalized_path = simple_index_path.format(version=version).strip("/") + "/"
    owner, repository_name = repository.split("/", 1)
    index_url = index_base_url or f"https://{owner}.github.io/{repository_name}/{normalized_path}"

    wheels: dict[str, dict[str, Any]] = {}
    for record in wheel_records:
        if record.get("kind") != "wheel":
            continue
        distribution = str(record["distribution"])
        wheels[distribution] = {
            "asset": Path(str(record["name"])).name,
            "sha256": str(record["sha256"]),
            "size": int(record["size"]),
        }
    required_names = set().union(*(dependency_closure(graph, item.name) for item in roots))
    if set(wheels) != set(project_by_name):
        raise InstallationError("release wheel records differ from the coordinated project graph")

    installation_dir = output / "installation"
    installation_dir.mkdir(parents=True, exist_ok=True)
    component_items: list[dict[str, Any]] = []
    records: list[dict[str, Any]] = []
    for project in roots:
        closure = dependency_closure(graph, project.name)
        lock_name = f"pylock.{project.name}.toml"
        lock_path = installation_dir / lock_name
        packages = _write_component_lock(
            root,
            lock_path,
            package=project.name,
            closure=closure,
            project_versions=project_versions,
            wheels=wheels,
            index_url=index_url,
            asset_base_url=release_base,
            uv_version=tools["uv"],
        )
        lock_relative = lock_path.relative_to(output).as_posix()
        lock_sha256 = sha256_file(lock_path)
        lock_url = release_base + lock_name
        platform_requirements = {
            platform: _platform_requirements(
                lock_path,
                platform=platform,
                python_version=tools["python"],
            )
            for platform in SUPPORTED_PLATFORMS
        }
        commands = {
            "linux-x64": _posix_commands(
                package=project.name,
                version=version,
                lock_name=lock_name,
                lock_url=lock_url,
                index_url=index_url,
                python=tools["python"],
                requirements=[
                    item["requirement"]
                    for item in platform_requirements["linux-x64"]
                    if item["name"] != project.name
                ],
            ),
            "macos-arm64": _posix_commands(
                package=project.name,
                version=version,
                lock_name=lock_name,
                lock_url=lock_url,
                index_url=index_url,
                python=tools["python"],
                requirements=[
                    item["requirement"]
                    for item in platform_requirements["macos-arm64"]
                    if item["name"] != project.name
                ],
            ),
            "windows-x64": _windows_commands(
                package=project.name,
                version=version,
                lock_name=lock_name,
                lock_url=lock_url,
                index_url=index_url,
                python=tools["python"],
                requirements=[
                    item["requirement"]
                    for item in platform_requirements["windows-x64"]
                    if item["name"] != project.name
                ],
            ),
        }
        component_items.append(
            {
                "root": project.name,
                "entry_points": _entry_points(root, project),
                "first_party_closure": [
                    {"name": name, "version": project_versions[name]} for name in sorted(closure)
                ],
                "resolved_packages": packages,
                "platform_requirements": platform_requirements,
                "lock": {
                    "asset": lock_name,
                    "path": lock_relative,
                    "url": lock_url,
                    "sha256": lock_sha256,
                },
                "commands": commands,
            }
        )
        records.append(
            {
                "kind": "install-lock",
                "name": lock_relative,
                "sha256": lock_sha256,
                "size": lock_path.stat().st_size,
                "distribution": project.name,
                "version": version,
                "license": "NOASSERTION",
                "dependencies": [
                    {"name": name, "version": project_versions[name]} for name in sorted(closure)
                ],
                "_components": [
                    {
                        "kind": "python",
                        "name": str(package["name"]),
                        "version": str(package["version"]),
                        "license": "NOASSERTION",
                    }
                    for package in packages
                ],
            }
        )

    snapshot_name = f"riverhog-python-index-v{version}.tar.gz"
    snapshot_path = installation_dir / snapshot_name
    write_index_snapshot(
        snapshot_path,
        simple_index_path=normalized_path,
        names=required_names,
        wheels=wheels,
        asset_base_url=release_base,
        source_epoch=source_epoch,
    )
    snapshot_relative = snapshot_path.relative_to(output).as_posix()
    snapshot_sha256 = sha256_file(snapshot_path)
    records.append(
        {
            "kind": "install-index",
            "name": snapshot_relative,
            "sha256": snapshot_sha256,
            "size": snapshot_path.stat().st_size,
            "version": version,
            "license": "NOASSERTION",
            "dependencies": [
                {"name": name, "version": project_versions[name]} for name in sorted(required_names)
            ],
            "_components": [
                {
                    "kind": "python",
                    "name": name,
                    "version": project_versions[name],
                    "license": "NOASSERTION",
                }
                for name in sorted(required_names)
            ],
        }
    )
    listener_contract = listener_release_contract()
    listener_reference_name = f"gogurt-listener-v{version}.md"
    listener_reference_path = installation_dir / listener_reference_name
    listener_reference_path.write_text(
        _render_listener_reference(listener_contract, version=version),
        encoding="utf-8",
    )
    listener_reference_relative = listener_reference_path.relative_to(output).as_posix()
    listener_reference_sha256 = sha256_file(listener_reference_path)
    records.append(
        {
            "kind": "install-reference",
            "name": listener_reference_relative,
            "sha256": listener_reference_sha256,
            "size": listener_reference_path.stat().st_size,
            "distribution": "gogurt",
            "version": version,
            "license": "Apache-2.0",
            "dependencies": [],
        }
    )
    manifest = {
        "schema": INSTALLATION_SCHEMA,
        "version": version,
        "tag": tag,
        "source_sha": source_sha,
        "method": "uv-tool",
        "lock_enforcement": "direct-hashed-wheels-with-pep751-sync-verification",
        "wheel_only": True,
        "platforms": list(SUPPORTED_PLATFORMS),
        "toolchain": {
            "uv": tools["uv"],
            "python": tools["python"],
            "python_provider": "uv-managed-cpython",
        },
        "index": {
            "url": index_url,
            "path": normalized_path,
            "snapshot_asset": snapshot_name,
            "snapshot_path": snapshot_relative,
            "snapshot_sha256": snapshot_sha256,
            "first_party_projects": sorted(required_names),
        },
        "wheels": {name: wheels[name] for name in sorted(required_names)},
        "components": component_items,
        "gogurt_listener": {
            "contract": listener_contract,
            "reference_asset": listener_reference_name,
            "reference_path": listener_reference_relative,
            "reference_sha256": listener_reference_sha256,
        },
    }
    manifest_path = output / "install-manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return manifest, records


def _render_listener_reference(contract: dict[str, object], *, version: str) -> str:
    platforms = cast(dict[str, str], contract["platforms"])
    dispatch = cast(dict[str, str], contract["dispatch"])
    health = cast(dict[str, str], contract["health"])
    state = cast(dict[str, str], contract["state"])
    return (
        f"# Gogurt listener v{version}\n\n"
        "Gogurt installs a current-user listener. It resumes at the user's next login; "
        "it is not a pre-login system service. Installation requires explicit `--autorun`.\n\n"
        "## Install and operate\n\n"
        "```console\n"
        "gogurt listener install --config /absolute/path/gogurt-routes.yaml "
        "--actions-dir /absolute/path/actions --autorun\n"
        "gogurt listener status\n"
        "gogurt listener status --json\n"
        "gogurt listener start\n"
        "gogurt listener stop\n"
        "gogurt listener restart\n"
        "gogurt listener uninstall\n"
        "```\n\n"
        "The registration binds the absolute executable from the independent Gogurt "
        "installation. It does not depend on shell `PATH`, system Python, administrator "
        "access, or another Riverhog component's runtime.\n\n"
        "## Platform registration\n\n"
        + "".join(f"- `{platform}`: `{manager}`\n" for platform, manager in platforms.items())
        + "\n## Dispatch and troubleshooting\n\n"
        "`gogurt listener status --json` is the authoritative health and durable-dispatch "
        "diagnostic. `healthy` confirms a current versioned heartbeat and valid global "
        "configuration; `stopped` means the registration remains installed but is not "
        "running; `stale` or `failed` requires operator attention. A global configuration "
        "failure prevents dispatch and appears in `diagnostic`; bounded per-volume input or "
        "I/O problems appear in `mount_attention` without stopping other volumes. The "
        "`dispatches.attention` rows expose retry, uncertain, and failed actions.\n\n"
        f"- Healthy contract: `{health['healthy']}`.\n"
        f"- Failed contract: `{health['failed']}`.\n"
        f"- Mount attention: `{health['mount_attention']}`.\n"
        f"- Completed observations: `{dispatch['completed']}`.\n"
        f"- Crash-window actions: `{dispatch['running_after_crash']}`.\n"
        f"- Known failures: `{dispatch['known_failure']}`.\n"
        f"- Replay boundary: `{dispatch['downstream']}`.\n\n"
        f"Replacement contract: `{contract['replacement']}`. POSIX state is "
        f"`{state['posix']}`; Windows state uses `{state['windows']}`.\n\n"
        "Use `restart` for a stale process after reviewing its diagnostic. Use `uninstall` "
        "to remove the native registration, listener database, heartbeat, lock, and bounded "
        "logs. Reinstalling the same version is replacement-safe and retains completed "
        "dispatch history.\n"
    )


def verify_installation_artifacts(output: Path, manifest: dict[str, Any]) -> None:
    """Verify positive parity between the manifest and every generated artifact."""

    if manifest.get("schema") != INSTALLATION_SCHEMA:
        raise InstallationError("install manifest uses another schema")
    if tuple(manifest.get("platforms", [])) != SUPPORTED_PLATFORMS:
        raise InstallationError("install manifest platform support differs from v1")
    components = manifest.get("components")
    if not isinstance(components, list):
        raise InstallationError("install manifest components are not a list")
    if tuple(item.get("root") for item in components) != END_USER_ROOTS:
        raise InstallationError("install manifest roots differ from v1")
    wheel_names = set(manifest.get("wheels", {}))
    if wheel_names != set(manifest["index"]["first_party_projects"]):
        raise InstallationError("install manifest wheel and index inventories differ")
    for component in components:
        lock = component["lock"]
        path = output / str(lock["path"])
        if not path.is_file() or sha256_file(path) != lock["sha256"]:
            raise InstallationError(f"component lock does not verify: {component['root']}")
        parsed = tomllib.loads(path.read_text(encoding="utf-8"))
        locked = {
            normalize_name(str(item["name"])): str(item["version"]) for item in parsed["packages"]
        }
        expected = {
            str(item["name"]): str(item["version"]) for item in component["resolved_packages"]
        }
        if locked != expected:
            raise InstallationError(f"component lock inventory differs: {component['root']}")
        for platform in SUPPORTED_PLATFORMS:
            commands = component["commands"].get(platform)
            if not isinstance(commands, list) or len(commands) != 5:
                raise InstallationError(f"component commands are incomplete: {component['root']}")
    snapshot = output / str(manifest["index"]["snapshot_path"])
    if not snapshot.is_file() or sha256_file(snapshot) != manifest["index"]["snapshot_sha256"]:
        raise InstallationError("Simple index snapshot does not verify")
    with tarfile.open(snapshot, mode="r:gz") as archive:
        files = {member.name for member in archive.getmembers() if member.isfile()}
    expected_files = {str(PurePosixPath(manifest["index"]["path"]) / "index.html")}
    expected_files |= {
        str(PurePosixPath(manifest["index"]["path"]) / normalize_name(name) / "index.html")
        for name in wheel_names
    }
    if files != expected_files:
        raise InstallationError("Simple index snapshot inventory differs from its manifest")
    listener = manifest.get("gogurt_listener")
    if not isinstance(listener, dict) or listener.get("contract") != listener_release_contract():
        raise InstallationError("Gogurt listener contract differs from its executable")
    reference = output / str(listener["reference_path"])
    if not reference.is_file() or sha256_file(reference) != listener["reference_sha256"]:
        raise InstallationError("Gogurt listener generated reference does not verify")
    reference_text = reference.read_text(encoding="utf-8")
    if any(
        f"gogurt listener {operation}" not in reference_text
        for operation in listener["contract"]["operations"]
    ):
        raise InstallationError("Gogurt listener generated reference omits an operation")
