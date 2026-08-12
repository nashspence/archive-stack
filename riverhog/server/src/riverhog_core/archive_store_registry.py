from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from riverhog_core.ports.archive_objects import (
    ArchiveMultipartObjectStore,
    ArchiveObjectRangeStore,
    ImmutableArchiveObjectStore,
)
from riverhog_core.ports.archive_store import ArchiveStore


@dataclass(frozen=True, slots=True)
class ArchiveStoreBinding:
    """All capabilities of one configured archive store."""

    store: ArchiveStore
    multipart_objects: ArchiveMultipartObjectStore
    immutable_objects: ImmutableArchiveObjectStore
    object_ranges: ArchiveObjectRangeStore


class ArchiveStoreRegistry:
    def __init__(
        self,
        stores: Mapping[str, ArchiveStoreBinding],
    ) -> None:
        self._stores = dict(stores)
        if not self._stores:
            raise ValueError("at least one archive store is required")

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(self._stores)

    def require(self, name: str) -> ArchiveStoreBinding:
        try:
            return self._stores[name]
        except KeyError as exc:
            raise ValueError(f"archive store is not registered: {name}") from exc

    def items(self) -> tuple[tuple[str, ArchiveStoreBinding], ...]:
        return tuple(self._stores.items())
