from __future__ import annotations

from collections.abc import Iterator, Sequence
from datetime import timedelta
from typing import Protocol

from riverhog_protocol import (
    CatalogSyncChangePage,
    CatalogSyncCheckpoint,
    CatalogSyncCollectionPage,
    CollectionAccessGroupStatus,
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


class CollectionDescriptionService(Protocol):
    def replace(
        self,
        collection_id: int,
        *,
        description: str | None,
        expected_identity: str,
        principal: ApplicationPrincipal,
    ) -> JsonObject: ...

    def requeue_interrupted_for_startup(self, *, limit: int = 100) -> int: ...

    def process_due(self, *, limit: int = 1) -> int: ...


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


class CollectionAccessGroupService(Protocol):
    def create(
        self,
        *,
        idempotency_key: str,
        display_label: str | None,
        creator: ApplicationPrincipal,
    ) -> JsonObject: ...
    def get(
        self,
        group_id: str,
    ) -> JsonObject: ...
    def update(
        self,
        group_id: str,
        *,
        display_label: str | None,
        status: CollectionAccessGroupStatus,
    ) -> JsonObject: ...
    def list(
        self,
        *,
        page_size: int,
        position: BrowsePosition,
        q: str | None,
        status: CollectionAccessGroupStatus | None,
        sort: str,
        order: str,
    ) -> JsonObject: ...
    def iter_groups(
        self,
        *,
        q: str | None,
        status: CollectionAccessGroupStatus | None,
        sort: str,
        order: str,
    ) -> Iterator[JsonObject]: ...
    def list_members(
        self,
        group_id: str,
        *,
        page_size: int,
        position: BrowsePosition,
    ) -> JsonObject: ...
    def list_collection_groups(
        self,
        collection_id: int,
        *,
        page_size: int,
        position: BrowsePosition,
    ) -> JsonObject: ...
    def add_member(
        self,
        group_id: str,
        collection_id: int,
        *,
        principal: ApplicationPrincipal,
    ) -> JsonObject: ...
    def remove_member(
        self,
        group_id: str,
        collection_id: int,
        *,
        principal: ApplicationPrincipal,
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
    def process_due(self, *, limit: int = 10) -> int: ...


class CatalogSyncService(Protocol):
    def checkpoint(self, *, principal: ApplicationPrincipal) -> CatalogSyncCheckpoint: ...
    def collections(
        self,
        *,
        cursor: str,
        limit: int,
        principal: ApplicationPrincipal,
    ) -> CatalogSyncCollectionPage: ...
    def changes(
        self,
        *,
        cursor: str,
        limit: int,
        principal: ApplicationPrincipal,
    ) -> CatalogSyncChangePage: ...
    def reap_expired_history(self, *, limit: int | None = None) -> int: ...


class RetrievalService(Protocol):
    def request_cache_accounting_reconciliation_for_startup(self) -> int: ...
    def process_cache_accounting_reconciliation(self, *, limit: int = 100) -> int: ...

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
        idempotency_key: str | None = None,
        lease: timedelta | None = None,
        restore_policy: str = "allow",
        principal: ApplicationPrincipal | None = None,
    ) -> JsonObject: ...
    def get_plan(
        self,
        *,
        app: str,
        plan_id: str,
        key_id: str | None = None,
    ) -> JsonObject: ...
    def advance_plan(
        self,
        *,
        app: str,
        plan_id: str,
        key_id: str | None = None,
    ) -> JsonObject: ...
    def list_plan_files(
        self,
        *,
        app: str,
        plan_id: str,
        etag: str,
        start_ordinal: int,
        page_size: int,
        key_id: str | None = None,
    ) -> JsonObject: ...
    def create(
        self,
        *,
        app: str,
        key_id: str | None = None,
        plan_id: str,
        plan_etag: str,
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
        offset: int = 0,
        size: int | None = None,
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
    def reap_expired_contexts(self) -> int: ...


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
