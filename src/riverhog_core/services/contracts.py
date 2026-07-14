from __future__ import annotations

from collections.abc import AsyncIterable, Awaitable, Iterable, Sequence
from typing import Protocol, TypedDict

from riverhog_core.domain.models import (
    ArchiveRestoreListPage,
    ArchiveRestoreSummary,
    ArchiveUsageReport,
    CollectionListPage,
    CollectionSummary,
    DiscSummary,
    FetchListPage,
    FetchSummary,
)
from riverhog_core.iso.streaming import IsoStream

JsonObject = dict[str, object]
IsoBody = AsyncIterable[bytes] | Iterable[bytes]
IsoStreamResult = IsoStream | IsoBody
PlanningIsoResult = IsoStreamResult | Awaitable[IsoStreamResult]


class FileStatePayload(TypedDict):
    target: str
    collection: str
    path: str
    bytes: int
    sha256: str
    hot: bool
    disc_coverage: bool


class FilesPayload(TypedDict):
    target: str
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
        notify: dict[str, object] | None = None,
    ) -> JsonObject: ...
    def create_or_resume_upload_session(
        self,
        *,
        upload_slug: str,
        ingest_source: str | None = None,
        upload_timestamp: str | None = None,
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
    def get(
        self,
        collection_id: str,
        *,
        coverage_path_limit: int = 100,
    ) -> CollectionSummary: ...
    def list(
        self,
        *,
        page: int,
        per_page: int,
        q: str | None,
        disc_redundancy_state: str | None,
        sort: str = "id",
        order: str = "asc",
    ) -> CollectionListPage: ...


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
        disc_coverage: bool | None = None,
    ) -> dict[str, object]: ...


class PlanningService(Protocol):
    def process_due_refresh(self, *, limit: int = 1) -> int: ...
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
    ) -> JsonObject: ...
    def list_images(
        self,
        *,
        page: int,
        per_page: int,
        sort: str,
        order: str,
        q: str | None,
        collection: str | None,
        has_discs: bool | None,
    ) -> JsonObject: ...
    def get_image(self, image_id: str) -> JsonObject: ...
    def finalize_image(self, image_id: str) -> JsonObject: ...
    def get_iso_stream(self, image_id: str) -> PlanningIsoResult: ...


class ArchiveUploadService(Protocol):
    def requeue_failed_uploads_for_startup(self, *, limit: int = 100) -> int: ...
    def publish_restore_catalog(self) -> int: ...
    def process_due_uploads(self, *, limit: int = 1) -> int: ...


class ArchiveReportingService(Protocol):
    def get_report(
        self,
        *,
        image_id: str | None = None,
        collection: str | None = None,
    ) -> ArchiveUsageReport: ...


class ArchiveRestoreService(Protocol):
    def list(
        self,
        *,
        page: int,
        per_page: int,
        sort: str,
        order: str,
        terminal: str = "all",
        restore_type: str | None = None,
        state: str | None = None,
        collection: str | None = None,
        image: str | None = None,
    ) -> ArchiveRestoreListPage: ...
    def get(self, restore_id: str) -> ArchiveRestoreSummary: ...
    def create_or_resume_for_collection(
        self,
        collection_id: str,
        *,
        paths: Sequence[str] | None = None,
    ) -> ArchiveRestoreSummary: ...
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
    def get_for_image(self, image_id: str) -> ArchiveRestoreSummary: ...
    def complete(self, restore_id: str) -> ArchiveRestoreSummary: ...
    def cancel(self, restore_id: str) -> ArchiveRestoreSummary: ...
    def pause(self, restore_id: str) -> ArchiveRestoreSummary: ...
    def resume(self, restore_id: str) -> ArchiveRestoreSummary: ...
    def iter_restored_iso(self, restore_id: str, image_id: str) -> IsoBody: ...
    def process_due_restores(self, *, limit: int = 100) -> int: ...
    def repair_missing_fetch_hot_files(self, *, limit: int = 100) -> int: ...


class DiscService(Protocol):
    def register(
        self, image_id: str, location: str, *, disc_id: str | None = None
    ) -> DiscSummary: ...
    def list_for_image(self, image_id: str) -> list[DiscSummary]: ...
    def list_discs(
        self,
        *,
        page: int,
        per_page: int,
        sort: str,
        order: str,
        q: str | None,
        image_id: str | None,
    ) -> JsonObject: ...
    def get_disc(self, disc_id: str) -> JsonObject: ...
    def notify_label_needed(self, image_id: str, disc_id: str) -> DiscSummary: ...
    def update(
        self,
        image_id: str,
        disc_id: str,
        *,
        location: str | None = None,
        state: str | None = None,
        verification_state: str | None = None,
    ) -> DiscSummary: ...


class FetchService(Protocol):
    def create(self, *, name: str, targets: Sequence[str] | None = None) -> FetchSummary: ...
    def list(
        self,
        *,
        page: int,
        per_page: int,
        state: str | None = None,
        q: str | None = None,
        sort: str = "order",
        order: str = "asc",
    ) -> FetchListPage: ...
    def add_targets(self, fetch_id: str, targets: Sequence[str]) -> FetchSummary: ...
    def remove_targets(self, fetch_id: str, targets: Sequence[str]) -> FetchSummary: ...
    def start(self, fetch_id: str, *, archive: bool = False) -> FetchSummary: ...
    def start_plan(self, fetch_id: str, *, archive: bool = False) -> JsonObject: ...
    def cancel(self, fetch_id: str) -> FetchSummary: ...
    def evict(self, targets: Sequence[str], *, dry_run: bool = False) -> JsonObject: ...
    def get(self, fetch_id: str) -> FetchSummary: ...
    def status(self, fetch_id: str, *, limit: int = 25) -> JsonObject: ...
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
        disc_coverage: bool | None = None,
    ) -> JsonObject: ...
    def manifest(self, fetch_id: str) -> JsonObject: ...
    def create_or_resume_upload(self, fetch_id: str, entry_id: str) -> JsonObject: ...
    def append_upload_chunk(
        self,
        fetch_id: str,
        entry_id: str,
        *,
        offset: int,
        checksum: str,
        content: bytes,
    ) -> JsonObject: ...
    def get_entry_upload(self, fetch_id: str, entry_id: str) -> JsonObject: ...
    def cancel_entry_upload(self, fetch_id: str, entry_id: str) -> None: ...
    def expire_stale_uploads(self) -> None: ...
    def deliver_due_queued_notifications(self, *, limit: int = 100) -> int: ...
    def complete(self, fetch_id: str) -> JsonObject: ...


class FileService(Protocol):
    def query_by_target(
        self,
        raw_target: str,
        *,
        page: int,
        per_page: int,
    ) -> dict[str, object]: ...
    def get_content(self, raw_target: str) -> bytes: ...
