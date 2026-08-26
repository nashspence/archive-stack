"""macOS mount discovery and launchd integration for Gogurt."""

from __future__ import annotations

import os
import plistlib
import re
import shutil
import subprocess
import time
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
LAUNCHD_SETTLE_SECONDS = 20.0


def default_listener_paths(
    *,
    environment: Mapping[str, str] | None = None,
    home: Path | None = None,
) -> ListenerRuntimePaths:
    del environment
    user_home = (home or Path.home()).expanduser().resolve()
    state_dir = user_home / "Library" / "Application Support" / "Gogurt"
    return ListenerRuntimePaths(
        state_dir=state_dir,
        config_file=state_dir / "listener.json",
        database_file=state_dir / "listener.sqlite3",
        heartbeat_file=state_dir / "heartbeat.json",
        lock_file=state_dir / "listener.lock",
        log_file=state_dir / "listener.log",
        stop_file=state_dir / "stop.request",
    )


def _default_registration_file(*, home: Path | None = None) -> Path:
    user_home = (home or Path.home()).expanduser().resolve()
    return user_home / "Library" / "LaunchAgents" / f"{LISTENER_LABEL}.plist"


def render_launchd_plist(command: Sequence[str]) -> bytes:
    return plistlib.dumps(
        {
            "Label": LISTENER_LABEL,
            "ProgramArguments": list(command),
            "RunAtLoad": True,
            "KeepAlive": {"SuccessfulExit": False},
            "ThrottleInterval": 5,
            "StandardOutPath": "/dev/null",
            "StandardErrorPath": "/dev/null",
        },
        fmt=plistlib.FMT_XML,
        sort_keys=False,
    )


class LaunchdUserAdapter:
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

    @staticmethod
    def _domain() -> str:
        getuid = getattr(os, "getuid", None)
        if getuid is None:
            raise ListenerPlatformError("launchd user identity is unavailable")
        return f"gui/{getuid()}"

    def _target(self) -> str:
        return f"{self._domain()}/{LISTENER_LABEL}"

    def _print(self) -> subprocess.CompletedProcess[str]:
        return self._run(
            ["launchctl", "print", self._target()],
            allowed=frozenset({0, 3, 113}),
        )

    def _wait_unloaded(self) -> None:
        deadline = time.monotonic() + LAUNCHD_SETTLE_SECONDS
        while True:
            if self._print().returncode != 0:
                return
            if time.monotonic() >= deadline:
                raise ListenerPlatformError("launchd did not unload the Gogurt listener")
            time.sleep(0.1)

    @staticmethod
    def _is_running(completed: subprocess.CompletedProcess[str]) -> bool:
        if completed.returncode != 0:
            return False
        state = re.search(r"(?m)^\s*state\s*=\s*([^\s]+)\s*$", completed.stdout)
        pid = re.search(r"(?m)^\s*pid\s*=\s*([0-9]+)\s*$", completed.stdout)
        return (
            state is not None
            and state.group(1).casefold() == "running"
            and pid is not None
            and int(pid.group(1)) > 0
        )

    def register(self, paths: ListenerRuntimePaths, command: Sequence[str]) -> None:
        del paths
        registration = self.registration_file
        registration.parent.mkdir(parents=True, exist_ok=True)
        atomic_write(registration, render_launchd_plist(command), mode=PRIVATE_FILE_MODE)
        self._run(["launchctl", "bootstrap", self._domain(), str(registration)])

    def status(self, paths: ListenerRuntimePaths) -> NativeListenerStatus:
        del paths
        registration = self.registration_file
        state = self._print()
        loaded = state.returncode == 0
        installed = registration.is_file() or loaded
        return NativeListenerStatus(
            installed=installed,
            enabled=installed,
            running=self._is_running(state),
        )

    def start(self, paths: ListenerRuntimePaths) -> None:
        del paths
        registration = self.registration_file
        state = self._print()
        if state.returncode != 0:
            if not registration.is_file():
                raise ListenerPlatformError("Gogurt listener is not installed")
            self._run(["launchctl", "bootstrap", self._domain(), str(registration)])
        elif not self._is_running(state):
            self._run(["launchctl", "kickstart", self._target()])

    def stop(self, paths: ListenerRuntimePaths) -> None:
        del paths
        if self._print().returncode == 0:
            self._run(
                ["launchctl", "bootout", self._target()],
                allowed=frozenset({0, 3, 113}),
            )
            self._wait_unloaded()

    def unregister(self, paths: ListenerRuntimePaths) -> None:
        registration = self.registration_file
        self.stop(paths)
        registration.unlink(missing_ok=True)

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
    del environment
    return LaunchdUserAdapter(_default_registration_file(home=home))


def resolve_listener_executable(raw: str | None = None) -> Path:
    import sys

    candidate = raw or sys.argv[0]
    source = Path(candidate).expanduser()
    resolved = shutil.which(candidate)
    path = (Path(resolved) if resolved is not None else source).resolve()
    if path.is_file() and os.access(path, os.X_OK):
        return path
    raise ListenerPlatformError(f"installed Gogurt executable is absent or not executable: {path}")


def macos_mount_points(volumes_dir: Path = Path("/Volumes")) -> tuple[Path, ...]:
    points = {Path("/")}
    if volumes_dir.is_dir():
        points.update(volumes_dir.iterdir())
    return tuple(sorted(points, key=lambda path: str(path).casefold()))


def discover_mount_points() -> tuple[Path, ...]:
    return macos_mount_points()


__all__ = [
    "LaunchdUserAdapter",
    "default_listener_paths",
    "discover_mount_points",
    "listener_adapter",
    "macos_mount_points",
    "render_launchd_plist",
    "resolve_listener_executable",
]
