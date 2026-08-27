"""Portable identity retained for one explicitly selected Gogurt provider."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal, cast

GOGURT_PROVIDER_REFERENCE_FORMAT = "gogurt-provider-reference/v1"
type GogurtProviderKind = Literal["mounted-volume", "listener-host"]


def _canonical(value: object, field: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip() or len(value) > 255:
        raise ValueError(f"Gogurt provider {field} must be a bounded canonical string")
    return value


@dataclass(frozen=True, slots=True)
class GogurtProviderReference:
    """Exact installed-provider identity persisted across listener restarts."""

    kind: GogurtProviderKind
    name: str
    provider_id: str
    distribution: str | None = None
    version: str | None = None
    format: str = GOGURT_PROVIDER_REFERENCE_FORMAT

    def __post_init__(self) -> None:
        if self.format != GOGURT_PROVIDER_REFERENCE_FORMAT:
            raise ValueError("unsupported Gogurt provider reference format")
        if self.kind not in {"mounted-volume", "listener-host"}:
            raise ValueError("unsupported Gogurt provider kind")
        _canonical(self.name, "name")
        _canonical(self.provider_id, "identity")
        if (self.distribution is None) != (self.version is None):
            raise ValueError("Gogurt provider distribution and version must appear together")
        if self.distribution is not None:
            _canonical(self.distribution, "distribution")
            _canonical(self.version, "version")

    def as_dict(self) -> dict[str, str]:
        payload = {
            "format": self.format,
            "kind": self.kind,
            "name": self.name,
            "provider_id": self.provider_id,
        }
        if self.distribution is not None and self.version is not None:
            payload["distribution"] = self.distribution
            payload["version"] = self.version
        return payload

    @classmethod
    def from_mapping(cls, value: object) -> GogurtProviderReference:
        if not isinstance(value, Mapping):
            raise ValueError("Gogurt provider reference must be an object")
        required = {"format", "kind", "name", "provider_id"}
        optional = {"distribution", "version"}
        if not required <= set(value) or set(value) - required - optional:
            raise ValueError("Gogurt provider reference fields are invalid")
        return cls(
            format=cast(str, value["format"]),
            kind=cast(GogurtProviderKind, value["kind"]),
            name=cast(str, value["name"]),
            provider_id=cast(str, value["provider_id"]),
            distribution=cast(str | None, value.get("distribution")),
            version=cast(str | None, value.get("version")),
        )


__all__ = [
    "GOGURT_PROVIDER_REFERENCE_FORMAT",
    "GogurtProviderKind",
    "GogurtProviderReference",
]
