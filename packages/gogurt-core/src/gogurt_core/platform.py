"""Ports implemented by one native Gogurt platform package."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


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
    stop_file: Path
    registration_file: Path | None


@dataclass(frozen=True, slots=True)
class NativeListenerStatus:
    installed: bool
    enabled: bool
    running: bool


class ListenerAdapter(Protocol):
    def register(self, paths: ListenerPaths, command: Sequence[str]) -> None: ...

    def status(self, paths: ListenerPaths) -> NativeListenerStatus: ...

    def start(self, paths: ListenerPaths) -> None: ...

    def stop(self, paths: ListenerPaths) -> None: ...

    def unregister(self, paths: ListenerPaths) -> None: ...

    def process_is_running(self, pid: int) -> bool: ...


__all__ = [
    "ListenerAdapter",
    "ListenerPaths",
    "ListenerPlatformError",
    "NativeListenerStatus",
]
