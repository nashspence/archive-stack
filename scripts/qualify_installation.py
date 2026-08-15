#!/usr/bin/env python3
"""Qualify final-version Riverhog uv-tool artifacts through a staged HTTP origin."""

from __future__ import annotations

import argparse
import hashlib
import http.server
import importlib.util
import io
import json
import os
import shutil
import stat
import string
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import urllib.parse
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, cast

import release
import release_installation as installation

ROOT = Path(__file__).resolve().parents[1]
SCHEMA = "riverhog-installation-qualification/v1"
EVENT_SUBJECT = "qualification-sentinel"
EVENT_PAGE = {
    "events": [
        {
            "specversion": "1.0",
            "id": "00000000-0000-4000-8000-000000000497",
            "source": "https://qualification.invalid/riverhog",
            "type": "org.riverhog.qualification.observed.v1",
            "subject": EVENT_SUBJECT,
            "time": "2026-08-14T00:00:00Z",
            "datacontenttype": "application/json",
            "data": {"qualification": "installed-client"},
        }
    ],
    "next_cursor": "1",
    "has_more": False,
}


class QualificationError(RuntimeError):
    """The staged installation differs from the v1 release contract."""


class QualificationHandler(http.server.SimpleHTTPRequestHandler):
    requests: list[str] = []

    def do_GET(self) -> None:  # noqa: N802 - stdlib handler interface
        parsed = urllib.parse.urlsplit(self.path)
        type(self).requests.append(parsed.path)
        if parsed.path == "/v1/events":
            payload = json.dumps(EVENT_PAGE, sort_keys=True).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        super().do_GET()

    def log_message(self, _format: str, *_args: object) -> None:
        return


def _run(
    command: list[str],
    *,
    cwd: Path,
    env: dict[str, str] | None = None,
    capture: bool = False,
) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        command,
        cwd=cwd,
        env=env,
        check=False,
        text=True,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.PIPE if capture else None,
    )
    if completed.returncode != 0:
        detail = (
            (completed.stderr or "").strip()
            or (completed.stdout or "").strip()
            or "no captured diagnostic"
        )
        raise QualificationError(
            f"{Path(command[0]).name} exited {completed.returncode}: {detail[-4000:]}"
        )
    return completed


def _source_checkout(root: Path, destination: Path, source_sha: str) -> None:
    archive = subprocess.run(
        ["git", "archive", "--format=tar", source_sha],
        cwd=root,
        check=True,
        stdout=subprocess.PIPE,
    ).stdout
    destination.mkdir()
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:") as stream:
        stream.extractall(destination, filter="data")


def _wheel_records(
    checkout: Path, projects: list[release.Project], version: str
) -> list[dict[str, Any]]:
    validated = release._validate_distribution_artifacts(checkout, projects, version=version)
    records: list[dict[str, Any]] = []
    for filename, (project, _dependencies) in sorted(validated.items()):
        if not filename.endswith(".whl"):
            continue
        path = checkout / "dist" / filename
        records.append(
            {
                "kind": "wheel",
                "name": f"python/{filename}",
                "sha256": installation.sha256_file(path),
                "size": path.stat().st_size,
                "distribution": project.name,
            }
        )
    return records


def _build_distributions(checkout: Path, projects: list[release.Project]) -> None:
    dist = checkout / "dist"
    shutil.rmtree(dist, ignore_errors=True)
    dist.mkdir()
    for project in projects:
        _run(
            [
                "uv",
                "--no-config",
                "build",
                "--package",
                project.name,
                "--no-create-gitignore",
            ],
            cwd=checkout,
        )


def _stage_artifacts(
    root: Path,
    scratch: Path,
    *,
    version: str,
    source_sha: str,
    base_url: str,
) -> dict[str, Any]:
    checkout = scratch / "checkout"
    _source_checkout(root, checkout, source_sha)
    release.apply_release_version(checkout, version)
    _run(["uv", "lock", "--offline"], cwd=checkout)
    projects = release.validate_release_contract(checkout, expected_version=version)
    _build_distributions(checkout, projects)
    wheel_records = _wheel_records(checkout, projects, version)

    web_root = scratch / "web"
    assets = web_root / "assets"
    assets.mkdir(parents=True)
    for wheel in (checkout / "dist").glob("*.whl"):
        shutil.copy2(wheel, assets / wheel.name)

    output = scratch / "installation"
    output.mkdir()
    config = release._load_config(checkout)
    source_epoch = int(
        _run(
            ["git", "show", "-s", "--format=%ct", source_sha],
            cwd=root,
            capture=True,
        ).stdout.strip()
    )
    manifest, _records = installation.build_installation_artifacts(
        checkout,
        output,
        projects,
        wheel_records,
        version=version,
        source_sha=source_sha,
        source_epoch=source_epoch,
        repository=str(config["governance"]["repository"]),
        simple_index_path=str(config["installation"]["simple_index_path"]),
        asset_base_url=base_url + "/assets/",
        index_base_url=base_url
        + "/"
        + str(config["installation"]["simple_index_path"]).format(version=version),
    )
    installation.verify_installation_artifacts(output, manifest)
    for lock in (output / "installation").glob("pylock.*.toml"):
        shutil.copy2(lock, assets / lock.name)
    snapshot = output / str(manifest["index"]["snapshot_path"])
    with tarfile.open(snapshot, mode="r:gz") as archive:
        archive.extractall(web_root, filter="data")
    return manifest


def _server(web_root: Path) -> tuple[http.server.ThreadingHTTPServer, threading.Thread, str]:
    handler = lambda *args, **kwargs: QualificationHandler(  # noqa: E731
        *args, directory=str(web_root), **kwargs
    )
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    address = server.server_address
    host, port = str(address[0]), int(address[1])
    return server, thread, f"http://{host}:{port}"


def _tool_environment(scratch: Path, root: str) -> dict[str, str]:
    environment = os.environ.copy()
    for name in (
        "PYTHONHOME",
        "PYTHONPATH",
        "UV_CONSTRAINT",
        "UV_DEFAULT_INDEX",
        "UV_FIND_LINKS",
        "UV_INDEX",
        "UV_INDEX_URL",
        "UV_PROJECT",
        "VIRTUAL_ENV",
    ):
        environment.pop(name, None)
    isolated = scratch / "tools" / root
    environment.update(
        {
            "UV_CACHE_DIR": str(scratch / "uv-cache"),
            "UV_PYTHON_INSTALL_DIR": str(scratch / "uv-python"),
            "UV_TOOL_BIN_DIR": str(isolated / "bin"),
            "UV_TOOL_DIR": str(isolated / "environments"),
            "RIVERHOG_ALLOW_INSECURE_HTTP": "true",
            "RIVERHOG_HTTP2": "false",
            "MUNCHY_ALLOW_INSECURE_HTTP": "true",
            "MUNCHY_HTTP2": "false",
            "JEB_ALLOW_INSECURE_HTTP": "true",
            "JEB_HTTP2": "false",
        }
    )
    return environment


def _executable(bin_dir: Path, name: str) -> Path:
    suffix = ".exe" if os.name == "nt" else ""
    path = bin_dir / f"{name}{suffix}"
    if not path.is_file():
        raise QualificationError(f"installed entry point is absent: {path}")
    return path


def _tool_python(tool_dir: Path, root: str) -> Path:
    relative = Path("Scripts/python.exe") if os.name == "nt" else Path("bin/python")
    path = tool_dir / root / relative
    if not path.is_file():
        raise QualificationError(f"installed tool interpreter is absent: {path}")
    return path


def _installed_inventory(python: Path, cwd: Path, environment: dict[str, str]) -> dict[str, str]:
    program = (
        "import importlib.metadata as m,json,re;"
        "n=lambda v:re.sub(r'[-_.]+','-',v).lower();"
        "print(json.dumps({n(d.metadata['Name']):d.version "
        "for d in m.distributions()},sort_keys=True))"
    )
    completed = _run(
        [str(python), "-I", "-c", program],
        cwd=cwd,
        env=environment,
        capture=True,
    )
    value = json.loads(completed.stdout)
    if not isinstance(value, dict):
        raise QualificationError("installed distribution inventory is not an object")
    return {str(name): str(version) for name, version in value.items()}


def _python_version(python: Path, cwd: Path, environment: dict[str, str]) -> str:
    return _run(
        [str(python), "-I", "-c", "import platform;print(platform.python_version())"],
        cwd=cwd,
        env=environment,
        capture=True,
    ).stdout.strip()


def _run_client_operation(
    root: str,
    executable: Path,
    *,
    base_url: str,
    cwd: Path,
    environment: dict[str, str],
) -> str:
    if root == "riverhog-client":
        environment["RIVERHOG_BASE_URL"] = base_url
        command = [str(executable), "event", "list", "--json"]
    elif root == "munchy-client":
        command = [str(executable), "event", "list", "--server-url", base_url, "--json"]
    elif root == "jeb-client":
        environment["JEB_BASE_URL"] = base_url
        command = [str(executable), "event", "list", "--json"]
    else:
        raise QualificationError(f"no disposable client operation for {root}")
    completed = _run(command, cwd=cwd, env=environment, capture=True)
    payload = json.loads(completed.stdout)
    if payload != EVENT_PAGE:
        raise QualificationError(f"{root} changed the disposable event response")
    return "lifecycle-event-list"


def _run_gogurt(
    executable: Path,
    *,
    source_root: Path,
    scratch: Path,
    environment: dict[str, str],
    listener_lifecycle: bool,
    listener_lifecycle_repetitions: int,
    gogurt_evidence_dir: Path | None,
) -> str:
    examples = scratch / "gogurt-examples"
    shutil.copytree(source_root / "utilities/gogurt/config/examples", examples)
    mount = scratch / "gogurt-mount"
    mount.mkdir()
    (mount / ".gogurt").write_text("example-camera-card\n", encoding="utf-8")
    completed = _run(
        [
            str(executable),
            "run",
            str(mount),
            "--config",
            str(examples / "gogurt-routes.yaml"),
            "--dry-run",
        ],
        cwd=scratch,
        env=environment,
        capture=True,
    )
    if "gogurt would run" not in completed.stderr:
        raise QualificationError("installed Gogurt did not execute its portable foreground plan")
    if not listener_lifecycle:
        return "portable-foreground-dry-run"
    for repetition in range(1, listener_lifecycle_repetitions + 1):
        lifecycle_scratch = scratch / f"gogurt-listener-{repetition}"
        lifecycle_scratch.mkdir()
        evidence_dir = (
            gogurt_evidence_dir / f"repetition-{repetition}"
            if gogurt_evidence_dir is not None
            else None
        )
        _run_gogurt_listener_lifecycle(
            executable,
            scratch=lifecycle_scratch,
            environment=environment,
            evidence_dir=evidence_dir,
        )
    return "native-listener-lifecycle"


@contextmanager
def _qualification_mount(scratch: Path) -> Iterator[Path]:
    if sys.platform.startswith("linux"):
        backing = scratch / "gogurt-listener-volume"
        backing.mkdir()
        _run(
            [
                "sudo",
                "mount",
                "-t",
                "tmpfs",
                "-o",
                "size=16m",
                "gogurt-qualification",
                str(backing),
            ],
            cwd=scratch,
        )
        try:
            yield backing
        finally:
            subprocess.run(
                ["sudo", "umount", str(backing)],
                cwd=scratch,
                check=False,
                capture_output=True,
                text=True,
            )
        return
    if sys.platform == "darwin":
        backing = Path("/Volumes") / f"GogurtQualification-{os.getpid()}"
        image = scratch / "gogurt-listener.dmg"
        _run(
            [
                "hdiutil",
                "create",
                "-quiet",
                "-size",
                "16m",
                "-fs",
                "HFS+",
                "-volname",
                "GogurtQualification",
                str(image),
            ],
            cwd=scratch,
        )
        _run(
            [
                "hdiutil",
                "attach",
                "-quiet",
                "-nobrowse",
                "-mountpoint",
                str(backing),
                str(image),
            ],
            cwd=scratch,
        )
        try:
            yield backing
        finally:
            subprocess.run(
                ["hdiutil", "detach", "-quiet", str(backing)],
                cwd=scratch,
                check=False,
                capture_output=True,
                text=True,
            )
        return
    if sys.platform == "win32":
        backing = scratch / "gogurt-listener-volume"
        backing.mkdir()
        drive = next(
            (
                f"{letter}:"
                for letter in reversed(string.ascii_uppercase[3:])
                if not Path(f"{letter}:\\").exists()
            ),
            None,
        )
        if drive is None:
            raise QualificationError("no disposable Windows drive letter is available")
        _run(["subst.exe", drive, str(backing)], cwd=scratch)
        try:
            yield Path(drive + "\\")
        finally:
            subprocess.run(
                ["subst.exe", drive, "/D"],
                cwd=scratch,
                check=False,
                capture_output=True,
                text=True,
            )
        return
    raise QualificationError(f"no disposable mount strategy for {sys.platform}")


def _listener_status(
    executable: Path,
    *,
    scratch: Path,
    environment: dict[str, str],
) -> dict[str, Any]:
    completed = _run(
        [str(executable), "listener", "status", "--json"],
        cwd=scratch,
        env=environment,
        capture=True,
    )
    value = json.loads(completed.stdout)
    if not isinstance(value, dict):
        raise QualificationError("installed Gogurt listener status is not an object")
    return value


def _wait_for_listener(
    executable: Path,
    predicate: Callable[[dict[str, Any]], bool],
    *,
    scratch: Path,
    environment: dict[str, str],
    timeout_seconds: float = 30,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_seconds
    status = _listener_status(
        executable,
        scratch=scratch,
        environment=environment,
    )
    while not predicate(status) and time.monotonic() < deadline:
        time.sleep(0.2)
        status = _listener_status(
            executable,
            scratch=scratch,
            environment=environment,
        )
    if not predicate(status):
        raise QualificationError(
            f"installed Gogurt listener did not reach expected state: {status}"
        )
    return status


def _retain_gogurt_failure_evidence(
    executable: Path,
    *,
    state_dir: Path,
    scratch: Path,
    environment: dict[str, str],
    evidence_dir: Path | None,
    failure: BaseException,
    phase: str,
) -> None:
    if evidence_dir is None:
        return
    try:
        evidence_dir.mkdir(parents=True, exist_ok=True)
        diagnostic = " ".join(f"{type(failure).__name__}: {failure}".splitlines())[:4096]
        (evidence_dir / f"{phase}-failure.txt").write_text(
            diagnostic + "\n",
            encoding="utf-8",
        )
        status = subprocess.run(
            [str(executable), "listener", "status", "--json"],
            cwd=scratch,
            env=environment,
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
        status_payload = {
            "returncode": status.returncode,
            "stdout": status.stdout[-65536:],
            "stderr": status.stderr[-65536:],
        }
        (evidence_dir / f"{phase}-status.json").write_text(
            json.dumps(status_payload, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        for source in sorted(state_dir.glob("listener.log*")):
            if source.is_symlink():
                continue
            info = source.stat()
            if not stat.S_ISREG(info.st_mode) or info.st_size > 2 * 1024 * 1024:
                continue
            shutil.copy2(source, evidence_dir / source.name)
    except (OSError, subprocess.SubprocessError, ValueError) as evidence_error:
        try:
            evidence_dir.mkdir(parents=True, exist_ok=True)
            diagnostic = " ".join(
                f"{type(evidence_error).__name__}: {evidence_error}".splitlines()
            )[:4096]
            (evidence_dir / f"{phase}-evidence-error.txt").write_text(
                diagnostic + "\n",
                encoding="utf-8",
            )
        except OSError:
            return


def _run_gogurt_listener_lifecycle(
    executable: Path,
    *,
    scratch: Path,
    environment: dict[str, str],
    evidence_dir: Path | None,
) -> None:
    initial = _listener_status(
        executable,
        scratch=scratch,
        environment=environment,
    )
    if initial.get("installed") is not False:
        raise QualificationError("listener qualification would replace an existing installation")
    state_dir = Path(str(initial["state_dir"]))

    action = scratch / "gogurt-listener-action.py"
    action.write_text(
        "import sys,time\n"
        "from pathlib import Path\n"
        "sentinel=Path(sys.argv[2])\n"
        "with sentinel.open('a', encoding='utf-8') as stream:\n"
        "    stream.write('run\\n')\n"
        "time.sleep(0.75)\n",
        encoding="utf-8",
    )
    failure = scratch / "gogurt-listener-failure.py"
    failure.write_text("raise SystemExit(7)\n", encoding="utf-8")
    sentinel = scratch / "gogurt-listener-sentinel.txt"
    routes = scratch / "gogurt-listener-routes.yaml"
    routes.write_text(
        "schema_version: 1\n"
        "kind: gogurt.routes\n"
        "routes:\n"
        "  qualification-success:\n"
        "    command:\n"
        '      - "{python}"\n'
        f"      - {json.dumps(str(action))}\n"
        '      - "{mount_point}"\n'
        f"      - {json.dumps(str(sentinel))}\n"
        "  qualification-failure:\n"
        "    command:\n"
        '      - "{python}"\n'
        f"      - {json.dumps(str(failure))}\n"
        '      - "{mount_point}"\n',
        encoding="utf-8",
    )

    installed = False
    try:
        with _qualification_mount(scratch) as mount:
            marker = mount / ".gogurt"
            installed_payload = json.loads(
                _run(
                    [
                        str(executable),
                        "listener",
                        "install",
                        "--config",
                        str(routes),
                        "--interval",
                        "0.2",
                        "--autorun",
                        "--json",
                    ],
                    cwd=scratch,
                    env=environment,
                    capture=True,
                ).stdout
            )
            installed = True
            if installed_payload.get("health") != "healthy":
                raise QualificationError("installed Gogurt listener did not report healthy")
            human = _run(
                [str(executable), "listener", "status"],
                cwd=scratch,
                env=environment,
                capture=True,
            )
            if "health: healthy" not in human.stdout:
                raise QualificationError("Gogurt listener human status differs from JSON status")

            state_dir = Path(str(installed_payload["state_dir"]))
            if os.name != "nt":
                if stat.S_IMODE(state_dir.stat().st_mode) != 0o700:
                    raise QualificationError("Gogurt listener state directory is not private")
                private_names = (
                    "listener.json",
                    "listener.sqlite3",
                    "heartbeat.json",
                    "listener.lock",
                    "listener.log",
                )
                if any(
                    stat.S_IMODE((state_dir / name).stat().st_mode) != 0o600
                    for name in private_names
                ):
                    raise QualificationError("Gogurt listener state files are not private")

            routes_content = routes.read_text(encoding="utf-8")
            routes.write_text("not: [valid", encoding="utf-8")
            failed = _wait_for_listener(
                executable,
                lambda status: status.get("health") == "failed",
                scratch=scratch,
                environment=environment,
            )
            if "global configuration" not in str(failed.get("diagnostic")):
                raise QualificationError("Gogurt global config failure lacks a diagnostic")
            human_failed = _run(
                [str(executable), "listener", "status"],
                cwd=scratch,
                env=environment,
                capture=True,
            )
            if (
                "health: failed" not in human_failed.stdout
                or "diagnostic:" not in human_failed.stdout
            ):
                raise QualificationError("Gogurt human status hid global config failure")
            if sentinel.exists():
                raise QualificationError("Gogurt dispatched while global config was invalid")
            routes.write_text(routes_content, encoding="utf-8")
            _wait_for_listener(
                executable,
                lambda status: status.get("health") == "healthy",
                scratch=scratch,
                environment=environment,
            )
            marker.write_text("qualification-success\n", encoding="utf-8")

            def one_completed(status: dict[str, Any]) -> bool:
                dispatches = status.get("dispatches")
                counts = dispatches.get("counts") if isinstance(dispatches, dict) else None
                return isinstance(counts, dict) and counts.get("completed") == 1

            _wait_for_listener(
                executable,
                one_completed,
                scratch=scratch,
                environment=environment,
            )
            time.sleep(1)
            if sentinel.read_text(encoding="utf-8").splitlines() != ["run"]:
                raise QualificationError(
                    "Gogurt listener dispatched one observation more than once"
                )

            stopped = json.loads(
                _run(
                    [str(executable), "listener", "stop", "--json"],
                    cwd=scratch,
                    env=environment,
                    capture=True,
                ).stdout
            )
            if stopped.get("health") != "stopped":
                raise QualificationError("Gogurt listener stop did not preserve stopped state")
            for operation in ("start", "restart"):
                payload = json.loads(
                    _run(
                        [str(executable), "listener", operation, "--json"],
                        cwd=scratch,
                        env=environment,
                        capture=True,
                    ).stdout
                )
                if payload.get("health") != "healthy":
                    raise QualificationError(f"Gogurt listener {operation} did not become healthy")
                time.sleep(0.5)
                if sentinel.read_text(encoding="utf-8").splitlines() != ["run"]:
                    raise QualificationError(
                        f"Gogurt listener replayed a completed observation after {operation}"
                    )

            reinstalled = json.loads(
                _run(
                    [
                        str(executable),
                        "listener",
                        "install",
                        "--config",
                        str(routes),
                        "--interval",
                        "0.2",
                        "--autorun",
                        "--json",
                    ],
                    cwd=scratch,
                    env=environment,
                    capture=True,
                ).stdout
            )
            if reinstalled.get("health") != "healthy":
                raise QualificationError("same-version listener reinstall did not become healthy")
            time.sleep(0.5)
            if sentinel.read_text(encoding="utf-8").splitlines() != ["run"]:
                raise QualificationError("same-version listener reinstall replayed completed work")

            replacement = mount / ".gogurt.replacement"
            replacement.write_text("qualification-failure\n", encoding="utf-8")
            os.replace(replacement, marker)

            def failure_visible(status: dict[str, Any]) -> bool:
                dispatches = status.get("dispatches")
                attention = dispatches.get("attention") if isinstance(dispatches, dict) else None
                return isinstance(attention, list) and any(
                    isinstance(item, dict)
                    and item.get("state") == "retry"
                    and item.get("exit_code") == 7
                    for item in attention
                )

            _wait_for_listener(
                executable,
                failure_visible,
                scratch=scratch,
                environment=environment,
            )
    except BaseException as exc:
        _retain_gogurt_failure_evidence(
            executable,
            state_dir=state_dir,
            scratch=scratch,
            environment=environment,
            evidence_dir=evidence_dir,
            failure=exc,
            phase="lifecycle",
        )
        raise
    finally:
        if installed:
            try:
                removed = json.loads(
                    _run(
                        [str(executable), "listener", "uninstall", "--json"],
                        cwd=scratch,
                        env=environment,
                        capture=True,
                    ).stdout
                )
            except BaseException as exc:
                _retain_gogurt_failure_evidence(
                    executable,
                    state_dir=state_dir,
                    scratch=scratch,
                    environment=environment,
                    evidence_dir=evidence_dir,
                    failure=exc,
                    phase="uninstall",
                )
                raise
            if removed.get("installed") is not False or removed.get("health") != "absent":
                raise QualificationError("Gogurt listener uninstall left native registration")
            if Path(str(removed["state_dir"])).exists():
                raise QualificationError("Gogurt listener uninstall left durable state")


def _write_ots_fixture(path: Path) -> Path:
    program = path / "ots_fixture.py"
    program.write_text(
        """import hashlib
import sys
from pathlib import Path

if len(sys.argv) != 5 or sys.argv[1] != "verify" or sys.argv[3] != "-f":
    raise SystemExit(2)
proof = Path(sys.argv[2]).read_text(encoding="utf-8")
manifest = Path(sys.argv[4]).read_bytes()
if proof != f"sha256:{hashlib.sha256(manifest).hexdigest()}\\n":
    raise SystemExit(1)
""",
        encoding="utf-8",
    )
    if os.name == "nt":
        wrapper = path / "ots-fixture.cmd"
        wrapper.write_text(
            f'@"{sys.executable}" "{program}" %*\r\n',
            encoding="utf-8",
        )
    else:
        wrapper = path / "ots-fixture"
        wrapper.write_text(
            f'#!/bin/sh\nexec "{sys.executable}" "{program}" "$@"\n',
            encoding="utf-8",
        )
        wrapper.chmod(0o700)
    return wrapper


def _run_recovery(
    executable: Path,
    *,
    source_root: Path,
    scratch: Path,
    environment: dict[str, str],
) -> str:
    fixture_path = source_root / "riverhog/recovery/tests/test_recovery.py"
    spec = importlib.util.spec_from_file_location(
        "_riverhog_recovery_qualification_fixture",
        fixture_path,
    )
    if spec is None or spec.loader is None:
        raise QualificationError("could not load the independent recovery fixture")
    fixture = importlib.util.module_from_spec(spec)
    sys.path.insert(0, str(source_root))
    try:
        spec.loader.exec_module(fixture)
    finally:
        sys.path.remove(str(source_root))
    passphrase_value = cast(str, fixture.PASSPHRASE)
    write_archive = cast(
        Callable[[Path], tuple[dict[str, bytes], bytes | None]],
        fixture._write_archive,
    )

    archive = scratch / "recovery-archive"
    archive.mkdir()
    expected, _journal = write_archive(archive)
    passphrase = scratch / "passphrase.txt"
    passphrase.write_text(
        # codeql[py/clear-text-storage-sensitive-data]
        passphrase_value + "\n",
        encoding="utf-8",
    )
    passphrase.chmod(0o600)
    output = scratch / "recovered"
    ots = _write_ots_fixture(scratch)
    try:
        _run(
            [
                str(executable),
                str(archive),
                str(output),
                "--passphrase-file",
                str(passphrase),
                "--ots-command",
                str(ots),
            ],
            cwd=scratch,
            env=environment,
        )
    finally:
        passphrase.unlink(missing_ok=True)
    actual = {
        path.relative_to(output).as_posix(): path.read_bytes()
        for path in output.rglob("*")
        if path.is_file()
    }
    if actual != expected:
        raise QualificationError("installed recovery output differs from the release fixture")
    expected_digest = hashlib.sha256(
        b"".join(name.encode() + b"\0" + expected[name] for name in sorted(expected))
    ).hexdigest()
    actual_digest = hashlib.sha256(
        b"".join(name.encode() + b"\0" + actual[name] for name in sorted(actual))
    ).hexdigest()
    if actual_digest != expected_digest:
        raise QualificationError("installed recovery output identity differs")
    return actual_digest


def _install_command(component: dict[str, Any], manifest: dict[str, Any]) -> list[str]:
    platform = {
        "darwin": "macos-arm64",
        "linux": "linux-x64",
        "win32": "windows-x64",
    }.get(sys.platform)
    if platform is None:
        raise QualificationError(f"unsupported qualification platform: {sys.platform}")
    command = [
        "uv",
        "--no-config",
        "--allow-insecure-host",
        "127.0.0.1",
        "tool",
        "install",
        f"{component['root']}=={manifest['version']}",
        "--index",
        str(manifest["index"]["url"]),
        "--default-index",
        "https://pypi.org/simple",
        "--index-strategy",
        "first-index",
        "--python",
        str(manifest["toolchain"]["python"]),
        "--managed-python",
        "--no-build",
    ]
    for item in component["platform_requirements"][platform]:
        if item["name"] != component["root"]:
            command.extend(("--with", str(item["requirement"])))
    return command


def _qualify_component(
    component: dict[str, Any],
    manifest: dict[str, Any],
    *,
    source_root: Path,
    artifact_root: Path,
    scratch: Path,
    base_url: str,
    all_project_names: set[str],
    listener_lifecycle: bool,
    listener_lifecycle_repetitions: int,
    gogurt_evidence_dir: Path | None,
) -> dict[str, Any]:
    root = str(component["root"])
    environment = _tool_environment(scratch, root)
    tool_dir = Path(environment["UV_TOOL_DIR"])
    bin_dir = Path(environment["UV_TOOL_BIN_DIR"])
    lock = artifact_root / str(component["lock"]["path"])
    command = _install_command(component, manifest)
    request_offset = len(QualificationHandler.requests)
    _run(command, cwd=scratch, env=environment)
    _run(command, cwd=scratch, env=environment)
    _run([*command, "--reinstall"], cwd=scratch, env=environment)

    python = _tool_python(tool_dir, root)
    if _python_version(python, scratch, environment) != manifest["toolchain"]["python"]:
        raise QualificationError(f"{root} did not use the exact managed CPython patch")
    inventory = _installed_inventory(python, scratch, environment)
    expected_first_party = {
        str(item["name"]): str(item["version"]) for item in component["first_party_closure"]
    }
    actual_first_party = {
        name: version for name, version in inventory.items() if name in all_project_names
    }
    if actual_first_party != expected_first_party:
        raise QualificationError(f"{root} installed another first-party closure")
    locked = {str(item["name"]): str(item["version"]) for item in component["resolved_packages"]}
    if any(locked.get(name) != version for name, version in inventory.items()):
        raise QualificationError(f"{root} installed a version outside its PEP 751 lock")
    sync = _run(
        [
            "uv",
            "--no-config",
            "--allow-insecure-host",
            "127.0.0.1",
            "pip",
            "sync",
            str(lock),
            "--python",
            str(python),
            "--dry-run",
            "--strict",
            "--no-build",
        ],
        cwd=scratch,
        env=environment,
        capture=True,
    )
    if "Would make no changes" not in sync.stdout + sync.stderr:
        raise QualificationError(f"{root} is not already synchronized to its PEP 751 lock")

    entry_points = [str(value) for value in component["entry_points"]]
    executables = [_executable(bin_dir, name) for name in entry_points]
    primary = executables[0]
    version = _run([str(primary), "--version"], cwd=scratch, env=environment, capture=True)
    if version.stdout.strip() != manifest["version"]:
        raise QualificationError(f"{root} --version differs from the installed release")
    _run([str(primary), "--help"], cwd=scratch, env=environment, capture=True)

    if root in {"riverhog-client", "munchy-client", "jeb-client"}:
        operation = _run_client_operation(
            root,
            primary,
            base_url=base_url,
            cwd=scratch,
            environment=environment,
        )
    elif root == "gogurt":
        operation = _run_gogurt(
            primary,
            source_root=source_root,
            scratch=scratch,
            environment=environment,
            listener_lifecycle=listener_lifecycle,
            listener_lifecycle_repetitions=listener_lifecycle_repetitions,
            gogurt_evidence_dir=gogurt_evidence_dir,
        )
    else:
        operation = _run_recovery(
            primary,
            source_root=source_root,
            scratch=scratch,
            environment=environment,
        )

    requests = QualificationHandler.requests[request_offset:]
    wheel_assets = {
        "/assets/" + str(manifest["wheels"][name]["asset"]) for name in expected_first_party
    }
    if not wheel_assets <= set(requests):
        raise QualificationError(f"{root} did not consume every staged first-party wheel")
    if any(path.endswith((".tar.gz", ".zip")) for path in requests):
        raise QualificationError(f"{root} consumed a non-wheel staged distribution")

    _run(
        ["uv", "--no-config", "tool", "uninstall", root],
        cwd=scratch,
        env=environment,
    )
    if (tool_dir / root).exists() or any(path.exists() for path in executables):
        raise QualificationError(f"{root} uninstall left an installed environment or entry point")
    return {
        "root": root,
        "first_party": expected_first_party,
        "entry_points": entry_points,
        "operation": operation,
        "python": manifest["toolchain"]["python"],
        "reinstall": "passed",
        "uninstall": "passed",
        "wheel_only": True,
    }


def qualify(
    root: Path,
    *,
    version: str,
    listener_lifecycle: bool = False,
    listener_lifecycle_repetitions: int = 1,
    gogurt_evidence_dir: Path | None = None,
) -> dict[str, Any]:
    if listener_lifecycle_repetitions < 1:
        raise QualificationError("listener lifecycle repetitions must be at least one")
    release._ensure_clean(root)
    source_sha = release._source_sha(root)
    actual_uv = _run(["uv", "--version"], cwd=root, capture=True).stdout.split()[1]
    tools = installation.qualified_tool_versions(root)
    if actual_uv != tools["uv"]:
        raise QualificationError("qualification is not running the mise-locked uv version")
    with tempfile.TemporaryDirectory(prefix="riverhog-install-qualification.") as temporary:
        scratch = Path(temporary)
        web_root = scratch / "web"
        web_root.mkdir()
        server, thread, base_url = _server(web_root)
        try:
            manifest = _stage_artifacts(
                root,
                scratch,
                version=version,
                source_sha=source_sha,
                base_url=base_url,
            )
            artifact_root = scratch / "installation"
            expected_index = base_url + "/" + str(manifest["index"]["path"])
            if manifest["index"]["url"] != expected_index:
                raise QualificationError("staged index URL does not match its HTTP origin")
            projects = release.validate_release_contract(root)
            all_project_names = {project.name for project in projects}
            results = [
                _qualify_component(
                    component,
                    manifest,
                    source_root=root,
                    artifact_root=artifact_root,
                    scratch=scratch,
                    base_url=base_url,
                    all_project_names=all_project_names,
                    listener_lifecycle=listener_lifecycle,
                    listener_lifecycle_repetitions=listener_lifecycle_repetitions,
                    gogurt_evidence_dir=gogurt_evidence_dir,
                )
                for component in manifest["components"]
            ]
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
    return {
        "schema": SCHEMA,
        "source_sha": source_sha,
        "version": version,
        "uv": tools["uv"],
        "python": tools["python"],
        "platform": sys.platform,
        "components": results,
        "staged_http": "passed",
        "published": False,
        "listener_lifecycle": listener_lifecycle,
        "listener_lifecycle_repetitions": listener_lifecycle_repetitions,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", required=True)
    parser.add_argument("--summary", type=Path)
    parser.add_argument(
        "--listener-lifecycle",
        action="store_true",
        help="Exercise the real per-user service manager and a disposable mounted volume.",
    )
    parser.add_argument(
        "--listener-lifecycle-repetitions",
        type=int,
        default=1,
        help="Run isolated listener lifecycles repeatedly from the same candidate artifacts.",
    )
    parser.add_argument(
        "--gogurt-evidence-dir",
        type=Path,
        help="Retain bounded dummy lifecycle status and logs when Gogurt qualification fails.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        payload = qualify(
            ROOT,
            version=args.version,
            listener_lifecycle=args.listener_lifecycle,
            listener_lifecycle_repetitions=args.listener_lifecycle_repetitions,
            gogurt_evidence_dir=args.gogurt_evidence_dir,
        )
    except (
        OSError,
        QualificationError,
        installation.InstallationError,
        release.ReleaseError,
        subprocess.CalledProcessError,
    ) as exc:
        raise SystemExit(f"installation qualification error: {exc}") from exc
    rendered = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    if args.summary is not None:
        args.summary.parent.mkdir(parents=True, exist_ok=True)
        args.summary.write_text(rendered, encoding="utf-8")
    print(rendered, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
