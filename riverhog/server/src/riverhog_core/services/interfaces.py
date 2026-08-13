from __future__ import annotations

from collections.abc import Iterator, Sequence
from datetime import datetime, timedelta
from typing import Protocol

from lifecycle_events import EventPage

from riverhog_core.app_permissions import ApplicationAccess, ApplicationPrincipal
from riverhog_core.domain.models import (
    ArchiveStoreListPage,
    ArchiveStoreSummary,
    CollectionListPage,
    CollectionSummary,
)

JsonObject = dict[str, object]


class CollectionService(Protocol):
    def get(
        self,
        collection_id: int,
        *,
        principal: ApplicationPrincipal | None = None,
    ) -> CollectionSummary: ...
    def list(
        self,
        *,
        page: int,
        per_page: int,
        q: str | None,
        tag: str | None = None,
        sort: str = "id",
        order: str = "asc",
        all_items: bool = False,
        principal: ApplicationPrincipal | None = None,
    ) -> CollectionListPage: ...


class ProvenanceService(Protocol):
    def list_files(
        self,
        collection_id: int,
        *,
        page: int,
        per_page: int,
        q: str | None,
        status: str | None,
        sort: str,
        order: str,
        all_items: bool,
        principal: ApplicationPrincipal,
    ) -> JsonObject: ...
    def show_file(
        self,
        collection_id: int,
        path: str,
        *,
        principal: ApplicationPrincipal,
    ) -> JsonObject: ...
    def trace_file(
        self,
        collection_id: int,
        path: str,
        *,
        principal: ApplicationPrincipal,
    ) -> JsonObject: ...
    def export_journal(
        self,
        collection_id: int,
        journal_id: str,
        *,
        principal: ApplicationPrincipal,
    ) -> tuple[bytes, str]: ...
    def verify(
        self,
        collection_id: int,
        *,
        principal: ApplicationPrincipal,
    ) -> JsonObject: ...


class TagService(Protocol):
    def create(
        self,
        tag: str,
        *,
        creator: ApplicationPrincipal,
    ) -> JsonObject: ...
    def get(
        self,
        tag: str,
        *,
        principal: ApplicationPrincipal,
    ) -> JsonObject: ...
    def plan_deletion(self, tag: str) -> JsonObject: ...
    def delete(self, tag: str, *, challenge: str) -> JsonObject: ...
    def list(
        self,
        *,
        page: int,
        per_page: int,
        q: str | None,
        sort: str,
        order: str,
        all_items: bool,
        principal: ApplicationPrincipal,
    ) -> JsonObject: ...
    def get_collection(
        self,
        collection_id: int,
        *,
        principal: ApplicationPrincipal,
    ) -> JsonObject: ...
    def replace_collection(
        self,
        collection_id: int,
        tags: Sequence[str],
        *,
        principal: ApplicationPrincipal,
        event_context: dict[str, object] | None = None,
    ) -> JsonObject: ...
    def add_collection_tag(
        self,
        collection_id: int,
        tag: str,
        *,
        principal: ApplicationPrincipal,
        event_context: dict[str, object] | None = None,
    ) -> JsonObject: ...
    def remove_collection_tag(
        self,
        collection_id: int,
        tag: str,
        *,
        principal: ApplicationPrincipal,
        event_context: dict[str, object] | None = None,
    ) -> JsonObject: ...


class CollectionDeletionService(Protocol):
    def plan(self, collection_id: int) -> JsonObject: ...
    def delete(
        self,
        collection_id: int,
        *,
        challenge: str,
        initiator: ApplicationPrincipal,
        event_context: dict[str, object] | None = None,
    ) -> JsonObject: ...


class RetrievalService(Protocol):
    def abort_incomplete_cache_multipart_uploads(
        self,
        *,
        initiated_before: datetime,
    ) -> int: ...

    def collection_manifest(
        self,
        collection_id: int,
        *,
        principal: ApplicationPrincipal | None = None,
    ) -> tuple[JsonObject, str]: ...
    def resource_list_page(
        self,
        *,
        page: int,
        per_page: int,
        principal: ApplicationPrincipal | None = None,
    ) -> JsonObject: ...
    def resource_list_pages(
        self,
        *,
        per_page: int,
        principal: ApplicationPrincipal | None = None,
    ) -> int: ...
    def change_list(
        self,
        *,
        after: int = 0,
        limit: int = 1000,
        principal: ApplicationPrincipal | None = None,
    ) -> JsonObject: ...
    def cache_status(
        self,
        *,
        principal: ApplicationPrincipal | None = None,
    ) -> JsonObject: ...
    def list_cache_objects(
        self,
        *,
        page: int,
        per_page: int,
        q: str | None,
        tag: str | None,
        collection_id: int | None = None,
        source_store: str | None = None,
        state: str | None = None,
        protection: str | None = None,
        expires_before: str | None = None,
        expires_after: str | None = None,
        sort: str,
        order: str,
        all_items: bool = False,
        principal: ApplicationPrincipal | None = None,
    ) -> JsonObject: ...
    def get_cache_object(
        self,
        *,
        collection_id: int,
        source_store: str,
        object_id: str,
        principal: ApplicationPrincipal | None = None,
    ) -> JsonObject: ...
    def plan(
        self,
        files: Sequence[tuple[int, str]],
        *,
        lease: timedelta | None = None,
        restore_policy: str = "allow",
        principal: ApplicationPrincipal | None = None,
    ) -> JsonObject: ...
    def create(
        self,
        *,
        app: str,
        key_id: str | None = None,
        files: Sequence[tuple[int, str]],
        plan_etag: str,
        lease: timedelta | None = None,
        restore_policy: str = "allow",
        event_context: dict[str, object] | None = None,
        principal: ApplicationPrincipal | None = None,
    ) -> JsonObject: ...
    def get(self, *, app: str, job_id: str, key_id: str | None = None) -> JsonObject: ...
    def renew(
        self,
        *,
        app: str,
        job_id: str,
        lease: timedelta,
        key_id: str | None = None,
    ) -> JsonObject: ...
    def acknowledge(
        self,
        *,
        app: str,
        job_id: str,
        key_id: str | None = None,
    ) -> JsonObject: ...
    def cancel(
        self,
        *,
        app: str,
        job_id: str,
        key_id: str | None = None,
    ) -> JsonObject: ...
    def content_metadata(
        self,
        *,
        app: str,
        job_id: str,
        collection_id: int,
        path: str,
        key_id: str | None = None,
    ) -> tuple[int, str]: ...
    def content(
        self,
        *,
        app: str,
        job_id: str,
        collection_id: int,
        path: str,
        key_id: str | None = None,
    ) -> tuple[Iterator[bytes], int, str]: ...
    def process_due(self, *, limit: int = 10) -> int: ...
    def requeue_interrupted_cache_cleanup_for_startup(self) -> int: ...
    def sweep(self, *, limit: int = 100) -> int: ...


class AppKeyService(Protocol):
    def authenticate(self, token: str) -> ApplicationPrincipal | None: ...
    def create(
        self,
        *,
        app: str,
        access: Sequence[ApplicationAccess | tuple[str, str]],
        grantor: ApplicationPrincipal,
        expires_in: timedelta | None = None,
    ) -> JsonObject: ...
    def rotate(
        self,
        *,
        app: str,
        key_id: str,
        grantor: ApplicationPrincipal,
    ) -> JsonObject: ...
    def revoke(self, *, app: str, key_id: str) -> JsonObject: ...
    def replace_access(
        self,
        *,
        app: str,
        key_id: str,
        access: Sequence[ApplicationAccess | tuple[str, str]],
        grantor: ApplicationPrincipal,
    ) -> JsonObject: ...
    def add_access(
        self,
        *,
        app: str,
        key_id: str,
        access: ApplicationAccess | tuple[str, str],
        grantor: ApplicationPrincipal,
    ) -> JsonObject: ...
    def remove_access(
        self,
        *,
        app: str,
        key_id: str,
        access: ApplicationAccess | tuple[str, str],
    ) -> JsonObject: ...
    def list_access(
        self,
        *,
        page: int,
        per_page: int,
        q: str | None,
        sort: str,
        order: str,
        app: str | None = None,
        key_id: str | None = None,
        permission: str | None = None,
        resource: str | None = None,
        active: bool | None = None,
        all_items: bool = False,
    ) -> JsonObject: ...
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
        collection: int | None = None,
        all_items: bool = False,
        principal: ApplicationPrincipal | None = None,
    ) -> JsonObject: ...


class ArchiveMaintenanceService(Protocol):
    def requeue_interrupted_metadata_publications_for_startup(self) -> int: ...
    def process_due_metadata_publications(self, *, limit: int = 10) -> int: ...
    def abort_incomplete_multipart_uploads(
        self,
        *,
        initiated_before: datetime,
    ) -> int: ...


class ArchiveCopyService(Protocol):
    def requeue_interrupted_copies_for_startup(self, *, limit: int = 100) -> int: ...
    def create_or_resume(
        self,
        collection_id: int,
        *,
        destination_store: str,
        source_store: str | None = None,
        initiator: ApplicationPrincipal,
        event_context: dict[str, object] | None = None,
    ) -> JsonObject: ...
    def get(
        self,
        collection_id: int,
        *,
        destination_store: str,
        principal: ApplicationPrincipal | None = None,
    ) -> JsonObject: ...
    def cancel(
        self,
        collection_id: int,
        *,
        destination_store: str,
        principal: ApplicationPrincipal | None = None,
    ) -> JsonObject: ...
    def list(
        self,
        *,
        page: int,
        per_page: int,
        q: str | None,
        sort: str,
        order: str,
        all_items: bool,
        state: str | None = None,
        principal: ApplicationPrincipal | None = None,
    ) -> JsonObject: ...
    def process_due(self, *, limit: int = 1) -> int: ...


class ProofMaturationService(Protocol):
    def requeue_interrupted_for_startup(self) -> int: ...
    def schedule_missing(self, *, limit: int = 1000) -> int: ...
    def process_due(self, *, limit: int = 10) -> int: ...


class ArchiveAttestationService(Protocol):
    def requeue_interrupted_for_startup(self) -> int: ...
    def schedule_missing(self, *, limit: int = 1000) -> int: ...
    def process_due(self, *, limit: int = 10) -> int: ...


class ArchiveCopyRetirementService(Protocol):
    def plan(self, collection_id: int, *, store: str) -> JsonObject: ...
    def retire(self, collection_id: int, *, store: str, challenge: str) -> JsonObject: ...


class ArchiveStoreService(Protocol):
    def get(
        self,
        store: str,
        *,
        principal: ApplicationPrincipal | None = None,
    ) -> ArchiveStoreSummary: ...
    def list(
        self,
        *,
        page: int,
        per_page: int,
        q: str | None,
        sort: str,
        order: str,
        all_items: bool = False,
        principal: ApplicationPrincipal | None = None,
    ) -> ArchiveStoreListPage: ...
