from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from riverhog_core.ports.archive_ingress_store import ArchiveMultipartObjectStore
from riverhog_core.ports.archive_manifest_store import ImmutableArchiveObjectStore
from riverhog_core.ports.archive_range_store import ArchiveObjectRangeStore


@dataclass(frozen=True, slots=True)
class ArchiveIngressStore:
    multipart: ArchiveMultipartObjectStore
    root: ImmutableArchiveObjectStore
    ranges: ArchiveObjectRangeStore


class ArchiveIngressStoreRegistry:
    def __init__(self, stores: Mapping[str, ArchiveIngressStore]) -> None:
        self._stores = dict(stores)
        if not self._stores:
            raise ValueError("at least one archive ingress store is required")

    def require(self, name: str) -> ArchiveIngressStore:
        try:
            return self._stores[name]
        except KeyError as exc:
            raise ValueError(f"archive ingress store is not registered: {name}") from exc
