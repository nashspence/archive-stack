from __future__ import annotations

import math
import time
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable

from gogurt_core.providers import GogurtProviderReference

MIN_GOGURT_INTERVAL_SECONDS = 0.1
MAX_GOGURT_INTERVAL_SECONDS = 3600.0
GOGURT_MOUNTED_VOLUME_PROVIDER_ENTRY_POINT_GROUP = "gogurt.mounted-volume-providers"
GOGURT_MOUNTED_VOLUME_PROVIDER_BINDING_FORMAT = "gogurt-mounted-volume-provider-binding/v1"
MAX_GOGURT_MARKER_IDENTITY_CHARS = 1024
type MountDiscovery = Callable[[], Sequence[Path]]


@dataclass(frozen=True, slots=True)
class MountedMarkerSnapshot:
    """Raw marker bytes and a provider-owned restart-stable observation identity.

    An unchanged marker at the same mounted root must retain this opaque identity
    across provider and process restarts. Any change relevant to Gogurt's
    unchanged-before-dispatch check must change it.
    """

    content: bytes
    identity: str

    def __post_init__(self) -> None:
        if not isinstance(self.content, bytes):
            raise TypeError("Gogurt mounted marker content must be bytes")
        if (
            not isinstance(self.identity, str)
            or not self.identity
            or self.identity != self.identity.strip()
            or len(self.identity) > MAX_GOGURT_MARKER_IDENTITY_CHARS
        ):
            raise ValueError("Gogurt mounted marker identity must be bounded and canonical")


@runtime_checkable
class MountedVolumeAccess(Protocol):
    """Complete physical custody of path-mounted marker observation and publication.

    Gogurt treats returned roots as opaque mounted-volume identifiers except when
    rendering them into an action argv. The provider alone owns discovery, root
    viability, safe bounded observation, and complete marker publication.
    """

    def discover(self) -> Sequence[Path]: ...

    def observe_marker(
        self,
        mount_point: Path,
        marker_name: str,
        *,
        max_bytes: int,
    ) -> MountedMarkerSnapshot | None:
        """Return one complete bounded snapshot, or ``None`` when absent."""
        ...

    def publish_marker(
        self,
        mount_point: Path,
        marker_name: str,
        content: bytes,
    ) -> MountedMarkerSnapshot:
        """Publish the complete content and return its resulting snapshot."""
        ...


@runtime_checkable
class MountedVolumeProvider(MountedVolumeAccess, Protocol):
    """Selected provider plus its exact persisted application identity."""

    @property
    def reference(self) -> GogurtProviderReference: ...


@dataclass(frozen=True, slots=True)
class MountedVolumeProviderBinding:
    """One independently distributed mounted-volume implementation."""

    provider_id: str
    access: MountedVolumeAccess
    format: str = GOGURT_MOUNTED_VOLUME_PROVIDER_BINDING_FORMAT

    def __post_init__(self) -> None:
        if self.format != GOGURT_MOUNTED_VOLUME_PROVIDER_BINDING_FORMAT:
            raise ValueError("unsupported Gogurt mounted-volume provider binding format")
        if (
            not self.provider_id
            or self.provider_id != self.provider_id.strip()
            or len(self.provider_id) > 255
        ):
            raise ValueError("Gogurt mounted-volume provider identity must be canonical")
        if not isinstance(self.access, MountedVolumeAccess):
            raise TypeError("Gogurt mounted-volume provider access is invalid")


def validate_gogurt_interval(value: object) -> float:
    """Return a finite polling interval within the supported v1 range."""

    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("Gogurt polling interval must be a number")
    interval = float(value)
    if not math.isfinite(interval):
        raise ValueError("Gogurt polling interval must be finite")
    if not MIN_GOGURT_INTERVAL_SECONDS <= interval <= MAX_GOGURT_INTERVAL_SECONDS:
        raise ValueError(
            "Gogurt polling interval must be between "
            f"{MIN_GOGURT_INTERVAL_SECONDS} and {MAX_GOGURT_INTERVAL_SECONDS} seconds"
        )
    return interval


def iter_new_mounts(
    *,
    discover: Callable[[], Sequence[Path]],
    interval_seconds: float = 2.0,
    include_existing: bool = False,
    sleep: Callable[[float], None] = time.sleep,
) -> Iterator[Path]:
    interval = validate_gogurt_interval(interval_seconds)
    known = set() if include_existing else set(discover())
    while True:
        current = set(discover())
        yield from sorted(current - known, key=lambda path: str(path).casefold())
        known = current
        sleep(interval)
