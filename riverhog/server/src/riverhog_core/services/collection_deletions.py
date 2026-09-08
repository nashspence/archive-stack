from __future__ import annotations

import json
import secrets
from collections.abc import Callable, Sequence
from datetime import datetime
from functools import cache
from typing import cast

from riverhog_protocol.errors import BadRequest, Conflict, InvalidState, NotFound
from riverhog_protocol.transport import COLLECTION_DELETION_BLOCKER_CATEGORY_SAMPLE_MAX
from sqlalchemy import Table, and_, case, delete, func, or_, select, update
from sqlalchemy.orm import Session
from time_formats import format_utc_timestamp, utc_now

from riverhog_core.app_permissions import ApplicationPrincipal
from riverhog_core.archive_safety import ARCHIVE_DATA_LOSS_WARNING
from riverhog_core.archive_store_registry import ArchiveStoreRegistry
from riverhog_core.catalog_base import Base
from riverhog_core.catalog_db import SessionFactory, make_session_factory, session_scope
from riverhog_core.catalog_events import (
    begin_catalog_event,
    publish_catalog_event,
)
from riverhog_core.catalog_models import (
    ArchiveCopyJobRecord,
    ArchiveCopyRetirementRecord,
    CatalogEventRecord,
    CollectionArchiveCopyRecord,
    CollectionArchiveObjectRecord,
    CollectionDeletionRecord,
    CollectionDescriptionPublicationRecord,
    CollectionFileRecord,
    CollectionRecord,
    CollectionTagMembershipRecord,
    CollectionTagPublicationRecord,
    CollectionTagRecord,
    CollectionUploadFileRecord,
    CollectionUploadRecord,
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
from riverhog_core.catalog_workflow_models import CollectionProcessingClaimRecord
from riverhog_core.ports.archive_store import ArchiveObjectIdentity
from riverhog_core.ports.retrieval_cache import RetrievalCache
from riverhog_core.runtime_config import RuntimeConfig
from riverhog_core.services.archive_copy_states import ARCHIVE_COPY_BLOCKING_STATES
from riverhog_core.services.archive_records import (
    archive_copy_aggregates,
    archive_copy_is_complete,
)
from riverhog_core.services.collection_workflows import (
    processing_claim_blockers,
    require_retirement_exemption,
)
from riverhog_core.services.collections import _normalize_collection_id_or_raise
from riverhog_core.services.lifecycle_events import (
    SqlAlchemyLifecycleEventService,
    event_context_json,
)
from riverhog_core.services.operation_plans import (
    PLAN_TTL,
    challenge_expiry,
    challenge_has_shape,
    plan_challenge,
)
from riverhog_core.services.retrieval_cache_accounting import adjust_cache_committed_bytes

_CHALLENGE_PREFIX = "delete"
_ACTIVE_RETRIEVAL_STATES = {"requested", "ready"}
_EXECUTION_KEY = "_execution"
_CATALOG_EVENT_SEQUENCE_KEY = "_catalog_event_sequence"
_CATALOG_DELETE_BATCH = 100


class SqlAlchemyCollectionDeletionService:
    def __init__(
        self,
        config: RuntimeConfig,
        archive_stores: ArchiveStoreRegistry,
        retrieval_cache: RetrievalCache | None,
        *,
        session_factory: SessionFactory | None = None,
    ) -> None:
        self._archive_stores = archive_stores
        self._retrieval_cache = retrieval_cache
        self._session_factory = session_factory or make_session_factory(config.database_url)
        self._lifecycle_events = SqlAlchemyLifecycleEventService(
            config,
            session_factory=self._session_factory,
        )

    def plan(
        self,
        collection_id: int,
        *,
        principal: ApplicationPrincipal | None = None,
        retirement_claim_id: str | None = None,
    ) -> dict[str, object]:
        normalized_id = _normalize_collection_id_or_raise(collection_id)
        with session_scope(self._session_factory) as session:
            active = session.get(CollectionDeletionRecord, normalized_id)
            if active is not None:
                return _public_plan(cast(dict[str, object], json.loads(active.plan_json)))
            retirement = None
            if retirement_claim_id is not None:
                if principal is None:
                    raise Conflict("retirement deletion requires an authenticated claim owner")
                retirement = require_retirement_exemption(
                    session,
                    claim_id=retirement_claim_id,
                    collection_id=normalized_id,
                    principal=principal,
                )
            expires = (utc_now() + PLAN_TTL).replace(microsecond=0)
            plan = _build_plan(
                session,
                collection_id=normalized_id,
                expires_at=expires,
                exempt_claim_id=retirement_claim_id,
            )
            plan["retirement_claim"] = retirement
            plan["challenge"] = (
                None if plan["blockers"] else plan_challenge(_CHALLENGE_PREFIX, plan, expires)
            )
            return plan

    def delete(
        self,
        collection_id: int,
        *,
        challenge: str,
        initiator: ApplicationPrincipal,
        event_context: dict[str, object] | None = None,
        retirement_claim_id: str | None = None,
    ) -> dict[str, object]:
        normalized_id = _normalize_collection_id_or_raise(collection_id)
        supplied_challenge = challenge.strip()
        if not supplied_challenge:
            raise BadRequest("collection deletion challenge is required")
        normalized_context_json = event_context_json(event_context)

        with session_scope(self._session_factory) as session:
            collection = session.scalar(
                select(CollectionRecord)
                .where(
                    CollectionRecord.id == normalized_id,
                    CollectionRecord.is_published.is_(True),
                )
                .with_for_update()
            )
            active = session.get(CollectionDeletionRecord, normalized_id)
            if active is not None:
                if not secrets.compare_digest(active.challenge, supplied_challenge):
                    raise Conflict("collection deletion challenge does not match active deletion")
                plan = cast(dict[str, object], json.loads(active.plan_json))
                expected_retirement = _retirement_claim_id(plan)
                if expected_retirement != retirement_claim_id:
                    raise Conflict("collection deletion retirement claim changed")
            elif collection is None:
                if not challenge_has_shape(supplied_challenge, prefix=_CHALLENGE_PREFIX):
                    raise NotFound(f"collection not found: {normalized_id}")
                return _deletion_result(
                    {
                        "collection_id": normalized_id,
                        "file_count": 0,
                        "bytes": 0,
                        "remote_storage_bytes": 0,
                    },
                    status="already_absent",
                )
            else:
                expires = challenge_expiry(
                    supplied_challenge,
                    prefix=_CHALLENGE_PREFIX,
                    operation="collection deletion",
                )
                if utc_now() > expires:
                    raise Conflict("collection deletion plan has expired; request a new plan")
                retirement = None
                if retirement_claim_id is not None:
                    retirement = require_retirement_exemption(
                        session,
                        claim_id=retirement_claim_id,
                        collection_id=normalized_id,
                        principal=initiator,
                    )
                plan = _build_plan(
                    session,
                    collection_id=normalized_id,
                    expires_at=expires,
                    exempt_claim_id=retirement_claim_id,
                )
                plan["retirement_claim"] = retirement
                if not secrets.compare_digest(
                    plan_challenge(_CHALLENGE_PREFIX, plan, expires),
                    supplied_challenge,
                ):
                    raise Conflict("collection deletion plan changed; request a new plan")
                blockers = cast(list[str], plan["blockers"])
                if blockers:
                    raise Conflict("collection deletion is blocked: " + "; ".join(blockers))
                plan[_EXECUTION_KEY] = {
                    "app": initiator.app,
                    "key_id": initiator.key_id,
                    "event_context_json": normalized_context_json,
                }
                plan["challenge"] = supplied_challenge
                plan["status"] = "deleting"
                session.add(
                    CollectionDeletionRecord(
                        collection_id=normalized_id,
                        challenge=supplied_challenge,
                        plan_json=json.dumps(plan, sort_keys=True, separators=(",", ":")),
                        started_at=format_utc_timestamp(utc_now()),
                    )
                )
                collection.is_published = False

        return _deletion_result(plan, status="deleting")

    def process_due(self, *, limit: int = 10) -> int:
        """Advance at most ``limit`` physical-object or catalog deletion steps."""

        if limit < 1:
            return 0
        progressed = 0
        for _ in range(limit):
            collection_id = self._next_processable_collection()
            if collection_id is None:
                break
            if not self._process_one(collection_id):
                break
            progressed += 1
        return progressed

    def _next_processable_collection(self) -> int | None:
        active_lease = (
            select(RetrievalCacheLeaseRecord.owner)
            .where(
                RetrievalCacheLeaseRecord.source_store == RetrievalCacheObjectRecord.source_store,
                RetrievalCacheLeaseRecord.collection_id == RetrievalCacheObjectRecord.collection_id,
                RetrievalCacheLeaseRecord.object_id == RetrievalCacheObjectRecord.object_id,
            )
            .exists()
        )
        any_cache = (
            select(RetrievalCacheObjectRecord.collection_id)
            .where(
                RetrievalCacheObjectRecord.collection_id == CollectionDeletionRecord.collection_id
            )
            .exists()
        )
        eligible_cache = (
            select(RetrievalCacheObjectRecord.collection_id)
            .where(
                RetrievalCacheObjectRecord.collection_id == CollectionDeletionRecord.collection_id,
                ~active_lease,
            )
            .exists()
        )
        with session_scope(self._session_factory) as session:
            return session.scalar(
                select(CollectionDeletionRecord.collection_id)
                .where(eligible_cache | ~any_cache)
                .order_by(
                    CollectionDeletionRecord.started_at,
                    CollectionDeletionRecord.collection_id,
                )
                .limit(1)
            )

    def _process_one(self, collection_id: int) -> bool:
        if self._delete_retrieval_references(collection_id):
            return True
        cached = self._claim_cached_object(collection_id)
        if cached is not None:
            self._delete_cached_object(cached)
            return True
        with session_scope(self._session_factory) as session:
            if (
                session.scalar(
                    select(RetrievalCacheObjectRecord.object_id)
                    .where(RetrievalCacheObjectRecord.collection_id == collection_id)
                    .limit(1)
                )
                is not None
            ):
                return False
            description = session.scalar(
                select(CollectionDescriptionPublicationRecord)
                .where(
                    CollectionDescriptionPublicationRecord.collection_id == collection_id,
                    CollectionDescriptionPublicationRecord.object_path.is_not(None),
                )
                .order_by(CollectionDescriptionPublicationRecord.store)
                .limit(1)
            )
            if description is not None:
                copy = session.get(
                    CollectionArchiveCopyRecord,
                    (collection_id, description.store),
                )
                if copy is None or copy.archive_storage_prefix is None:
                    raise Conflict("collection description has no owned archive copy")
                description_store = description.store
                description_prefix = copy.archive_storage_prefix
            else:
                description_store = None
                description_prefix = None
        if description_store is not None and description_prefix is not None:
            self._archive_stores.require(description_store).store.delete_collection_description(
                collection_id=collection_id,
                archive_storage_prefix=description_prefix,
            )
            with session_scope(self._session_factory) as session:
                current = session.get(
                    CollectionDescriptionPublicationRecord,
                    (collection_id, description_store),
                )
                if current is not None and current.object_path is not None:
                    session.delete(current)
            return True
        with session_scope(self._session_factory) as session:
            tag_publication = session.scalar(
                select(CollectionTagPublicationRecord)
                .where(CollectionTagPublicationRecord.collection_id == collection_id)
                .order_by(CollectionTagPublicationRecord.store)
                .limit(1)
            )
            if tag_publication is not None:
                copy = session.get(
                    CollectionArchiveCopyRecord,
                    (collection_id, tag_publication.store),
                )
                if copy is None or copy.archive_storage_prefix is None:
                    raise Conflict("collection tags have no owned archive copy")
                tag_store = tag_publication.store
                tag_prefix = copy.archive_storage_prefix
            else:
                tag_store = None
                tag_prefix = None
        if tag_store is not None and tag_prefix is not None:
            self._archive_stores.require(tag_store).store.delete_collection_tags(
                collection_id=collection_id,
                archive_storage_prefix=tag_prefix,
            )
            with session_scope(self._session_factory) as session:
                current_tag_publication = session.get(
                    CollectionTagPublicationRecord,
                    (collection_id, tag_store),
                )
                if current_tag_publication is not None:
                    session.delete(current_tag_publication)
            return True
        with session_scope(self._session_factory) as session:
            archive = session.scalar(
                select(CollectionArchiveObjectRecord)
                .where(CollectionArchiveObjectRecord.collection_id == collection_id)
                .order_by(
                    CollectionArchiveObjectRecord.store,
                    CollectionArchiveObjectRecord.object_order,
                )
                .limit(1)
            )
            if archive is None:
                active = session.get(CollectionDeletionRecord, collection_id)
                if active is None:
                    return False
                plan = cast(dict[str, object], json.loads(active.plan_json))
                challenge = active.challenge
            else:
                identity = _archive_object_identity(archive)
                store_name = archive.store
        if archive is not None:
            self._delete_archive_identity(collection_id, store_name, identity)
            with session_scope(self._session_factory) as session:
                current_archive = session.get(
                    CollectionArchiveObjectRecord,
                    (collection_id, store_name, identity.object_id),
                )
                if (
                    current_archive is not None
                    and current_archive.object_path == identity.object_path
                ):
                    session.delete(current_archive)
            return True
        if self._delete_catalog_batch(collection_id, plan):
            return True
        self._finish(collection_id, challenge, plan)
        return True

    def _delete_catalog_batch(
        self,
        collection_id: int,
        plan: dict[str, object],
    ) -> bool:
        """Delete one bounded batch of collection-owned catalog state."""

        with session_scope(self._session_factory) as session:
            claim_ids = tuple(
                session.scalars(
                    select(CollectionProcessingClaimRecord.id)
                    .where(CollectionProcessingClaimRecord.output_collection_id == collection_id)
                    .order_by(CollectionProcessingClaimRecord.id)
                    .limit(_CATALOG_DELETE_BATCH)
                )
            )
            if claim_ids:
                session.execute(
                    update(CollectionProcessingClaimRecord)
                    .where(CollectionProcessingClaimRecord.id.in_(claim_ids))
                    .values(output_collection_id=None)
                )
                return True

            for table in _collection_cascade_tables():
                primary_key = tuple(table.primary_key.columns)
                rows = list(
                    session.execute(
                        select(*primary_key)
                        .where(table.c.collection_id == collection_id)
                        .order_by(*primary_key)
                        .limit(_CATALOG_DELETE_BATCH)
                    )
                )
                if not rows:
                    continue
                predicates = [
                    and_(*(column == value for column, value in zip(primary_key, row, strict=True)))
                    for row in rows
                ]
                session.execute(delete(table).where(or_(*predicates)))
                return True

            event_sequence = plan.get(_CATALOG_EVENT_SEQUENCE_KEY)
            event_created = False
            if event_sequence is None:
                active = session.get(CollectionDeletionRecord, collection_id)
                collection = session.get(CollectionRecord, collection_id)
                if active is None:
                    return False
                if collection is None:
                    raise RuntimeError("collection deletion catalog authority is unavailable")
                created_event = begin_catalog_event(
                    session,
                    change="deleted",
                    collection_id=collection_id,
                    occurred_at=active.started_at,
                    inventory_identity=str(plan["inventory_identity"]),
                    before_tag_revision=collection.tag_revision,
                    after_tag_revision=None,
                )
                event_sequence = created_event.sequence
                plan[_CATALOG_EVENT_SEQUENCE_KEY] = event_sequence
                active.plan_json = json.dumps(plan, sort_keys=True, separators=(",", ":"))
                event_created = True
            if not isinstance(event_sequence, int):
                raise RuntimeError("collection deletion catalog event identity is invalid")
            current_event = session.get(CatalogEventRecord, event_sequence)
            if current_event is None or current_event.published:
                raise RuntimeError("collection deletion catalog event is unavailable")
            memberships = list(
                session.scalars(
                    select(CollectionTagMembershipRecord)
                    .where(CollectionTagMembershipRecord.collection_id == collection_id)
                    .order_by(CollectionTagMembershipRecord.tag_sha256)
                    .limit(_CATALOG_DELETE_BATCH)
                )
            )
            if memberships:
                tag_ids = tuple(current.tag_sha256 for current in memberships)
                session.execute(
                    update(CollectionTagRecord)
                    .where(CollectionTagRecord.tag_sha256.in_(tag_ids))
                    .values(
                        collection_count=CollectionTagRecord.collection_count - 1,
                    )
                )
                for current in memberships:
                    session.delete(current)
                return True
            if event_created:
                return True
        return False

    def _delete_retrieval_references(self, collection_id: int) -> bool:
        with session_scope(self._session_factory) as session:
            plan_id = session.scalar(
                select(RetrievalPlanFileRecord.plan_id)
                .where(RetrievalPlanFileRecord.collection_id == collection_id)
                .order_by(RetrievalPlanFileRecord.plan_id)
                .limit(1)
            )
            if plan_id is None:
                return False
            progress_rows = list(
                session.scalars(
                    select(RetrievalJobObjectProgressRecord)
                    .where(RetrievalJobObjectProgressRecord.plan_id == plan_id)
                    .order_by(
                        RetrievalJobObjectProgressRecord.job_id,
                        RetrievalJobObjectProgressRecord.object_order,
                    )
                    .limit(_CATALOG_DELETE_BATCH)
                )
            )
            if progress_rows:
                for progress in progress_rows:
                    session.delete(progress)
                return True
            placement_rows = list(
                session.scalars(
                    select(RetrievalPlanPlacementRecord)
                    .where(RetrievalPlanPlacementRecord.plan_id == plan_id)
                    .order_by(
                        RetrievalPlanPlacementRecord.file_order,
                        RetrievalPlanPlacementRecord.sequence,
                    )
                    .limit(_CATALOG_DELETE_BATCH)
                )
            )
            if placement_rows:
                for placement in placement_rows:
                    session.delete(placement)
                return True
            object_rows = list(
                session.scalars(
                    select(RetrievalPlanObjectRecord)
                    .where(RetrievalPlanObjectRecord.plan_id == plan_id)
                    .order_by(RetrievalPlanObjectRecord.object_order)
                    .limit(_CATALOG_DELETE_BATCH)
                )
            )
            if object_rows:
                for object_row in object_rows:
                    session.delete(object_row)
                return True
            file_rows = list(
                session.scalars(
                    select(RetrievalPlanFileRecord)
                    .where(RetrievalPlanFileRecord.plan_id == plan_id)
                    .order_by(
                        case(
                            (RetrievalPlanFileRecord.collection_id == collection_id, 1),
                            else_=0,
                        ),
                        RetrievalPlanFileRecord.file_order,
                    )
                    .limit(_CATALOG_DELETE_BATCH)
                )
            )
            if file_rows:
                for file_row in file_rows:
                    session.delete(file_row)
                session.flush()
                has_files = session.scalar(
                    select(RetrievalPlanFileRecord.plan_id)
                    .where(RetrievalPlanFileRecord.plan_id == plan_id)
                    .limit(1)
                )
                if has_files is None:
                    job = session.scalar(
                        select(RetrievalJobRecord).where(RetrievalJobRecord.plan_id == plan_id)
                    )
                    if job is not None:
                        session.delete(job)
                        session.flush()
                    plan = session.get(RetrievalPlanRecord, plan_id)
                    if plan is not None:
                        session.delete(plan)
                return True
        return False

    def _claim_cached_object(
        self,
        collection_id: int,
    ) -> tuple[str, int, str, str, str, str | None, int] | None:
        active_lease = (
            select(RetrievalCacheLeaseRecord.owner)
            .where(
                RetrievalCacheLeaseRecord.source_store == RetrievalCacheObjectRecord.source_store,
                RetrievalCacheLeaseRecord.collection_id == RetrievalCacheObjectRecord.collection_id,
                RetrievalCacheLeaseRecord.object_id == RetrievalCacheObjectRecord.object_id,
            )
            .exists()
        )
        with session_scope(self._session_factory) as session:
            cached = session.scalar(
                select(RetrievalCacheObjectRecord)
                .where(
                    RetrievalCacheObjectRecord.collection_id == collection_id,
                    ~active_lease,
                )
                .order_by(
                    RetrievalCacheObjectRecord.source_store,
                    RetrievalCacheObjectRecord.object_id,
                )
                .limit(1)
                .with_for_update(skip_locked=True)
            )
            if cached is None:
                return None
            cached.state = "deleting"
            return (
                cached.source_store,
                cached.collection_id,
                cached.object_id,
                cached.cache_store,
                cached.object_path,
                cached.revision,
                cached.stored_bytes,
            )

    def _delete_cached_object(
        self,
        identity: tuple[str, int, str, str, str, str | None, int],
    ) -> None:
        if self._retrieval_cache is None:
            raise Conflict("collection retrieval-cache objects cannot be removed")
        try:
            self._retrieval_cache.delete(
                cache_store=identity[3],
                object_path=identity[4],
                revision=identity[5],
            )
        except Exception:
            with session_scope(self._session_factory) as session:
                current = session.get(RetrievalCacheObjectRecord, identity[:3])
                if current is not None and current.object_path == identity[4]:
                    current.state = "delete_pending"
            raise
        with session_scope(self._session_factory) as session:
            current = session.scalar(
                select(RetrievalCacheObjectRecord)
                .where(
                    RetrievalCacheObjectRecord.source_store == identity[0],
                    RetrievalCacheObjectRecord.collection_id == identity[1],
                    RetrievalCacheObjectRecord.object_id == identity[2],
                )
                .with_for_update()
            )
            if current is None:
                return
            if current.object_path != identity[4] or current.stored_bytes != identity[6]:
                raise RuntimeError("retrieval cache deletion ownership changed")
            accounting = session.scalar(
                select(RetrievalCacheStoreAccountingRecord)
                .where(RetrievalCacheStoreAccountingRecord.cache_store == identity[3])
                .with_for_update()
            )
            if accounting is None:
                raise RuntimeError("retrieval cache committed accounting is inconsistent")
            adjust_cache_committed_bytes(accounting, delta=-identity[6])
            session.delete(current)

    def _delete_archive_identity(
        self,
        collection_id: int,
        store_name: str,
        identity: ArchiveObjectIdentity,
    ) -> None:
        self._archive_stores.require(store_name).store.delete_collection_archive(
            collection_id=collection_id,
            objects=(identity,),
        )

    def _finish(
        self,
        collection_id: int,
        challenge: str,
        plan: dict[str, object],
    ) -> dict[str, object]:
        with session_scope(self._session_factory) as session:
            active = session.get(CollectionDeletionRecord, collection_id)
            if active is None:
                return _deletion_result(plan, status="already_absent")
            if not secrets.compare_digest(active.challenge, challenge):
                raise Conflict("collection deletion challenge does not match active deletion")
            blockers = _active_blockers(
                session,
                collection_id,
                exempt_claim_id=_retirement_claim_id(plan),
            )
            if blockers:
                raise Conflict("collection activity began during deletion: " + "; ".join(blockers))
            collection = session.get(CollectionRecord, collection_id)
            if collection is not None:
                execution = _execution(plan)
                self._lifecycle_events.emit_collection(
                    type="collection.deleted",
                    collection_id=collection_id,
                    details={
                        "files": cast(int, plan["file_count"]),
                        "bytes": cast(int, plan["bytes"]),
                        "remote_storage_bytes": cast(int, plan["remote_storage_bytes"]),
                    },
                    terminal=True,
                    initiator=ApplicationPrincipal(
                        app=str(execution["app"]),
                        key_id=(
                            str(execution["key_id"])
                            if execution.get("key_id") is not None
                            else None
                        ),
                        access=frozenset(),
                    ),
                    event_context_json=(
                        str(execution["event_context_json"])
                        if execution.get("event_context_json") is not None
                        else None
                    ),
                    session=session,
                )
                event = session.get(CatalogEventRecord, _catalog_event_sequence(plan))
                if event is None or event.published:
                    raise RuntimeError("collection deletion catalog event is unavailable")
                publish_catalog_event(session, event=event)
                session.delete(collection)
            session.delete(active)
        return _deletion_result(plan, status="deleted")


def _build_plan(
    session: Session,
    *,
    collection_id: int,
    expires_at: datetime,
    exempt_claim_id: str | None = None,
) -> dict[str, object]:
    collection = session.get(CollectionRecord, collection_id)
    if collection is None:
        raise NotFound(f"collection not found: {collection_id}")
    archives = list(
        session.scalars(
            select(CollectionArchiveCopyRecord)
            .where(CollectionArchiveCopyRecord.collection_id == collection_id)
            .order_by(CollectionArchiveCopyRecord.store)
        )
    )
    if not archives or any(not archive_copy_is_complete(copy) for copy in archives):
        raise InvalidState(
            f"collection archive copies are not completely uploaded and verified: {collection_id}"
        )
    file_count, file_bytes = session.execute(
        select(
            func.count(CollectionFileRecord.path),
            func.coalesce(func.sum(CollectionFileRecord.bytes), 0),
        ).where(CollectionFileRecord.collection_id == collection_id)
    ).one()
    aggregates = archive_copy_aggregates(session, collection_ids=[collection_id])
    archive_copies: list[dict[str, str | int]] = [
        {
            "store": archive.store,
            "objects": aggregates.get((collection_id, archive.store), (0, 0))[0],
            "stored_bytes": aggregates.get((collection_id, archive.store), (0, 0))[1],
        }
        for archive in archives
    ]
    archive_object_count = sum(int(copy["objects"]) for copy in archive_copies)
    remote_storage_bytes = sum(int(copy["stored_bytes"]) for copy in archive_copies)
    upload = session.get(CollectionUploadRecord, collection_id)
    upload_file_count = int(
        session.scalar(
            select(func.count(CollectionUploadFileRecord.path)).where(
                CollectionUploadFileRecord.collection_id == collection_id
            )
        )
        or 0
    )
    blockers = _active_blockers(session, collection_id, exempt_claim_id=exempt_claim_id)
    if upload is not None and upload.state not in {"canceled", "expired"}:
        blockers.append(f"collection upload is active: {upload.state or 'unknown'}")
    tag_count = int(
        session.scalar(
            select(func.count(CollectionTagMembershipRecord.tag_sha256)).where(
                CollectionTagMembershipRecord.collection_id == collection_id
            )
        )
        or 0
    )
    return {
        "status": "blocked" if blockers else "ready",
        "collection_id": collection_id,
        "warning": ARCHIVE_DATA_LOSS_WARNING,
        "expires_at": format_utc_timestamp(expires_at),
        "file_count": int(file_count),
        "bytes": int(file_bytes),
        "archive_copies": archive_copies,
        "archive_object_count": archive_object_count,
        "remote_storage_bytes": remote_storage_bytes,
        "upload_file_count": upload_file_count,
        "inventory_identity": collection.inventory_identity,
        "metadata_rows": {
            "collections": 1,
            "collection_files": int(file_count),
            "collection_archive_copies": len(archives),
            "collection_tag_memberships": tag_count,
            "collection_uploads": int(upload is not None),
            "collection_upload_files": upload_file_count,
        },
        "blockers": blockers,
        "billing_note": (
            "Measured remote bytes are catalog values. Provider retention, object versions, "
            "minimum-storage duration, and billing timing can affect realized savings."
        ),
    }


def _public_plan(plan: dict[str, object]) -> dict[str, object]:
    return {
        key: value
        for key, value in plan.items()
        if key not in {_EXECUTION_KEY, _CATALOG_EVENT_SEQUENCE_KEY}
    }


def _execution(plan: dict[str, object]) -> dict[str, object]:
    execution = plan.get(_EXECUTION_KEY)
    if not isinstance(execution, dict) or not str(execution.get("app") or ""):
        raise Conflict("collection deletion has no authenticated initiator")
    return cast(dict[str, object], execution)


def _active_blockers(
    session: Session,
    collection_id: int,
    *,
    exempt_claim_id: str | None = None,
) -> list[str]:
    blockers: list[str] = processing_claim_blockers(
        session,
        collection_id,
        exempt_claim_id=exempt_claim_id,
        limit=COLLECTION_DELETION_BLOCKER_CATEGORY_SAMPLE_MAX,
    )
    retrieval_jobs = list(
        session.scalars(
            select(RetrievalJobRecord.id)
            .join(
                RetrievalPlanFileRecord,
                RetrievalPlanFileRecord.plan_id == RetrievalJobRecord.plan_id,
            )
            .where(
                RetrievalPlanFileRecord.collection_id == collection_id,
                RetrievalJobRecord.state.in_(_ACTIVE_RETRIEVAL_STATES),
            )
            .order_by(RetrievalJobRecord.id)
            .limit(COLLECTION_DELETION_BLOCKER_CATEGORY_SAMPLE_MAX + 1)
        )
    )
    blockers.extend(
        _bounded_blocker_sample(
            retrieval_jobs,
            render=lambda job_id: f"retrieval job is active: {job_id}",
            overflow="additional active retrieval jobs exist; list retrievals for details",
        )
    )
    active_plans = list(
        session.scalars(
            select(RetrievalPlanRecord.id)
            .join(RetrievalPlanFileRecord)
            .where(
                RetrievalPlanFileRecord.collection_id == collection_id,
                RetrievalPlanRecord.state.in_({"planning", "ready"}),
            )
            .order_by(RetrievalPlanRecord.id)
            .limit(COLLECTION_DELETION_BLOCKER_CATEGORY_SAMPLE_MAX + 1)
        )
    )
    blockers.extend(
        _bounded_blocker_sample(
            active_plans,
            render=lambda plan_id: f"retrieval plan is active: {plan_id}",
            overflow="additional active retrieval plans exist; request fresh deletion plan",
        )
    )
    copy_jobs = list(
        session.execute(
            select(ArchiveCopyJobRecord.source_store, ArchiveCopyJobRecord.destination_store)
            .where(
                ArchiveCopyJobRecord.collection_id == collection_id,
                ArchiveCopyJobRecord.state.in_(ARCHIVE_COPY_BLOCKING_STATES),
            )
            .order_by(ArchiveCopyJobRecord.destination_store)
            .limit(COLLECTION_DELETION_BLOCKER_CATEGORY_SAMPLE_MAX + 1)
        )
    )
    blockers.extend(
        _bounded_blocker_sample(
            copy_jobs,
            render=lambda value: f"archive copy is active: {value[0]} -> {value[1]}",
            overflow="additional active archive copies exist; list archive-copy jobs for details",
        )
    )
    retirements = list(
        session.scalars(
            select(ArchiveCopyRetirementRecord.store)
            .where(ArchiveCopyRetirementRecord.collection_id == collection_id)
            .order_by(ArchiveCopyRetirementRecord.store)
            .limit(COLLECTION_DELETION_BLOCKER_CATEGORY_SAMPLE_MAX + 1)
        )
    )
    blockers.extend(
        _bounded_blocker_sample(
            retirements,
            render=lambda store: f"archive copy retirement is active: {store}",
            overflow=(
                "additional archive copy retirements exist; list collection archive copies "
                "for details"
            ),
        )
    )
    collection = session.get(CollectionRecord, collection_id)
    if collection is not None and collection.description_mutation_state != "idle":
        blockers.append("collection description replacement is active")
    if collection is not None and collection.tag_mutation_operation_id is not None:
        blockers.append("collection tag mutation is active")
    active_tag_publications = list(
        session.scalars(
            select(CollectionTagPublicationRecord.store)
            .where(
                CollectionTagPublicationRecord.collection_id == collection_id,
                CollectionTagPublicationRecord.state.in_({"publishing_nodes", "publishing_head"}),
            )
            .order_by(CollectionTagPublicationRecord.store)
            .limit(COLLECTION_DELETION_BLOCKER_CATEGORY_SAMPLE_MAX + 1)
        )
    )
    blockers.extend(
        _bounded_blocker_sample(
            active_tag_publications,
            render=lambda store: f"collection tag publication is active: {store}",
            overflow="additional collection tag publications are active",
        )
    )
    description_publications = list(
        session.scalars(
            select(CollectionDescriptionPublicationRecord.store)
            .where(
                CollectionDescriptionPublicationRecord.collection_id == collection_id,
                CollectionDescriptionPublicationRecord.state == "publishing",
            )
            .order_by(CollectionDescriptionPublicationRecord.store)
            .limit(COLLECTION_DELETION_BLOCKER_CATEGORY_SAMPLE_MAX + 1)
        )
    )
    blockers.extend(
        _bounded_blocker_sample(
            description_publications,
            render=lambda store: f"collection description publication is active: {store}",
            overflow="additional collection description publications are active",
        )
    )
    return blockers


def _bounded_blocker_sample[T](
    values: Sequence[T],
    *,
    render: Callable[[T], str],
    overflow: str,
) -> list[str]:
    maximum = COLLECTION_DELETION_BLOCKER_CATEGORY_SAMPLE_MAX
    result = [render(value) for value in values[:maximum]]
    if len(values) > maximum:
        result.append(overflow)
    return result


def _deletion_result(plan: dict[str, object], *, status: str) -> dict[str, object]:
    return {
        "status": status,
        "collection_id": plan["collection_id"],
        "files": plan["file_count"],
        "bytes": plan["bytes"],
        "remote_storage_bytes": plan["remote_storage_bytes"],
    }


def _archive_object_identity(record: CollectionArchiveObjectRecord) -> ArchiveObjectIdentity:
    return ArchiveObjectIdentity(
        object_id=record.object_id,
        kind=record.kind,
        object_path=record.object_path,
        plaintext_bytes=record.plaintext_bytes,
        stored_bytes=record.stored_bytes,
        sha256=record.sha256,
        stored_sha256=record.stored_sha256,
        revision=record.revision,
    )


@cache
def _collection_cascade_tables() -> tuple[Table, ...]:
    """Return collection-owned cascade tables in bounded deletion order."""

    reachable = {CollectionRecord.__tablename__}
    changed = True
    while changed:
        changed = False
        for table in Base.metadata.sorted_tables:
            if table.name in reachable:
                continue
            for constraint in table.foreign_key_constraints:
                if (
                    str(constraint.ondelete or "").upper() == "CASCADE"
                    and constraint.referred_table.name in reachable
                ):
                    reachable.add(table.name)
                    changed = True
                    break
    excluded = {
        CollectionRecord.__tablename__,
        CollectionTagMembershipRecord.__tablename__,
        CollectionArchiveObjectRecord.__tablename__,
        CollectionDescriptionPublicationRecord.__tablename__,
        RetrievalCacheObjectRecord.__tablename__,
        RetrievalCacheLeaseRecord.__tablename__,
    }
    result = tuple(
        table
        for table in reversed(Base.metadata.sorted_tables)
        if table.name in reachable and table.name not in excluded
    )
    missing_key = [table.name for table in result if "collection_id" not in table.c]
    if missing_key:
        raise RuntimeError(
            "collection cascade tables need an explicit bounded deletion key: "
            + ", ".join(sorted(missing_key))
        )
    return result


def _catalog_event_sequence(plan: dict[str, object]) -> int:
    value = plan.get(_CATALOG_EVENT_SEQUENCE_KEY)
    if not isinstance(value, int) or value < 1:
        raise Conflict("collection deletion has no catalog event authority")
    return value


def _retirement_claim_id(plan: dict[str, object]) -> str | None:
    value = plan.get("retirement_claim")
    if not isinstance(value, dict):
        return None
    claim_id = value.get("claim_id")
    return str(claim_id) if claim_id else None
