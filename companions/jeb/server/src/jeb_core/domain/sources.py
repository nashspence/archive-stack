"""Jeb source identity and scheduling configuration."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

Cadence = Literal["weekly", "monthly", "seasonal", "manual"]
Cleanup = Literal["never", "after_target_success"]


class SourceRegistryError(ValueError):
    pass


class SourceNotFoundError(SourceRegistryError):
    pass


@dataclass(frozen=True)
class SourceConfig:
    id: str
    enabled: bool
    path: Path
    adapters: tuple[str, ...]
    stable_seconds: int
    include_extensions: frozenset[str]
    target: str
    target_config: dict[str, Any]
    threshold_bytes: int
    cleanup: Cleanup
    cadence: Cadence
    weekday: int
    hour: int
    minute: int

    def summary(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "enabled": self.enabled,
            "adapters": list(self.adapters),
            "stable_seconds": self.stable_seconds,
            "include_extensions": sorted(self.include_extensions),
            "target": self.target,
            "target_config": self.target_config,
            "threshold_bytes": self.threshold_bytes,
            "cleanup": self.cleanup,
            "cadence": self.cadence,
            "weekday": self.weekday,
            "hour": self.hour,
            "minute": self.minute,
        }
