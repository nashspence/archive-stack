from __future__ import annotations

import math
import time
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path

MIN_GOGURT_INTERVAL_SECONDS = 0.1
MAX_GOGURT_INTERVAL_SECONDS = 3600.0
GOGURT_MOUNT_PROVIDER_ENTRY_POINT_GROUP = "gogurt.mount-providers"
GOGURT_MOUNT_PROVIDER_BINDING_FORMAT = "gogurt-mount-provider-binding/v1"
type MountDiscovery = Callable[[], Sequence[Path]]


@dataclass(frozen=True, slots=True)
class MountProviderBinding:
    """One independently distributed mount-discovery implementation."""

    provider_id: str
    discover: MountDiscovery
    format: str = GOGURT_MOUNT_PROVIDER_BINDING_FORMAT

    def __post_init__(self) -> None:
        if self.format != GOGURT_MOUNT_PROVIDER_BINDING_FORMAT:
            raise ValueError("unsupported Gogurt mount-provider binding format")
        if (
            not self.provider_id
            or self.provider_id != self.provider_id.strip()
            or len(self.provider_id) > 255
        ):
            raise ValueError("Gogurt mount-provider identity must be canonical")
        if not callable(self.discover):
            raise TypeError("Gogurt mount-provider discovery must be callable")


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
