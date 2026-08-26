"""Linux mount discovery and systemd-user integration for Gogurt."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path

from gogurt_listener_runtime.filesystem import PRIVATE_FILE_MODE, atomic_write
from gogurt_listener_runtime.platform import (
    ListenerAdapter,
    ListenerPlatformError,
    ListenerRuntimePaths,
    NativeListenerStatus,
)

LISTENER_LABEL = "io.github.nashspence.gogurt"
_MOUNTINFO_ESCAPE_RE = re.compile(r"\\([0-7]{3})")


def default_listener_paths(
    *,
    environment: Mapping[str, str] | None = None,
    home: Path | None = None,
) -> ListenerRuntimePaths:
    env = environment or os.environ
    user_home = (home or Path.home()).expanduser().resolve()
    state_root = Path(env.get("XDG_STATE_HOME", user_home / ".local" / "state"))
    state_dir = state_root.expanduser().resolve() / "gogurt"
    return ListenerRuntimePaths(
        state_dir=state_dir,
        config_file=state_dir / "listener.json",
        database_file=state_dir / "listener.sqlite3",
        heartbeat_file=state_dir / "heartbeat.json",
        lock_file=state_dir / "listener.lock",
        log_file=state_dir / "listener.log",
        stop_file=state_dir / "stop.request",
    )


def _default_registration_file(
    *,
    environment: Mapping[str, str] | None = None,
    home: Path | None = None,
) -> Path:
    env = environment or os.environ
    user_home = (home or Path.home()).expanduser().resolve()
    config_root = Path(env.get("XDG_CONFIG_HOME", user_home / ".config"))
    return config_root.expanduser().resolve() / "systemd" / "user" / "gogurt-listener.service"


def _systemd_quote(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def render_systemd_unit(command: Sequence[str]) -> bytes:
    rendered = " ".join(_systemd_quote(value) for value in command)
    return (
        "[Unit]\n"
        "Description=Gogurt mounted-volume listener\n\n"
        "[Service]\n"
        "Type=simple\n"
        f"ExecStart={rendered}\n"
        "Restart=on-failure\n"
        "RestartSec=5\n"
        "NoNewPrivileges=true\n\n"
        "[Install]\n"
        "WantedBy=default.target\n"
    ).encode()


class SystemdUserAdapter:
    def __init__(self, registration_file: Path) -> None:
        self.registration_file = registration_file

    def _run(
        self,
        command: Sequence[str],
        *,
        allowed: frozenset[int] = frozenset({0}),
    ) -> subprocess.CompletedProcess[str]:
        completed = subprocess.run(list(command), check=False, capture_output=True, text=True)
        if completed.returncode not in allowed:
            detail = completed.stderr.strip() or completed.stdout.strip() or "no diagnostic"
            raise ListenerPlatformError(f"{' '.join(command)} failed: {detail}")
        return completed

    def register(self, paths: ListenerRuntimePaths, command: Sequence[str]) -> None:
        del paths
        registration = self.registration_file
        registration.parent.mkdir(parents=True, exist_ok=True)
        atomic_write(registration, render_systemd_unit(command), mode=PRIVATE_FILE_MODE)
        self._run(["systemctl", "--user", "daemon-reload"])
        self._run(["systemctl", "--user", "enable", registration.name])
        self._run(["systemctl", "--user", "start", registration.name])

    def status(self, paths: ListenerRuntimePaths) -> NativeListenerStatus:
        del paths
        registration = self.registration_file
        loaded = self._run(
            ["systemctl", "--user", "show", registration.name, "--property=LoadState", "--value"],
            allowed=frozenset({0, 1, 3, 4}),
        )
        manager_loaded = loaded.returncode == 0 and loaded.stdout.strip() not in {"", "not-found"}
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
        return NativeListenerStatus(
            installed=registration.is_file() or manager_loaded or enabled or running,
            enabled=enabled,
            running=running,
        )

    def start(self, paths: ListenerRuntimePaths) -> None:
        registration = self.registration_file
        if not self.status(paths).installed:
            raise ListenerPlatformError("Gogurt listener is not installed")
        self._run(["systemctl", "--user", "start", registration.name])

    def stop(self, paths: ListenerRuntimePaths) -> None:
        registration = self.registration_file
        if self.status(paths).installed:
            self._run(
                ["systemctl", "--user", "stop", registration.name],
                allowed=frozenset({0, 5}),
            )

    def unregister(self, paths: ListenerRuntimePaths) -> None:
        registration = self.registration_file
        self.stop(paths)
        self._run(
            ["systemctl", "--user", "disable", registration.name],
            allowed=frozenset({0, 1, 5}),
        )
        registration.unlink(missing_ok=True)
        self._run(["systemctl", "--user", "daemon-reload"])
        self._run(
            ["systemctl", "--user", "reset-failed", registration.name],
            allowed=frozenset({0, 1}),
        )

    @staticmethod
    def process_is_running(pid: int) -> bool:
        try:
            os.kill(pid, 0)
        except (OverflowError, ProcessLookupError, ValueError):
            return False
        except PermissionError:
            return True
        return True


def listener_adapter(
    *,
    environment: Mapping[str, str] | None = None,
    home: Path | None = None,
) -> ListenerAdapter:
    return SystemdUserAdapter(_default_registration_file(environment=environment, home=home))


def resolve_listener_executable(raw: str | None = None) -> Path:
    candidate = raw or os.fsdecode(sys.argv[0])
    source = Path(candidate).expanduser()
    resolved = shutil.which(candidate)
    path = (Path(resolved) if resolved is not None else source).resolve()
    if path.is_file() and os.access(path, os.X_OK):
        return path
    raise ListenerPlatformError(f"installed Gogurt executable is absent or not executable: {path}")


def linux_mount_points(mountinfo: str) -> tuple[Path, ...]:
    points = {
        Path(_MOUNTINFO_ESCAPE_RE.sub(lambda match: chr(int(match.group(1), 8)), fields[4]))
        for line in mountinfo.splitlines()
        if len(fields := line.split()) >= 5
    }
    return tuple(sorted(points, key=lambda path: str(path)))


def discover_mount_points() -> tuple[Path, ...]:
    return linux_mount_points(Path("/proc/self/mountinfo").read_text(encoding="utf-8"))


__all__ = [
    "SystemdUserAdapter",
    "default_listener_paths",
    "discover_mount_points",
    "linux_mount_points",
    "listener_adapter",
    "render_systemd_unit",
    "resolve_listener_executable",
]
