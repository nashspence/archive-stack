from __future__ import annotations

import hashlib
import json
import os
import uuid
from collections.abc import Iterator, Mapping, Sequence
from datetime import datetime, timedelta
from typing import Any, cast

from riverhog_protocol import PortableCollectionRecord, RetrievalFileReferenceSetDocument
from riverhog_protocol.errors import BadRequest, Conflict, InvalidState, NotFound
from riverhog_protocol.paths import PathNormalizationError, normalize_collection_id
from sqlalchemy import case, delete, desc, exists, func, or_, select, update
from sqlalchemy.orm import Session
from state_schema import read_snapshot
from time_formats import format_utc_timestamp, parse_utc_timestamp, utc_now

from riverhog_core.app_permissions import CATALOG_READ, RETRIEVAL_MANAGE, ApplicationPrincipal
from riverhog_core.archive_store_registry import ArchiveStoreRegistry
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
    RetrievalJobFileRecord,
    RetrievalJobObjectRecord,
    RetrievalJobRecord,
)
from riverhog_core.collection_access import collection_access_filter, require_collection_access
from riverhog_core.collection_metadata import collection_record_manifest
from riverhog_core.domain.archive import StoredArchivePart
from riverhog_core.pack_retrieval import (
    PackMemberRangeReader,
    PackMemberRetrievalSource,
    PackRangeRetrievalPolicy,
    PackVolumeRetrievalSource,
    plan_pack_range_retrieval,
)
from riverhog_core.ports.archive_objects import ArchiveObjectRangeStore
from riverhog_core.ports.archive_store import ArchiveObjectIdentity
from riverhog_core.ports.download_allowance import DownloadAllowance, DownloadAttribution
from riverhog_core.ports.retrieval_cache import RetrievalCache, RetrievalCacheReceipt
from riverhog_core.raw_retrieval import (
    RawFileRangeReader,
    RawVolumeRangeReader,
    RawVolumeRetrievalSource,
)
from riverhog_core.runtime_config import RuntimeConfig
from riverhog_core.services.archive_records import archive_copy_is_complete
from riverhog_core.services.lifecycle_events import (
    SqlAlchemyLifecycleEventService,
    event_context_json,
)
from riverhog_core.streaming_age import ResumableAgeSessionCache
from riverhog_core.throughput import (
    ArchiveThroughputTuning,
    ArchiveTransferResources,
    log_transfer_timing,
)

_DATA_KINDS = {"pack", "segment"}
_CACHE_SORT_FIELDS = {
    "collection_id",
    "source_store",
    "object_id",
    "stored_bytes",
    "cached_at",
    "verified_at",
    "protected_until",
}
_CACHE_STATES = {"ready", "delete_pending", "deleting"}
_CACHE_PROTECTION_FILTERS = {"protected", "unleased"}


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

    def abort_incomplete_cache_writes(
        self,
        *,
        initiated_before: datetime,
    ) -> int:
        if self._cache is None:
            return 0
        return self._cache.abort_incomplete_writes(initiated_before=initiated_before)

    def collection_manifest(
        self,
        collection_id: int,
        *,
        principal: ApplicationPrincipal | None = None,
    ) -> tuple[PortableCollectionRecord, str]:
        normalized_id = _normalize_collection_id_or_raise(collection_id)
        if principal is not None and principal.artifact_scope is not None:
            raise NotFound(f"collection manifest not found: {normalized_id}")
        with session_scope(self._session_factory) as session:
            collection = session.get(CollectionRecord, normalized_id)
            if collection is None:
                raise NotFound(f"collection not found: {normalized_id}")
            require_collection_access(session, principal, CATALOG_READ, normalized_id)
            files = list(
                session.scalars(
                    select(CollectionFileRecord)
                    .where(CollectionFileRecord.collection_id == normalized_id)
                    .order_by(CollectionFileRecord.path)
                )
            )
            tags = tuple(
                session.scalars(
                    select(CollectionTagRecord.tag_id)
                    .where(CollectionTagRecord.collection_id == normalized_id)
                    .order_by(CollectionTagRecord.tag_id)
                ).all()
            )
            payload, etag = collection_record_manifest(
                collection_id=normalized_id,
                content_identity=collection.content_identity,
                encryption_format=collection.encryption_format,
                passphrase_id=collection.passphrase_id,
                provenance_mode=collection.provenance_mode,
                provenance_identity=collection.provenance_identity,
                metadata_revision=collection.metadata_revision,
                tags=tags,
                files=((row.path, row.bytes, row.sha256) for row in files),
            )
            if etag != collection.record_etag:
                raise InvalidState("collection record does not match its catalog ETag")
            return payload, etag

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
        with session_scope(self._session_factory) as session:
            total = int(
                session.scalar(select(func.count()).select_from(CollectionRecord).where(visible))
                or 0
            )
            rows = session.execute(
                select(CollectionRecord.id, CollectionRecord.record_etag)
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
                    {"collection_id": row.id, "etag": str(row.record_etag)} for row in rows
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
        with session_scope(self._session_factory) as session:
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
        with session_scope(self._session_factory) as session:
            scanned_sequences = list(
                session.scalars(
                    select(CatalogEventRecord.sequence)
                    .where(CatalogEventRecord.sequence > after)
                    .order_by(CatalogEventRecord.sequence)
                    .limit(limit + 1)
                ).all()
            )
            has_more = len(scanned_sequences) > limit
            page_sequences = scanned_sequences[:limit]
            cursor = int(page_sequences[-1]) if page_sequences else after
            if not page_sequences:
                return {"cursor": cursor, "has_more": False, "changes": []}
            visibility, projected_change = catalog_event_projection(principal, CATALOG_READ)
            rows = session.execute(
                select(CatalogEventRecord, projected_change.label("projected_change"))
                .where(CatalogEventRecord.sequence.in_(page_sequences), visibility)
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
                        "etag": event.record_etag,
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
        with session_scope(self._session_factory) as session:
            objects, stored_bytes, protected = session.execute(
                select(
                    func.count(),
                    func.coalesce(func.sum(RetrievalCacheObjectRecord.stored_bytes), 0),
                    func.coalesce(func.sum(case((active_lease, 1), else_=0)), 0),
                ).where(visible, RetrievalCacheObjectRecord.state == "ready")
            ).one()
        return {
            "configured": self._cache is not None,
            "new_archive_enabled": (
                self._cache is not None and self._config.retrieval_cache_new_archive_enabled
            ),
            "objects": int(objects),
            "stored_bytes": int(stored_bytes),
            "protected_objects": int(protected),
            "unleased_objects": int(objects) - int(protected),
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
        principal: ApplicationPrincipal | None = None,
    ) -> dict[str, object]:
        if page < 1 or per_page < 1 or per_page > 100:
            raise BadRequest("retrieval cache page and page size are invalid")
        if sort not in _CACHE_SORT_FIELDS:
            raise BadRequest(f"sort must be one of {', '.join(sorted(_CACHE_SORT_FIELDS))}")
        if order not in {"asc", "desc"}:
            raise BadRequest("order must be asc or desc")
        normalized_collection_id = (
            _normalize_collection_id_or_raise(collection_id) if collection_id is not None else None
        )
        normalized_store = (
            source_store.strip().casefold() if source_store and source_store.strip() else None
        )
        normalized_state = state.strip().casefold() if state and state.strip() else None
        if normalized_state is not None and normalized_state not in _CACHE_STATES:
            raise BadRequest(f"state must be one of {', '.join(sorted(_CACHE_STATES))}")
        normalized_protection = (
            protection.strip().casefold() if protection and protection.strip() else None
        )
        if (
            normalized_protection is not None
            and normalized_protection not in _CACHE_PROTECTION_FILTERS
        ):
            raise BadRequest(
                f"protection must be one of {', '.join(sorted(_CACHE_PROTECTION_FILTERS))}"
            )
        normalized_expires_before = _normalize_cache_expiry(
            expires_before,
            name="expires_before",
        )
        normalized_expires_after = _normalize_cache_expiry(
            expires_after,
            name="expires_after",
        )
        if (
            normalized_expires_before is not None
            and normalized_expires_after is not None
            and normalized_expires_after > normalized_expires_before
        ):
            raise BadRequest("expires_after must not be later than expires_before")
        normalized_tag = tag.strip().casefold() if tag and tag.strip() else None
        needle = q.strip().casefold() if q and q.strip() else None
        now = format_utc_timestamp(utc_now())
        lease_summary = _active_cache_lease_summary(now).subquery()
        statement = (
            select(
                RetrievalCacheObjectRecord,
                lease_summary.c.protected_until,
                lease_summary.c.new_archive_expires_at,
                lease_summary.c.retrieval_job_leases,
            )
            .outerjoin(
                lease_summary,
                (lease_summary.c.source_store == RetrievalCacheObjectRecord.source_store)
                & (lease_summary.c.collection_id == RetrievalCacheObjectRecord.collection_id)
                & (lease_summary.c.object_id == RetrievalCacheObjectRecord.object_id),
            )
            .where(
                collection_access_filter(
                    RetrievalCacheObjectRecord.collection_id,
                    principal,
                    CATALOG_READ,
                ),
            )
        )
        if normalized_collection_id is not None:
            statement = statement.where(
                RetrievalCacheObjectRecord.collection_id == normalized_collection_id
            )
        if normalized_store is not None:
            statement = statement.where(RetrievalCacheObjectRecord.source_store == normalized_store)
        if normalized_state is not None:
            statement = statement.where(RetrievalCacheObjectRecord.state == normalized_state)
        if normalized_protection == "protected":
            statement = statement.where(lease_summary.c.protected_until.is_not(None))
        elif normalized_protection == "unleased":
            statement = statement.where(lease_summary.c.protected_until.is_(None))
        if normalized_expires_before is not None:
            statement = statement.where(
                lease_summary.c.protected_until <= normalized_expires_before
            )
        if normalized_expires_after is not None:
            statement = statement.where(lease_summary.c.protected_until >= normalized_expires_after)
        if normalized_tag is not None:
            statement = statement.where(
                exists(
                    select(1).where(
                        CollectionTagRecord.collection_id
                        == RetrievalCacheObjectRecord.collection_id,
                        CollectionTagRecord.tag_id == normalized_tag,
                    )
                )
            )
        if needle is not None:
            filters = [
                func.lower(RetrievalCacheObjectRecord.source_store).contains(needle),
                func.lower(RetrievalCacheObjectRecord.object_id).contains(needle),
            ]
            if needle.isdigit():
                filters.append(RetrievalCacheObjectRecord.collection_id == int(needle))
            statement = statement.where(or_(*filters))
        sort_expressions = {
            "collection_id": RetrievalCacheObjectRecord.collection_id,
            "source_store": RetrievalCacheObjectRecord.source_store,
            "object_id": RetrievalCacheObjectRecord.object_id,
            "stored_bytes": RetrievalCacheObjectRecord.stored_bytes,
            "cached_at": RetrievalCacheObjectRecord.cached_at,
            "verified_at": RetrievalCacheObjectRecord.verified_at,
            "protected_until": lease_summary.c.protected_until,
        }
        expression = sort_expressions[sort]
        ordered = desc(expression) if order == "desc" else expression.asc()
        statement = statement.order_by(
            ordered,
            RetrievalCacheObjectRecord.collection_id,
            RetrievalCacheObjectRecord.source_store,
            RetrievalCacheObjectRecord.object_id,
        )
        with session_scope(self._session_factory) as session:
            total = int(session.scalar(select(func.count()).select_from(statement.subquery())) or 0)
            statement = statement.offset((page - 1) * per_page).limit(per_page)
            rows = list(session.execute(statement))
            tags = _collection_tags(
                session,
                {current.collection_id for current, *_ in rows},
            )
        return {
            "page": page,
            "per_page": per_page,
            "total": total,
            "pages": ((total + per_page - 1) // per_page),
            "sort": sort,
            "order": order,
            "query": needle,
            "filters": {
                "tag": normalized_tag,
                "collection_id": normalized_collection_id,
                "source_store": normalized_store,
                "state": normalized_state,
                "protection": normalized_protection,
                "expires_before": normalized_expires_before,
                "expires_after": normalized_expires_after,
            },
            "objects": [
                _cache_object_payload(
                    current,
                    protected_until=protected_until,
                    new_archive_expires_at=new_archive_expires_at,
                    retrieval_job_leases=int(retrieval_job_leases or 0),
                    tags=tags.get(current.collection_id, ()),
                )
                for current, protected_until, new_archive_expires_at, retrieval_job_leases in rows
            ],
        }

    def iter_cache_objects(
        self,
        *,
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
        principal: ApplicationPrincipal | None = None,
    ) -> Iterator[dict[str, object]]:
        now = format_utc_timestamp(utc_now())
        statement, _, _ = _cache_list_statement(
            q=q,
            tag=tag,
            collection_id=collection_id,
            source_store=source_store,
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
                tags = _collection_tags(
                    session,
                    {current.collection_id for current, *_ in partition},
                )
                for current, protected_until, new_archive_expires_at, job_leases in partition:
                    yield _cache_object_payload(
                        current,
                        protected_until=protected_until,
                        new_archive_expires_at=new_archive_expires_at,
                        retrieval_job_leases=int(job_leases or 0),
                        tags=tags.get(current.collection_id, ()),
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
        lease_summary = _active_cache_lease_summary(now).subquery()
        with session_scope(self._session_factory) as session:
            require_collection_access(session, principal, CATALOG_READ, normalized_id)
            row = session.execute(
                select(
                    RetrievalCacheObjectRecord,
                    lease_summary.c.protected_until,
                    lease_summary.c.new_archive_expires_at,
                    lease_summary.c.retrieval_job_leases,
                )
                .outerjoin(
                    lease_summary,
                    (lease_summary.c.source_store == RetrievalCacheObjectRecord.source_store)
                    & (lease_summary.c.collection_id == RetrievalCacheObjectRecord.collection_id)
                    & (lease_summary.c.object_id == RetrievalCacheObjectRecord.object_id),
                )
                .where(
                    RetrievalCacheObjectRecord.source_store == normalized_store,
                    RetrievalCacheObjectRecord.collection_id == normalized_id,
                    RetrievalCacheObjectRecord.object_id == normalized_object,
                )
            ).one_or_none()
            if row is None:
                raise NotFound("retrieval cache object not found")
            current, protected_until, new_archive_expires_at, retrieval_job_leases = row
            tags = _collection_tags(session, {normalized_id}).get(normalized_id, ())
            return _cache_object_payload(
                current,
                protected_until=protected_until,
                new_archive_expires_at=new_archive_expires_at,
                retrieval_job_leases=int(retrieval_job_leases or 0),
                tags=tags,
            )

    def plan(
        self,
        files: Sequence[tuple[int, str]],
        *,
        lease: timedelta | None = None,
        restore_policy: str = "allow",
        principal: ApplicationPrincipal | None = None,
    ) -> dict[str, object]:
        normalized = _normalize_file_refs(files)
        normalized_restore_policy = _normalize_restore_policy(restore_policy)
        requested_lease = lease or self._config.retrieval_default_lease
        if requested_lease.total_seconds() <= 0:
            raise BadRequest("retrieval lease must be positive")
        if requested_lease > self._config.retrieval_max_lease:
            raise BadRequest("retrieval lease exceeds the configured maximum")
        with session_scope(self._session_factory) as session:
            for collection_id, path in normalized:
                require_collection_access(
                    session,
                    principal,
                    RETRIEVAL_MANAGE,
                    collection_id,
                )
                if principal is not None and not principal.allows_artifact(
                    RETRIEVAL_MANAGE,
                    collection_id,
                    path,
                ):
                    raise NotFound(f"collection file not found: {collection_id}/{path}")
            payload = self._build_plan(
                session,
                normalized,
                requested_lease,
                restore_policy=normalized_restore_policy,
            )
        canonical = _canonical_json(payload)
        return {
            **payload,
            "etag": hashlib.sha256(canonical).hexdigest(),
        }

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
    ) -> dict[str, object]:
        normalized = _normalize_file_refs(files)
        if principal is not None:
            app = principal.app
            key_id = principal.key_id
        plan = self.plan(
            normalized,
            lease=lease,
            restore_policy=restore_policy,
            principal=principal,
        )
        if not plan_etag or plan_etag != plan["etag"]:
            raise Conflict("retrieval plan changed; request a fresh plan")
        job_id = uuid.uuid4().hex
        now = utc_now()
        now_text = format_utc_timestamp(now)
        lease_seconds = int(str(plan["lease_seconds"]))
        planned_files = cast(list[dict[str, object]], plan["files"])
        planned_objects = cast(list[dict[str, object]], plan["objects"])
        requested = any(current["read_mode"] == "restore_required" for current in planned_objects)
        if requested and plan["restore_policy"] == "never":
            raise Conflict("retrieval requires archive restoration but restore_policy is never")
        state = "requested" if requested else "ready"
        expires_at = format_utc_timestamp(now + timedelta(seconds=lease_seconds))
        remote_bytes = sum(
            int(str(current["retrieval_bytes"]))
            + (
                int(str(current["stored_bytes"]))
                if current["read_mode"] == "restore_required"
                else 0
            )
            for current in planned_objects
        )
        if key_id is not None and self._download_allowance is not None:
            self._download_allowance.reserve_retrieval(
                key_id=key_id,
                job_id=job_id,
                expected_bytes=remote_bytes,
                expires_at=format_utc_timestamp(now + self._config.retrieval_pending_timeout),
            )
        try:
            with session_scope(self._session_factory) as session:
                for collection_id in sorted({collection_id for collection_id, _path in normalized}):
                    collection = session.scalar(
                        select(CollectionRecord)
                        .where(CollectionRecord.id == collection_id)
                        .with_for_update()
                    )
                    if collection is None:
                        raise NotFound(f"collection not found: {collection_id}")
                    if session.get(CollectionDeletionRecord, collection_id) is not None:
                        raise Conflict(f"collection deletion is active: {collection_id}")
                planned_sources = {
                    (
                        cast(int, current["collection_id"]),
                        str(current["source_store"]),
                    )
                    for current in planned_objects
                }
                for collection_id, source_store in sorted(planned_sources):
                    if (
                        session.get(
                            ArchiveCopyRetirementRecord,
                            (collection_id, source_store),
                        )
                        is not None
                    ):
                        raise Conflict(
                            f"archive copy retirement is active: {collection_id} in {source_store}"
                        )
                record = RetrievalJobRecord(
                    id=job_id,
                    app=app,
                    initiated_by_key_id=key_id,
                    event_context_json=event_context_json(event_context),
                    state=state,
                    plan_etag=str(plan["etag"]),
                    constraints_json=json.dumps(plan, sort_keys=True, separators=(",", ":")),
                    created_at=now_text,
                    requested_at=now_text if requested else None,
                    ready_at=None if requested else now_text,
                    expires_at=None if requested else expires_at,
                    next_poll_at=now_text if requested else None,
                )
                session.add(record)
                for order, current in enumerate(planned_files):
                    record.files.append(
                        RetrievalJobFileRecord(
                            job_id=job_id,
                            collection_id=cast(int, current["collection_id"]),
                            path=str(current["path"]),
                            file_order=order,
                        )
                    )
                for order, current in enumerate(planned_objects):
                    record.objects.append(
                        RetrievalJobObjectRecord(
                            job_id=job_id,
                            collection_id=cast(int, current["collection_id"]),
                            source_store=str(current["source_store"]),
                            object_id=str(current["object_id"]),
                            object_order=order,
                            read_mode=str(current["read_mode"]),
                        )
                    )
                    if current["read_mode"] == "cache":
                        self._lease_cached_object(
                            session,
                            owner=_job_owner(job_id),
                            source_store=str(current["source_store"]),
                            collection_id=cast(int, current["collection_id"]),
                            object_id=str(current["object_id"]),
                            expires_at=expires_at,
                        )
                self._lifecycle_events.emit_retrieval(
                    type="retrieval.requested",
                    job=record,
                    details={
                        "files": len(planned_files),
                        "objects": len(planned_objects),
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
            if key_id is not None and self._download_allowance is not None:
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
            plan = json.loads(record.constraints_json)
            plan["lease_seconds"] = int(lease.total_seconds())
            record.constraints_json = json.dumps(plan, sort_keys=True, separators=(",", ":"))
            record.expires_at = expires_at
            for current in record.objects:
                if current.read_mode not in {"cache", "restore_required"}:
                    continue
                cached = session.get(
                    RetrievalCacheObjectRecord,
                    (current.source_store, current.collection_id, current.object_id),
                )
                if cached is None or cached.state != "ready":
                    raise InvalidState("retrieval cache object disappeared before renewal")
                self._lease_cached_object(
                    session,
                    owner=_job_owner(job_id),
                    source_store=current.source_store,
                    collection_id=current.collection_id,
                    object_id=current.object_id,
                    expires_at=expires_at,
                )
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
                session.execute(
                    delete(RetrievalCacheLeaseRecord).where(
                        RetrievalCacheLeaseRecord.owner == _job_owner(job_id)
                    )
                )
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
                session.execute(
                    delete(RetrievalCacheLeaseRecord).where(
                        RetrievalCacheLeaseRecord.owner == _job_owner(job_id)
                    )
                )
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
        key_id: str | None = None,
    ) -> tuple[Iterator[bytes], int, str]:
        with session_scope(self._session_factory) as session:
            job = self._require_job(session, app=app, key_id=key_id, job_id=job_id)
            self._expire_job_if_due(session, job)
            if job.state != "ready":
                raise InvalidState("retrieval job is not ready")
            if session.get(RetrievalJobFileRecord, (job_id, collection_id, path)) is None:
                raise NotFound("file is not part of this retrieval job")
            file_record = session.get(CollectionFileRecord, (collection_id, path))
            if file_record is None:
                raise NotFound("file is no longer present")
            source_store = str(
                session.scalar(
                    select(RetrievalJobObjectRecord.source_store)
                    .where(
                        RetrievalJobObjectRecord.job_id == job_id,
                        RetrievalJobObjectRecord.collection_id == collection_id,
                    )
                    .limit(1)
                )
                or ""
            )
            if not source_store:
                raise InvalidState("retrieval job has no source archive")
            placements = list(
                session.scalars(
                    select(CollectionArchiveFileObjectRecord)
                    .where(
                        CollectionArchiveFileObjectRecord.collection_id == collection_id,
                        CollectionArchiveFileObjectRecord.store == source_store,
                        CollectionArchiveFileObjectRecord.path == path,
                    )
                    .order_by(CollectionArchiveFileObjectRecord.sequence)
                )
            )
            if not placements:
                raise InvalidState("retrieval file has no archive placement")
            records: list[
                tuple[
                    CollectionArchiveFileObjectRecord,
                    CollectionArchiveObjectRecord,
                    RetrievalCacheObjectRecord | None,
                ]
            ] = []
            for placement in placements:
                if (
                    session.get(
                        RetrievalJobObjectRecord,
                        (job_id, collection_id, source_store, placement.object_id),
                    )
                    is None
                ):
                    raise InvalidState("retrieval file object is outside the job plan")
                object_record = session.get(
                    CollectionArchiveObjectRecord,
                    (collection_id, source_store, placement.object_id),
                )
                if object_record is None or object_record.kind not in _DATA_KINDS:
                    raise InvalidState("retrieval archive volume is missing")
                cached = session.get(
                    RetrievalCacheObjectRecord,
                    (source_store, collection_id, placement.object_id),
                )
                if cached is not None and cached.state != "ready":
                    cached = None
                records.append((placement, object_record, cached))
            attribution = _download_attribution(job)
            expected_bytes = file_record.bytes
            expected_sha256 = file_record.sha256
            collection = session.get(CollectionRecord, collection_id)
            if collection is None:
                raise NotFound(f"collection not found: {collection_id}")
            passphrase_id = collection.passphrase_id
            passphrase = self._config.archive_passphrase_for(passphrase_id)

        kinds = {record.kind for _placement, record, _cached in records}
        if kinds == {"pack"} and len(records) == 1:
            placement, record, cached = records[0]
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
            ).iter_member(source, member)
            return chunks, expected_bytes, expected_sha256

        if kinds == {"segment"}:
            sources: list[RawVolumeRetrievalSource] = []
            range_stores: dict[str, ArchiveObjectRangeStore] = {}
            for placement, record, cached in records:
                if not record.age_state_json or not record.archive_parts_json:
                    raise InvalidState("raw volume is missing its retrieval state")
                sources.append(
                    RawVolumeRetrievalSource(
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
                )
                range_stores[record.object_path] = self._range_store(
                    source_store=source_store,
                    object_record=record,
                    cached=cached,
                    attribution=attribution,
                )
            chunks = RawFileRangeReader(
                RawVolumeRangeReader(
                    _DispatchArchiveRangeStore(range_stores),
                    passphrase=passphrase,
                    request_concurrency=self._throughput.retrieval_request_concurrency,
                    read_working_bytes=self._throughput.retrieval_read_chunk_bytes,
                    resources=self._resources,
                    session_cache=self._age_sessions[passphrase_id],
                    timing_observer=log_transfer_timing,
                )
            ).iter_file(sources)
            return chunks, expected_bytes, expected_sha256

        raise InvalidState("retrieval file has inconsistent archive volume kinds")

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
            if session.get(RetrievalJobFileRecord, (job_id, collection_id, path)) is None:
                raise NotFound("file is not part of this retrieval job")
            file_record = session.get(CollectionFileRecord, (collection_id, path))
            if file_record is None:
                raise NotFound("file is no longer present")
            return file_record.bytes, file_record.sha256

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
        now_text = format_utc_timestamp(utc_now())
        with session_scope(self._session_factory) as session:
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
                session.execute(
                    delete(RetrievalCacheLeaseRecord).where(
                        RetrievalCacheLeaseRecord.owner == _job_owner(job.id)
                    )
                )
                _release_job_reservation(session, job.id)
            session.execute(
                delete(RetrievalCacheLeaseRecord).where(
                    RetrievalCacheLeaseRecord.expires_at <= now_text
                )
            )
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
                    cached.object_path,
                    cached.revision,
                )
                for cached in candidates
            ]
            for cached in candidates:
                cached.state = "deleting"

        removed = 0
        for source_store, collection_id, object_id, object_path, revision in cleanup:
            try:
                self._cache.delete(
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
                    session.delete(cache_record)
                    removed += 1
        return removed

    def _build_plan(
        self,
        session: Session,
        files: tuple[tuple[int, str], ...],
        lease: timedelta,
        *,
        restore_policy: str,
    ) -> dict[str, object]:
        files_payload: list[dict[str, object]] = []
        objects_payload: list[dict[str, object]] = []
        object_payloads: dict[tuple[int, str, str], dict[str, object]] = {}
        copy_by_collection: dict[int, CollectionArchiveCopyRecord] = {}
        for collection_id, path in files:
            file_record = session.get(CollectionFileRecord, (collection_id, path))
            if file_record is None:
                raise NotFound(f"file not found: {collection_id}/{path}")
            copy = copy_by_collection.get(collection_id)
            if copy is None:
                copy = self._select_copy(session, collection_id)
                copy_by_collection[collection_id] = copy
            files_payload.append(
                {
                    "collection_id": collection_id,
                    "path": path,
                    "bytes": file_record.bytes,
                    "sha256": file_record.sha256,
                }
            )
            placements = list(
                session.scalars(
                    select(CollectionArchiveFileObjectRecord)
                    .where(
                        CollectionArchiveFileObjectRecord.collection_id == collection_id,
                        CollectionArchiveFileObjectRecord.store == copy.store,
                        CollectionArchiveFileObjectRecord.path == path,
                    )
                    .order_by(CollectionArchiveFileObjectRecord.sequence)
                )
            )
            if not placements:
                raise InvalidState(f"archive placement is missing: {collection_id}/{path}")
            for placement in placements:
                object_id = placement.object_id
                identity = (collection_id, copy.store, str(object_id))
                payload = object_payloads.get(identity)
                if payload is None:
                    payload = self._plan_object_payload(
                        session,
                        collection_id=collection_id,
                        store=copy.store,
                        object_id=str(object_id),
                    )
                    object_payloads[identity] = payload
                    objects_payload.append(payload)
                cast(list[dict[str, object]], payload["placements"]).append(
                    {
                        "path": placement.path,
                        "sequence": placement.sequence,
                        "file_offset": placement.file_offset,
                        "object_offset": placement.object_offset,
                        "bytes": placement.bytes,
                        "member": placement.member,
                    }
                )
        for payload in objects_payload:
            payload["retrieval_bytes"] = self._planned_object_retrieval_bytes(
                session,
                payload,
            )
        return {
            "format": "riverhog-retrieval-plan/v1",
            "lease_seconds": int(lease.total_seconds()),
            "restore_policy": restore_policy,
            "requires_restore": any(
                current["read_mode"] == "restore_required" for current in objects_payload
            ),
            "files": files_payload,
            "objects": objects_payload,
        }

    def _plan_object_payload(
        self,
        session: Session,
        *,
        collection_id: int,
        store: str,
        object_id: str,
    ) -> dict[str, object]:
        identity = (collection_id, store, object_id)
        object_record = session.get(CollectionArchiveObjectRecord, identity)
        if object_record is None:
            raise InvalidState("archive object record is missing")
        cached = session.get(RetrievalCacheObjectRecord, (store, collection_id, object_id))
        if cached is not None and cached.state != "ready":
            cached = None
        if object_record.kind not in _DATA_KINDS:
            raise InvalidState("retrieval plan contains a non-data archive object")
        read_mode = (
            "cache" if cached is not None else self._archive_stores.require(store).store.read_mode()
        )
        return {
            "collection_id": collection_id,
            "source_store": store,
            "object_id": object_record.object_id,
            "kind": object_record.kind,
            "plaintext_bytes": object_record.plaintext_bytes,
            "stored_bytes": object_record.stored_bytes,
            "sha256": object_record.sha256,
            "retrieval_bytes": 0,
            "read_mode": read_mode,
            "placements": [],
        }

    def _planned_object_retrieval_bytes(
        self,
        session: Session,
        payload: dict[str, object],
    ) -> int:
        collection_id = int(str(payload["collection_id"]))
        store = str(payload["source_store"])
        object_id = str(payload["object_id"])
        object_record = session.get(
            CollectionArchiveObjectRecord,
            (collection_id, store, object_id),
        )
        if object_record is None:
            raise InvalidState("archive object record is missing")
        if object_record.kind == "segment":
            return object_record.stored_bytes
        if object_record.kind != "pack" or not object_record.age_state_json:
            raise InvalidState("pack retrieval state is missing")
        source = PackVolumeRetrievalSource(
            volume_id=object_record.object_id,
            object_path=object_record.object_path,
            revision=object_record.revision,
            plaintext_bytes=object_record.plaintext_bytes,
            stored_bytes=object_record.stored_bytes,
            age_state_json=object_record.age_state_json,
        )
        policy = PackRangeRetrievalPolicy.from_env(os.environ, store_name=store)
        total = 0
        for placement in cast(list[dict[str, object]], payload["placements"]):
            path = str(placement["path"])
            file_record = session.get(CollectionFileRecord, (collection_id, path))
            if file_record is None:
                raise InvalidState("pack retrieval file record is missing")
            total += plan_pack_range_retrieval(
                source,
                (
                    PackMemberRetrievalSource(
                        path=path,
                        bytes=file_record.bytes,
                        sha256=file_record.sha256,
                        data_offset=int(str(placement["object_offset"])),
                    ),
                ),
                policy=policy,
            ).accounted_remote_bytes
        return total

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
        pending_failed = False
        with session_scope(self._session_factory) as session:
            job = session.get(RetrievalJobRecord, job_id)
            if job is None or job.state != "requested":
                return
            pending_deadline = (
                parse_utc_timestamp(job.created_at) + self._config.retrieval_pending_timeout
            )
            if pending_deadline <= utc_now():
                self._fail_pending_job(
                    session,
                    job,
                    "retrieval exceeded the configured pending timeout",
                )
                pending_failed = True
            else:
                groups = self._missing_groups(session, job)
                restore_requested_at = job.restore_requested_at
                plan = json.loads(job.constraints_json)
                attribution = _download_attribution(job)
                lease_seconds = int(plan["lease_seconds"])
                pending_expires_at = format_utc_timestamp(pending_deadline)
                for current in job.objects:
                    if current.read_mode != "restore_required":
                        continue
                    if (
                        session.get(
                            RetrievalCacheObjectRecord,
                            (current.source_store, current.collection_id, current.object_id),
                        )
                        is not None
                    ):
                        self._lease_cached_object(
                            session,
                            owner=_job_owner(job_id),
                            source_store=current.source_store,
                            collection_id=current.collection_id,
                            object_id=current.object_id,
                            expires_at=pending_expires_at,
                        )
        if pending_failed:
            if self._download_allowance is not None:
                self._download_allowance.release_retrieval(job_id=job_id)
            return
        try:
            if groups and restore_requested_at is None:
                restore_requested_at = format_utc_timestamp(utc_now())
                for (store_name, collection_id), objects in groups.items():
                    self._archive_stores.require(store_name).store.prepare_archive_objects_read(
                        collection_id=collection_id,
                        objects=objects,
                    )
                with session_scope(self._session_factory) as session:
                    job = session.scalar(
                        select(RetrievalJobRecord)
                        .where(RetrievalJobRecord.id == job_id)
                        .with_for_update()
                    )
                    if job is None or job.state != "requested":
                        return
                    if job.restore_requested_at is None:
                        job.restore_requested_at = restore_requested_at
                        job.failure = None
                    else:
                        restore_requested_at = job.restore_requested_at

            all_ready = True
            restore_expired = False
            for (store_name, collection_id), objects in groups.items():
                store = self._archive_stores.require(store_name).store
                status = store.get_archive_objects_read_status(
                    collection_id=collection_id,
                    objects=objects,
                )
                if status.state == "expired":
                    all_ready = False
                    restore_expired = True
                    continue
                if status.state != "ready":
                    all_ready = False
                    continue
                if self._cache is None:
                    raise RuntimeError("retrieval cache is unavailable")
                for object_identity in objects:
                    receipt = self._cache.put(
                        source_store=store_name,
                        collection_id=collection_id,
                        object_id=object_identity.object_id,
                        content=store.iter_stored_archive_object(
                            collection_id=collection_id,
                            object=object_identity,
                            attribution=attribution,
                        ),
                        content_length=object_identity.stored_bytes,
                    )
                    try:
                        _validate_cache_receipt(receipt, object_identity)
                    except Exception as receipt_error:
                        try:
                            self._cache.delete(
                                object_path=receipt.object_path,
                                revision=receipt.revision,
                            )
                        except Exception as cleanup_error:
                            raise RuntimeError(
                                f"{receipt_error}; retrieval cache cleanup also failed: "
                                f"{cleanup_error}"
                            ) from cleanup_error
                        raise
                    with session_scope(self._session_factory) as session:
                        session.merge(
                            RetrievalCacheObjectRecord(
                                source_store=store_name,
                                collection_id=collection_id,
                                object_id=object_identity.object_id,
                                object_path=receipt.object_path,
                                revision=receipt.revision,
                                stored_bytes=receipt.stored_bytes,
                                stored_sha256=receipt.stored_sha256,
                                cached_at=receipt.cached_at,
                                verified_at=receipt.verified_at,
                                state="ready",
                            )
                        )
                        session.flush()
                        self._lease_cached_object(
                            session,
                            owner=_job_owner(job_id),
                            source_store=store_name,
                            collection_id=collection_id,
                            object_id=object_identity.object_id,
                            expires_at=pending_expires_at,
                        )
            if all_ready:
                now = utc_now()
                expires_at = format_utc_timestamp(now + timedelta(seconds=lease_seconds))
                with session_scope(self._session_factory) as session:
                    job = session.get(RetrievalJobRecord, job_id)
                    if job is None or job.state != "requested":
                        return
                    for current in job.objects:
                        if current.read_mode == "restore_required":
                            self._lease_cached_object(
                                session,
                                owner=_job_owner(job_id),
                                source_store=current.source_store,
                                collection_id=current.collection_id,
                                object_id=current.object_id,
                                expires_at=expires_at,
                            )
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
            else:
                with session_scope(self._session_factory) as session:
                    job = session.get(RetrievalJobRecord, job_id)
                    if job is not None and job.state == "requested":
                        if restore_expired:
                            job.restore_requested_at = None
                            job.next_poll_at = format_utc_timestamp(utc_now())
                            job.failure = None
                        else:
                            job.next_poll_at = format_utc_timestamp(
                                utc_now() + self._config.retrieval_restore_poll_interval
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

    def _missing_groups(
        self,
        session: Session,
        job: RetrievalJobRecord,
    ) -> dict[tuple[str, int], list[ArchiveObjectIdentity]]:
        groups: dict[tuple[str, int], list[ArchiveObjectIdentity]] = {}
        for current in job.objects:
            if current.read_mode != "restore_required":
                continue
            cache_key = (current.source_store, current.collection_id, current.object_id)
            cached = session.get(RetrievalCacheObjectRecord, cache_key)
            if cached is not None and cached.state == "ready":
                continue
            object_record = session.get(
                CollectionArchiveObjectRecord,
                (current.collection_id, current.source_store, current.object_id),
            )
            if object_record is None:
                raise InvalidState("retrieval archive object is missing")
            groups.setdefault((current.source_store, current.collection_id), []).append(
                _object_identity(object_record)
            )
        return groups

    def _fail_pending_job(
        self,
        session: Session,
        job: RetrievalJobRecord,
        failure: str,
    ) -> None:
        job.state = "failed"
        job.failure = failure
        job.next_poll_at = None
        session.execute(
            delete(RetrievalCacheLeaseRecord).where(
                RetrievalCacheLeaseRecord.owner == _job_owner(job.id)
            )
        )
        _release_job_reservation(session, job.id)
        self._lifecycle_events.emit_retrieval(
            type="retrieval.failed",
            job=job,
            details={"error": failure},
            terminal=True,
            session=session,
        )

    @staticmethod
    def _lease_cached_object(
        session: Session,
        *,
        owner: str,
        source_store: str,
        collection_id: int,
        object_id: str,
        expires_at: str,
    ) -> None:
        cached = session.scalar(
            select(RetrievalCacheObjectRecord)
            .where(
                RetrievalCacheObjectRecord.source_store == source_store,
                RetrievalCacheObjectRecord.collection_id == collection_id,
                RetrievalCacheObjectRecord.object_id == object_id,
            )
            .with_for_update()
        )
        if cached is None or cached.state != "ready":
            raise InvalidState("planned retrieval cache object is missing")
        session.merge(
            RetrievalCacheLeaseRecord(
                owner=owner,
                source_store=source_store,
                collection_id=collection_id,
                object_id=object_id,
                expires_at=expires_at,
            )
        )

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
            session.execute(
                delete(RetrievalCacheLeaseRecord).where(
                    RetrievalCacheLeaseRecord.owner == _job_owner(job.id)
                )
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
        cache_object_path: str,
        cache_revision: str | None,
    ) -> None:
        self._cache = cache
        self._archive_object_path = archive_object_path
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


class _DispatchArchiveRangeStore:
    def __init__(self, stores: Mapping[str, ArchiveObjectRangeStore]) -> None:
        self._stores = dict(stores)

    def iter_object_range(
        self,
        *,
        object_path: str,
        revision: str | None,
        expected_bytes: int,
        offset: int,
        size: int,
    ) -> Iterator[bytes]:
        try:
            store = self._stores[object_path]
        except KeyError as exc:
            raise ValueError("raw retrieval object range store is missing") from exc
        return store.iter_object_range(
            object_path=object_path,
            revision=revision,
            expected_bytes=expected_bytes,
            offset=offset,
            size=size,
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
    state: str | None,
    protection: str | None,
    expires_before: str | None,
    expires_after: str | None,
    sort: str,
    order: str,
    principal: ApplicationPrincipal | None,
    now: str,
) -> tuple[Any, dict[str, object], str | None]:
    if sort not in _CACHE_SORT_FIELDS:
        raise BadRequest(f"sort must be one of {', '.join(sorted(_CACHE_SORT_FIELDS))}")
    if order not in {"asc", "desc"}:
        raise BadRequest("order must be asc or desc")
    normalized_collection_id = (
        _normalize_collection_id_or_raise(collection_id) if collection_id is not None else None
    )
    normalized_store = (
        source_store.strip().casefold() if source_store and source_store.strip() else None
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
    lease_summary = _active_cache_lease_summary(now).subquery()
    statement = (
        select(
            RetrievalCacheObjectRecord,
            lease_summary.c.protected_until,
            lease_summary.c.new_archive_expires_at,
            lease_summary.c.retrieval_job_leases,
        )
        .outerjoin(
            lease_summary,
            (lease_summary.c.source_store == RetrievalCacheObjectRecord.source_store)
            & (lease_summary.c.collection_id == RetrievalCacheObjectRecord.collection_id)
            & (lease_summary.c.object_id == RetrievalCacheObjectRecord.object_id),
        )
        .where(
            collection_access_filter(
                RetrievalCacheObjectRecord.collection_id, principal, CATALOG_READ
            )
        )
    )
    if normalized_collection_id is not None:
        statement = statement.where(
            RetrievalCacheObjectRecord.collection_id == normalized_collection_id
        )
    if normalized_store is not None:
        statement = statement.where(RetrievalCacheObjectRecord.source_store == normalized_store)
    if normalized_state is not None:
        statement = statement.where(RetrievalCacheObjectRecord.state == normalized_state)
    if normalized_protection == "protected":
        statement = statement.where(lease_summary.c.protected_until.is_not(None))
    elif normalized_protection == "unleased":
        statement = statement.where(lease_summary.c.protected_until.is_(None))
    if normalized_expires_before is not None:
        statement = statement.where(lease_summary.c.protected_until <= normalized_expires_before)
    if normalized_expires_after is not None:
        statement = statement.where(lease_summary.c.protected_until >= normalized_expires_after)
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
        filters = [
            func.lower(RetrievalCacheObjectRecord.source_store).contains(needle),
            func.lower(RetrievalCacheObjectRecord.object_id).contains(needle),
        ]
        if needle.isdigit():
            filters.append(RetrievalCacheObjectRecord.collection_id == int(needle))
        statement = statement.where(or_(*filters))
    sort_expressions = {
        "collection_id": RetrievalCacheObjectRecord.collection_id,
        "source_store": RetrievalCacheObjectRecord.source_store,
        "object_id": RetrievalCacheObjectRecord.object_id,
        "stored_bytes": RetrievalCacheObjectRecord.stored_bytes,
        "cached_at": RetrievalCacheObjectRecord.cached_at,
        "verified_at": RetrievalCacheObjectRecord.verified_at,
        "protected_until": lease_summary.c.protected_until,
    }
    expression = sort_expressions[sort]
    ordered = desc(expression) if order == "desc" else expression.asc()
    statement = statement.order_by(
        ordered,
        RetrievalCacheObjectRecord.collection_id,
        RetrievalCacheObjectRecord.source_store,
        RetrievalCacheObjectRecord.object_id,
    )
    normalized_filters: dict[str, object] = {
        "tag": normalized_tag,
        "collection_id": normalized_collection_id,
        "source_store": normalized_store,
        "state": normalized_state,
        "protection": normalized_protection,
        "expires_before": normalized_expires_before,
        "expires_after": normalized_expires_after,
    }
    return statement, normalized_filters, needle


def _active_cache_lease_summary(now: str) -> Any:
    return (
        select(
            RetrievalCacheLeaseRecord.source_store.label("source_store"),
            RetrievalCacheLeaseRecord.collection_id.label("collection_id"),
            RetrievalCacheLeaseRecord.object_id.label("object_id"),
            func.max(RetrievalCacheLeaseRecord.expires_at).label("protected_until"),
            func.max(
                case(
                    (
                        RetrievalCacheLeaseRecord.owner == "new-archive",
                        RetrievalCacheLeaseRecord.expires_at,
                    ),
                    else_=None,
                )
            ).label("new_archive_expires_at"),
            func.sum(
                case(
                    (RetrievalCacheLeaseRecord.owner.startswith("job:"), 1),
                    else_=0,
                )
            ).label("retrieval_job_leases"),
        )
        .where(RetrievalCacheLeaseRecord.expires_at > now)
        .group_by(
            RetrievalCacheLeaseRecord.source_store,
            RetrievalCacheLeaseRecord.collection_id,
            RetrievalCacheLeaseRecord.object_id,
        )
    )


def _collection_tags(session: Session, collection_ids: set[int]) -> dict[int, tuple[str, ...]]:
    if not collection_ids:
        return {}
    rows = session.execute(
        select(CollectionTagRecord.collection_id, CollectionTagRecord.tag_id)
        .where(CollectionTagRecord.collection_id.in_(collection_ids))
        .order_by(CollectionTagRecord.collection_id, CollectionTagRecord.tag_id)
    )
    result: dict[int, list[str]] = {}
    for collection_id, tag in rows:
        result.setdefault(int(collection_id), []).append(str(tag))
    return {collection_id: tuple(tags) for collection_id, tags in result.items()}


def _cache_object_payload(
    current: RetrievalCacheObjectRecord,
    *,
    protected_until: str | None,
    new_archive_expires_at: str | None,
    retrieval_job_leases: int,
    tags: Sequence[str],
) -> dict[str, object]:
    categories: list[str] = []
    if new_archive_expires_at is not None:
        categories.append("new_archive")
    if retrieval_job_leases:
        categories.append("retrieval_job")
    return {
        "collection_id": current.collection_id,
        "source_store": current.source_store,
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
        "tags": list(tags),
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
        not receipt.object_path
        or receipt.stored_bytes != identity.stored_bytes
        or (identity.stored_sha256 is not None and receipt.stored_sha256 != identity.stored_sha256)
        or len(receipt.stored_sha256) != 64
        or not receipt.cached_at
        or not receipt.verified_at
    ):
        raise RuntimeError("retrieval cache receipt does not match verified archive metadata")
    parse_utc_timestamp(receipt.cached_at)
    parse_utc_timestamp(receipt.verified_at)


def _job_owner(job_id: str) -> str:
    return f"job:{job_id}"


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
    plan = json.loads(record.constraints_json)
    return {
        "id": record.id,
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
        "lease_seconds": plan["lease_seconds"],
        "restore_policy": plan["restore_policy"],
        "requires_restore": plan["requires_restore"],
        "files": plan["files"],
        "objects": plan["objects"],
    }
