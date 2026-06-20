from __future__ import annotations

from typing import Protocol

from riverhog_core.domain.models import CollectionSummary, FileRef, PinSummary, Target
from riverhog_core.domain.types import CollectionId, TargetStr


class CatalogRepo(Protocol):
    def collection_exists(self, collection_id: CollectionId) -> bool: ...
    def create_collection_from_scan(
        self, collection_id: CollectionId, staging_path: str
    ) -> CollectionSummary: ...
    def get_collection_summary(self, collection_id: CollectionId) -> CollectionSummary: ...
    def search(
        self,
        *,
        q: str | None,
        page: int,
        per_page: int,
        sort: str,
        order: str,
        collection: str | None = None,
        hot: bool | None = None,
        archived: bool | None = None,
    ) -> dict[str, object]: ...
    def resolve_target_files(self, target: Target) -> list[FileRef]: ...
    def list_pins(self) -> list[PinSummary]: ...
    def has_exact_pin(self, target: TargetStr) -> bool: ...
    def add_pin(self, target: TargetStr) -> None: ...
    def remove_pin(self, target: TargetStr) -> None: ...
