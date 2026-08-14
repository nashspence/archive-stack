from __future__ import annotations

import csv
import os
import plistlib
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

LISTENER_LABEL = "io.github.nashspence.gogurt"
WINDOWS_TASK_NAME = "Riverhog.Gogurt"


class ListenerPlatformError(RuntimeError):
    """The native per-user service manager rejected a listener operation."""


@dataclass(frozen=True, slots=True)
class ListenerPaths:
    state_dir: Path
    config_file: Path
    database_file: Path
    heartbeat_file: Path
    lock_file: Path
    log_file: Path
    registration_file: Path | None


@dataclass(frozen=True, slots=True)
class NativeListenerStatus:
    installed: bool
    enabled: bool
    running: bool


def default_listener_paths(
    *,
    platform: str | None = None,
    environment: Mapping[str, str] | None = None,
    home: Path | None = None,
) -> ListenerPaths:
    current = platform or sys.platform
    env = environment or os.environ
    user_home = (home or Path.home()).expanduser().resolve()
    if current == "darwin":
        state_dir = user_home / "Library" / "Application Support" / "Gogurt"
        registration = user_home / "Library" / "LaunchAgents" / f"{LISTENER_LABEL}.plist"
    elif current.startswith("linux"):
        state_root = Path(env.get("XDG_STATE_HOME", user_home / ".local" / "state"))
        config_root = Path(env.get("XDG_CONFIG_HOME", user_home / ".config"))
        state_dir = state_root.expanduser().resolve() / "gogurt"
        registration = (
            config_root.expanduser().resolve() / "systemd" / "user" / "gogurt-listener.service"
        )
    elif current == "win32":
        raw_local = env.get("LOCALAPPDATA")
        if not raw_local:
            raise ListenerPlatformError("LOCALAPPDATA is required for the Gogurt listener")
        state_dir = Path(raw_local).expanduser().resolve() / "Gogurt"
        registration = None
    else:
        raise ListenerPlatformError(f"Gogurt listener installation is unsupported on {current}")
    return ListenerPaths(
        state_dir=state_dir,
        config_file=state_dir / "listener.json",
        database_file=state_dir / "listener.sqlite3",
        heartbeat_file=state_dir / "heartbeat.json",
        lock_file=state_dir / "listener.lock",
        log_file=state_dir / "listener.log",
        registration_file=registration,
    )


def _write_private(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, raw_temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(raw_temporary)
    try:
        os.chmod(temporary, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _systemd_quote(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def render_systemd_unit(command: Sequence[str]) -> bytes:
    rendered = " ".join(_systemd_quote(value) for value in command)
    return (
        "[Unit]\n"
        "Description=Gogurt mounted-volume listener\n"
        "\n"
        "[Service]\n"
        "Type=simple\n"
        f"ExecStart={rendered}\n"
        "Restart=on-failure\n"
        "RestartSec=5\n"
        "NoNewPrivileges=true\n\n"
        "[Install]\n"
        "WantedBy=default.target\n"
    ).encode()


def render_launchd_plist(command: Sequence[str]) -> bytes:
    return plistlib.dumps(
        {
            "Label": LISTENER_LABEL,
            "ProgramArguments": list(command),
            "RunAtLoad": True,
            "KeepAlive": {"SuccessfulExit": False},
            "ProcessType": "Background",
            "ThrottleInterval": 5,
            "StandardOutPath": "/dev/null",
            "StandardErrorPath": "/dev/null",
        },
        fmt=plistlib.FMT_XML,
        sort_keys=False,
    )


class ListenerAdapter(Protocol):
    def register(self, paths: ListenerPaths, command: Sequence[str]) -> None: ...

    def status(self, paths: ListenerPaths) -> NativeListenerStatus: ...

    def start(self, paths: ListenerPaths) -> None: ...

    def stop(self, paths: ListenerPaths) -> None: ...

    def unregister(self, paths: ListenerPaths) -> None: ...


class _CommandAdapter:
    def _run(
        self,
        command: Sequence[str],
        *,
        allowed: frozenset[int] = frozenset({0}),
    ) -> subprocess.CompletedProcess[str]:
        completed = subprocess.run(
            list(command),
            check=False,
            capture_output=True,
            text=True,
        )
        if completed.returncode not in allowed:
            detail = completed.stderr.strip() or completed.stdout.strip() or "no diagnostic"
            raise ListenerPlatformError(f"{' '.join(command)} failed: {detail}")
        return completed


class SystemdUserAdapter(_CommandAdapter):
    def _require_registration(self, paths: ListenerPaths) -> Path:
        if paths.registration_file is None:
            raise ListenerPlatformError("systemd listener registration path is absent")
        return paths.registration_file

    def register(self, paths: ListenerPaths, command: Sequence[str]) -> None:
        registration = self._require_registration(paths)
        _write_private(registration, render_systemd_unit(command))
        self._run(["systemctl", "--user", "daemon-reload"])
        self._run(["systemctl", "--user", "enable", registration.name])
        self._run(["systemctl", "--user", "restart", registration.name])

    def status(self, paths: ListenerPaths) -> NativeListenerStatus:
        registration = self._require_registration(paths)
        installed = registration.is_file()
        if not installed:
            return NativeListenerStatus(installed=False, enabled=False, running=False)
        enabled = (
            self._run(
                ["systemctl", "--user", "is-enabled", registration.name],
                allowed=frozenset({0, 1, 3, 4}),
            ).returncode
            == 0
        )
        running = (
            self._run(
                ["systemctl", "--user", "is-active", registration.name],
                allowed=frozenset({0, 1, 3, 4}),
            ).returncode
            == 0
        )
        return NativeListenerStatus(installed=True, enabled=enabled, running=running)

    def start(self, paths: ListenerPaths) -> None:
        registration = self._require_registration(paths)
        if not registration.is_file():
            raise ListenerPlatformError("Gogurt listener is not installed")
        self._run(["systemctl", "--user", "start", registration.name])

    def stop(self, paths: ListenerPaths) -> None:
        registration = self._require_registration(paths)
        if registration.is_file():
            self._run(
                ["systemctl", "--user", "stop", registration.name],
                allowed=frozenset({0, 5}),
            )

    def unregister(self, paths: ListenerPaths) -> None:
        registration = self._require_registration(paths)
        self._run(
            ["systemctl", "--user", "disable", "--now", registration.name],
            allowed=frozenset({0, 1, 5}),
        )
        registration.unlink(missing_ok=True)
        self._run(["systemctl", "--user", "daemon-reload"])
        self._run(
            ["systemctl", "--user", "reset-failed", registration.name],
            allowed=frozenset({0, 1}),
        )


class LaunchdUserAdapter(_CommandAdapter):
    @staticmethod
    def _domain() -> str:
        getuid = getattr(os, "getuid", None)
        if getuid is None:
            raise ListenerPlatformError("launchd user identity is unavailable")
        return f"gui/{getuid()}"

    def _require_registration(self, paths: ListenerPaths) -> Path:
        if paths.registration_file is None:
            raise ListenerPlatformError("launchd listener registration path is absent")
        return paths.registration_file

    def _target(self) -> str:
        return f"{self._domain()}/{LISTENER_LABEL}"

    def register(self, paths: ListenerPaths, command: Sequence[str]) -> None:
        registration = self._require_registration(paths)
        _write_private(registration, render_launchd_plist(command))
        self._run(
            ["launchctl", "bootout", self._target()],
            allowed=frozenset({0, 3, 113}),
        )
        self._run(["launchctl", "bootstrap", self._domain(), str(registration)])

    def status(self, paths: ListenerPaths) -> NativeListenerStatus:
        registration = self._require_registration(paths)
        installed = registration.is_file()
        if not installed:
            return NativeListenerStatus(installed=False, enabled=False, running=False)
        running = (
            self._run(
                ["launchctl", "print", self._target()],
                allowed=frozenset({0, 3, 113}),
            ).returncode
            == 0
        )
        return NativeListenerStatus(installed=True, enabled=True, running=running)

    def start(self, paths: ListenerPaths) -> None:
        registration = self._require_registration(paths)
        if not registration.is_file():
            raise ListenerPlatformError("Gogurt listener is not installed")
        if not self.status(paths).running:
            self._run(["launchctl", "bootstrap", self._domain(), str(registration)])

    def stop(self, paths: ListenerPaths) -> None:
        registration = self._require_registration(paths)
        if registration.is_file():
            self._run(
                ["launchctl", "bootout", self._target()],
                allowed=frozenset({0, 3, 113}),
            )

    def unregister(self, paths: ListenerPaths) -> None:
        registration = self._require_registration(paths)
        self.stop(paths)
        registration.unlink(missing_ok=True)


class TaskSchedulerUserAdapter(_CommandAdapter):
    @staticmethod
    def _task_command(command: Sequence[str]) -> str:
        return subprocess.list2cmdline(list(command))

    def register(self, paths: ListenerPaths, command: Sequence[str]) -> None:
        self._run(
            ["schtasks.exe", "/End", "/TN", WINDOWS_TASK_NAME],
            allowed=frozenset({0, 1}),
        )
        self._run(
            [
                "schtasks.exe",
                "/Create",
                "/TN",
                WINDOWS_TASK_NAME,
                "/SC",
                "ONLOGON",
                "/TR",
                self._task_command(command),
                "/RL",
                "LIMITED",
                "/IT",
                "/F",
            ]
        )
        self._run(["schtasks.exe", "/Run", "/TN", WINDOWS_TASK_NAME])

    def status(self, paths: ListenerPaths) -> NativeListenerStatus:
        del paths
        completed = self._run(
            ["schtasks.exe", "/Query", "/TN", WINDOWS_TASK_NAME, "/FO", "CSV", "/NH"],
            allowed=frozenset({0, 1}),
        )
        installed = completed.returncode == 0
        rows = list(csv.reader(completed.stdout.splitlines())) if installed else []
        running = bool(rows and rows[0] and rows[0][-1].strip().casefold() == "running")
        return NativeListenerStatus(installed=installed, enabled=installed, running=running)

    def start(self, paths: ListenerPaths) -> None:
        del paths
        self._run(["schtasks.exe", "/Run", "/TN", WINDOWS_TASK_NAME])

    def stop(self, paths: ListenerPaths) -> None:
        del paths
        self._run(
            ["schtasks.exe", "/End", "/TN", WINDOWS_TASK_NAME],
            allowed=frozenset({0, 1}),
        )

    def unregister(self, paths: ListenerPaths) -> None:
        del paths
        self._run(
            ["schtasks.exe", "/End", "/TN", WINDOWS_TASK_NAME],
            allowed=frozenset({0, 1}),
        )
        self._run(
            ["schtasks.exe", "/Delete", "/TN", WINDOWS_TASK_NAME, "/F"],
            allowed=frozenset({0, 1}),
        )


def listener_adapter(platform: str | None = None) -> ListenerAdapter:
    current = platform or sys.platform
    if current == "darwin":
        return LaunchdUserAdapter()
    if current.startswith("linux"):
        return SystemdUserAdapter()
    if current == "win32":
        return TaskSchedulerUserAdapter()
    raise ListenerPlatformError(f"Gogurt listener installation is unsupported on {current}")


def resolve_listener_executable(raw: str | None = None) -> Path:
    candidate = raw or sys.argv[0]
    source = Path(candidate).expanduser()
    resolved = shutil.which(candidate)
    candidates = [Path(resolved)] if resolved is not None else [source]
    if sys.platform == "win32" and source.suffix == "":
        extensions = os.environ.get("PATHEXT", ".COM;.EXE;.BAT;.CMD").split(";")
        candidates.extend(Path(f"{source}{extension}") for extension in extensions if extension)
    for value in candidates:
        path = value.resolve()
        if path.is_file():
            return path
    path = source.resolve()
    raise ListenerPlatformError(f"installed Gogurt executable is absent: {path}")
