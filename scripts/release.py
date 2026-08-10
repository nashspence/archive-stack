from __future__ import annotations

import argparse
import io
import json
import os
import re
import subprocess
import tarfile
import tempfile
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

ROOT = Path(__file__).resolve().parents[1]
RELEASE_CONFIG = "release.toml"
RELEASE_SCHEMA = "riverhog-release/v1"
RELEASE_ROLES = (
    "end_user_artifact",
    "deployed_implementation",
    "reusable_library",
    "internal_build_unit",
    "test_only_artifact",
)
PROJECT_README = {
    "text": "Riverhog v1 component. See the project URL for documentation and releases.",
    "content-type": "text/markdown",
}
PROJECT_PEOPLE = [{"name": "Nash Spence"}]
PROJECT_CLASSIFIERS = [
    "Programming Language :: Python :: 3 :: Only",
    "Programming Language :: Python :: 3.12",
    "Typing :: Typed",
]
PROJECT_URLS = {
    "Documentation": "https://nashspence.github.io/riverhog/v1/",
    "Issues": "https://github.com/nashspence/riverhog/issues",
    "Repository": "https://github.com/nashspence/riverhog",
}
RUNTIME_IMAGE_TARGETS = {
    "riverhog": {
        "distribution": "riverhog-server",
        "repository": "ghcr.io/nashspence/riverhog",
    },
    "jeb": {
        "distribution": "jeb-server",
        "repository": "ghcr.io/nashspence/riverhog-jeb",
    },
    "mango-fish": {
        "distribution": "mango-fish",
        "repository": "ghcr.io/nashspence/riverhog-mango-fish",
    },
    "munchy-server": {
        "distribution": "munchy-server",
        "repository": "ghcr.io/nashspence/riverhog-munchy",
    },
    "munchy-av1-nvenc": {
        "distribution": "munchy-av1-nvenc-target",
        "repository": "ghcr.io/nashspence/riverhog-munchy-av1-nvenc",
    },
}
TEST_IMAGE_TARGETS = {"test": {"local_tag": "riverhog-test:dev"}}
VERSION_RE = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+")
PROJECT_VERSION_RE = re.compile(r'(?m)^version = "(?P<version>[^"]+)"$')


@dataclass(frozen=True, slots=True)
class Project:
    name: str
    path: str
    role: str
    version: str
    description: str


class ReleaseError(RuntimeError):
    """The release contract is incomplete or inconsistent."""


def _run(
    command: list[str],
    *,
    cwd: Path,
    capture: bool = False,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    environment = None
    if env is not None:
        environment = os.environ.copy()
        environment.update(env)
    return subprocess.run(
        command,
        cwd=cwd,
        check=True,
        text=True,
        env=environment,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.PIPE if capture else None,
    )


def _git_output(root: Path, *args: str) -> str:
    return _run(["git", *args], cwd=root, capture=True).stdout.strip()


def _normalize_name(value: str) -> str:
    return value.replace("_", "-").lower()


def _version(value: str) -> tuple[int, int, int]:
    if VERSION_RE.fullmatch(value) is None:
        raise ReleaseError(f"release version must be MAJOR.MINOR.PATCH: {value}")
    parsed = tuple(int(part) for part in value.split("."))
    return parsed[0], parsed[1], parsed[2]


def _dependency_range(version: str) -> str:
    major, minor, _ = _version(version)
    if major == 0:
        return f">=0.{minor},<0.{minor + 1}"
    return f">={major}.0,<{major + 1}.0"


def _workspace_pyprojects(root: Path) -> list[Path]:
    workspace = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    result: list[Path] = []
    for pattern in workspace["tool"]["uv"]["workspace"]["members"]:
        for member in root.glob(pattern):
            pyproject = member / "pyproject.toml"
            if not pyproject.is_file():
                raise ReleaseError(f"workspace member has no pyproject.toml: {member}")
            result.append(pyproject)
    return sorted(result)


def _load_config(root: Path) -> dict[str, Any]:
    return tomllib.loads((root / RELEASE_CONFIG).read_text(encoding="utf-8"))


def _project_metadata(path: Path) -> dict[str, Any]:
    return cast(
        dict[str, Any],
        tomllib.loads(path.read_text(encoding="utf-8"))["project"],
    )


def _bake_targets(root: Path) -> set[str]:
    text = (root / "docker-bake.hcl").read_text(encoding="utf-8")
    group = re.search(r'group "default" \{(?P<body>.*?)\n\}', text, re.DOTALL)
    if group is None:
        raise ReleaseError("docker-bake.hcl has no default target group")
    targets = re.search(r"targets\s*=\s*\[(?P<body>.*?)\]", group.group("body"), re.DOTALL)
    if targets is None:
        raise ReleaseError("docker-bake.hcl default group has no targets")
    return set(re.findall(r'"([^"]+)"', targets.group("body")))


def _dependency_name(value: str) -> str:
    match = re.match(r"[A-Za-z0-9_.-]+", value)
    if match is None:
        raise ReleaseError(f"dependency has no distribution name: {value}")
    return _normalize_name(match.group())


def validate_release_contract(root: Path, *, expected_version: str | None = None) -> list[Project]:
    config = _load_config(root)
    if config.get("schema") != RELEASE_SCHEMA:
        raise ReleaseError("release.toml has another schema")
    if config.get("series") != "v1" or config.get("release_branch") != "release/v1":
        raise ReleaseError("release.toml does not describe the v1 release line")
    if config.get("tag_template") != "v{version}":
        raise ReleaseError("release tags must use v{version}")
    if config.get("version_policy") != "coordinated":
        raise ReleaseError("Riverhog requires one coordinated product version")

    workspace = {
        path.parent.relative_to(root).as_posix(): path for path in _workspace_pyprojects(root)
    }
    classified: dict[str, str] = {}
    python_config = config.get("python")
    if not isinstance(python_config, dict) or set(python_config) != set(RELEASE_ROLES):
        raise ReleaseError("release.toml must contain every Python release-unit role")
    for role in RELEASE_ROLES:
        values = python_config[role]
        if not isinstance(values, list) or any(not isinstance(item, str) for item in values):
            raise ReleaseError(f"release role {role} must be a path list")
        for path in values:
            if path in classified:
                raise ReleaseError(f"release unit is classified more than once: {path}")
            classified[path] = role
    if set(classified) != set(workspace):
        missing = sorted(set(workspace) - set(classified))
        extra = sorted(set(classified) - set(workspace))
        raise ReleaseError(
            f"release-unit inventory differs from workspace: missing={missing} extra={extra}"
        )

    projects: list[Project] = []
    seen_names: set[str] = set()
    for relative, pyproject in sorted(workspace.items()):
        metadata = _project_metadata(pyproject)
        name = _normalize_name(str(metadata["name"]))
        if name in seen_names:
            raise ReleaseError(f"workspace repeats distribution name: {name}")
        seen_names.add(name)
        version = str(metadata["version"])
        _version(version)
        if expected_version is not None and version != expected_version:
            raise ReleaseError(f"{name} is {version}, expected {expected_version}")
        if metadata.get("readme") != PROJECT_README:
            raise ReleaseError(f"{name} does not carry the common package README")
        if metadata.get("authors") != PROJECT_PEOPLE:
            raise ReleaseError(f"{name} does not carry canonical authorship")
        if metadata.get("maintainers") != PROJECT_PEOPLE:
            raise ReleaseError(f"{name} does not carry canonical maintainership")
        if metadata.get("classifiers") != PROJECT_CLASSIFIERS:
            raise ReleaseError(f"{name} does not carry canonical classifiers")
        if metadata.get("urls") != PROJECT_URLS:
            raise ReleaseError(f"{name} does not carry canonical project URLs")
        projects.append(
            Project(
                name=name,
                path=relative,
                role=classified[relative],
                version=version,
                description=str(metadata["description"]),
            )
        )

    versions = {item.version for item in projects}
    if len(versions) != 1:
        raise ReleaseError(f"coordinated distributions have different versions: {sorted(versions)}")
    current_version = next(iter(versions))
    expected_range = _dependency_range(current_version)
    for project in projects:
        metadata = _project_metadata(root / project.path / "pyproject.toml")
        for dependency in metadata.get("dependencies", []):
            dependency_name = _dependency_name(str(dependency))
            if dependency_name in seen_names and dependency != dependency_name + expected_range:
                raise ReleaseError(
                    f"{project.name} dependency {dependency_name} must use {expected_range}"
                )

    locked = tomllib.loads((root / "uv.lock").read_text(encoding="utf-8"))
    locked_versions = {
        _normalize_name(str(item["name"])): str(item["version"])
        for item in locked["package"]
        if _normalize_name(str(item["name"])) in seen_names
    }
    if set(locked_versions) != seen_names:
        raise ReleaseError("uv.lock does not contain every release distribution exactly once")
    if any(value != current_version for value in locked_versions.values()):
        raise ReleaseError("uv.lock release-unit versions differ from pyproject metadata")

    runtime_images = config.get("images", {}).get("runtime", {})
    test_images = config.get("images", {}).get("test_only", {})
    if runtime_images != RUNTIME_IMAGE_TARGETS or test_images != TEST_IMAGE_TARGETS:
        raise ReleaseError("release image inventory differs from the canonical bake graph")
    if set(runtime_images) | set(test_images) != _bake_targets(root):
        raise ReleaseError("release image inventory differs from docker-bake.hcl")
    image_distributions = {
        _normalize_name(str(value.get("distribution", ""))) for value in runtime_images.values()
    }
    if not image_distributions <= seen_names:
        raise ReleaseError("a runtime image refers to an unknown distribution")
    repositories = [str(value.get("repository", "")) for value in runtime_images.values()]
    if len(set(repositories)) != len(repositories) or any(
        not value.startswith("ghcr.io/nashspence/riverhog") for value in repositories
    ):
        raise ReleaseError("runtime image repositories are absent, duplicated, or outside GHCR")
    compatibility = config.get("compatibility")
    if not isinstance(compatibility, dict) or set(compatibility) != {
        "components",
        "http_api",
        "cli",
        "archive",
        "recovery",
        "configuration",
        "persistent_state",
    }:
        raise ReleaseError("release.toml lacks the complete v1 compatibility policy")
    if any(not str(value).strip() for value in compatibility.values()):
        raise ReleaseError("v1 compatibility promises must be visible")
    return projects


def apply_release_version(root: Path, version: str) -> list[Project]:
    major, _, _ = _version(version)
    if major != 1:
        raise ReleaseError("release/v1 accepts only 1.x.y versions")
    projects = validate_release_contract(root)
    current_version = projects[0].version
    current_range = _dependency_range(current_version)
    target_range = _dependency_range(version)
    names = {item.name for item in projects}
    for project in projects:
        path = root / project.path / "pyproject.toml"
        text = path.read_text(encoding="utf-8")
        updated, count = PROJECT_VERSION_RE.subn(f'version = "{version}"', text, count=1)
        if count != 1:
            raise ReleaseError(f"cannot update project version in {project.path}")
        for name in names:
            updated = updated.replace(
                f'"{name}{current_range}"',
                f'"{name}{target_range}"',
            )
        path.write_text(updated, encoding="utf-8")
    return projects


def _ensure_clean(root: Path) -> None:
    status = _git_output(root, "status", "--porcelain=v1", "--untracked-files=all")
    if status:
        raise ReleaseError("release operations require a clean worktree")


def _trusted_config_paths(checkout: Path) -> str:
    existing = os.environ.get("MISE_TRUSTED_CONFIG_PATHS")
    return os.pathsep.join(value for value in (str(checkout), existing) if value)


def _source_sha(root: Path) -> str:
    value = _git_output(root, "rev-parse", "--verify", "HEAD")
    if re.fullmatch(r"[0-9a-f]{40}", value) is None:
        raise ReleaseError("Git did not return a full source SHA")
    return value


def _previous_tag(root: Path) -> str | None:
    result = subprocess.run(
        ["git", "describe", "--tags", "--abbrev=0", "--match", "v[0-9]*"],
        cwd=root,
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    return result.stdout.strip() or None


def _release_notes(root: Path, source_sha: str) -> tuple[str | None, list[dict[str, str]]]:
    previous = _previous_tag(root)
    if previous is None:
        return None, []
    revision_range = f"{previous}..{source_sha}"
    output = _git_output(root, "log", "--format=%H%x09%s", revision_range)
    commits = []
    for line in output.splitlines():
        sha, separator, subject = line.partition("\t")
        if separator:
            commits.append({"sha": sha, "subject": subject})
    return previous, commits


def build_release_plan(root: Path, version: str, *, allow_dirty: bool = False) -> dict[str, Any]:
    major, _, _ = _version(version)
    if major != 1:
        raise ReleaseError("release/v1 accepts only 1.x.y versions")
    if not allow_dirty:
        _ensure_clean(root)
    projects = validate_release_contract(root)
    config = _load_config(root)
    source_sha = _source_sha(root)
    previous_tag, commits = _release_notes(root, source_sha)
    python_artifacts = []
    for project in projects:
        artifact_name = project.name.replace("-", "_")
        python_artifacts.append(
            {
                "name": project.name,
                "path": project.path,
                "role": project.role,
                "current_version": project.version,
                "release_version": version,
                "artifacts": [
                    f"dist/{artifact_name}-{version}-py3-none-any.whl",
                    f"dist/{artifact_name}-{version}.tar.gz",
                ],
            }
        )
    images = []
    for target, value in config["images"]["runtime"].items():
        repository = str(value["repository"])
        images.append(
            {
                "target": target,
                "distribution": value["distribution"],
                "repository": repository,
                "tags": [f"{repository}:{version}", f"{repository}:sha-{source_sha}"],
            }
        )
    supporting = {
        "documentation": config["artifacts"]["documentation"].format(version=version),
        "source": config["artifacts"]["source"].format(version=version),
        "evidence": list(config["artifacts"]["evidence"]),
    }
    return {
        "schema": RELEASE_SCHEMA,
        "series": config["series"],
        "version": version,
        "tag": config["tag_template"].format(version=version),
        "source_sha": source_sha,
        "release_branch": config["release_branch"],
        "version_policy": config["version_policy"],
        "compatibility": config["compatibility"],
        "python": python_artifacts,
        "images": images,
        "supporting_artifacts": supporting,
        "release_notes": {
            "previous_tag": previous_tag,
            "commits": commits,
        },
    }


def render_release_markdown(plan: dict[str, Any]) -> str:
    lines = [
        f"# Riverhog {plan['tag']}",
        "",
        f"Source: `{plan['source_sha']}`",
        "",
        "## Release units",
        "",
        "| Distribution | Role | Version |",
        "| --- | --- | --- |",
    ]
    lines.extend(
        f"| `{item['name']}` | `{item['role']}` | `{item['release_version']}` |"
        for item in plan["python"]
    )
    lines.extend(["", "## Runtime images", ""])
    lines.extend(f"- `{item['tags'][0]}` and `{item['tags'][1]}`" for item in plan["images"])
    lines.extend(["", "## Changes", ""])
    commits = plan["release_notes"]["commits"]
    lines.extend(f"- {item['subject']} (`{item['sha'][:12]}`)" for item in commits)
    if not commits:
        if plan["release_notes"]["previous_tag"] is None:
            lines.append("- Initial v1 release; there is no previous release tag.")
        else:
            lines.append("- No commits after the previous release tag.")
    return "\n".join(lines) + "\n"


def apply_command(root: Path, version: str, *, allow_dirty: bool) -> None:
    if not allow_dirty:
        _ensure_clean(root)
    apply_release_version(root, version)
    _run(["uv", "lock", "--offline"], cwd=root)
    validate_release_contract(root, expected_version=version)


def dry_run(root: Path, version: str) -> dict[str, Any]:
    _ensure_clean(root)
    source_sha = _source_sha(root)
    archive = subprocess.run(
        ["git", "archive", "--format=tar", source_sha],
        cwd=root,
        check=True,
        stdout=subprocess.PIPE,
    ).stdout
    with tempfile.TemporaryDirectory(prefix="riverhog-release-dry-run.") as temporary:
        checkout = Path(temporary) / "riverhog"
        checkout.mkdir()
        with tarfile.open(fileobj=io.BytesIO(archive), mode="r:") as stream:
            stream.extractall(checkout, filter="data")
        apply_release_version(checkout, version)
        _run(["uv", "lock", "--offline"], cwd=checkout)
        projects = validate_release_contract(checkout, expected_version=version)
        _run(
            ["make", "dist-smoke"],
            cwd=checkout,
            env={"MISE_TRUSTED_CONFIG_PATHS": _trusted_config_paths(checkout)},
        )
    return {
        "schema": RELEASE_SCHEMA,
        "version": version,
        "tag": f"v{version}",
        "source_sha": source_sha,
        "python_distributions": len(projects),
        "published": False,
        "validation": ["uv lock --offline", "make dist-smoke"],
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Plan, validate, and dry-run the coordinated Riverhog v1 release."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("check", help="Validate release inventory, metadata, and versions.")

    plan = subparsers.add_parser("plan", help="Generate SHA-bound release notes and inventory.")
    plan.add_argument("--version", required=True)
    plan.add_argument("--format", choices=("json", "markdown"), default="json")
    plan.add_argument("--allow-dirty", action="store_true")

    apply = subparsers.add_parser("apply", help="Apply one coordinated v1 version and relock.")
    apply.add_argument("--version", required=True)
    apply.add_argument("--allow-dirty", action="store_true")

    dry = subparsers.add_parser(
        "dry-run",
        help="Apply the version in a temporary exact-SHA checkout and run distribution smoke.",
    )
    dry.add_argument("--version", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "check":
            projects = validate_release_contract(ROOT)
            payload: dict[str, Any] = {
                "schema": RELEASE_SCHEMA,
                "version": projects[0].version,
                "python_distributions": len(projects),
            }
            print(json.dumps(payload, indent=2, sort_keys=True))
        elif args.command == "plan":
            payload = build_release_plan(
                ROOT,
                args.version,
                allow_dirty=args.allow_dirty,
            )
            if args.format == "markdown":
                print(render_release_markdown(payload), end="")
            else:
                print(json.dumps(payload, indent=2, sort_keys=True))
        elif args.command == "apply":
            apply_command(ROOT, args.version, allow_dirty=args.allow_dirty)
        else:
            payload = dry_run(ROOT, args.version)
            print(json.dumps(payload, indent=2, sort_keys=True))
    except (OSError, ReleaseError, subprocess.CalledProcessError) as exc:
        raise SystemExit(f"release error: {exc}") from exc
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
