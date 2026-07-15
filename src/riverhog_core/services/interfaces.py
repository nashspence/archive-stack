from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol, TypedDict

from riverhog_core.domain.models import (
    ArchiveRestoreListPage,
    ArchiveRestoreSummary,
    ArchiveUsageReport,
    CollectionListPage,
    CollectionSummary,
    FetchListPage,
    FetchSummary,
)

JsonObject = dict[str, object]


class FileStatePayload(TypedDict):
    logical_path: str
    collection_id: str
    collection_path: str
    bytes: int
    sha256: str
    hot: bool


class FilesPayload(TypedDict):
    path: str
    page: int
    per_page: int
    total: int
    pages: int
    files: list[FileStatePayload]


class CollectionService(Protocol):
    def create_or_resume_upload(
        self,
        *,
        upload_slug: str,
        files: list[dict[str, object]],
        ingest_source: str | None = None,
        upload_timestamp: str | None = None,
        retain_hot: bool = False,
        notify: dict[str, object] | None = None,
    ) -> JsonObject: ...
    def create_or_resume_upload_session(
        self,
        *,
        upload_slug: str,
        ingest_source: str | None = None,
        upload_timestamp: str | None = None,
        retain_hot: bool = False,
        notify: dict[str, object] | None = None,
    ) -> JsonObject: ...
    def register_upload_session_file(
        self,
        collection_id: str,
        file: dict[str, object],
    ) -> JsonObject: ...
    def create_or_resume_registered_file_upload(
        self,
        collection_id: str,
        file: dict[str, object],
    ) -> JsonObject: ...
    def sync_finished_upload_target(self, target_path: str) -> JsonObject | None: ...
    def complete_upload_session(self, collection_id: str) -> JsonObject: ...
    def cancel_upload_session(self, collection_id: str) -> JsonObject: ...
    def get_upload(self, collection_id: str) -> JsonObject: ...
    def create_or_resume_file_upload(self, collection_id: str, path: str) -> JsonObject: ...
    def append_upload_chunk(
        self,
        collection_id: str,
        path: str,
        *,
        offset: int,
        checksum: str,
        content: bytes,
    ) -> JsonObject: ...
    def get_file_upload(self, collection_id: str, path: str) -> JsonObject: ...
    def cancel_file_upload(self, collection_id: str, path: str) -> None: ...
    def expire_stale_uploads(self) -> None: ...
    def get(self, collection_id: str) -> CollectionSummary: ...
    def list(
        self,
        *,
        page: int,
        per_page: int,
        q: str | None,
        sort: str = "id",
        order: str = "asc",
        all_items: bool = False,
    ) -> CollectionListPage: ...


class CollectionDeletionService(Protocol):
    def plan(self, collection_id: str) -> JsonObject: ...
    def delete(self, collection_id: str, *, challenge: str) -> JsonObject: ...


class SearchService(Protocol):
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
    ) -> JsonObject: ...


class ArchiveUploadService(Protocol):
    def requeue_failed_uploads_for_startup(self, *, limit: int = 100) -> int: ...
    def publish_restore_catalog(self) -> int: ...
    def process_due_uploads(self, *, limit: int = 1) -> int: ...


class ArchiveReportingService(Protocol):
    def get_report(self, *, collection: str | None = None) -> ArchiveUsageReport: ...


class ArchiveRestoreService(Protocol):
    def list(
        self,
        *,
        page: int,
        per_page: int,
        sort: str,
        order: str,
        terminal: str = "all",
        state: str | None = None,
        collection: str | None = None,
    ) -> ArchiveRestoreListPage: ...
    def get(self, restore_id: str) -> ArchiveRestoreSummary: ...
    def create_or_resume_for_collection(self, collection_id: str) -> ArchiveRestoreSummary: ...
    def list_for_fetch(
        self,
        fetch_id: str,
        *,
        page: int,
        per_page: int,
        sort: str,
        order: str,
        state: str | None = None,
    ) -> ArchiveRestoreListPage: ...
    def create_or_resume_for_fetch(self, fetch_id: str) -> ArchiveRestoreListPage: ...
    def cancel_for_fetch(self, fetch_id: str) -> ArchiveRestoreListPage: ...
    def cancel(self, restore_id: str) -> ArchiveRestoreSummary: ...
    def process_due_restores(self, *, limit: int = 100) -> int: ...
    def repair_missing_fetch_hot_files(self, *, limit: int = 100) -> int: ...


class FetchService(Protocol):
    def create(self, *, name: str, collections: Sequence[str] | None = None) -> FetchSummary: ...
    def list(
        self,
        *,
        page: int,
        per_page: int,
        state: str | None = None,
        q: str | None = None,
        sort: str = "order",
        order: str = "asc",
        all_items: bool = False,
    ) -> FetchListPage: ...
    def add_collections(self, fetch_id: str, collections: Sequence[str]) -> FetchSummary: ...
    def remove_collections(self, fetch_id: str, collections: Sequence[str]) -> FetchSummary: ...
    def start(self, fetch_id: str) -> FetchSummary: ...
    def cancel(self, fetch_id: str) -> FetchSummary: ...
    def evict(self, collections: Sequence[str], *, dry_run: bool = False) -> JsonObject: ...
    def get(self, fetch_id: str) -> FetchSummary: ...
    def status(self, fetch_id: str) -> JsonObject: ...
    def files(
        self,
        fetch_id: str,
        *,
        page: int,
        per_page: int,
        sort: str,
        order: str,
        q: str | None = None,
        hot: bool | None = None,
    ) -> JsonObject: ...


class FileService(Protocol):
    def query_by_path(
        self,
        raw_path: str,
        *,
        page: int,
        per_page: int,
    ) -> dict[str, object]: ...
    def get_content(self, raw_path: str) -> bytes: ...
