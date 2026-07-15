from __future__ import annotations

from collections.abc import Mapping

from riverhog_core.ports.archive_store import ArchiveStore


class ArchiveStoreRegistry:
    def __init__(
        self,
        stores: Mapping[str, ArchiveStore],
        *,
        default_store: str,
    ) -> None:
        self._stores = dict(stores)
        self.default_store = default_store
        if not self._stores:
            raise ValueError("at least one archive store is required")
        if default_store not in self._stores:
            raise ValueError(f"default archive store is not registered: {default_store}")

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
