"""Port implemented by one native Gogurt platform package."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable

GOGURT_LISTENER_HOST_PROVIDER_ENTRY_POINT_GROUP = "gogurt.listener-host-providers"
GOGURT_LISTENER_HOST_PROVIDER_BINDING_FORMAT = "gogurt-listener-host-provider-binding/v1"


class ListenerPlatformError(RuntimeError):
    """The native per-user service manager rejected a listener operation."""


@dataclass(frozen=True, slots=True)
class ListenerRuntimePaths:
    state_dir: Path
    config_file: Path
    database_file: Path
    heartbeat_file: Path
    lock_file: Path
    log_file: Path
    stop_file: Path


@dataclass(frozen=True, slots=True)
class NativeListenerStatus:
    installed: bool
    enabled: bool
    running: bool


@runtime_checkable
class ListenerAdapter(Protocol):
    def register(self, paths: ListenerRuntimePaths, command: Sequence[str]) -> None: ...

    def status(self, paths: ListenerRuntimePaths) -> NativeListenerStatus: ...

    def start(self, paths: ListenerRuntimePaths) -> None: ...

    def stop(self, paths: ListenerRuntimePaths) -> None: ...

    def unregister(self, paths: ListenerRuntimePaths) -> None: ...

    def process_is_running(self, pid: int) -> bool: ...


@dataclass(frozen=True, slots=True)
class ListenerHostProviderBinding:
    """One independently distributed native listener-host implementation."""

    provider_id: str
    paths: Callable[[], ListenerRuntimePaths]
    adapter: Callable[[], ListenerAdapter]
    executable: Callable[[str | None], Path]
    format: str = GOGURT_LISTENER_HOST_PROVIDER_BINDING_FORMAT

    def __post_init__(self) -> None:
        if self.format != GOGURT_LISTENER_HOST_PROVIDER_BINDING_FORMAT:
            raise ValueError("unsupported Gogurt listener-host binding format")
        if (
            not self.provider_id
            or self.provider_id != self.provider_id.strip()
            or len(self.provider_id) > 255
        ):
            raise ValueError("Gogurt listener-host provider identity must be canonical")
        if not all(callable(value) for value in (self.paths, self.adapter, self.executable)):
            raise TypeError("Gogurt listener-host provider capabilities must be callable")


__all__ = [
    "GOGURT_LISTENER_HOST_PROVIDER_BINDING_FORMAT",
    "GOGURT_LISTENER_HOST_PROVIDER_ENTRY_POINT_GROUP",
    "ListenerAdapter",
    "ListenerHostProviderBinding",
    "ListenerRuntimePaths",
    "ListenerPlatformError",
    "NativeListenerStatus",
]
