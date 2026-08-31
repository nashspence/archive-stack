from __future__ import annotations

from collections.abc import Iterator, Sequence
from datetime import datetime, timedelta
from typing import Protocol

from riverhog_protocol import (
    PortableCollectionFile,
    PortableCollectionHeader,
    PortableCollectionInventoryPage,
)
from riverhog_protocol.lifecycle_events import RiverhogEventPage

from riverhog_core.app_permissions import ApplicationAccess, ApplicationPrincipal
from riverhog_core.domain.models import (
    ArchiveStoreListPage,
    ArchiveStoreSummary,
    CollectionListPage,
    CollectionSummary,
)

JsonObject = dict[str, object]
BrowsePosition = tuple[str | int | bool | bytes | None, ...] | None


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
        page_size: int,
        position: BrowsePosition,
        q: str | None,
        tag: str | None = None,
        encryption_format: str | None = None,
        passphrase_id: str | None = None,
        sort: str = "id",
        order: str = "asc",
        principal: ApplicationPrincipal | None = None,
    ) -> CollectionListPage: ...
    def iter_collections(
        self,
        *,
        q: str | None,
        tag: str | None = None,
        encryption_format: str | None = None,
        passphrase_id: str | None = None,
        sort: str = "id",
        order: str = "asc",
        principal: ApplicationPrincipal | None = None,
    ) -> Iterator[CollectionSummary]: ...
    def list_archive_copies(
        self,
        collection_id: int,
        *,
        page_size: int,
        position: BrowsePosition,
        principal: ApplicationPrincipal | None = None,
    ) -> JsonObject: ...
    def iter_archive_copies(
        self,
        collection_id: int,
        *,
        principal: ApplicationPrincipal | None = None,
    ) -> Iterator[JsonObject]: ...


class ProvenanceService(Protocol):
    def list_files(
        self,
        collection_id: int,
        *,
        page_size: int,
        position: BrowsePosition,
        q: str | None,
        status: str | None,
        sort: str,
        order: str,
        principal: ApplicationPrincipal,
    ) -> JsonObject: ...
    def iter_files(
        self,
        collection_id: int,
        *,
        q: str | None,
        status: str | None,
        sort: str,
        order: str,
        principal: ApplicationPrincipal,
    ) -> Iterator[JsonObject]: ...
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
        page_size: int,
        position: BrowsePosition,
        principal: ApplicationPrincipal,
    ) -> JsonObject: ...
    def iter_trace_file(
        self,
        collection_id: int,
        path: str,
        *,
        principal: ApplicationPrincipal,
    ) -> Iterator[JsonObject]: ...
    def journal_metadata(
        self,
        collection_id: int,
        journal_id: str,
        *,
        principal: ApplicationPrincipal,
    ) -> tuple[int, str]: ...
    def iter_journal(
        self,
        collection_id: int,
        journal_id: str,
        *,
        principal: ApplicationPrincipal,
    ) -> Iterator[bytes]: ...
    def iter_journal_range(
        self,
        collection_id: int,
        journal_id: str,
        *,
        offset: int,
        size: int,
        principal: ApplicationPrincipal,
    ) -> Iterator[bytes]: ...
    def list_journal_agents(
        self,
        collection_id: int,
        journal_id: str,
        *,
        page_size: int,
        position: BrowsePosition,
        principal: ApplicationPrincipal,
    ) -> JsonObject: ...
    def iter_journal_agents(
        self,
        collection_id: int,
        journal_id: str,
        *,
        principal: ApplicationPrincipal,
    ) -> Iterator[JsonObject]: ...
    def request_verification(
        self,
        collection_id: int,
        *,
        principal: ApplicationPrincipal,
    ) -> JsonObject: ...
    def get_verification(
        self,
        collection_id: int,
        *,
        principal: ApplicationPrincipal,
    ) -> JsonObject: ...
    def cancel_verification(
        self,
        collection_id: int,
        *,
        principal: ApplicationPrincipal,
    ) -> JsonObject: ...
    def requeue_interrupted_verifications_for_startup(self) -> int: ...
    def process_due_verifications(self, *, limit: int = 1) -> int: ...


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
        page_size: int,
        position: BrowsePosition,
        q: str | None,
        sort: str,
        order: str,
        principal: ApplicationPrincipal,
    ) -> JsonObject: ...
    def iter_tags(
        self,
        *,
        q: str | None,
        sort: str,
        order: str,
        principal: ApplicationPrincipal,
    ) -> Iterator[JsonObject]: ...
    def list_collection_tags(
        self,
        collection_id: int,
        *,
        page_size: int,
        position: BrowsePosition,
        principal: ApplicationPrincipal,
    ) -> JsonObject: ...
    def iter_collection_tags(
        self,
        collection_id: int,
        *,
        principal: ApplicationPrincipal,
    ) -> Iterator[JsonObject]: ...
    def add_collection_tag(
        self,
        collection_id: int,
        tag: str,
        *,
        principal: ApplicationPrincipal,
        event_context: dict[str, object] | None = None,
    ) -> JsonObject: ...
    def replace_collection_tags(
        self,
        collection_id: int,
        tags: Sequence[str],
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
    def plan(
        self,
        collection_id: int,
        *,
        principal: ApplicationPrincipal | None = None,
        retirement_claim_id: str | None = None,
    ) -> JsonObject: ...
    def delete(
        self,
        collection_id: int,
        *,
        challenge: str,
        initiator: ApplicationPrincipal,
        event_context: dict[str, object] | None = None,
        retirement_claim_id: str | None = None,
    ) -> JsonObject: ...


class RetrievalService(Protocol):
    def abort_incomplete_cache_writes(
        self,
        *,
        initiated_before: datetime,
    ) -> int: ...

    def collection_inventory(
        self,
        collection_id: int,
        *,
        principal: ApplicationPrincipal | None = None,
    ) -> tuple[
        PortableCollectionHeader,
        Iterator[PortableCollectionFile],
        str,
        int,
        int,
    ]: ...
    def collection_inventory_page(
        self,
        collection_id: int,
        *,
        cursor: str | None,
        limit: int,
        expected_identity: str | None,
        principal: ApplicationPrincipal | None = None,
    ) -> PortableCollectionInventoryPage: ...
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
        page_size: int,
        position: BrowsePosition,
        q: str | None,
        tag: str | None,
        collection_id: int | None = None,
        source_store: str | None = None,
        cache_store: str | None = None,
        state: str | None = None,
        protection: str | None = None,
        expires_before: str | None = None,
        expires_after: str | None = None,
        sort: str,
        order: str,
        principal: ApplicationPrincipal | None = None,
    ) -> JsonObject: ...
    def iter_cache_objects(
        self,
        *,
        q: str | None,
        tag: str | None,
        collection_id: int | None = None,
        source_store: str | None = None,
        cache_store: str | None = None,
        state: str | None = None,
        protection: str | None = None,
        expires_before: str | None = None,
        expires_after: str | None = None,
        sort: str,
        order: str,
        principal: ApplicationPrincipal | None = None,
    ) -> Iterator[JsonObject]: ...
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
        page_size: int,
        position: BrowsePosition,
        q: str | None,
        sort: str,
        order: str,
        app: str | None = None,
        key_id: str | None = None,
        permission: str | None = None,
        resource: str | None = None,
        active: bool | None = None,
    ) -> JsonObject: ...
    def iter_access(
        self,
        *,
        q: str | None,
        sort: str,
        order: str,
        app: str | None = None,
        key_id: str | None = None,
        permission: str | None = None,
        resource: str | None = None,
        active: bool | None = None,
    ) -> Iterator[JsonObject]: ...
    def list_apps(
        self,
        *,
        page_size: int,
        position: BrowsePosition,
        q: str | None,
        sort: str,
        order: str,
        active: bool | None = None,
    ) -> JsonObject: ...
    def iter_apps(
        self,
        *,
        q: str | None,
        sort: str,
        order: str,
        active: bool | None = None,
    ) -> Iterator[JsonObject]: ...
    def list_keys(
        self,
        *,
        app: str,
        page_size: int,
        position: BrowsePosition,
        q: str | None,
        sort: str,
        order: str,
        active: bool | None = None,
    ) -> JsonObject: ...
    def iter_keys(
        self,
        *,
        app: str,
        q: str | None,
        sort: str,
        order: str,
        active: bool | None = None,
    ) -> Iterator[JsonObject]: ...


class LifecycleEventService(Protocol):
    def page(
        self,
        *,
        owner_app: str | None,
        after: str | None,
        limit: int,
    ) -> RiverhogEventPage: ...


class SearchService(Protocol):
    def search(
        self,
        *,
        q: str | None,
        page_size: int,
        position: BrowsePosition,
        sort: str,
        order: str,
        collection: int | None = None,
        principal: ApplicationPrincipal | None = None,
    ) -> JsonObject: ...
    def iter_files(
        self,
        *,
        q: str | None,
        sort: str,
        order: str,
        collection: int | None = None,
        principal: ApplicationPrincipal | None = None,
    ) -> Iterator[JsonObject]: ...


class ArchiveMaintenanceService(Protocol):
    def requeue_interrupted_metadata_publications_for_startup(self) -> int: ...
    def process_due_metadata_publications(self, *, limit: int = 10) -> int: ...
    def abort_incomplete_writes(
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
        page_size: int,
        position: BrowsePosition,
        q: str | None,
        sort: str,
        order: str,
        state: str | None = None,
        principal: ApplicationPrincipal | None = None,
    ) -> JsonObject: ...
    def iter_jobs(
        self,
        *,
        q: str | None,
        sort: str,
        order: str,
        state: str | None = None,
        principal: ApplicationPrincipal | None = None,
    ) -> Iterator[JsonObject]: ...
    def process_due(self, *, limit: int = 1) -> int: ...


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
        page_size: int,
        position: BrowsePosition,
        q: str | None,
        sort: str,
        order: str,
        principal: ApplicationPrincipal | None = None,
    ) -> ArchiveStoreListPage: ...
    def iter_stores(
        self,
        *,
        q: str | None,
        sort: str,
        order: str,
        principal: ApplicationPrincipal | None = None,
    ) -> Iterator[ArchiveStoreSummary]: ...
