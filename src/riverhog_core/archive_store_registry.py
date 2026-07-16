from __future__ import annotations

from collections.abc import Mapping

from riverhog_core.ports.archive_store import ArchiveStore


class ArchiveStoreRegistry:
    def __init__(
        self,
        stores: Mapping[str, ArchiveStore],
    ) -> None:
        self._stores = dict(stores)
        if not self._stores:
            raise ValueError("at least one archive store is required")

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(self._stores)

    def require(self, name: str) -> ArchiveStore:
        try:
            return self._stores[name]
        except KeyError as exc:
            raise ValueError(f"archive store is not registered: {name}") from exc

    def items(self) -> tuple[tuple[str, ArchiveStore], ...]:
        return tuple(self._stores.items())
