from __future__ import annotations

import base64
import binascii
import hashlib
import json
import os
import uuid
from collections.abc import Iterator, Sequence
from datetime import datetime, timedelta
from typing import Any, Literal, cast

from http_api_contracts import canonical_json_bytes, closed_literal_values
from riverhog_protocol import (
    RETRIEVAL_FILE_BATCH_MAX,
    ImmutableFileIdentityDocument,
    PortableCollectionFile,
    PortableCollectionHeader,
    PortableCollectionInventoryAuthority,
    PortableCollectionInventoryPage,
    RetrievalCacheProtection,
    RetrievalCacheSort,
    RetrievalCacheState,
    RetrievalFileReferenceSetDocument,
    SortOrder,
)
from riverhog_protocol.errors import (
    BadRequest,
    Conflict,
    InvalidRange,
    InvalidState,
    NotFound,
    PreconditionFailed,
    RiverhogError,
)
from riverhog_protocol.paths import (
    PathNormalizationError,
    normalize_collection_id,
    relpath_sort_key,
    validate_canonical_relpath,
)
from sqlalchemy import case, delete, exists, func, or_, select, update
from sqlalchemy.orm import Session
from sqlalchemy.sql.elements import ColumnElement
from state_schema import read_snapshot
from time_formats import format_utc_timestamp, parse_utc_timestamp, utc_now

from riverhog_core.app_permissions import CATALOG_READ, RETRIEVAL_MANAGE, ApplicationPrincipal
from riverhog_core.archive_store_registry import ArchiveStoreRegistry
from riverhog_core.artifact_access import require_artifact_scope
from riverhog_core.browse import bounded_page, keyset_statement, validate_page_size
from riverhog_core.catalog_db import SessionFactory, make_session_factory, session_scope
from riverhog_core.catalog_events import catalog_event_projection
from riverhog_core.catalog_models import (
    ArchiveCopyRetirementRecord,
    CatalogEventRecord,
    CollectionArchiveCopyRecord,
    CollectionArchiveFileObjectRecord,
    CollectionArchiveObjectRecord,
    CollectionDeletionRecord,
    CollectionFileRecord,
    CollectionRecord,
    CollectionTagRecord,
    RetrievalCacheLeaseRecord,
    RetrievalCacheObjectRecord,
    RetrievalCacheStoreAccountingRecord,
    RetrievalJobObjectProgressRecord,
    RetrievalJobRecord,
    RetrievalPlanFileRecord,
    RetrievalPlanObjectRecord,
    RetrievalPlanPlacementRecord,
    RetrievalPlanRecord,
)
from riverhog_core.collection_access import collection_access_filter, require_collection_access
from riverhog_core.domain.archive import StoredArchivePart
from riverhog_core.pack_retrieval import (
    PackMemberRangeReader,
    PackMemberRetrievalSource,
    PackRangeRetrievalPolicy,
    PackVolumeRetrievalSource,
    plan_pack_range_retrieval,
)
from riverhog_core.ports.archive_objects import ArchiveObjectRangeStore
from riverhog_core.ports.archive_store import ArchiveObjectIdentity, ArchiveStore
from riverhog_core.ports.download_allowance import DownloadAllowance, DownloadAttribution
from riverhog_core.ports.retrieval_cache import RetrievalCache, RetrievalCacheReceipt
from riverhog_core.raw_retrieval import RawVolumeRangeReader, RawVolumeRetrievalSource
from riverhog_core.runtime_config import RuntimeConfig
from riverhog_core.services.archive_records import archive_copy_is_complete
from riverhog_core.services.lifecycle_events import (
    SqlAlchemyLifecycleEventService,
    event_context_json,
)
from riverhog_core.services.retrieval_cache import register_cache_ready
from riverhog_core.services.retrieval_cache_accounting import adjust_cache_committed_bytes
from riverhog_core.streaming_age import ResumableAgeSessionCache
from riverhog_core.throughput import (
    ArchiveThroughputTuning,
    ArchiveTransferResources,
    log_transfer_timing,
)

_DATA_KINDS = {"pack", "segment"}
_INVENTORY_PAGE_LIMIT = 1000
_RETRIEVAL_PLAN_SEGMENT_BATCH = 32
_RETRIEVAL_PLAN_FILE_PAGE_MAX = 100
_RETRIEVAL_PLAN_INITIAL_COMMITMENT = hashlib.sha256(
    b"riverhog-retrieval-plan-segments/v1\x00"
).hexdigest()
_RETRIEVAL_PLAN_INITIAL_FILE_COMMITMENT = hashlib.sha256(
    b"riverhog-retrieval-plan-files/v1\x00"
).hexdigest()


def _encode_inventory_cursor(
    *,
    collection_id: int,
    inventory_identity: str,
    after: str,
) -> str:
    payload = canonical_json_bytes(
        {
            "format": "riverhog-private-inventory-cursor/v1",
            "collection_id": collection_id,
            "inventory_identity": inventory_identity,
            "after": after,
        }
    )
    return base64.urlsafe_b64encode(payload).rstrip(b"=").decode("ascii")


def _decode_inventory_cursor(cursor: str) -> tuple[int, str, str]:
    try:
        padding = "=" * (-len(cursor) % 4)
        payload = json.loads(base64.urlsafe_b64decode(cursor + padding))
        if not isinstance(payload, dict) or set(payload) != {
            "format",
            "collection_id",
            "inventory_identity",
            "after",
        }:
            raise ValueError
        if payload["format"] != "riverhog-private-inventory-cursor/v1":
            raise ValueError
        collection_id = normalize_collection_id(payload["collection_id"])
        inventory_identity = str(payload["inventory_identity"])
        if len(inventory_identity) != 64 or any(
            character not in "0123456789abcdef" for character in inventory_identity
        ):
            raise ValueError
        after = validate_canonical_relpath(str(payload["after"]))
    except (binascii.Error, TypeError, ValueError, UnicodeError, json.JSONDecodeError) as exc:
        raise BadRequest("collection inventory cursor is invalid") from exc
    return collection_id, inventory_identity, after


_CACHE_SORT_FIELDS = closed_literal_values(RetrievalCacheSort)
_CACHE_STATES = closed_literal_values(RetrievalCacheState)
_CACHE_PROTECTION_FILTERS = closed_literal_values(RetrievalCacheProtection)
_SORT_ORDERS = closed_literal_values(SortOrder)


class SqlAlchemyRetrievalService:
    def __init__(
        self,
        config: RuntimeConfig,
        archive_stores: ArchiveStoreRegistry,
        retrieval_cache: RetrievalCache | None,
        download_allowance: DownloadAllowance | None = None,
        *,
        session_factory: SessionFactory | None = None,
        throughput_tuning: ArchiveThroughputTuning | None = None,
        transfer_resources: ArchiveTransferResources | None = None,
    ) -> None:
        self._config = config
        self._archive_stores = archive_stores
        self._cache = retrieval_cache
        self._download_allowance = download_allowance
        self._session_factory = session_factory or make_session_factory(config.database_url)
        self._throughput = throughput_tuning or ArchiveThroughputTuning.from_env(os.environ)
        self._resources = transfer_resources or ArchiveTransferResources.from_tuning(
            self._throughput
        )
        self._age_sessions = {
            passphrase_id: ResumableAgeSessionCache(
                passphrase,
                max_entries=self._throughput.age_session_cache_entries,
                derivation_gate=self._resources.age_derivations,
            )
            for passphrase_id, passphrase in config.archive_passphrases.items()
        }
        self._lifecycle_events = SqlAlchemyLifecycleEventService(
            config,
            session_factory=self._session_factory,
        )

    def request_cache_accounting_reconciliation_for_startup(self) -> int:
        if self._cache is None:
            return 0
        return self._cache.request_accounting_reconciliation_for_startup()

    def process_cache_accounting_reconciliation(self, *, limit: int = 100) -> int:
        if self._cache is None:
            return 0
        return self._cache.process_accounting_reconciliation(limit=limit)

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
    ]:
        normalized_id = _normalize_collection_id_or_raise(collection_id)
        if principal is not None and principal.has_artifact_scope:
            raise NotFound(f"collection manifest not found: {normalized_id}")
        with read_snapshot(self._session_factory) as session:
            collection = session.get(CollectionRecord, normalized_id)
            if collection is None:
                raise NotFound(f"collection not found: {normalized_id}")
            require_collection_access(session, principal, CATALOG_READ, normalized_id)
            header = PortableCollectionHeader(
                collection=normalized_id,
                content_identity=collection.content_identity,
                encryption_format=collection.encryption_format,
                passphrase_id=collection.passphrase_id,
                provenance_mode=cast(
                    Literal["captured", "mixed", "omitted"],
                    collection.provenance_mode,
                ),
                provenance_identity=collection.provenance_identity,
            )
            file_count = int(collection.file_count)
            file_bytes = int(collection.file_bytes)
            inventory_identity = collection.inventory_identity

        def files() -> Iterator[PortableCollectionFile]:
            statement = (
                select(
                    CollectionFileRecord.path,
                    CollectionFileRecord.bytes,
                    CollectionFileRecord.sha256,
                )
                .where(CollectionFileRecord.collection_id == normalized_id)
                .order_by(CollectionFileRecord.path_sort_key)
                .execution_options(yield_per=100)
            )
            with read_snapshot(self._session_factory) as session:
                for path, byte_count, sha256 in session.execute(statement).tuples():
                    yield PortableCollectionFile(
                        path=str(path),
                        bytes=int(byte_count),
                        sha256=str(sha256),
                    )

        return header, files(), inventory_identity, file_count, file_bytes

    def collection_inventory_page(
        self,
        collection_id: int,
        *,
        cursor: str | None,
        limit: int,
        expected_identity: str | None,
        principal: ApplicationPrincipal | None = None,
    ) -> PortableCollectionInventoryPage:
        """Read one bounded page from one immutable collection inventory."""

        normalized_id = _normalize_collection_id_or_raise(collection_id)
        if limit < 1 or limit > _INVENTORY_PAGE_LIMIT:
            raise BadRequest("collection inventory limit must be between 1 and 1000")
        if principal is not None and principal.has_artifact_scope:
            raise NotFound(f"collection manifest not found: {normalized_id}")

        cursor_after: str | None = None
        cursor_identity: str | None = None
        if cursor is not None:
            cursor_collection, cursor_identity, cursor_after = _decode_inventory_cursor(cursor)
            if cursor_collection != normalized_id:
                raise PreconditionFailed(
                    "collection inventory cursor belongs to another collection"
                )

        with read_snapshot(self._session_factory) as session:
            collection = session.get(CollectionRecord, normalized_id)
            if collection is None:
                raise NotFound(f"collection not found: {normalized_id}")
            require_collection_access(session, principal, CATALOG_READ, normalized_id)
            inventory_identity = str(collection.inventory_identity)
            if expected_identity is not None and expected_identity != inventory_identity:
                raise PreconditionFailed("collection inventory identity changed")
            if cursor_identity is not None and cursor_identity != inventory_identity:
                raise PreconditionFailed("collection inventory cursor is stale")

            header = PortableCollectionHeader(
                collection=normalized_id,
                content_identity=collection.content_identity,
                encryption_format=collection.encryption_format,
                passphrase_id=collection.passphrase_id,
                provenance_mode=cast(
                    Literal["captured", "mixed", "omitted"],
                    collection.provenance_mode,
                ),
                provenance_identity=collection.provenance_identity,
            )
            file_count = int(collection.file_count)
            file_bytes = int(collection.file_bytes)
            statement = (
                select(
                    CollectionFileRecord.path,
                    CollectionFileRecord.bytes,
                    CollectionFileRecord.sha256,
                )
                .where(CollectionFileRecord.collection_id == normalized_id)
                .order_by(CollectionFileRecord.path_sort_key)
                .limit(limit + 1)
            )
            if cursor_after is not None:
                statement = statement.where(
                    CollectionFileRecord.path_sort_key > relpath_sort_key(cursor_after)
                )
            rows = list(session.execute(statement).tuples())

        has_more = len(rows) > limit
        selected = rows[:limit]
        files = [
            ImmutableFileIdentityDocument(
                path=str(path),
                bytes=int(byte_count),
                sha256=str(sha256),
            )
            for path, byte_count, sha256 in selected
        ]
        next_cursor = (
            _encode_inventory_cursor(
                collection_id=normalized_id,
                inventory_identity=inventory_identity,
                after=files[-1].path,
            )
            if has_more and files
            else None
        )
        return PortableCollectionInventoryPage(
            authority=PortableCollectionInventoryAuthority(
                header=header,
                inventory_identity=inventory_identity,
                file_count=file_count,
                file_bytes=file_bytes,
            ),
            files=files,
            next_cursor=next_cursor,
            complete=not has_more,
        )

    def resource_list_page(
        self,
        *,
        page: int,
        per_page: int,
        principal: ApplicationPrincipal | None = None,
    ) -> dict[str, object]:
        if page < 1:
            raise BadRequest("resource-list page must be positive")
        if per_page < 1 or per_page > 10_000:
            raise BadRequest("resource-list page size must be between 1 and 10000")
        visible = collection_access_filter(CollectionRecord.id, principal, CATALOG_READ)
        with read_snapshot(self._session_factory) as session:
            total = int(
                session.scalar(select(func.count()).select_from(CollectionRecord).where(visible))
                or 0
            )
            rows = session.execute(
                select(CollectionRecord.id, CollectionRecord.inventory_identity)
                .where(visible)
                .order_by(CollectionRecord.id)
                .offset((page - 1) * per_page)
                .limit(per_page)
            ).all()
            return {
                "page": page,
                "per_page": per_page,
                "total": total,
                "pages": (total + per_page - 1) // per_page if total else 0,
                "resources": [
                    {"collection_id": row.id, "etag": str(row.inventory_identity)} for row in rows
                ],
            }

    def resource_list_pages(
        self,
        *,
        per_page: int,
        principal: ApplicationPrincipal | None = None,
    ) -> int:
        if per_page < 1 or per_page > 10_000:
            raise BadRequest("resource-list page size must be between 1 and 10000")
        visible = collection_access_filter(CollectionRecord.id, principal, CATALOG_READ)
        with read_snapshot(self._session_factory) as session:
            total = int(
                session.scalar(select(func.count()).select_from(CollectionRecord).where(visible))
                or 0
            )
        return (total + per_page - 1) // per_page if total else 0

    def change_list(
        self,
        *,
        after: int = 0,
        limit: int = 1000,
        principal: ApplicationPrincipal | None = None,
    ) -> dict[str, object]:
        if after < 0:
            raise BadRequest("catalog cursor must be non-negative")
        if limit < 1 or limit > 10_000:
            raise BadRequest("catalog change limit must be between 1 and 10000")
        with read_snapshot(self._session_factory) as session:
            scanned = list(
                session.execute(
                    select(CatalogEventRecord.sequence, CatalogEventRecord.published)
                    .where(CatalogEventRecord.sequence > after)
                    .order_by(CatalogEventRecord.sequence)
                    .limit(limit + 1)
                )
            )
            published_prefix: list[int] = []
            for sequence, published in scanned:
                if not published:
                    break
                published_prefix.append(int(sequence))
            page_sequences = published_prefix[:limit]
            has_more = len(scanned) > len(page_sequences)
            cursor = int(page_sequences[-1]) if page_sequences else after
            if not page_sequences:
                return {"cursor": cursor, "has_more": has_more, "changes": []}
            visibility, projected_change = catalog_event_projection(principal, CATALOG_READ)
            rows = session.execute(
                select(CatalogEventRecord, projected_change.label("projected_change"))
                .where(
                    CatalogEventRecord.sequence.in_(page_sequences),
                    CatalogEventRecord.published.is_(True),
                    visibility,
                )
                .order_by(CatalogEventRecord.sequence)
            ).all()
            return {
                "cursor": cursor,
                "has_more": has_more,
                "changes": [
                    {
                        "sequence": event.sequence,
                        "change": projected,
                        "collection_id": event.collection_id,
                        "occurred_at": event.occurred_at,
                        "etag": event.inventory_identity,
                    }
                    for event, projected in rows
                ],
            }

    def cache_status(
        self,
        *,
        principal: ApplicationPrincipal | None = None,
    ) -> dict[str, object]:
        now = format_utc_timestamp(utc_now())
        visible = collection_access_filter(
            RetrievalCacheObjectRecord.collection_id,
            principal,
            CATALOG_READ,
        )
        active_lease = exists(
            select(1).where(
                RetrievalCacheLeaseRecord.source_store == RetrievalCacheObjectRecord.source_store,
                RetrievalCacheLeaseRecord.collection_id == RetrievalCacheObjectRecord.collection_id,
                RetrievalCacheLeaseRecord.object_id == RetrievalCacheObjectRecord.object_id,
                RetrievalCacheLeaseRecord.expires_at > now,
            )
        )
        active_retrieval = _active_retrieval_cache_reference(now)
        with session_scope(self._session_factory) as session:
            objects, stored_bytes, protected = session.execute(
                select(
                    func.count(),
                    func.coalesce(func.sum(RetrievalCacheObjectRecord.stored_bytes), 0),
                    func.coalesce(
                        func.sum(case((active_lease | active_retrieval, 1), else_=0)),
                        0,
                    ),
                ).where(visible, RetrievalCacheObjectRecord.state == "ready")
            ).one()
            accounting = {
                row.cache_store: row
                for row in session.scalars(select(RetrievalCacheStoreAccountingRecord))
            }
        return {
            "configured": self._cache is not None,
            "new_archive_enabled": (
                self._cache is not None and self._config.retrieval_cache_new_archive_enabled
            ),
            "objects": int(objects),
            "stored_bytes": int(stored_bytes),
            "protected_objects": int(protected),
            "unleased_objects": int(objects) - int(protected),
            "stores": [
                {
                    "cache_store": name,
                    "priority": priority,
                    "admission_enabled": registration.admission_enabled,
                    "admission_budget_bytes": registration.admission_budget_bytes,
                    "reserved_bytes": (
                        accounting[name].reserved_bytes if name in accounting else 0
                    ),
                    "committed_bytes": (
                        accounting[name].committed_bytes if name in accounting else 0
                    ),
                }
                for priority, (name, registration) in enumerate(
                    self._config.retrieval_cache_stores.items(),
                    start=1,
                )
            ],
            "policy": {
                "new_archive_lease_seconds": int(
                    self._config.retrieval_cache_new_archive_lease.total_seconds()
                ),
                "retrieval_default_lease_seconds": int(
                    self._config.retrieval_default_lease.total_seconds()
                ),
                "retrieval_max_lease_seconds": int(
                    self._config.retrieval_max_lease.total_seconds()
                ),
                "pending_timeout_seconds": int(
                    self._config.retrieval_pending_timeout.total_seconds()
                ),
                "sweep_interval_seconds": int(
                    self._config.retrieval_cache_sweep_interval.total_seconds()
                ),
                "restore_poll_interval_seconds": int(
                    self._config.retrieval_restore_poll_interval.total_seconds()
                ),
            },
        }

    def list_cache_objects(
        self,
        *,
        page_size: int,
        position: tuple[str | int | bool | bytes | None, ...] | None,
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
    ) -> dict[str, object]:
        validate_page_size(page_size)
        now = format_utc_timestamp(utc_now())
        statement, key_columns, normalized_filters, needle = _cache_list_statement(
            q=q,
            tag=tag,
            collection_id=collection_id,
            source_store=source_store,
            cache_store=cache_store,
            state=state,
            protection=protection,
            expires_before=expires_before,
            expires_after=expires_after,
            sort=sort,
            order=order,
            principal=principal,
            now=now,
        )
        with read_snapshot(self._session_factory) as session:
            rows, next_position = bounded_page(
                list(
                    session.execute(
                        keyset_statement(
                            statement,
                            columns=key_columns,
                            position=position,
                            order=order,
                            page_size=page_size,
                        )
                    )
                ),
                page_size=page_size,
                position_of=lambda row: _cache_list_position(row, sort=sort),
            )
        return {
            "page_size": page_size,
            "_next_position": next_position,
            "sort": sort,
            "order": order,
            "query": needle,
            "filters": normalized_filters,
            "objects": [
                _cache_object_payload(
                    current,
                    protected_until=protected_until,
                    new_archive_expires_at=new_archive_expires_at,
                    retrieval_job_leases=int(retrieval_job_leases or 0),
                    tag_count=int(tag_count),
                )
                for (
                    current,
                    protected_until,
                    new_archive_expires_at,
                    retrieval_job_leases,
                    tag_count,
                ) in rows
            ],
        }

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
    ) -> Iterator[dict[str, object]]:
        now = format_utc_timestamp(utc_now())
        statement, _, _, _ = _cache_list_statement(
            q=q,
            tag=tag,
            collection_id=collection_id,
            source_store=source_store,
            cache_store=cache_store,
            state=state,
            protection=protection,
            expires_before=expires_before,
            expires_after=expires_after,
            sort=sort,
            order=order,
            principal=principal,
            now=now,
        )
        with read_snapshot(self._session_factory) as session:
            rows = session.execute(statement.execution_options(yield_per=100))
            for partition in rows.partitions():
                for (
                    current,
                    protected_until,
                    new_archive_expires_at,
                    job_leases,
                    tag_count,
                ) in partition:
                    yield _cache_object_payload(
                        current,
                        protected_until=protected_until,
                        new_archive_expires_at=new_archive_expires_at,
                        retrieval_job_leases=int(job_leases or 0),
                        tag_count=int(tag_count),
                    )

    def get_cache_object(
        self,
        *,
        collection_id: int,
        source_store: str,
        object_id: str,
        principal: ApplicationPrincipal | None = None,
    ) -> dict[str, object]:
        normalized_id = _normalize_collection_id_or_raise(collection_id)
        normalized_store = source_store.strip().casefold()
        normalized_object = object_id.strip()
        if not normalized_store or not normalized_object:
            raise BadRequest("retrieval cache object identity is required")
        now = format_utc_timestamp(utc_now())
        protected_until, new_archive_expires_at, retrieval_job_leases = _cache_lease_projections(
            now
        )
        with session_scope(self._session_factory) as session:
            require_collection_access(session, principal, CATALOG_READ, normalized_id)
            row = session.execute(
                select(
                    RetrievalCacheObjectRecord,
                    protected_until.label("protected_until"),
                    new_archive_expires_at.label("new_archive_expires_at"),
                    retrieval_job_leases.label("retrieval_job_leases"),
                ).where(
                    RetrievalCacheObjectRecord.source_store == normalized_store,
                    RetrievalCacheObjectRecord.collection_id == normalized_id,
                    RetrievalCacheObjectRecord.object_id == normalized_object,
                )
            ).one_or_none()
            if row is None:
                raise NotFound("retrieval cache object not found")
            current, protected_until, new_archive_expires_at, retrieval_job_leases = row
            return _cache_object_payload(
                current,
                protected_until=protected_until,
                new_archive_expires_at=new_archive_expires_at,
                retrieval_job_leases=int(retrieval_job_leases or 0),
                tag_count=int(
                    session.scalar(
                        select(func.count())
                        .select_from(CollectionTagRecord)
                        .where(CollectionTagRecord.collection_id == normalized_id)
                    )
                    or 0
                ),
            )

    def plan(
        self,
        files: Sequence[tuple[int, str]],
        *,
        idempotency_key: str | None = None,
        lease: timedelta | None = None,
        restore_policy: str = "allow",
        principal: ApplicationPrincipal | None = None,
    ) -> dict[str, object]:
        normalized = _normalize_file_refs(files)
        normalized_idempotency_key = _normalize_plan_idempotency_key(
            uuid.uuid4().hex if idempotency_key is None else idempotency_key
        )
        normalized_restore_policy = _normalize_restore_policy(restore_policy)
        requested_lease = lease or self._config.retrieval_default_lease
        if requested_lease.total_seconds() <= 0:
            raise BadRequest("retrieval lease must be positive")
        if requested_lease > self._config.retrieval_max_lease:
            raise BadRequest("retrieval lease exceeds the configured maximum")
        plan_id = uuid.uuid4().hex
        now = utc_now()
        owner_app = principal.app if principal is not None else ""
        owner_key_id = principal.key_id if principal is not None else None
        request_json = json.dumps(
            [{"collection_id": collection_id, "path": path} for collection_id, path in normalized],
            sort_keys=True,
            separators=(",", ":"),
        )
        creation_identity_sha256 = hashlib.sha256(
            b"riverhog-retrieval-plan-request/v1\x00"
            + canonical_json_bytes(
                {
                    "files": json.loads(request_json),
                    "lease_seconds": int(requested_lease.total_seconds()),
                    "restore_policy": normalized_restore_policy,
                }
            )
        ).hexdigest()
        with session_scope(self._session_factory) as session:
            for collection_id, path in normalized:
                require_collection_access(
                    session,
                    principal,
                    RETRIEVAL_MANAGE,
                    collection_id,
                )
                require_artifact_scope(session, principal, collection_id, path)
            existing = session.scalar(
                select(RetrievalPlanRecord).where(
                    RetrievalPlanRecord.app == owner_app,
                    RetrievalPlanRecord.initiated_by_key_id == owner_key_id,
                    RetrievalPlanRecord.idempotency_key == normalized_idempotency_key,
                )
            )
            if existing is not None:
                if existing.creation_identity_sha256 != creation_identity_sha256:
                    raise Conflict("retrieval plan idempotency identity changed")
                plan_id = existing.id
            else:
                session.add(
                    RetrievalPlanRecord(
                        id=plan_id,
                        app=owner_app,
                        initiated_by_key_id=owner_key_id,
                        idempotency_key=normalized_idempotency_key,
                        creation_identity_sha256=creation_identity_sha256,
                        state="planning",
                        request_json=request_json,
                        lease_seconds=int(requested_lease.total_seconds()),
                        restore_policy=normalized_restore_policy,
                        created_at=format_utc_timestamp(now),
                        expires_at=format_utc_timestamp(
                            now + self._config.retrieval_pending_timeout
                        ),
                        file_commitment_sha256=_RETRIEVAL_PLAN_INITIAL_FILE_COMMITMENT,
                        segment_commitment_sha256=_RETRIEVAL_PLAN_INITIAL_COMMITMENT,
                    )
                )
        return self.advance_plan(
            app=owner_app,
            key_id=owner_key_id,
            plan_id=plan_id,
        )

    def get_plan(
        self,
        *,
        app: str,
        plan_id: str,
        key_id: str | None = None,
    ) -> dict[str, object]:
        with session_scope(self._session_factory) as session:
            plan = self._require_plan(session, app=app, key_id=key_id, plan_id=plan_id)
            self._expire_plan_if_due(plan)
            return _plan_payload(plan)

    def advance_plan(
        self,
        *,
        app: str,
        plan_id: str,
        key_id: str | None = None,
    ) -> dict[str, object]:
        with session_scope(self._session_factory) as session:
            plan = self._require_plan(session, app=app, key_id=key_id, plan_id=plan_id, lock=True)
            self._expire_plan_if_due(plan)
            if plan.state != "planning":
                return _plan_payload(plan)
            try:
                self._advance_plan_record(session, plan)
            except RiverhogError as exc:
                plan.state = "failed"
                plan.failure = str(exc) or exc.__class__.__name__
            return _plan_payload(plan)

    def list_plan_files(
        self,
        *,
        app: str,
        plan_id: str,
        etag: str,
        start_ordinal: int,
        page_size: int,
        key_id: str | None = None,
    ) -> dict[str, object]:
        if start_ordinal < 0 or start_ordinal > RETRIEVAL_FILE_BATCH_MAX:
            raise BadRequest("retrieval plan file ordinal is invalid")
        if page_size < 1 or page_size > _RETRIEVAL_PLAN_FILE_PAGE_MAX:
            raise BadRequest("retrieval plan file page size is invalid")
        with read_snapshot(self._session_factory) as session:
            plan = self._require_plan(session, app=app, key_id=key_id, plan_id=plan_id)
            if plan.state not in {"ready", "consumed"} or plan.etag is None:
                raise InvalidState("retrieval plan is not sealed")
            if etag != plan.etag:
                raise PreconditionFailed("retrieval plan identity changed")
            rows = list(
                session.scalars(
                    select(RetrievalPlanFileRecord)
                    .where(
                        RetrievalPlanFileRecord.plan_id == plan_id,
                        RetrievalPlanFileRecord.file_order >= start_ordinal,
                    )
                    .order_by(RetrievalPlanFileRecord.file_order)
                    .limit(page_size + 1)
                )
            )
        selected = rows[:page_size]
        complete = len(rows) <= page_size
        return {
            "format": "riverhog-retrieval-plan-files/v1",
            "plan_id": plan_id,
            "etag": etag,
            "start_ordinal": start_ordinal,
            "next_ordinal": (selected[-1].file_order + 1 if selected and not complete else None),
            "complete": complete,
            "files": [_plan_file_payload(current) for current in selected],
        }

    def _advance_plan_record(self, session: Session, plan: RetrievalPlanRecord) -> None:
        requested = cast(list[dict[str, object]], json.loads(plan.request_json))
        remaining = _RETRIEVAL_PLAN_SEGMENT_BATCH
        while remaining and plan.next_file_order < len(requested):
            current_ref = requested[plan.next_file_order]
            collection_id = int(str(current_ref["collection_id"]))
            path = str(current_ref["path"])
            plan_file = session.get(
                RetrievalPlanFileRecord,
                (plan.id, plan.next_file_order),
            )
            if plan_file is None:
                if session.get(CollectionDeletionRecord, collection_id) is not None:
                    raise Conflict(f"collection deletion is active: {collection_id}")
                file_record = session.get(CollectionFileRecord, (collection_id, path))
                if file_record is None:
                    raise NotFound(f"file not found: {collection_id}/{path}")
                copy = self._select_copy(session, collection_id)
                plan_file = RetrievalPlanFileRecord(
                    plan_id=plan.id,
                    file_order=plan.next_file_order,
                    collection_id=collection_id,
                    path=path,
                    bytes=file_record.bytes,
                    sha256=file_record.sha256,
                    source_store=copy.store,
                    requires_restore=False,
                )
                session.add(plan_file)
                plan.file_commitment_sha256 = _chain_commitment(
                    plan.file_commitment_sha256,
                    {
                        "collection_id": collection_id,
                        "path": path,
                        "bytes": file_record.bytes,
                        "sha256": file_record.sha256,
                    },
                )
                session.flush()

            rows = list(
                session.scalars(
                    select(CollectionArchiveFileObjectRecord)
                    .where(
                        CollectionArchiveFileObjectRecord.collection_id == collection_id,
                        CollectionArchiveFileObjectRecord.store == plan_file.source_store,
                        CollectionArchiveFileObjectRecord.path == path,
                        CollectionArchiveFileObjectRecord.sequence >= plan.next_placement_sequence,
                    )
                    .order_by(CollectionArchiveFileObjectRecord.sequence)
                    .limit(remaining + 1)
                )
            )
            if not rows:
                raise InvalidState(f"archive placement is missing: {collection_id}/{path}")
            selected = rows[:remaining]
            has_more = len(rows) > remaining
            object_by_identity: dict[tuple[int, str, str], RetrievalPlanObjectRecord] = {}
            previous_placement = session.scalar(
                select(RetrievalPlanPlacementRecord)
                .where(
                    RetrievalPlanPlacementRecord.plan_id == plan.id,
                    RetrievalPlanPlacementRecord.file_order == plan_file.file_order,
                )
                .order_by(RetrievalPlanPlacementRecord.sequence.desc())
                .limit(1)
            )
            expected_file_offset = (
                0
                if previous_placement is None
                else previous_placement.file_offset + previous_placement.bytes
            )
            previous_sequence: int | None = None
            for placement in selected:
                if (
                    previous_sequence is not None and placement.sequence <= previous_sequence
                ) or placement.file_offset != expected_file_offset:
                    raise InvalidState("retrieval plan placement order is not canonical")
                previous_sequence = placement.sequence
                expected_file_offset += placement.bytes
                identity = (collection_id, plan_file.source_store, placement.object_id)
                planned_object = object_by_identity.get(identity)
                if planned_object is None:
                    planned_object = session.scalar(
                        select(RetrievalPlanObjectRecord).where(
                            RetrievalPlanObjectRecord.plan_id == plan.id,
                            RetrievalPlanObjectRecord.collection_id == collection_id,
                            RetrievalPlanObjectRecord.source_store == plan_file.source_store,
                            RetrievalPlanObjectRecord.object_id == placement.object_id,
                        )
                    )
                if planned_object is None:
                    object_record = session.get(
                        CollectionArchiveObjectRecord,
                        identity,
                    )
                    if object_record is None or object_record.kind not in _DATA_KINDS:
                        raise InvalidState("retrieval plan archive object is missing")
                    cached = session.get(
                        RetrievalCacheObjectRecord,
                        (plan_file.source_store, collection_id, placement.object_id),
                    )
                    if cached is not None and cached.state != "ready":
                        cached = None
                    read_mode = (
                        "cache"
                        if cached is not None
                        else self._archive_stores.require(plan_file.source_store).store.read_mode()
                    )
                    planned_object = RetrievalPlanObjectRecord(
                        plan_id=plan.id,
                        object_order=plan.object_count,
                        collection_id=collection_id,
                        source_store=plan_file.source_store,
                        object_id=object_record.object_id,
                        kind=object_record.kind,
                        plaintext_bytes=object_record.plaintext_bytes,
                        stored_bytes=object_record.stored_bytes,
                        sha256=object_record.sha256,
                        read_mode=read_mode,
                        cache_store=cached.cache_store if cached is not None else None,
                        retrieval_bytes=0,
                    )
                    session.add(planned_object)
                    object_by_identity[identity] = planned_object
                    plan.object_count += 1
                    if read_mode == "restore_required":
                        plan.requires_restore = True
                        plan.retrieval_bytes += object_record.stored_bytes
                    session.flush()
                if planned_object.read_mode == "restore_required":
                    plan_file.requires_restore = True

                retrieval_bytes = self._placement_retrieval_bytes(
                    session,
                    planned_object=planned_object,
                    plan_file=plan_file,
                    placement=placement,
                )
                planned_object.retrieval_bytes += retrieval_bytes
                plan.retrieval_bytes += retrieval_bytes
                session.add(
                    RetrievalPlanPlacementRecord(
                        plan_id=plan.id,
                        file_order=plan_file.file_order,
                        sequence=placement.sequence,
                        object_order=planned_object.object_order,
                        file_offset=placement.file_offset,
                        object_offset=placement.object_offset,
                        bytes=placement.bytes,
                        member=placement.member,
                    )
                )
                plan.segment_commitment_sha256 = _chain_commitment(
                    plan.segment_commitment_sha256,
                    {
                        "file_order": plan_file.file_order,
                        "sequence": str(placement.sequence),
                        "collection_id": collection_id,
                        "path": path,
                        "object_order": str(planned_object.object_order),
                        "object_id": planned_object.object_id,
                        "kind": planned_object.kind,
                        "plaintext_bytes": planned_object.plaintext_bytes,
                        "stored_bytes": planned_object.stored_bytes,
                        "sha256": planned_object.sha256,
                        "read_mode": planned_object.read_mode,
                        "cache_store": planned_object.cache_store,
                        "file_offset": placement.file_offset,
                        "object_offset": placement.object_offset,
                        "bytes": placement.bytes,
                        "member": placement.member,
                    },
                )
                plan.next_placement_sequence = placement.sequence + 1
                remaining -= 1

            if not has_more:
                if expected_file_offset != plan_file.bytes:
                    raise InvalidState("retrieval plan placements do not cover the file")
                plan.next_file_order += 1
                plan.next_placement_sequence = 0
        if plan.next_file_order >= len(requested):
            self._seal_plan(plan, file_count=len(requested))

    def _placement_retrieval_bytes(
        self,
        session: Session,
        *,
        planned_object: RetrievalPlanObjectRecord,
        plan_file: RetrievalPlanFileRecord,
        placement: CollectionArchiveFileObjectRecord,
    ) -> int:
        if planned_object.read_mode != "immediate":
            return 0
        if planned_object.kind == "segment":
            return planned_object.stored_bytes
        object_record = session.get(
            CollectionArchiveObjectRecord,
            (
                planned_object.collection_id,
                planned_object.source_store,
                planned_object.object_id,
            ),
        )
        if object_record is None or not object_record.age_state_json:
            raise InvalidState("pack retrieval state is missing")
        source = PackVolumeRetrievalSource(
            volume_id=object_record.object_id,
            object_path=object_record.object_path,
            revision=object_record.revision,
            plaintext_bytes=object_record.plaintext_bytes,
            stored_bytes=object_record.stored_bytes,
            age_state_json=object_record.age_state_json,
        )
        return plan_pack_range_retrieval(
            source,
            (
                PackMemberRetrievalSource(
                    path=plan_file.path,
                    bytes=plan_file.bytes,
                    sha256=plan_file.sha256,
                    data_offset=placement.object_offset,
                ),
            ),
            policy=PackRangeRetrievalPolicy.from_env(
                os.environ,
                store_name=planned_object.source_store,
            ),
        ).accounted_remote_bytes

    @staticmethod
    def _seal_plan(plan: RetrievalPlanRecord, *, file_count: int) -> None:
        plan.etag = hashlib.sha256(
            _canonical_json(
                {
                    "format": "riverhog-retrieval-plan-authority/v1",
                    "lease_seconds": plan.lease_seconds,
                    "restore_policy": plan.restore_policy,
                    "file_count": file_count,
                    "file_identity": plan.file_commitment_sha256,
                    "segment_identity": plan.segment_commitment_sha256,
                    "object_count": str(plan.object_count),
                    "retrieval_bytes": str(plan.retrieval_bytes),
                    "requires_restore": plan.requires_restore,
                }
            )
        ).hexdigest()
        plan.state = "ready"
        plan.ready_at = format_utc_timestamp(utc_now())

    def create(
        self,
        *,
        app: str,
        key_id: str | None = None,
        plan_id: str,
        plan_etag: str,
        event_context: dict[str, object] | None = None,
        principal: ApplicationPrincipal | None = None,
    ) -> dict[str, object]:
        if principal is not None:
            app = principal.app
            key_id = principal.key_id
        job_id = uuid.uuid4().hex
        normalized_event_context = event_context_json(event_context)
        allowance_reserved = False
        now = utc_now()
        now_text = format_utc_timestamp(now)
        try:
            with session_scope(self._session_factory) as session:
                plan = self._require_plan(
                    session,
                    app=app,
                    key_id=key_id,
                    plan_id=plan_id,
                    lock=True,
                )
                self._expire_plan_if_due(plan)
                if not plan_etag or plan_etag != plan.etag:
                    raise Conflict("retrieval plan changed; request a fresh plan")
                if plan.state == "consumed":
                    existing = session.scalar(
                        select(RetrievalJobRecord).where(RetrievalJobRecord.plan_id == plan.id)
                    )
                    if existing is None:
                        raise InvalidState("consumed retrieval plan has no job")
                    if existing.event_context_json != normalized_event_context:
                        raise Conflict("retrieval job retry changed its event context")
                    job_id = existing.id
                    return _job_payload(existing)
                if plan.state != "ready" or plan.etag is None:
                    raise InvalidState("retrieval plan is not ready")
                if plan.requires_restore and plan.restore_policy == "never":
                    raise Conflict(
                        "retrieval requires archive restoration but restore_policy is never"
                    )
                requested = plan.requires_restore
                state = "requested" if requested else "ready"
                expires_at = format_utc_timestamp(now + timedelta(seconds=plan.lease_seconds))
                if key_id is not None and self._download_allowance is not None:
                    self._download_allowance.reserve_retrieval(
                        key_id=key_id,
                        job_id=job_id,
                        expected_bytes=plan.retrieval_bytes,
                        expires_at=format_utc_timestamp(
                            now + self._config.retrieval_pending_timeout
                        ),
                    )
                    allowance_reserved = True
                record = RetrievalJobRecord(
                    id=job_id,
                    plan_id=plan_id,
                    app=app,
                    initiated_by_key_id=key_id,
                    event_context_json=normalized_event_context,
                    state=state,
                    plan_etag=plan.etag,
                    lease_seconds=plan.lease_seconds,
                    created_at=now_text,
                    requested_at=now_text if requested else None,
                    ready_at=None if requested else now_text,
                    expires_at=None if requested else expires_at,
                    next_poll_at=now_text if requested else None,
                )
                session.add(record)
                plan.state = "consumed"
                self._lifecycle_events.emit_retrieval(
                    type="retrieval.requested",
                    job=record,
                    details={
                        "files": len(json.loads(plan.request_json)),
                        "objects": plan.object_count,
                        "restore_required": requested,
                    },
                    session=session,
                )
                if not requested:
                    self._lifecycle_events.emit_retrieval(
                        type="retrieval.ready",
                        job=record,
                        details={"expires_at": expires_at},
                        session=session,
                    )
        except Exception:
            if allowance_reserved and key_id is not None and self._download_allowance is not None:
                self._download_allowance.release_retrieval(job_id=job_id)
            raise
        return self.get(app=app, key_id=key_id, job_id=job_id)

    def get(self, *, app: str, job_id: str, key_id: str | None = None) -> dict[str, object]:
        with session_scope(self._session_factory) as session:
            record = self._require_job(session, app=app, key_id=key_id, job_id=job_id)
            self._expire_job_if_due(session, record)
            return _job_payload(record)

    def renew(
        self,
        *,
        app: str,
        job_id: str,
        lease: timedelta,
        key_id: str | None = None,
    ) -> dict[str, object]:
        if lease.total_seconds() <= 0:
            raise BadRequest("retrieval lease must be positive")
        if lease > self._config.retrieval_max_lease:
            raise BadRequest("retrieval lease exceeds the configured maximum")
        expires_at = format_utc_timestamp(utc_now() + lease)
        with session_scope(self._session_factory) as session:
            record = self._require_job(session, app=app, key_id=key_id, job_id=job_id)
            self._expire_job_if_due(session, record)
            if record.state != "ready":
                raise InvalidState("only a ready retrieval job can be renewed")
            missing_cached = session.scalar(
                select(RetrievalPlanObjectRecord.object_order)
                .where(
                    RetrievalPlanObjectRecord.plan_id == record.plan_id,
                    RetrievalPlanObjectRecord.read_mode.in_({"cache", "restore_required"}),
                    ~exists(
                        select(1).where(
                            RetrievalCacheObjectRecord.source_store
                            == RetrievalPlanObjectRecord.source_store,
                            RetrievalCacheObjectRecord.collection_id
                            == RetrievalPlanObjectRecord.collection_id,
                            RetrievalCacheObjectRecord.object_id
                            == RetrievalPlanObjectRecord.object_id,
                            RetrievalCacheObjectRecord.state == "ready",
                        )
                    ),
                )
                .limit(1)
            )
            if missing_cached is not None:
                raise InvalidState("retrieval cache object disappeared before renewal")
            record.lease_seconds = int(lease.total_seconds())
            record.expires_at = expires_at
            self._lifecycle_events.emit_retrieval(
                type="retrieval.renewed",
                job=record,
                details={"expires_at": expires_at},
                session=session,
            )
            return _job_payload(record)

    def acknowledge(self, *, app: str, job_id: str, key_id: str | None = None) -> dict[str, object]:
        with session_scope(self._session_factory) as session:
            record = self._require_job(session, app=app, key_id=key_id, job_id=job_id)
            if record.state not in {"ready", "completed"}:
                raise InvalidState("only a ready retrieval job can be acknowledged")
            if record.state != "completed":
                record.state = "completed"
                record.completed_at = format_utc_timestamp(utc_now())
                self._lifecycle_events.emit_retrieval(
                    type="retrieval.completed",
                    job=record,
                    terminal=True,
                    session=session,
                )
            payload = _job_payload(record)
        if self._download_allowance is not None:
            self._download_allowance.release_retrieval(job_id=job_id)
        return payload

    def cancel(self, *, app: str, job_id: str, key_id: str | None = None) -> dict[str, object]:
        with session_scope(self._session_factory) as session:
            record = self._require_job(session, app=app, key_id=key_id, job_id=job_id)
            self._expire_job_if_due(session, record)
            if record.state in {"completed", "expired"}:
                raise InvalidState(f"retrieval job is already {record.state}")
            if record.state != "canceled":
                record.state = "canceled"
                record.canceled_at = format_utc_timestamp(utc_now())
                record.next_poll_at = None
                self._lifecycle_events.emit_retrieval(
                    type="retrieval.canceled",
                    job=record,
                    terminal=True,
                    session=session,
                )
            payload = _job_payload(record)
        if self._download_allowance is not None:
            self._download_allowance.release_retrieval(job_id=job_id)
        return payload

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
    ) -> tuple[Iterator[bytes], int, str]:
        with session_scope(self._session_factory) as session:
            job = self._require_job(session, app=app, key_id=key_id, job_id=job_id)
            self._expire_job_if_due(session, job)
            if job.state != "ready":
                raise InvalidState("retrieval job is not ready")
            plan_file = session.scalar(
                select(RetrievalPlanFileRecord).where(
                    RetrievalPlanFileRecord.plan_id == job.plan_id,
                    RetrievalPlanFileRecord.collection_id == collection_id,
                    RetrievalPlanFileRecord.path == path,
                )
            )
            if plan_file is None:
                raise NotFound("file is not part of this retrieval job")
            planned = list(
                session.execute(
                    select(RetrievalPlanPlacementRecord, RetrievalPlanObjectRecord)
                    .join(
                        RetrievalPlanObjectRecord,
                        (RetrievalPlanObjectRecord.plan_id == RetrievalPlanPlacementRecord.plan_id)
                        & (
                            RetrievalPlanObjectRecord.object_order
                            == RetrievalPlanPlacementRecord.object_order
                        ),
                    )
                    .where(
                        RetrievalPlanPlacementRecord.plan_id == job.plan_id,
                        RetrievalPlanPlacementRecord.file_order == plan_file.file_order,
                    )
                    .order_by(RetrievalPlanPlacementRecord.sequence)
                    .limit(2)
                )
            )
            if not planned:
                raise InvalidState("retrieval file has no archive placement")
            attribution = _download_attribution(job)
            expected_bytes = plan_file.bytes
            expected_sha256 = plan_file.sha256
            requested_size = expected_bytes - offset if size is None else size
            if offset < 0 or requested_size < 0 or offset + requested_size > expected_bytes:
                raise InvalidRange("retrieval content range is invalid")
            collection = session.get(CollectionRecord, collection_id)
            if collection is None:
                raise NotFound(f"collection not found: {collection_id}")
            passphrase_id = collection.passphrase_id
            passphrase = self._config.archive_passphrase_for(passphrase_id)
            plan_id = job.plan_id
            file_order = plan_file.file_order
            source_store = plan_file.source_store

        kinds = {current.kind for _placement, current in planned}
        if kinds == {"pack"} and len(planned) == 1:
            placement, planned_object = planned[0]
            record = self._catalog_archive_object(planned_object)
            cached = self._current_cached_object(planned_object)
            if not record.age_state_json:
                raise InvalidState("pack volume is missing its age state")
            source = PackVolumeRetrievalSource(
                volume_id=record.object_id,
                object_path=record.object_path,
                revision=record.revision,
                plaintext_bytes=record.plaintext_bytes,
                stored_bytes=record.stored_bytes,
                age_state_json=record.age_state_json,
            )
            member = PackMemberRetrievalSource(
                path=path,
                bytes=expected_bytes,
                sha256=expected_sha256,
                data_offset=placement.object_offset,
            )
            chunks = PackMemberRangeReader(
                self._range_store(
                    source_store=source_store,
                    object_record=record,
                    cached=cached,
                    attribution=attribution,
                ),
                passphrase=passphrase,
                read_working_bytes=self._throughput.retrieval_read_chunk_bytes,
                resources=self._resources,
                session_cache=self._age_sessions[passphrase_id],
                timing_observer=log_transfer_timing,
                policy=PackRangeRetrievalPolicy.from_env(
                    os.environ,
                    store_name=source_store,
                ),
            ).iter_member_range(source, member, offset=offset, size=requested_size)
            return chunks, expected_bytes, expected_sha256

        if kinds == {"segment"}:
            chunks = self._iter_raw_plan_range(
                plan_id=plan_id,
                file_order=file_order,
                source_store=source_store,
                path=path,
                expected_bytes=expected_bytes,
                expected_sha256=expected_sha256,
                offset=offset,
                size=requested_size,
                passphrase=passphrase,
                passphrase_id=passphrase_id,
                attribution=attribution,
            )
            return chunks, expected_bytes, expected_sha256

        raise InvalidState("retrieval file has inconsistent archive volume kinds")

    def _catalog_archive_object(
        self,
        planned: RetrievalPlanObjectRecord,
    ) -> CollectionArchiveObjectRecord:
        with read_snapshot(self._session_factory) as session:
            record = session.get(
                CollectionArchiveObjectRecord,
                (planned.collection_id, planned.source_store, planned.object_id),
            )
            if record is None or record.kind != planned.kind:
                raise InvalidState("retrieval archive volume is missing")
            return record

    def _current_cached_object(
        self,
        planned: RetrievalPlanObjectRecord,
    ) -> RetrievalCacheObjectRecord | None:
        if planned.read_mode == "immediate":
            return None
        with read_snapshot(self._session_factory) as session:
            cached = session.get(
                RetrievalCacheObjectRecord,
                (planned.source_store, planned.collection_id, planned.object_id),
            )
            if cached is None or cached.state != "ready":
                raise InvalidState("retrieval cache object is unavailable")
            return cached

    def _iter_raw_plan_range(
        self,
        *,
        plan_id: str,
        file_order: int,
        source_store: str,
        path: str,
        expected_bytes: int,
        expected_sha256: str,
        offset: int,
        size: int,
        passphrase: str | bytes,
        passphrase_id: str,
        attribution: DownloadAttribution | None,
    ) -> Iterator[bytes]:
        requested_end = offset + size
        next_sequence = 0
        logical_cursor = offset
        emitted = 0
        digest = hashlib.sha256() if offset == 0 and size == expected_bytes else None
        while logical_cursor < requested_end:
            with read_snapshot(self._session_factory) as session:
                rows = list(
                    session.execute(
                        select(RetrievalPlanPlacementRecord, RetrievalPlanObjectRecord)
                        .join(
                            RetrievalPlanObjectRecord,
                            (
                                RetrievalPlanObjectRecord.plan_id
                                == RetrievalPlanPlacementRecord.plan_id
                            )
                            & (
                                RetrievalPlanObjectRecord.object_order
                                == RetrievalPlanPlacementRecord.object_order
                            ),
                        )
                        .where(
                            RetrievalPlanPlacementRecord.plan_id == plan_id,
                            RetrievalPlanPlacementRecord.file_order == file_order,
                            RetrievalPlanPlacementRecord.sequence >= next_sequence,
                            RetrievalPlanPlacementRecord.file_offset
                            + RetrievalPlanPlacementRecord.bytes
                            > offset,
                            RetrievalPlanPlacementRecord.file_offset < requested_end,
                        )
                        .order_by(RetrievalPlanPlacementRecord.sequence)
                        .limit(_RETRIEVAL_PLAN_SEGMENT_BATCH)
                    )
                )
            if not rows:
                raise InvalidState("retrieval plan does not cover the requested file range")
            for placement, planned_object in rows:
                volume_start = placement.file_offset
                volume_end = volume_start + placement.bytes
                current_start = max(offset, volume_start)
                current_end = min(requested_end, volume_end)
                if current_start != logical_cursor:
                    raise InvalidState("retrieval plan raw segments are not contiguous")
                record = self._catalog_archive_object(planned_object)
                cached = self._current_cached_object(planned_object)
                if not record.age_state_json or not record.archive_parts_json:
                    raise InvalidState("raw volume is missing its retrieval state")
                source = RawVolumeRetrievalSource(
                    volume_id=record.object_id,
                    object_path=record.object_path,
                    revision=record.revision,
                    source_path=path,
                    file_offset=placement.file_offset,
                    plaintext_bytes=placement.bytes,
                    file_bytes=expected_bytes,
                    file_sha256=expected_sha256,
                    age_state_json=record.age_state_json,
                    parts=_stored_parts(record.archive_parts_json),
                )
                reader = RawVolumeRangeReader(
                    self._range_store(
                        source_store=source_store,
                        object_record=record,
                        cached=cached,
                        attribution=attribution,
                    ),
                    passphrase=passphrase,
                    request_concurrency=self._throughput.retrieval_request_concurrency,
                    read_working_bytes=self._throughput.retrieval_read_chunk_bytes,
                    resources=self._resources,
                    session_cache=self._age_sessions[passphrase_id],
                    timing_observer=log_transfer_timing,
                )
                for chunk in reader.iter_volume_range(
                    source,
                    offset=current_start - volume_start,
                    size=current_end - current_start,
                ):
                    emitted += len(chunk)
                    if digest is not None:
                        digest.update(chunk)
                    yield chunk
                logical_cursor = current_end
                next_sequence = placement.sequence + 1
                if logical_cursor == requested_end:
                    break
        if emitted != size:
            raise InvalidState("retrieval raw range emitted an unexpected byte count")
        if digest is not None and digest.hexdigest() != expected_sha256:
            raise InvalidState("retrieval raw file verification failed")

    def _range_store(
        self,
        *,
        source_store: str,
        object_record: CollectionArchiveObjectRecord,
        cached: RetrievalCacheObjectRecord | None,
        attribution: DownloadAttribution | None,
    ) -> ArchiveObjectRangeStore:
        if cached is None:
            base = self._archive_stores.require(source_store).object_ranges
            tracked_store = source_store
        else:
            if self._cache is None:
                raise RuntimeError("retrieval cache is unavailable")
            base = _CachedArchiveRangeStore(
                self._cache,
                archive_object_path=object_record.object_path,
                cache_store=cached.cache_store,
                cache_object_path=cached.object_path,
                cache_revision=cached.revision,
            )
            tracked_store = "retrieval-cache"
        if self._download_allowance is None:
            return base
        return _TrackedArchiveRangeStore(
            base,
            allowance=self._download_allowance,
            store_name=tracked_store,
            attribution=attribution,
        )

    def content_metadata(
        self,
        *,
        app: str,
        job_id: str,
        collection_id: int,
        path: str,
        key_id: str | None = None,
    ) -> tuple[int, str]:
        with session_scope(self._session_factory) as session:
            job = self._require_job(session, app=app, key_id=key_id, job_id=job_id)
            self._expire_job_if_due(session, job)
            if job.state != "ready":
                raise InvalidState("retrieval job is not ready")
            plan_file = session.scalar(
                select(RetrievalPlanFileRecord).where(
                    RetrievalPlanFileRecord.plan_id == job.plan_id,
                    RetrievalPlanFileRecord.collection_id == collection_id,
                    RetrievalPlanFileRecord.path == path,
                )
            )
            if plan_file is None:
                raise NotFound("file is not part of this retrieval job")
            return plan_file.bytes, plan_file.sha256

    def process_due(self, *, limit: int = 10) -> int:
        if limit < 1:
            return 0
        now_text = format_utc_timestamp(utc_now())
        with session_scope(self._session_factory) as session:
            job_ids = list(
                session.scalars(
                    select(RetrievalJobRecord.id)
                    .where(
                        RetrievalJobRecord.state == "requested",
                        RetrievalJobRecord.next_poll_at <= now_text,
                    )
                    .order_by(RetrievalJobRecord.next_poll_at, RetrievalJobRecord.id)
                    .limit(limit)
                )
            )
        for job_id in job_ids:
            self._process_one(job_id)
        return len(job_ids)

    def requeue_interrupted_cache_cleanup_for_startup(self) -> int:
        with session_scope(self._session_factory) as session:
            result = session.execute(
                update(RetrievalCacheObjectRecord)
                .where(RetrievalCacheObjectRecord.state == "deleting")
                .values(state="delete_pending")
            )
            return int(getattr(result, "rowcount", 0) or 0)

    def sweep(self, *, limit: int = 100) -> int:
        if limit < 1:
            return 0
        if self._cache is not None:
            self._cache.reap_abandoned_populations(limit=limit)
        now_text = format_utc_timestamp(utc_now())
        with session_scope(self._session_factory) as session:
            expired_plans = session.scalars(
                select(RetrievalPlanRecord)
                .where(
                    RetrievalPlanRecord.state.in_({"planning", "ready"}),
                    RetrievalPlanRecord.expires_at <= now_text,
                )
                .order_by(RetrievalPlanRecord.expires_at, RetrievalPlanRecord.id)
                .limit(limit)
                .with_for_update(skip_locked=True)
            ).all()
            for plan in expired_plans:
                plan.state = "expired"
            expired_jobs = session.scalars(
                select(RetrievalJobRecord)
                .where(
                    RetrievalJobRecord.state == "ready",
                    RetrievalJobRecord.expires_at <= now_text,
                )
                .order_by(RetrievalJobRecord.expires_at, RetrievalJobRecord.id)
                .limit(limit)
                .with_for_update(skip_locked=True)
            ).all()
            for job in expired_jobs:
                job.state = "expired"
                self._lifecycle_events.emit_retrieval(
                    type="retrieval.expired",
                    job=job,
                    terminal=True,
                    session=session,
                )
                _release_job_reservation(session, job.id)
            expired_leases = list(
                session.scalars(
                    select(RetrievalCacheLeaseRecord)
                    .where(RetrievalCacheLeaseRecord.expires_at <= now_text)
                    .order_by(
                        RetrievalCacheLeaseRecord.expires_at,
                        RetrievalCacheLeaseRecord.owner,
                    )
                    .limit(limit)
                )
            )
            for lease in expired_leases:
                session.delete(lease)
            if self._cache is None:
                return 0
            candidates = list(
                session.scalars(
                    select(RetrievalCacheObjectRecord)
                    .where(
                        (RetrievalCacheObjectRecord.state == "delete_pending")
                        | (
                            (RetrievalCacheObjectRecord.state == "ready")
                            & ~select(RetrievalCacheLeaseRecord.owner)
                            .where(
                                RetrievalCacheLeaseRecord.source_store
                                == RetrievalCacheObjectRecord.source_store,
                                RetrievalCacheLeaseRecord.collection_id
                                == RetrievalCacheObjectRecord.collection_id,
                                RetrievalCacheLeaseRecord.object_id
                                == RetrievalCacheObjectRecord.object_id,
                            )
                            .exists()
                            & ~_active_retrieval_cache_reference(now_text)
                        )
                    )
                    .order_by(
                        case(
                            (RetrievalCacheObjectRecord.state == "delete_pending", 0),
                            else_=1,
                        ),
                        RetrievalCacheObjectRecord.cached_at,
                        RetrievalCacheObjectRecord.source_store,
                        RetrievalCacheObjectRecord.collection_id,
                        RetrievalCacheObjectRecord.object_id,
                    )
                    .limit(limit)
                    .with_for_update(skip_locked=True)
                )
            )
            cleanup = [
                (
                    cached.source_store,
                    cached.collection_id,
                    cached.object_id,
                    cached.cache_store,
                    cached.object_path,
                    cached.revision,
                )
                for cached in candidates
            ]
            for cached in candidates:
                cached.state = "deleting"

        removed = 0
        for source_store, collection_id, object_id, cache_store, object_path, revision in cleanup:
            try:
                self._cache.delete(
                    cache_store=cache_store,
                    object_path=object_path,
                    revision=revision,
                )
            except Exception:
                with session_scope(self._session_factory) as session:
                    cache_record = session.get(
                        RetrievalCacheObjectRecord,
                        (source_store, collection_id, object_id),
                    )
                    if (
                        cache_record is not None
                        and cache_record.state == "deleting"
                        and cache_record.object_path == object_path
                        and cache_record.revision == revision
                    ):
                        cache_record.state = "delete_pending"
                continue
            with session_scope(self._session_factory) as session:
                cache_record = session.get(
                    RetrievalCacheObjectRecord,
                    (source_store, collection_id, object_id),
                )
                if (
                    cache_record is not None
                    and cache_record.state == "deleting"
                    and cache_record.object_path == object_path
                    and cache_record.revision == revision
                ):
                    accounting = session.get(
                        RetrievalCacheStoreAccountingRecord,
                        cache_record.cache_store,
                    )
                    if accounting is None:
                        raise RuntimeError("retrieval cache accounting is inconsistent")
                    adjust_cache_committed_bytes(
                        accounting,
                        delta=-cache_record.stored_bytes,
                    )
                    session.delete(cache_record)
                    removed += 1
        return removed

    def _select_copy(self, session: Session, collection_id: int) -> CollectionArchiveCopyRecord:
        retiring_stores = set(
            session.scalars(
                select(ArchiveCopyRetirementRecord.store).where(
                    ArchiveCopyRetirementRecord.collection_id == collection_id
                )
            ).all()
        )
        copies = {
            copy.store: copy
            for copy in session.scalars(
                select(CollectionArchiveCopyRecord).where(
                    CollectionArchiveCopyRecord.collection_id == collection_id
                )
            )
            if archive_copy_is_complete(copy) and copy.store not in retiring_stores
        }
        for store in self._config.archive_read_order:
            if store in copies:
                return copies[store]
        raise InvalidState(f"collection has no readable archive copy: {collection_id}")

    def _process_one(self, job_id: str) -> None:
        try:
            work = self._claim_restore_step(job_id)
            if work is None:
                if self._download_allowance is not None:
                    with read_snapshot(self._session_factory) as session:
                        state = session.scalar(
                            select(RetrievalJobRecord.state).where(RetrievalJobRecord.id == job_id)
                        )
                    if state == "failed":
                        self._download_allowance.release_retrieval(job_id=job_id)
                return
            action, planned, object_identity, attribution = work
            store = self._archive_stores.require(planned.source_store).store
            if action == "prepare":
                if self._cache is None:
                    raise RuntimeError("retrieval cache is unavailable")
                admission = self._cache.admit(
                    owner=_job_object_owner(job_id, planned.object_order),
                    source_store=planned.source_store,
                    collection_id=planned.collection_id,
                    object_id=planned.object_id,
                    expected_bytes=planned.stored_bytes,
                )
                if admission is None:
                    next_poll = format_utc_timestamp(
                        utc_now() + self._config.retrieval_restore_poll_interval
                    )
                    with session_scope(self._session_factory) as session:
                        progress = session.get(
                            RetrievalJobObjectProgressRecord,
                            (job_id, planned.object_order),
                        )
                        job = session.get(RetrievalJobRecord, job_id)
                        if progress is not None and job is not None and job.state == "requested":
                            progress.next_poll_at = next_poll
                            job.next_poll_at = next_poll
                    return
                store.prepare_archive_objects_read(
                    collection_id=planned.collection_id,
                    objects=(object_identity,),
                )
                now = utc_now()
                with session_scope(self._session_factory) as session:
                    progress = session.get(
                        RetrievalJobObjectProgressRecord,
                        (job_id, planned.object_order),
                    )
                    job = session.get(RetrievalJobRecord, job_id)
                    if progress is None or job is None or job.state != "requested":
                        return
                    progress.state = "requested"
                    progress.prepare_requested_at = format_utc_timestamp(now)
                    progress.next_poll_at = format_utc_timestamp(now)
                    if job.restore_requested_at is None:
                        job.restore_requested_at = format_utc_timestamp(now)
                    job.next_poll_at = format_utc_timestamp(now)
                    job.failure = None
                return

            status = store.get_archive_objects_read_status(
                collection_id=planned.collection_id,
                objects=(object_identity,),
            )
            if status.state == "expired":
                with session_scope(self._session_factory) as session:
                    progress = session.get(
                        RetrievalJobObjectProgressRecord,
                        (job_id, planned.object_order),
                    )
                    job = session.get(RetrievalJobRecord, job_id)
                    if progress is not None and job is not None and job.state == "requested":
                        progress.state = "preparing"
                        progress.next_poll_at = format_utc_timestamp(utc_now())
                        job.next_poll_at = progress.next_poll_at
                        job.failure = None
                return
            if status.state != "ready":
                next_poll = format_utc_timestamp(
                    utc_now() + self._config.retrieval_restore_poll_interval
                )
                with session_scope(self._session_factory) as session:
                    progress = session.get(
                        RetrievalJobObjectProgressRecord,
                        (job_id, planned.object_order),
                    )
                    job = session.get(RetrievalJobRecord, job_id)
                    if progress is not None and job is not None and job.state == "requested":
                        progress.next_poll_at = next_poll
                        job.next_poll_at = next_poll
                return
            self._cache_restored_object(
                job_id=job_id,
                planned=planned,
                object_identity=object_identity,
                attribution=attribution,
                store=store,
            )
        except Exception as exc:
            with session_scope(self._session_factory) as session:
                job = session.get(RetrievalJobRecord, job_id)
                if job is not None and job.state == "requested":
                    failure = str(exc) or exc.__class__.__name__
                    changed = job.failure != failure
                    job.failure = failure
                    job.next_poll_at = format_utc_timestamp(
                        utc_now() + self._config.retrieval_restore_poll_interval
                    )
                    if changed:
                        self._lifecycle_events.emit_retrieval(
                            type="retrieval.issue",
                            job=job,
                            details={"error": failure},
                            session=session,
                        )

    def _claim_restore_step(
        self,
        job_id: str,
    ) -> (
        tuple[
            str,
            RetrievalPlanObjectRecord,
            ArchiveObjectIdentity,
            DownloadAttribution | None,
        ]
        | None
    ):
        now = utc_now()
        now_text = format_utc_timestamp(now)
        with session_scope(self._session_factory) as session:
            job = session.scalar(
                select(RetrievalJobRecord).where(RetrievalJobRecord.id == job_id).with_for_update()
            )
            if job is None or job.state != "requested":
                return None
            if parse_utc_timestamp(job.created_at) + self._config.retrieval_pending_timeout <= now:
                self._fail_pending_job(
                    session,
                    job,
                    "retrieval exceeded the configured pending timeout",
                )
                return None

            planned = session.scalar(
                select(RetrievalPlanObjectRecord)
                .where(
                    RetrievalPlanObjectRecord.plan_id == job.plan_id,
                    RetrievalPlanObjectRecord.read_mode == "restore_required",
                    ~exists(
                        select(1).where(
                            RetrievalJobObjectProgressRecord.job_id == job.id,
                            RetrievalJobObjectProgressRecord.object_order
                            == RetrievalPlanObjectRecord.object_order,
                        )
                    ),
                )
                .order_by(RetrievalPlanObjectRecord.object_order)
                .limit(1)
            )
            if planned is not None:
                cached = session.get(
                    RetrievalCacheObjectRecord,
                    (planned.source_store, planned.collection_id, planned.object_id),
                )
                state = "ready" if cached is not None and cached.state == "ready" else "preparing"
                session.add(
                    RetrievalJobObjectProgressRecord(
                        job_id=job.id,
                        plan_id=job.plan_id,
                        object_order=planned.object_order,
                        state=state,
                        next_poll_at=now_text,
                        cache_store=(
                            cached.cache_store
                            if cached is not None and cached.state == "ready"
                            else None
                        ),
                    )
                )
                job.next_poll_at = now_text
                if state == "ready":
                    return None
                record = session.get(
                    CollectionArchiveObjectRecord,
                    (planned.collection_id, planned.source_store, planned.object_id),
                )
                if record is None:
                    raise InvalidState("retrieval archive object is missing")
                return "prepare", planned, _object_identity(record), _download_attribution(job)

            progress = session.scalar(
                select(RetrievalJobObjectProgressRecord)
                .where(
                    RetrievalJobObjectProgressRecord.job_id == job.id,
                    RetrievalJobObjectProgressRecord.state != "ready",
                    RetrievalJobObjectProgressRecord.next_poll_at <= now_text,
                )
                .order_by(RetrievalJobObjectProgressRecord.object_order)
                .limit(1)
            )
            if progress is None:
                remaining = session.scalar(
                    select(RetrievalJobObjectProgressRecord.next_poll_at)
                    .where(
                        RetrievalJobObjectProgressRecord.job_id == job.id,
                        RetrievalJobObjectProgressRecord.state != "ready",
                    )
                    .order_by(RetrievalJobObjectProgressRecord.next_poll_at)
                    .limit(1)
                )
                if remaining is not None:
                    job.next_poll_at = remaining
                    return None
                self._mark_job_ready(session, job, now=now)
                return None
            planned = session.get(
                RetrievalPlanObjectRecord,
                (job.plan_id, progress.object_order),
            )
            if planned is None:
                raise InvalidState("retrieval plan object is missing")
            record = session.get(
                CollectionArchiveObjectRecord,
                (planned.collection_id, planned.source_store, planned.object_id),
            )
            if record is None:
                raise InvalidState("retrieval archive object is missing")
            action = "prepare" if progress.state == "preparing" else "poll"
            return action, planned, _object_identity(record), _download_attribution(job)

    def _cache_restored_object(
        self,
        *,
        job_id: str,
        planned: RetrievalPlanObjectRecord,
        object_identity: ArchiveObjectIdentity,
        attribution: DownloadAttribution | None,
        store: ArchiveStore,
    ) -> None:
        if self._cache is None:
            raise RuntimeError("retrieval cache is unavailable")
        admission = self._cache.admit(
            owner=_job_object_owner(job_id, planned.object_order),
            source_store=planned.source_store,
            collection_id=planned.collection_id,
            object_id=planned.object_id,
            expected_bytes=planned.stored_bytes,
        )
        if admission is None:
            with session_scope(self._session_factory) as session:
                cached = session.get(
                    RetrievalCacheObjectRecord,
                    (planned.source_store, planned.collection_id, planned.object_id),
                )
                progress = session.get(
                    RetrievalJobObjectProgressRecord,
                    (job_id, planned.object_order),
                )
                job = session.get(RetrievalJobRecord, job_id)
                if progress is None or job is None or job.state != "requested":
                    return
                if cached is not None and cached.state == "ready":
                    progress.state = "ready"
                    progress.cache_store = cached.cache_store
                    progress.next_poll_at = format_utc_timestamp(utc_now())
                    job.next_poll_at = progress.next_poll_at
                else:
                    next_poll = format_utc_timestamp(
                        utc_now() + self._config.retrieval_restore_poll_interval
                    )
                    progress.next_poll_at = next_poll
                    job.next_poll_at = next_poll
            return
        receipt = self._cache.put(
            admission=admission,
            content=store.iter_stored_archive_object(
                collection_id=planned.collection_id,
                object=object_identity,
                attribution=attribution,
            ),
        )
        try:
            _validate_cache_receipt(receipt, object_identity)
        except Exception as receipt_error:
            try:
                self._cache.delete(
                    cache_store=receipt.cache_store,
                    object_path=receipt.object_path,
                    revision=receipt.revision,
                )
            except Exception as cleanup_error:
                raise RuntimeError(
                    f"{receipt_error}; retrieval cache cleanup also failed: {cleanup_error}"
                ) from cleanup_error
            raise
        with session_scope(self._session_factory) as session:
            register_cache_ready(
                session,
                source_store=planned.source_store,
                collection_id=planned.collection_id,
                object_id=planned.object_id,
                receipt=receipt,
            )
            progress = session.get(
                RetrievalJobObjectProgressRecord,
                (job_id, planned.object_order),
            )
            job = session.get(RetrievalJobRecord, job_id)
            if progress is not None and job is not None and job.state == "requested":
                progress.state = "ready"
                progress.cache_store = receipt.cache_store
                progress.next_poll_at = format_utc_timestamp(utc_now())
                job.next_poll_at = progress.next_poll_at
                job.failure = None
        self._cache.release(owner=_job_object_owner(job_id, planned.object_order))

    def _mark_job_ready(
        self,
        session: Session,
        job: RetrievalJobRecord,
        *,
        now: datetime,
    ) -> None:
        expires_at = format_utc_timestamp(now + timedelta(seconds=job.lease_seconds))
        job.state = "ready"
        job.ready_at = format_utc_timestamp(now)
        job.expires_at = expires_at
        job.next_poll_at = None
        job.failure = None
        self._lifecycle_events.emit_retrieval(
            type="retrieval.ready",
            job=job,
            details={"expires_at": expires_at},
            session=session,
        )

    def _fail_pending_job(
        self,
        session: Session,
        job: RetrievalJobRecord,
        failure: str,
    ) -> None:
        job.state = "failed"
        job.failure = failure
        job.next_poll_at = None
        _release_job_reservation(session, job.id)
        self._lifecycle_events.emit_retrieval(
            type="retrieval.failed",
            job=job,
            details={"error": failure},
            terminal=True,
            session=session,
        )

    @staticmethod
    def _require_plan(
        session: Session,
        *,
        app: str,
        plan_id: str,
        key_id: str | None = None,
        lock: bool = False,
    ) -> RetrievalPlanRecord:
        statement = select(RetrievalPlanRecord).where(RetrievalPlanRecord.id == plan_id)
        if lock:
            statement = statement.with_for_update()
        record = session.scalar(statement)
        if (
            record is None
            or (record.app and record.app != app)
            or (record.initiated_by_key_id is not None and record.initiated_by_key_id != key_id)
        ):
            raise NotFound(f"retrieval plan not found: {plan_id}")
        return record

    @staticmethod
    def _expire_plan_if_due(plan: RetrievalPlanRecord) -> None:
        if (
            plan.state in {"planning", "ready"}
            and parse_utc_timestamp(plan.expires_at) <= utc_now()
        ):
            plan.state = "expired"

    @staticmethod
    def _require_job(
        session: Session,
        *,
        app: str,
        job_id: str,
        key_id: str | None = None,
    ) -> RetrievalJobRecord:
        record = session.get(RetrievalJobRecord, job_id)
        if (
            record is None
            or record.app != app
            or (key_id is not None and record.initiated_by_key_id != key_id)
        ):
            raise NotFound(f"retrieval job not found: {job_id}")
        return record

    def _expire_job_if_due(self, session: Session, job: RetrievalJobRecord) -> None:
        if (
            job.state == "ready"
            and job.expires_at is not None
            and parse_utc_timestamp(job.expires_at) <= utc_now()
        ):
            job.state = "expired"
            self._lifecycle_events.emit_retrieval(
                type="retrieval.expired",
                job=job,
                terminal=True,
                session=session,
            )
            _release_job_reservation(session, job.id)


def _canonical_json(payload: object) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")


class _CachedArchiveRangeStore:
    def __init__(
        self,
        cache: RetrievalCache,
        *,
        archive_object_path: str,
        cache_store: str,
        cache_object_path: str,
        cache_revision: str | None,
    ) -> None:
        self._cache = cache
        self._archive_object_path = archive_object_path
        self._cache_store = cache_store
        self._cache_object_path = cache_object_path
        self._cache_revision = cache_revision

    def iter_object_range(
        self,
        *,
        object_path: str,
        revision: str | None,
        expected_bytes: int,
        offset: int,
        size: int,
    ) -> Iterator[bytes]:
        _ = revision
        if object_path != self._archive_object_path:
            raise ValueError("retrieval cache archive object identity changed")
        return self._cache.iter_object_range(
            cache_store=self._cache_store,
            object_path=self._cache_object_path,
            revision=self._cache_revision,
            expected_bytes=expected_bytes,
            offset=offset,
            size=size,
        )


class _TrackedArchiveRangeStore:
    def __init__(
        self,
        store: ArchiveObjectRangeStore,
        *,
        allowance: DownloadAllowance,
        store_name: str,
        attribution: DownloadAttribution | None,
    ) -> None:
        self._store = store
        self._allowance = allowance
        self._store_name = store_name
        self._attribution = attribution

    def iter_object_range(
        self,
        *,
        object_path: str,
        revision: str | None,
        expected_bytes: int,
        offset: int,
        size: int,
    ) -> Iterator[bytes]:
        content = self._store.iter_object_range(
            object_path=object_path,
            revision=revision,
            expected_bytes=expected_bytes,
            offset=offset,
            size=size,
        )
        return self._allowance.track(
            store=self._store_name,
            expected_bytes=size,
            content=content,
            attribution=self._attribution,
        )


def _stored_parts(content: str) -> tuple[StoredArchivePart, ...]:
    try:
        values = json.loads(content)
    except json.JSONDecodeError as exc:
        raise InvalidState("archive part receipts are not valid JSON") from exc
    if not isinstance(values, list):
        raise InvalidState("archive part receipts are not a list")
    try:
        return tuple(
            StoredArchivePart(
                number=int(value["number"]),
                plaintext_start=int(value["plaintext_start"]),
                plaintext_bytes=int(value["plaintext_bytes"]),
                plaintext_sha256=str(value["plaintext_sha256"]),
                stored_bytes=int(value["stored_bytes"]),
                stored_sha256=str(value["stored_sha256"]),
            )
            for value in values
            if isinstance(value, dict)
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise InvalidState("archive part receipt is invalid") from exc


def _normalize_file_refs(files: Sequence[tuple[int, str]]) -> tuple[tuple[int, str], ...]:
    try:
        document = RetrievalFileReferenceSetDocument.model_validate(
            {
                "files": [
                    {"collection_id": collection_id, "path": path} for collection_id, path in files
                ]
            }
        )
    except ValueError as exc:
        raise BadRequest(str(exc)) from exc
    return tuple((item.collection_id, item.path) for item in document.files)


def _normalize_restore_policy(value: str) -> str:
    normalized = value.strip().casefold()
    if normalized not in {"allow", "never"}:
        raise BadRequest("restore_policy must be allow or never")
    return normalized


def _cache_list_statement(
    *,
    q: str | None,
    tag: str | None,
    collection_id: int | None,
    source_store: str | None,
    cache_store: str | None,
    state: str | None,
    protection: str | None,
    expires_before: str | None,
    expires_after: str | None,
    sort: str,
    order: str,
    principal: ApplicationPrincipal | None,
    now: str,
) -> tuple[Any, tuple[Any, ...], dict[str, object], str | None]:
    if sort not in _CACHE_SORT_FIELDS:
        raise BadRequest(f"sort must be one of {', '.join(sorted(_CACHE_SORT_FIELDS))}")
    if order not in _SORT_ORDERS:
        raise BadRequest("order must be asc or desc")
    normalized_collection_id = (
        _normalize_collection_id_or_raise(collection_id) if collection_id is not None else None
    )
    normalized_store = (
        source_store.strip().casefold() if source_store and source_store.strip() else None
    )
    normalized_cache_store = (
        cache_store.strip().casefold() if cache_store and cache_store.strip() else None
    )
    normalized_state = state.strip().casefold() if state and state.strip() else None
    if normalized_state is not None and normalized_state not in _CACHE_STATES:
        raise BadRequest(f"state must be one of {', '.join(sorted(_CACHE_STATES))}")
    normalized_protection = (
        protection.strip().casefold() if protection and protection.strip() else None
    )
    if normalized_protection is not None and normalized_protection not in _CACHE_PROTECTION_FILTERS:
        raise BadRequest(
            f"protection must be one of {', '.join(sorted(_CACHE_PROTECTION_FILTERS))}"
        )
    normalized_expires_before = _normalize_cache_expiry(expires_before, name="expires_before")
    normalized_expires_after = _normalize_cache_expiry(expires_after, name="expires_after")
    if (
        normalized_expires_before is not None
        and normalized_expires_after is not None
        and normalized_expires_after > normalized_expires_before
    ):
        raise BadRequest("expires_after must not be later than expires_before")
    normalized_tag = tag.strip().casefold() if tag and tag.strip() else None
    needle = q.strip().casefold() if q and q.strip() else None
    protected_until, new_archive_expires_at, retrieval_job_leases = _cache_lease_projections(now)
    tag_count = (
        select(func.count())
        .select_from(CollectionTagRecord)
        .where(CollectionTagRecord.collection_id == RetrievalCacheObjectRecord.collection_id)
        .correlate(RetrievalCacheObjectRecord)
        .scalar_subquery()
    )
    statement = select(
        RetrievalCacheObjectRecord,
        protected_until.label("protected_until"),
        new_archive_expires_at.label("new_archive_expires_at"),
        retrieval_job_leases.label("retrieval_job_leases"),
        tag_count.label("tag_count"),
    ).where(
        collection_access_filter(RetrievalCacheObjectRecord.collection_id, principal, CATALOG_READ)
    )
    if normalized_collection_id is not None:
        statement = statement.where(
            RetrievalCacheObjectRecord.collection_id == normalized_collection_id
        )
    if normalized_store is not None:
        statement = statement.where(RetrievalCacheObjectRecord.source_store == normalized_store)
    if normalized_cache_store is not None:
        statement = statement.where(
            RetrievalCacheObjectRecord.cache_store == normalized_cache_store
        )
    if normalized_state is not None:
        statement = statement.where(RetrievalCacheObjectRecord.state == normalized_state)
    if normalized_protection == "protected":
        statement = statement.where(protected_until.is_not(None))
    elif normalized_protection == "unleased":
        statement = statement.where(protected_until.is_(None))
    if normalized_expires_before is not None:
        statement = statement.where(protected_until <= normalized_expires_before)
    if normalized_expires_after is not None:
        statement = statement.where(protected_until >= normalized_expires_after)
    if normalized_tag is not None:
        statement = statement.where(
            exists(
                select(1).where(
                    CollectionTagRecord.collection_id == RetrievalCacheObjectRecord.collection_id,
                    CollectionTagRecord.tag_id == normalized_tag,
                )
            )
        )
    if needle is not None:
        escaped = needle.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        search_filters: list[Any] = [
            RetrievalCacheObjectRecord.search_text.like(f"%{escaped}%", escape="\\")
        ]
        if needle.isdigit():
            search_filters.append(RetrievalCacheObjectRecord.collection_id == int(needle))
        statement = statement.where(or_(*search_filters))
    sort_expressions = {
        "collection_id": RetrievalCacheObjectRecord.collection_id,
        "source_store": RetrievalCacheObjectRecord.source_store,
        "object_id": RetrievalCacheObjectRecord.object_id,
        "stored_bytes": RetrievalCacheObjectRecord.stored_bytes,
        "cached_at": RetrievalCacheObjectRecord.cached_at,
        "verified_at": RetrievalCacheObjectRecord.verified_at,
        "protected_until": protected_until,
    }
    expression = sort_expressions[sort]
    if sort == "protected_until":
        expression = func.coalesce(expression, "")
    key_columns = tuple(
        dict.fromkeys(
            (
                expression,
                RetrievalCacheObjectRecord.collection_id,
                RetrievalCacheObjectRecord.source_store,
                RetrievalCacheObjectRecord.object_id,
            )
        )
    )
    normalized_filters: dict[str, object] = {
        "tag": normalized_tag,
        "collection_id": normalized_collection_id,
        "source_store": normalized_store,
        "cache_store": normalized_cache_store,
        "state": normalized_state,
        "protection": normalized_protection,
        "expires_before": normalized_expires_before,
        "expires_after": normalized_expires_after,
    }
    return statement, key_columns, normalized_filters, needle


def _cache_list_position(row: Any, *, sort: str) -> tuple[str | int, ...]:
    record = row[0]
    protected_until = row[1]
    values: dict[str, str | int] = {
        "collection_id": record.collection_id,
        "source_store": record.source_store,
        "object_id": record.object_id,
        "stored_bytes": record.stored_bytes,
        "cached_at": record.cached_at,
        "verified_at": record.verified_at or "",
        "protected_until": protected_until or "",
    }
    keys = [sort]
    for key in ("collection_id", "source_store", "object_id"):
        if key != sort:
            keys.append(key)
    return tuple(values[key] for key in keys)


def _cache_lease_projections(now: str) -> tuple[Any, Any, Any]:
    matches_object = (
        (RetrievalCacheLeaseRecord.source_store == RetrievalCacheObjectRecord.source_store)
        & (RetrievalCacheLeaseRecord.collection_id == RetrievalCacheObjectRecord.collection_id)
        & (RetrievalCacheLeaseRecord.object_id == RetrievalCacheObjectRecord.object_id)
        & (RetrievalCacheLeaseRecord.expires_at > now)
    )
    lease_protected_until = (
        select(func.max(RetrievalCacheLeaseRecord.expires_at))
        .where(matches_object)
        .correlate(RetrievalCacheObjectRecord)
        .scalar_subquery()
    )
    new_archive_expires_at = (
        select(func.max(RetrievalCacheLeaseRecord.expires_at))
        .where(matches_object, RetrievalCacheLeaseRecord.owner == "new-archive")
        .correlate(RetrievalCacheObjectRecord)
        .scalar_subquery()
    )
    retrieval_protected_until, retrieval_references = _retrieval_reference_projections(now)
    protected_until = case(
        (lease_protected_until.is_(None), retrieval_protected_until),
        (retrieval_protected_until.is_(None), lease_protected_until),
        (lease_protected_until >= retrieval_protected_until, lease_protected_until),
        else_=retrieval_protected_until,
    )
    return protected_until, new_archive_expires_at, retrieval_references


def _retrieval_reference_projections(now: str) -> tuple[Any, Any]:
    active_until = case(
        (
            RetrievalPlanRecord.state.in_({"planning", "ready"}),
            RetrievalPlanRecord.expires_at,
        ),
        (
            RetrievalJobRecord.state == "requested",
            RetrievalPlanRecord.expires_at,
        ),
        (
            RetrievalJobRecord.state == "ready",
            RetrievalJobRecord.expires_at,
        ),
        else_=None,
    )
    matches = (
        (RetrievalPlanObjectRecord.source_store == RetrievalCacheObjectRecord.source_store)
        & (RetrievalPlanObjectRecord.collection_id == RetrievalCacheObjectRecord.collection_id)
        & (RetrievalPlanObjectRecord.object_id == RetrievalCacheObjectRecord.object_id)
    )
    base = (
        select(RetrievalPlanObjectRecord)
        .join(RetrievalPlanRecord)
        .outerjoin(RetrievalJobRecord, RetrievalJobRecord.plan_id == RetrievalPlanRecord.id)
        .where(matches, active_until > now)
        .correlate(RetrievalCacheObjectRecord)
    )
    protected_until = base.with_only_columns(func.max(active_until)).scalar_subquery()
    references = base.with_only_columns(func.count()).scalar_subquery()
    return protected_until, references


def _cache_object_payload(
    current: RetrievalCacheObjectRecord,
    *,
    protected_until: str | None,
    new_archive_expires_at: str | None,
    retrieval_job_leases: int,
    tag_count: int,
) -> dict[str, object]:
    categories: list[str] = []
    if new_archive_expires_at is not None:
        categories.append("new_archive")
    if retrieval_job_leases:
        categories.append("retrieval_job")
    return {
        "collection_id": current.collection_id,
        "source_store": current.source_store,
        "cache_store": current.cache_store,
        "object_id": current.object_id,
        "state": current.state,
        "stored_bytes": current.stored_bytes,
        "stored_sha256": current.stored_sha256,
        "cached_at": current.cached_at,
        "verified_at": current.verified_at,
        "protected_until": protected_until,
        "new_archive_expires_at": new_archive_expires_at,
        "lease_categories": categories,
        "retrieval_job_leases": retrieval_job_leases,
        "tag_count": tag_count,
    }


def _normalize_cache_expiry(value: str | None, *, name: str) -> str | None:
    normalized = value.strip() if value and value.strip() else None
    if normalized is None:
        return None
    try:
        return format_utc_timestamp(parse_utc_timestamp(normalized))
    except ValueError as exc:
        raise BadRequest(f"{name} must be an ISO 8601 timestamp with a timezone") from exc


def _normalize_collection_id_or_raise(value: str | int) -> int:
    try:
        return normalize_collection_id(value)
    except PathNormalizationError as exc:
        raise BadRequest(str(exc)) from exc


def _object_identity(row: CollectionArchiveObjectRecord) -> ArchiveObjectIdentity:
    return ArchiveObjectIdentity(
        object_id=row.object_id,
        kind=row.kind,
        object_path=row.object_path,
        plaintext_bytes=row.plaintext_bytes,
        stored_bytes=row.stored_bytes,
        sha256=row.sha256,
        stored_sha256=row.stored_sha256,
        revision=row.revision,
    )


def _validate_cache_receipt(
    receipt: RetrievalCacheReceipt,
    identity: ArchiveObjectIdentity,
) -> None:
    if (
        not receipt.cache_store
        or not receipt.object_path
        or receipt.stored_bytes != identity.stored_bytes
        or (identity.stored_sha256 is not None and receipt.stored_sha256 != identity.stored_sha256)
        or (receipt.stored_sha256 is not None and len(receipt.stored_sha256) != 64)
        or not receipt.cached_at
        or not receipt.verified_at
    ):
        raise RuntimeError("retrieval cache receipt does not match verified archive metadata")
    parse_utc_timestamp(receipt.cached_at)
    parse_utc_timestamp(receipt.verified_at)


def _job_object_owner(job_id: str, object_order: int) -> str:
    return f"job-object:{job_id}:{object_order}"


def _active_retrieval_cache_reference(now: str) -> ColumnElement[bool]:
    return exists(
        select(1)
        .select_from(RetrievalPlanObjectRecord)
        .join(
            RetrievalPlanRecord,
            RetrievalPlanRecord.id == RetrievalPlanObjectRecord.plan_id,
        )
        .outerjoin(
            RetrievalJobRecord,
            RetrievalJobRecord.plan_id == RetrievalPlanRecord.id,
        )
        .where(
            RetrievalPlanObjectRecord.source_store == RetrievalCacheObjectRecord.source_store,
            RetrievalPlanObjectRecord.collection_id == RetrievalCacheObjectRecord.collection_id,
            RetrievalPlanObjectRecord.object_id == RetrievalCacheObjectRecord.object_id,
            or_(
                (
                    RetrievalPlanRecord.state.in_({"planning", "ready"})
                    & (RetrievalPlanRecord.expires_at > now)
                ),
                (
                    (RetrievalJobRecord.state == "requested")
                    & (RetrievalPlanRecord.expires_at > now)
                ),
                ((RetrievalJobRecord.state == "ready") & (RetrievalJobRecord.expires_at > now)),
            ),
        )
    )


def _download_attribution(job: RetrievalJobRecord) -> DownloadAttribution | None:
    if job.initiated_by_key_id is None:
        return None
    return DownloadAttribution(key_id=job.initiated_by_key_id, job_id=job.id)


def _release_job_reservation(session: Session, job_id: str) -> None:
    from riverhog_core.catalog_models import KeyDownloadReservationRecord

    session.execute(
        delete(KeyDownloadReservationRecord).where(
            KeyDownloadReservationRecord.job_id == job_id,
            KeyDownloadReservationRecord.kind == "job",
        )
    )


def _job_payload(record: RetrievalJobRecord) -> dict[str, object]:
    plan = record.plan
    return {
        "id": record.id,
        "plan_id": record.plan_id,
        "state": record.state,
        "plan_etag": record.plan_etag,
        "created_at": record.created_at,
        "requested_at": record.requested_at,
        "restore_requested_at": record.restore_requested_at,
        "ready_at": record.ready_at,
        "expires_at": record.expires_at,
        "completed_at": record.completed_at,
        "canceled_at": record.canceled_at,
        "failure": record.failure,
        "lease_seconds": record.lease_seconds,
        "restore_policy": plan.restore_policy,
        "requires_restore": plan.requires_restore,
    }


def _plan_payload(record: RetrievalPlanRecord) -> dict[str, object]:
    return {
        "format": "riverhog-retrieval-plan/v1",
        "id": record.id,
        "state": record.state,
        "created_at": record.created_at,
        "ready_at": record.ready_at,
        "expires_at": record.expires_at,
        "failure": record.failure,
        "lease_seconds": record.lease_seconds,
        "restore_policy": record.restore_policy,
        "requires_restore": record.requires_restore,
        "file_count": len(json.loads(record.request_json)),
        "etag": record.etag,
    }


def _normalize_plan_idempotency_key(value: str) -> str:
    normalized = value.strip()
    if not normalized or normalized != value or len(normalized) > 200:
        raise BadRequest("retrieval plan idempotency_key is invalid")
    return normalized


def _plan_file_payload(record: RetrievalPlanFileRecord) -> dict[str, object]:
    return {
        "collection_id": record.collection_id,
        "path": record.path,
        "bytes": record.bytes,
        "sha256": record.sha256,
        "requires_restore": record.requires_restore,
    }


def _chain_commitment(previous: str, value: object) -> str:
    digest = hashlib.sha256()
    digest.update(b"riverhog-retrieval-plan-chain/v1\x00")
    digest.update(bytes.fromhex(previous))
    digest.update(canonical_json_bytes(value))
    return digest.hexdigest()
