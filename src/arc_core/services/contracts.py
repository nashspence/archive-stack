from __future__ import annotations

from typing import Protocol

from arc_core.domain.models import CollectionSummary, FetchSummary, PinSummary


class CollectionService(Protocol):
    def close(self, staging_path: str) -> CollectionSummary: ...
    def get(self, collection_id: str) -> CollectionSummary: ...


class SearchService(Protocol):
    def search(self, query: str, limit: int) -> list[object]: ...


class PlanningService(Protocol):
    def get_plan(
        self,
        *,
        page: int,
        per_page: int,
        sort: str,
        order: str,
        q: str | None,
        collection: str | None,
        iso_ready: bool | None,
    ) -> object: ...
    def list_images(
        self,
        *,
        page: int,
        per_page: int,
        sort: str,
        order: str,
        q: str | None,
        collection: str | None,
        has_copies: bool | None,
    ) -> object: ...
    def get_image(self, image_id: str) -> object: ...
    def finalize_image(self, image_id: str) -> object: ...
    def get_iso_stream(self, image_id: str) -> object: ...


class CopyService(Protocol):
    def register(self, image_id: str, copy_id: str, location: str) -> object: ...


class PinService(Protocol):
    def pin(self, raw_target: str) -> object: ...
    def release(self, raw_target: str) -> object: ...
    def list_pins(self) -> list[PinSummary]: ...


class FetchService(Protocol):
    def get(self, fetch_id: str) -> FetchSummary: ...
    def manifest(self, fetch_id: str) -> object: ...
    def create_or_resume_upload(self, fetch_id: str, entry_id: str) -> object: ...
    def append_upload_chunk(
        self, fetch_id: str, entry_id: str, offset: int, checksum: str, content: bytes
    ) -> object: ...
    def complete(self, fetch_id: str) -> object: ...
