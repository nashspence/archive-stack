from __future__ import annotations

from typing import Protocol

from arc_core.domain.models import (
    CollectionSummary,
    FetchSummary,
    GlacierUsageReport,
    PinSummary,
    RecoverySessionSummary,
)


class CollectionService(Protocol):
    def create_or_resume_upload(
        self,
        *,
        collection_id: str,
        files: list[dict[str, object]],
        ingest_source: str | None = None,
    ) -> object: ...
    def get_upload(self, collection_id: str) -> object: ...
    def create_or_resume_file_upload(self, collection_id: str, path: str) -> object: ...
    def append_upload_chunk(
        self,
        collection_id: str,
        path: str,
        *,
        offset: int,
        checksum: str,
        content: bytes,
    ) -> object: ...
    def get_file_upload(self, collection_id: str, path: str) -> object: ...
    def cancel_file_upload(self, collection_id: str, path: str) -> None: ...
    def expire_stale_uploads(self) -> None: ...
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


class GlacierUploadService(Protocol):
    def process_due_uploads(self, *, limit: int = 1) -> int: ...


class GlacierReportingService(Protocol):
    def get_report(
        self,
        *,
        image_id: str | None = None,
        collection: str | None = None,
    ) -> GlacierUsageReport: ...


class RecoverySessionService(Protocol):
    def get(self, session_id: str) -> RecoverySessionSummary: ...
    def get_for_image(self, image_id: str) -> RecoverySessionSummary: ...
    def create_or_resume_for_image(self, image_id: str) -> RecoverySessionSummary: ...
    def approve(self, session_id: str) -> RecoverySessionSummary: ...
    def complete(self, session_id: str) -> RecoverySessionSummary: ...
    def process_due_sessions(self, *, limit: int = 100) -> int: ...


class CopyService(Protocol):
    def register(self, image_id: str, location: str, *, copy_id: str | None = None) -> object: ...
    def list_for_image(self, image_id: str) -> list[object]: ...
    def update(
        self,
        image_id: str,
        copy_id: str,
        *,
        location: str | None = None,
        state: str | None = None,
        verification_state: str | None = None,
    ) -> object: ...


class PinService(Protocol):
    def pin(self, raw_target: str) -> object: ...
    def release(self, raw_target: str) -> object: ...
    def list_pins(self) -> list[PinSummary]: ...


class FetchService(Protocol):
    def get(self, fetch_id: str) -> FetchSummary: ...
    def manifest(self, fetch_id: str) -> object: ...
    def create_or_resume_upload(self, fetch_id: str, entry_id: str) -> object: ...
    def append_upload_chunk(
        self,
        fetch_id: str,
        entry_id: str,
        *,
        offset: int,
        checksum: str,
        content: bytes,
    ) -> object: ...
    def get_entry_upload(self, fetch_id: str, entry_id: str) -> object: ...
    def cancel_entry_upload(self, fetch_id: str, entry_id: str) -> None: ...
    def expire_stale_uploads(self) -> None: ...
    def complete(self, fetch_id: str) -> object: ...


class FileService(Protocol):
    def list_collection_files(
        self,
        collection_id: str,
        *,
        page: int,
        per_page: int,
    ) -> dict[str, object]: ...
    def query_by_target(
        self,
        raw_target: str,
        *,
        page: int,
        per_page: int,
    ) -> dict[str, object]: ...
    def get_content(self, raw_target: str) -> bytes: ...
