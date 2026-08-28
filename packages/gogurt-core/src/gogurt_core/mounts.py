from __future__ import annotations

import math
import re
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable

from gogurt_core.providers import GogurtProviderReference

MIN_GOGURT_INTERVAL_SECONDS = 0.1
MAX_GOGURT_INTERVAL_SECONDS = 3600.0
GOGURT_MOUNTED_VOLUME_PROVIDER_ENTRY_POINT_GROUP = "gogurt.mounted-volume-providers"
GOGURT_MOUNTED_VOLUME_PROVIDER_BINDING_FORMAT = "gogurt-mounted-volume-provider-binding/v1"
GOGURT_ROUTE_MARKER_FORMAT = "gogurt-route-marker/v1"
GOGURT_ROUTE_PATTERN = r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$"
MAX_GOGURT_MARKER_IDENTITY_CHARS = 1024
_GOGURT_ROUTE_RE = re.compile(GOGURT_ROUTE_PATTERN)
type MountDiscovery = Callable[[], Sequence[Path]]


@dataclass(frozen=True, slots=True)
class GogurtRouteMarker:
    """Portable logical route marker owned by Gogurt core."""

    route: str
    format: str = GOGURT_ROUTE_MARKER_FORMAT

    def __post_init__(self) -> None:
        if self.format != GOGURT_ROUTE_MARKER_FORMAT:
            raise ValueError("unsupported Gogurt route marker format")
        if not isinstance(self.route, str) or _GOGURT_ROUTE_RE.fullmatch(self.route) is None:
            raise ValueError(f"invalid Gogurt route: {self.route!r}")

    def as_dict(self) -> dict[str, str]:
        return {"format": self.format, "route": self.route}

    @classmethod
    def from_mapping(cls, value: object) -> GogurtRouteMarker:
        if not isinstance(value, Mapping) or set(value) != {"format", "route"}:
            raise ValueError("Gogurt route marker fields are invalid")
        route = value["route"]
        format_value = value["format"]
        if not isinstance(route, str) or not isinstance(format_value, str):
            raise ValueError("Gogurt route marker fields are invalid")
        return cls(route=route, format=format_value)


@dataclass(frozen=True, slots=True)
class MountedMarkerObservation:
    """Logical marker and a provider-owned restart-stable observation identity.

    An unchanged marker at the same mounted root must retain this opaque identity
    across provider and process restarts. Any change relevant to Gogurt's
    unchanged-before-dispatch check must change it.
    """

    marker: GogurtRouteMarker
    identity: str

    def __post_init__(self) -> None:
        if not isinstance(self.marker, GogurtRouteMarker):
            raise TypeError("Gogurt mounted marker document is invalid")
        if (
            not isinstance(self.identity, str)
            or not self.identity
            or self.identity != self.identity.strip()
            or len(self.identity) > MAX_GOGURT_MARKER_IDENTITY_CHARS
        ):
            raise ValueError("Gogurt mounted marker identity must be bounded and canonical")


@runtime_checkable
class MountedVolumeAccess(Protocol):
    """Complete physical custody of mounted marker observation and publication.

    Gogurt treats returned roots as opaque mounted-volume identifiers except when
    rendering them into an action argv. The provider alone owns discovery, root
    viability, marker location and representation, safe observation, and complete
    publication of Gogurt's logical marker document. Other actors must not mutate
    that provider-owned representation outside the selected provider capability.
    """

    def discover(self) -> Sequence[Path]: ...

    def observe_marker(
        self,
        mount_point: Path,
    ) -> MountedMarkerObservation | None:
        """Return one complete logical observation, or ``None`` when absent."""
        ...

    def publish_marker(
        self,
        mount_point: Path,
        marker: GogurtRouteMarker,
        *,
        expected: MountedMarkerObservation | None,
    ) -> MountedMarkerObservation:
        """Conditionally publish against cooperating provider operations.

        Compatible provider instances must serialize publication and publish only
        when their provider-owned marker is absent or exactly ``expected``. This is
        not a physical compare-and-swap promise against actors that bypass the
        provider and mutate its private representation directly.
        """
        ...


@runtime_checkable
class MountedVolumeProvider(MountedVolumeAccess, Protocol):
    """Selected provider plus its exact persisted application identity."""

    @property
    def reference(self) -> GogurtProviderReference: ...


@dataclass(frozen=True, slots=True)
class MountedVolumeProviderBinding:
    """One exact mounted-volume implementation and representation contract.

    ``provider_id`` must change when representation or relevant configuration
    changes so a persisted listener fails closed rather than reinterpreting media.
    """

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
