from __future__ import annotations

from collections.abc import Iterator, Sequence
from datetime import datetime, timedelta
from typing import Protocol

from lifecycle_events import EventPage

from riverhog_core.app_permissions import ApplicationPrincipal
from riverhog_core.domain.models import ArchiveUsageReport, CollectionListPage, CollectionSummary

JsonObject = dict[str, object]


class CollectionService(Protocol):
    def create_or_resume_upload(
        self,
        *,
        upload_slug: str,
        files: list[dict[str, object]],
        ingest_source: str | None = None,
        upload_timestamp: str | None = None,
        archive_store: str | None = None,
        initiator: ApplicationPrincipal | None = None,
        event_context: dict[str, object] | None = None,
    ) -> JsonObject: ...
    def create_or_resume_upload_session(
        self,
        *,
        upload_slug: str,
        ingest_source: str | None = None,
        upload_timestamp: str | None = None,
        archive_store: str | None = None,
        initiator: ApplicationPrincipal | None = None,
        event_context: dict[str, object] | None = None,
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
    def sync_finished_upload_id(self, upload_id: str) -> JsonObject | None: ...
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


class RetrievalService(Protocol):
    def collection_manifest(self, collection_id: str) -> tuple[JsonObject, str]: ...
    def resource_list(self) -> list[dict[str, str]]: ...
    def change_list(self, *, after: int = 0, limit: int = 1000) -> JsonObject: ...
    def plan(
        self,
        files: Sequence[tuple[str, str]],
        *,
        lease: timedelta | None = None,
    ) -> JsonObject: ...
    def create(
        self,
        *,
        app: str,
        key_id: str | None = None,
        files: Sequence[tuple[str, str]],
        plan_etag: str,
        lease: timedelta | None = None,
        event_context: dict[str, object] | None = None,
    ) -> JsonObject: ...
    def get(self, *, app: str, job_id: str) -> JsonObject: ...
    def acknowledge(self, *, app: str, job_id: str) -> JsonObject: ...
    def cancel(self, *, app: str, job_id: str) -> JsonObject: ...
    def content_metadata(
        self,
        *,
        app: str,
        job_id: str,
        collection_id: str,
        path: str,
    ) -> tuple[int, str]: ...
    def content(
        self,
        *,
        app: str,
        job_id: str,
        collection_id: str,
        path: str,
    ) -> tuple[Iterator[bytes], int, str]: ...
    def process_due(self, *, limit: int = 10) -> int: ...
    def sweep(self) -> int: ...


class AppKeyService(Protocol):
    def authenticate(self, token: str) -> ApplicationPrincipal | None: ...
    def create(
        self,
        *,
        app: str,
        permissions: Sequence[str],
        grantor: ApplicationPrincipal,
        expires_in: timedelta | None = None,
    ) -> JsonObject: ...
    def revoke(self, *, app: str, key_id: str) -> JsonObject: ...
    def list_apps(
        self,
        *,
        page: int,
        per_page: int,
        q: str | None,
        sort: str,
        order: str,
        active: bool | None = None,
        all_items: bool = False,
    ) -> JsonObject: ...
    def list_keys(
        self,
        *,
        app: str,
        page: int,
        per_page: int,
        q: str | None,
        sort: str,
        order: str,
        active: bool | None = None,
        all_items: bool = False,
    ) -> JsonObject: ...


class LifecycleEventService(Protocol):
    def page(
        self,
        *,
        owner_app: str | None,
        after: str | None,
        limit: int,
    ) -> EventPage: ...


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
        all_items: bool = False,
    ) -> JsonObject: ...


class ArchiveUploadService(Protocol):
    def requeue_failed_uploads_for_startup(self, *, limit: int = 100) -> int: ...
    def publish_archive_catalog(self) -> int: ...
    def abort_incomplete_multipart_uploads(
        self,
        *,
        initiated_before: datetime,
    ) -> int: ...
    def process_due_uploads(self, *, limit: int = 1) -> int: ...


class ArchiveCopyService(Protocol):
    def requeue_interrupted_copies_for_startup(self, *, limit: int = 100) -> int: ...
    def create_or_resume(
        self,
        collection_id: str,
        *,
        destination_store: str,
        source_store: str | None = None,
    ) -> JsonObject: ...
    def process_due(self, *, limit: int = 1) -> int: ...


class ArchiveCopyRetirementService(Protocol):
    def plan(self, collection_id: str, *, store: str) -> JsonObject: ...
    def retire(self, collection_id: str, *, store: str, challenge: str) -> JsonObject: ...


class ArchiveReportingService(Protocol):
    def get_report(self, *, collection: str | None = None) -> ArchiveUsageReport: ...
