"""Explicit composition of independently distributed Gogurt host providers."""

from __future__ import annotations

import importlib.metadata
from dataclasses import dataclass
from pathlib import Path

from gogurt_core.mounts import (
    GOGURT_MOUNT_PROVIDER_ENTRY_POINT_GROUP,
    MountProviderBinding,
)
from gogurt_core.providers import GogurtProviderKind, GogurtProviderReference
from gogurt_listener_runtime.platform import (
    GOGURT_LISTENER_HOST_PROVIDER_ENTRY_POINT_GROUP,
    ListenerAdapter,
    ListenerHostProviderBinding,
    ListenerRuntimePaths,
)


@dataclass(frozen=True, slots=True)
class GogurtProviderMetadata:
    """Installed entry-point metadata available without executing provider code."""

    kind: GogurtProviderKind
    name: str
    value: str
    distribution: str | None
    version: str | None

    def as_dict(self) -> dict[str, str | None]:
        return {
            "kind": self.kind,
            "name": self.name,
            "entry_point": self.value,
            "distribution": self.distribution,
            "version": self.version,
        }


def _entry_points(group: str) -> tuple[importlib.metadata.EntryPoint, ...]:
    return tuple(importlib.metadata.entry_points(group=group))


def _metadata(
    entry_point: importlib.metadata.EntryPoint,
    *,
    kind: GogurtProviderKind,
) -> GogurtProviderMetadata:
    distribution = getattr(entry_point, "dist", None)
    return GogurtProviderMetadata(
        kind=kind,
        name=entry_point.name,
        value=entry_point.value,
        distribution=distribution.name if distribution is not None else None,
        version=distribution.version if distribution is not None else None,
    )


def _list(group: str, *, kind: GogurtProviderKind) -> tuple[GogurtProviderMetadata, ...]:
    return tuple(
        sorted(
            (_metadata(entry_point, kind=kind) for entry_point in _entry_points(group)),
            key=lambda item: (item.name, item.distribution or "", item.value),
        )
    )


def list_mount_providers() -> tuple[GogurtProviderMetadata, ...]:
    """List mount-provider metadata without loading provider code."""

    return _list(GOGURT_MOUNT_PROVIDER_ENTRY_POINT_GROUP, kind="mount")


def list_listener_host_providers() -> tuple[GogurtProviderMetadata, ...]:
    """List listener-host metadata without loading provider code."""

    return _list(GOGURT_LISTENER_HOST_PROVIDER_ENTRY_POINT_GROUP, kind="listener-host")


def _resolve_entry_point(group: str, name: str) -> importlib.metadata.EntryPoint:
    if not name or name != name.strip() or len(name) > 255:
        raise ValueError("Gogurt provider name must be a bounded canonical string")
    matches = [entry_point for entry_point in _entry_points(group) if entry_point.name == name]
    if len(matches) != 1:
        raise ValueError(f"Gogurt provider must resolve exactly once: {name}")
    return matches[0]


def _reference(
    metadata: GogurtProviderMetadata,
    *,
    provider_id: str,
) -> GogurtProviderReference:
    return GogurtProviderReference(
        kind=metadata.kind,
        name=metadata.name,
        provider_id=provider_id,
        distribution=metadata.distribution,
        version=metadata.version,
    )


def _require_expected(
    reference: GogurtProviderReference,
    expected: GogurtProviderReference | None,
) -> None:
    if expected is not None and reference != expected:
        raise ValueError(
            f"installed Gogurt {reference.kind} provider differs from persisted identity: "
            f"{reference.name}"
        )


@dataclass(frozen=True, slots=True)
class ResolvedMountProvider:
    metadata: GogurtProviderMetadata
    binding: MountProviderBinding
    reference: GogurtProviderReference

    def discover(self) -> tuple[Path, ...]:
        discovered = self.binding.discover()
        if isinstance(discovered, (str, bytes)):
            raise TypeError("Gogurt mount provider returned invalid mount paths")
        values = tuple(discovered)
        if any(not isinstance(value, Path) for value in values):
            raise TypeError("Gogurt mount provider returned invalid mount paths")
        return values

    def as_dict(self) -> dict[str, object]:
        return {**self.metadata.as_dict(), "reference": self.reference.as_dict()}


@dataclass(frozen=True, slots=True)
class ResolvedListenerHostProvider:
    metadata: GogurtProviderMetadata
    binding: ListenerHostProviderBinding
    reference: GogurtProviderReference

    def paths(self) -> ListenerRuntimePaths:
        value = self.binding.paths()
        if not isinstance(value, ListenerRuntimePaths):
            raise TypeError("Gogurt listener-host provider returned invalid runtime paths")
        return value

    def adapter(self) -> ListenerAdapter:
        value = self.binding.adapter()
        if not isinstance(value, ListenerAdapter):
            raise TypeError("Gogurt listener-host provider returned an invalid adapter")
        return value

    def executable(self, raw: str | None = None) -> Path:
        value = self.binding.executable(raw)
        if not isinstance(value, Path):
            raise TypeError("Gogurt listener-host provider returned an invalid executable path")
        return value

    def as_dict(self) -> dict[str, object]:
        return {**self.metadata.as_dict(), "reference": self.reference.as_dict()}


def resolve_mount_provider(
    name: str,
    *,
    expected: GogurtProviderReference | None = None,
) -> ResolvedMountProvider:
    entry_point = _resolve_entry_point(GOGURT_MOUNT_PROVIDER_ENTRY_POINT_GROUP, name)
    binding = entry_point.load()
    if not isinstance(binding, MountProviderBinding):
        raise TypeError(f"Gogurt mount provider has an invalid binding: {name}")
    metadata = _metadata(entry_point, kind="mount")
    reference = _reference(metadata, provider_id=binding.provider_id)
    _require_expected(reference, expected)
    return ResolvedMountProvider(metadata=metadata, binding=binding, reference=reference)


def resolve_listener_host_provider(
    name: str,
    *,
    expected: GogurtProviderReference | None = None,
) -> ResolvedListenerHostProvider:
    entry_point = _resolve_entry_point(GOGURT_LISTENER_HOST_PROVIDER_ENTRY_POINT_GROUP, name)
    binding = entry_point.load()
    if not isinstance(binding, ListenerHostProviderBinding):
        raise TypeError(f"Gogurt listener-host provider has an invalid binding: {name}")
    metadata = _metadata(entry_point, kind="listener-host")
    reference = _reference(metadata, provider_id=binding.provider_id)
    _require_expected(reference, expected)
    return ResolvedListenerHostProvider(metadata=metadata, binding=binding, reference=reference)


__all__ = [
    "GogurtProviderMetadata",
    "ResolvedListenerHostProvider",
    "ResolvedMountProvider",
    "list_listener_host_providers",
    "list_mount_providers",
    "resolve_listener_host_provider",
    "resolve_mount_provider",
]
