from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from collections.abc import Iterable, Iterator, Mapping, Sequence
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass, replace

from riverhog_age import UploadState
from riverhog_protocol.errors import BadRequest, Conflict, InvalidState, NotFound
from riverhog_protocol.paths import PathNormalizationError, normalize_collection_id
from sqlalchemy import String, cast, delete, func, or_, select
from sqlalchemy.orm import Session, selectinload
from time_formats import format_utc_timestamp, utc_now

from riverhog_core.app_permissions import ARCHIVES_MANAGE, ApplicationPrincipal
from riverhog_core.archive_formats import (
    PACK_VOLUME_STORAGE_FORMAT,
    RAW_VOLUME_STORAGE_FORMAT,
    archive_object_storage_format,
)
from riverhog_core.archive_store_registry import ArchiveStoreRegistry
from riverhog_core.catalog_db import SessionFactory, make_session_factory, session_scope
from riverhog_core.catalog_models import (
    ArchiveCopyJobRecord,
    ArchiveCopyObjectUploadRecord,
    CollectionArchiveCopyRecord,
    CollectionArchiveFileObjectRecord,
    CollectionArchiveObjectRecord,
    CollectionMetadataPublicationRecord,
    CollectionProofMaturationRecord,
    CollectionRecord,
    RetrievalCacheLeaseRecord,
    RetrievalCacheObjectRecord,
)
from riverhog_core.collection_access import collection_access_filter
from riverhog_core.pack_upload import PACK_VOLUME_CONTENT_TYPE
from riverhog_core.ports.archive_objects import (
    ArchiveMultipartObjectStore,
    ImmutableArchiveObjectStore,
    MultipartPartReceipt,
    MultipartUpload,
)
from riverhog_core.ports.archive_store import (
    ArchiveObjectIdentity,
    ArchiveStore,
    CollectionArchiveIdentity,
)
from riverhog_core.ports.retrieval_cache import RetrievalCache, RetrievalCacheReceipt
from riverhog_core.raw_upload import RAW_VOLUME_CONTENT_TYPE
from riverhog_core.runtime_config import RuntimeConfig
from riverhog_core.services.archive_copy_states import (
    ARCHIVE_COPY_STATES,
    ARCHIVE_COPY_TRANSFER_STATES,
)
from riverhog_core.services.archive_records import archive_copy_identity, archive_copy_is_complete
from riverhog_core.services.collection_mutations import require_collection_archive_idle
from riverhog_core.services.lifecycle_events import (
    SqlAlchemyLifecycleEventService,
    event_context_json,
)
from riverhog_core.stores.mirrored_archive_multipart_object_store import (
    MirroredArchiveMultipartObjectStore,
)
from riverhog_core.throughput import (
    ArchiveThroughputTuning,
    ArchiveTransferResources,
    TransferTiming,
    log_transfer_timing,
)

_LOG = logging.getLogger(__name__)
_SORT_FIELDS = {
    "collection_id",
    "source_store",
    "destination_store",
    "state",
    "requested_at",
}
_COPY_OBJECT_KINDS = frozenset(
    {"pack", "segment", "provenance-bundle", "provenance-index", "manifest", "proof"}
)


@dataclass(frozen=True, slots=True)
class _CopiedObject:
    object_id: str
    object_path: str
    version_id: str | None
    completed_at: str
    part_receipts_json: str | None = None
    retrieval_cache: RetrievalCacheReceipt | None = None


@dataclass(frozen=True, slots=True)
class _CopiedPart:
    receipt: MultipartPartReceipt
    timing: TransferTiming


class SqlAlchemyArchiveCopyService:
    def __init__(
        self,
        config: RuntimeConfig,
        archive_stores: ArchiveStoreRegistry,
        *,
        retrieval_cache: RetrievalCache | None = None,
        session_factory: SessionFactory | None = None,
        throughput_tuning: ArchiveThroughputTuning | None = None,
        transfer_resources: ArchiveTransferResources | None = None,
    ) -> None:
        self._config = config
        self._archive_stores = archive_stores
        self._retrieval_cache = retrieval_cache
        self._session_factory = session_factory or make_session_factory(config.database_url)
        self._throughput = throughput_tuning or ArchiveThroughputTuning.from_env(os.environ)
        self._resources = transfer_resources or ArchiveTransferResources.from_tuning(
            self._throughput
        )
        self._lifecycle_events = SqlAlchemyLifecycleEventService(
            config,
            session_factory=self._session_factory,
        )

    def requeue_interrupted_copies_for_startup(self, *, limit: int = 100) -> int:
        if limit < 1:
            return 0
        current_text = format_utc_timestamp(utc_now())
        with session_scope(self._session_factory) as session:
            jobs = session.scalars(
                select(ArchiveCopyJobRecord)
                .where(ArchiveCopyJobRecord.state.in_(("checking", "copying")))
                .order_by(ArchiveCopyJobRecord.requested_at)
                .limit(limit)
                .with_for_update()
            ).all()
            for job in jobs:
                job.state = "requested"
                job.next_attempt_at = current_text
            pending_cancellations = session.execute(
                select(
                    ArchiveCopyJobRecord.collection_id,
                    ArchiveCopyJobRecord.destination_store,
                )
                .where(
                    ArchiveCopyJobRecord.state == "canceling",
                )
                .limit(limit)
            ).all()
        for collection_id, destination_store in pending_cancellations:
            self._cleanup_canceled_destination(
                collection_id=collection_id,
                destination_store=str(destination_store),
            )
        return len(jobs) + len(pending_cancellations)

    def create_or_resume(
        self,
        collection_id: int,
        *,
        destination_store: str,
        source_store: str | None = None,
        initiator: ApplicationPrincipal,
        event_context: dict[str, object] | None = None,
    ) -> dict[str, object]:
        normalized_collection_id = _normalize_collection_id(collection_id)
        destination = self._configured_store(destination_store)
        source = self._configured_store(source_store) if source_store is not None else None
        if source == destination:
            raise BadRequest("archive copy source and destination stores must differ")
        destination_archive_store = self._archive_stores.require(destination).store
        current_text = format_utc_timestamp(utc_now())
        normalized_context_json = event_context_json(event_context)
        with session_scope(self._session_factory) as session:
            require_collection_archive_idle(session, normalized_collection_id)
            collection = session.get(CollectionRecord, normalized_collection_id)
            if collection is None:
                raise NotFound(f"collection not found: {normalized_collection_id}")
            existing = session.get(
                CollectionArchiveCopyRecord,
                (normalized_collection_id, destination),
            )
            if existing is not None and archive_copy_is_complete(existing):
                return _completed_payload(existing)
            source_copy = _select_source_copy(
                collection,
                config=self._config,
                destination_store=destination,
                source_store=source,
            )
            job = session.get(ArchiveCopyJobRecord, (normalized_collection_id, destination))
            if job is None:
                job = ArchiveCopyJobRecord(
                    collection_id=normalized_collection_id,
                    source_store=source_copy.store,
                    destination_store=destination,
                    destination_storage_prefix=(
                        destination_archive_store.new_collection_archive_storage_prefix()
                    ),
                    initiated_by_app=initiator.app,
                    initiated_by_key_id=initiator.key_id,
                    event_context_json=normalized_context_json,
                    state="requested",
                    requested_at=current_text,
                    next_attempt_at=current_text,
                )
                session.add(job)
                self._emit(job, type="archive_copy.requested", session=session)
            elif job.state not in ARCHIVE_COPY_TRANSFER_STATES:
                if job.state == "canceling":
                    raise Conflict("archive copy cancellation cleanup is still in progress")
                job.source_store = source_copy.store
                job.initiated_by_app = initiator.app
                job.initiated_by_key_id = initiator.key_id
                job.event_context_json = normalized_context_json
                job.state = "requested"
                job.requested_at = current_text
                job.next_attempt_at = current_text
                job.completed_at = None
                job.failure = None
                self._emit(job, type="archive_copy.requested", session=session)
            return _job_payload(job)

    def cancel(
        self,
        collection_id: int,
        *,
        destination_store: str,
        principal: ApplicationPrincipal | None = None,
    ) -> dict[str, object]:
        normalized_collection_id = _normalize_collection_id(collection_id)
        destination = self._configured_store(destination_store)
        copying = False
        with session_scope(self._session_factory) as session:
            job = session.scalar(
                select(ArchiveCopyJobRecord)
                .where(
                    ArchiveCopyJobRecord.collection_id == normalized_collection_id,
                    ArchiveCopyJobRecord.destination_store == destination,
                    collection_access_filter(
                        ArchiveCopyJobRecord.collection_id,
                        principal,
                        ARCHIVES_MANAGE,
                    ),
                )
                .with_for_update()
            )
            if job is None:
                raise NotFound(
                    f"archive copy job not found: {normalized_collection_id} to {destination}"
                )
            if job.state == "canceled":
                return _job_payload(job)
            if job.state == "canceling":
                return _job_payload(job)
            if job.state not in ARCHIVE_COPY_TRANSFER_STATES:
                raise InvalidState(f"archive copy cannot be canceled in state {job.state}")
            copying = job.state in {"checking", "copying"}
            job.state = "canceling"
            job.next_attempt_at = None
            job.failure = None
            job.completed_at = None
        if not copying:
            self._cleanup_source_read(
                collection_id=normalized_collection_id,
                destination_store=destination,
            )
            self._cleanup_canceled_destination(
                collection_id=normalized_collection_id,
                destination_store=destination,
            )
        return self.get(
            normalized_collection_id,
            destination_store=destination,
            principal=principal,
        )

    def get(
        self,
        collection_id: int,
        *,
        destination_store: str,
        principal: ApplicationPrincipal | None = None,
    ) -> dict[str, object]:
        normalized_collection_id = _normalize_collection_id(collection_id)
        destination = self._configured_store(destination_store)
        with session_scope(self._session_factory) as session:
            job = session.scalar(
                select(ArchiveCopyJobRecord).where(
                    ArchiveCopyJobRecord.collection_id == normalized_collection_id,
                    ArchiveCopyJobRecord.destination_store == destination,
                    collection_access_filter(
                        ArchiveCopyJobRecord.collection_id,
                        principal,
                        ARCHIVES_MANAGE,
                    ),
                )
            )
            if job is None:
                raise NotFound(
                    f"archive copy job not found: {normalized_collection_id} to {destination}"
                )
            return _job_payload(job)

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
    ) -> dict[str, object]:
        if page < 1:
            raise BadRequest("page must be at least 1")
        if per_page < 1:
            raise BadRequest("per_page must be at least 1")
        if sort not in _SORT_FIELDS:
            raise BadRequest(f"sort must be one of {', '.join(sorted(_SORT_FIELDS))}")
        if order not in {"asc", "desc"}:
            raise BadRequest("order must be asc or desc")
        query = q.strip().casefold() if q and q.strip() else None
        normalized_state = state.strip().casefold() if state and state.strip() else None
        if normalized_state is not None and normalized_state not in ARCHIVE_COPY_STATES:
            raise BadRequest(f"state must be one of {', '.join(sorted(ARCHIVE_COPY_STATES))}")
        filters = [
            collection_access_filter(
                ArchiveCopyJobRecord.collection_id,
                principal,
                ARCHIVES_MANAGE,
            )
        ]
        if normalized_state is not None:
            filters.append(ArchiveCopyJobRecord.state == normalized_state)
        if query is not None:
            escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            pattern = f"%{escaped}%"
            filters.append(
                or_(
                    cast(ArchiveCopyJobRecord.collection_id, String).like(
                        pattern,
                        escape="\\",
                    ),
                    func.lower(ArchiveCopyJobRecord.source_store).like(
                        pattern,
                        escape="\\",
                    ),
                    func.lower(ArchiveCopyJobRecord.destination_store).like(
                        pattern,
                        escape="\\",
                    ),
                    func.lower(ArchiveCopyJobRecord.state).like(
                        pattern,
                        escape="\\",
                    ),
                )
            )
        sort_columns = {
            "collection_id": ArchiveCopyJobRecord.collection_id,
            "source_store": ArchiveCopyJobRecord.source_store,
            "destination_store": ArchiveCopyJobRecord.destination_store,
            "state": ArchiveCopyJobRecord.state,
            "requested_at": ArchiveCopyJobRecord.requested_at,
        }
        direction = sort_columns[sort].desc() if order == "desc" else sort_columns[sort].asc()
        statement = (
            select(ArchiveCopyJobRecord)
            .where(*filters)
            .order_by(
                direction,
                ArchiveCopyJobRecord.collection_id.asc(),
                ArchiveCopyJobRecord.destination_store.asc(),
            )
        )
        with session_scope(self._session_factory) as session:
            total = int(
                session.scalar(
                    select(func.count()).select_from(ArchiveCopyJobRecord).where(*filters)
                )
                or 0
            )
            if not all_items:
                statement = statement.offset((page - 1) * per_page).limit(per_page)
            jobs = [_job_payload(job) for job in session.scalars(statement)]
        return {
            "page": 1 if all_items else page,
            "per_page": total if all_items else per_page,
            "total": total,
            "pages": (
                (1 if total else 0)
                if all_items
                else ((total + per_page - 1) // per_page if total else 0)
            ),
            "sort": sort,
            "order": order,
            "query": query,
            "filters": ({"state": normalized_state} if normalized_state is not None else {}),
            "copies": jobs,
        }

    def process_due(self, *, limit: int = 1) -> int:
        if limit < 1:
            return 0
        current_text = format_utc_timestamp(utc_now())
        with session_scope(self._session_factory) as session:
            jobs = session.execute(
                select(
                    ArchiveCopyJobRecord.collection_id,
                    ArchiveCopyJobRecord.destination_store,
                )
                .where(
                    ArchiveCopyJobRecord.state.in_(("requested", "waiting")),
                    or_(
                        ArchiveCopyJobRecord.next_attempt_at.is_(None),
                        ArchiveCopyJobRecord.next_attempt_at <= current_text,
                    ),
                )
                .order_by(
                    ArchiveCopyJobRecord.next_attempt_at,
                    ArchiveCopyJobRecord.requested_at,
                    ArchiveCopyJobRecord.collection_id,
                )
                .limit(limit)
            ).all()
        for collection_id, destination_store in jobs:
            try:
                self._process_one(
                    collection_id=collection_id,
                    destination_store=str(destination_store),
                )
            except Exception as exc:
                _LOG.exception(
                    "archive copy failed: collection=%s destination=%s",
                    collection_id,
                    destination_store,
                )
                self._record_failure(
                    collection_id=collection_id,
                    destination_store=str(destination_store),
                    exc=exc,
                )
                self._cleanup_source_read(
                    collection_id=collection_id,
                    destination_store=str(destination_store),
                )
                self._cleanup_canceled_destination(
                    collection_id=collection_id,
                    destination_store=str(destination_store),
                )
        return len(jobs)

    def _process_one(self, *, collection_id: int, destination_store: str) -> None:
        current = utc_now()
        current_text = format_utc_timestamp(current)
        with session_scope(self._session_factory) as session:
            job = session.scalar(
                select(ArchiveCopyJobRecord)
                .where(
                    ArchiveCopyJobRecord.collection_id == collection_id,
                    ArchiveCopyJobRecord.destination_store == destination_store,
                )
                .with_for_update()
            )
            if job is None or job.state not in {"requested", "waiting"}:
                return
            source_copy = _required_copy(session, collection_id, job.source_store)
            source_identity = archive_copy_identity(source_copy)
            source_store_name = job.source_store
            destination_store_name = job.destination_store
            data_objects = tuple(
                current for current in source_identity.objects if current.kind in _COPY_OBJECT_KINDS
            )
            read_requested_at = job.read_requested_at
            ready_at = job.ready_at
            expires_at = job.expires_at
            destination_storage_prefix = job.destination_storage_prefix
            job.state = "checking"
            job.next_attempt_at = None

        source_store = self._archive_stores.require(source_store_name).store
        if read_requested_at is None:
            status = source_store.prepare_archive_objects_read(
                collection_id=collection_id,
                objects=data_objects,
                retrieval_tier=self._config.retrieval_tier,
                hold_days=_read_hold_days(self._config),
                requested_at=current_text,
                estimated_ready_at=format_utc_timestamp(
                    current + self._config.retrieval_estimated_latency
                ),
            )
            read_requested_at = current_text
        else:
            status = source_store.get_archive_objects_read_status(
                collection_id=collection_id,
                objects=data_objects,
                requested_at=read_requested_at,
                estimated_ready_at=ready_at,
                estimated_expires_at=expires_at,
            )

        canceled = False
        with session_scope(self._session_factory) as session:
            job = session.scalar(
                select(ArchiveCopyJobRecord)
                .where(
                    ArchiveCopyJobRecord.collection_id == collection_id,
                    ArchiveCopyJobRecord.destination_store == destination_store,
                )
                .with_for_update()
            )
            if job is None:
                return
            if job.state not in {"checking", "canceling"}:
                raise Conflict("archive copy changed while checking source availability")
            job.read_requested_at = read_requested_at
            job.ready_at = status.ready_at or job.ready_at
            job.expires_at = status.expires_at or job.expires_at
            if job.state == "canceling":
                canceled = True
            elif status.state == "expired":
                job.state = "requested"
                job.read_requested_at = None
                job.ready_at = None
                job.expires_at = None
                job.next_attempt_at = current_text
                return
            elif status.state != "ready":
                job.state = "waiting"
                job.next_attempt_at = format_utc_timestamp(
                    current + self._config.retrieval_restore_poll_interval
                )
                return
            else:
                job.state = "copying"
                job.next_attempt_at = None

        if canceled:
            self._cleanup_source_read(
                collection_id=collection_id,
                destination_store=destination_store,
            )
            self._cleanup_canceled_destination(
                collection_id=collection_id,
                destination_store=destination_store,
            )
            return

        copied = self._copy_immutable_objects(
            collection_id=collection_id,
            source_store_name=source_store_name,
            destination_store_name=destination_store_name,
            source_identity=source_identity,
            destination_storage_prefix=destination_storage_prefix,
        )
        with session_scope(self._session_factory) as session:
            job = session.scalar(
                select(ArchiveCopyJobRecord)
                .where(
                    ArchiveCopyJobRecord.collection_id == collection_id,
                    ArchiveCopyJobRecord.destination_store == destination_store,
                )
                .with_for_update()
            )
            if job is None:
                raise Conflict("archive copy job disappeared during transfer")
            if job.state != "copying":
                raise Conflict("archive copy was canceled during transfer")
            source_copy = _required_copy(session, collection_id, job.source_store)
            self._record_completed_copy(
                session,
                source=source_copy,
                destination_store=destination_store,
                destination_storage_prefix=destination_storage_prefix,
                copied=copied,
            )
            session.execute(
                delete(ArchiveCopyObjectUploadRecord).where(
                    ArchiveCopyObjectUploadRecord.collection_id == collection_id,
                    ArchiveCopyObjectUploadRecord.destination_store == destination_store,
                )
            )
            collection = session.get(CollectionRecord, collection_id)
            assert collection is not None
            session.merge(
                CollectionMetadataPublicationRecord(
                    collection_id=collection_id,
                    store=destination_store,
                    desired_revision=collection.metadata_revision,
                    state="pending",
                    attempt_count=0,
                    next_attempt_at=format_utc_timestamp(utc_now()),
                )
            )
            session.merge(
                CollectionProofMaturationRecord(
                    collection_id=collection_id,
                    store=destination_store,
                    state=(
                        "matured"
                        if source_copy.proof_maturation is not None
                        and source_copy.proof_maturation.state == "matured"
                        else "pending"
                    ),
                    attempt_count=(
                        source_copy.proof_maturation.attempt_count
                        if source_copy.proof_maturation is not None
                        and source_copy.proof_maturation.state == "matured"
                        else 0
                    ),
                    next_attempt_at=format_utc_timestamp(utc_now()),
                    matured_at=(
                        source_copy.proof_maturation.matured_at
                        if source_copy.proof_maturation is not None
                        and source_copy.proof_maturation.state == "matured"
                        else None
                    ),
                )
            )
            job.state = "completed"
            job.completed_at = format_utc_timestamp(utc_now())
            job.next_attempt_at = None
            job.failure = None
            self._emit(
                job,
                type="archive_copy.completed",
                terminal=True,
                session=session,
            )
        source_store.cleanup_archive_objects_read(
            collection_id=collection_id,
            objects=data_objects,
        )

    def _plan_destination_upload(
        self,
        *,
        collection_id: int,
        destination_store: str,
        destination_storage_prefix: str,
        objects: Sequence[CollectionArchiveObjectRecord],
    ) -> None:
        expected = {
            current.object_id: current for current in objects if current.kind in {"pack", "segment"}
        }
        with session_scope(self._session_factory) as session:
            existing = {
                current.object_id: current
                for current in session.scalars(
                    select(ArchiveCopyObjectUploadRecord).where(
                        ArchiveCopyObjectUploadRecord.collection_id == collection_id,
                        ArchiveCopyObjectUploadRecord.destination_store == destination_store,
                    )
                )
            }
            if set(existing) - set(expected):
                raise Conflict("archive copy upload checkpoint does not match its manifest")
            for object_id, current in expected.items():
                record = existing.get(object_id)
                if record is None:
                    session.add(
                        ArchiveCopyObjectUploadRecord(
                            collection_id=collection_id,
                            destination_store=destination_store,
                            object_id=object_id,
                            kind=current.kind,
                            object_path=_destination_object_path(
                                source=current,
                                destination_storage_prefix=destination_storage_prefix,
                            ),
                            plaintext_bytes=current.plaintext_bytes,
                            sha256=current.sha256,
                            multipart_content_length=current.stored_bytes,
                        )
                    )
                    continue
                if (
                    record.kind != current.kind
                    or record.plaintext_bytes != current.plaintext_bytes
                    or record.sha256 != current.sha256
                    or record.multipart_content_length != current.stored_bytes
                ):
                    raise Conflict("archive copy upload checkpoint does not match its manifest")

    def _copy_immutable_objects(
        self,
        *,
        collection_id: int,
        source_store_name: str,
        destination_store_name: str,
        source_identity: CollectionArchiveIdentity,
        destination_storage_prefix: str,
    ) -> dict[str, _CopiedObject]:
        with session_scope(self._session_factory) as session:
            source_records = list(
                session.scalars(
                    select(CollectionArchiveObjectRecord)
                    .options(selectinload(CollectionArchiveObjectRecord.placements))
                    .where(
                        CollectionArchiveObjectRecord.collection_id == collection_id,
                        CollectionArchiveObjectRecord.store == source_store_name,
                        CollectionArchiveObjectRecord.kind.in_(_COPY_OBJECT_KINDS),
                    )
                    .order_by(CollectionArchiveObjectRecord.object_order)
                )
            )
        identities = {
            current.object_id: current
            for current in source_identity.objects
            if current.kind in _COPY_OBJECT_KINDS
        }
        if {current.object_id for current in source_records} != set(identities):
            raise Conflict("archive copy catalog identity changed during transfer")
        self._plan_destination_upload(
            collection_id=collection_id,
            destination_store=destination_store_name,
            destination_storage_prefix=destination_storage_prefix,
            objects=source_records,
        )
        source_store = self._archive_stores.require(source_store_name).store
        destination = self._archive_stores.require(destination_store_name)
        copied: dict[str, _CopiedObject] = {}
        for record in source_records:
            self._require_copy_active(collection_id, destination_store_name)
            identity = identities[record.object_id]
            if record.kind in {"pack", "segment"}:
                result = self._copy_volume(
                    collection_id=collection_id,
                    destination_store_name=destination_store_name,
                    destination_storage_prefix=destination_storage_prefix,
                    source_store=source_store,
                    destination_object_store=self._volume_object_store(
                        store_name=destination_store_name,
                        collection_id=collection_id,
                        object_id=record.object_id,
                    ),
                    source=record,
                    identity=identity,
                )
            else:
                result = self._copy_small_immutable_object(
                    collection_id=collection_id,
                    destination_storage_prefix=destination_storage_prefix,
                    source_store=source_store,
                    destination_store=destination.immutable_objects,
                    source=record,
                    identity=identity,
                )
            copied[result.object_id] = result
        return copied

    def _copy_volume(
        self,
        *,
        collection_id: int,
        destination_store_name: str,
        destination_storage_prefix: str,
        source_store: ArchiveStore,
        destination_object_store: ArchiveMultipartObjectStore,
        source: CollectionArchiveObjectRecord,
        identity: ArchiveObjectIdentity,
    ) -> _CopiedObject:
        destination_path = _destination_object_path(
            source=source,
            destination_storage_prefix=destination_storage_prefix,
        )
        part_rows = _part_rows(source.part_receipts_json)
        metadata = _volume_metadata(source)
        content_type = (
            PACK_VOLUME_CONTENT_TYPE if source.kind == "pack" else RAW_VOLUME_CONTENT_TYPE
        )
        completed = destination_object_store.head_completed_object(
            object_path=destination_path,
            expected_metadata=metadata,
        )
        if completed is not None:
            if completed.bytes != source.stored_bytes:
                raise Conflict("completed archive-copy volume has a different byte count")
            return _CopiedObject(
                source.object_id,
                completed.object_path,
                completed.version_id,
                completed.completed_at,
                source.part_receipts_json,
                completed.retrieval_cache,
            )

        with session_scope(self._session_factory) as session:
            checkpoint = session.get(
                ArchiveCopyObjectUploadRecord,
                (collection_id, destination_store_name, source.object_id),
            )
            if checkpoint is None:
                raise Conflict("archive copy upload checkpoint disappeared")
            upload = (
                MultipartUpload(destination_path, checkpoint.multipart_upload_id)
                if checkpoint.multipart_upload_id
                else None
            )
        if upload is None:
            upload = destination_object_store.create_multipart_upload(
                object_path=destination_path,
                content_type=content_type,
                metadata=metadata,
            )
            with session_scope(self._session_factory) as session:
                checkpoint = session.get(
                    ArchiveCopyObjectUploadRecord,
                    (collection_id, destination_store_name, source.object_id),
                )
                if checkpoint is None:
                    raise Conflict("archive copy upload checkpoint disappeared")
                checkpoint.multipart_upload_id = upload.upload_id
                checkpoint.object_path = destination_path

        remote_parts = {
            current.number: current
            for current in destination_object_store.list_parts(upload=upload)
        }
        if set(remote_parts) - {_part_int(current, "number") for current in part_rows}:
            raise Conflict("archive copy multipart upload has unexpected parts")
        committed = self._copy_volume_parts(
            collection_id=collection_id,
            destination_store_name=destination_store_name,
            source_store=source_store,
            destination_object_store=destination_object_store,
            source=source,
            identity=identity,
            upload=upload,
            part_rows=part_rows,
            remote_parts=remote_parts,
        )
        completed = destination_object_store.complete_multipart_upload(
            upload=upload,
            parts=committed,
            expected_bytes=source.stored_bytes,
            expected_metadata=metadata,
        )
        return _CopiedObject(
            source.object_id,
            completed.object_path,
            completed.version_id,
            completed.completed_at,
            _part_rows_json(part_rows, committed),
            completed.retrieval_cache,
        )

    def _volume_object_store(
        self,
        *,
        store_name: str,
        collection_id: int,
        object_id: str,
    ) -> ArchiveMultipartObjectStore:
        archive = self._archive_stores.require(store_name).multipart_objects
        if (
            not self._config.retrieval_cache_new_archive_enabled
            or self._retrieval_cache is None
            or self._config.archive_store(store_name).read_mode != "restore_required"
        ):
            return archive
        return MirroredArchiveMultipartObjectStore(
            archive=archive,
            cache=self._retrieval_cache,
            source_store=store_name,
            collection_id=collection_id,
            object_id=object_id,
        )

    def _copy_volume_parts(
        self,
        *,
        collection_id: int,
        destination_store_name: str,
        source_store: ArchiveStore,
        destination_object_store: ArchiveMultipartObjectStore,
        source: CollectionArchiveObjectRecord,
        identity: ArchiveObjectIdentity,
        upload: MultipartUpload,
        part_rows: Sequence[dict[str, object]],
        remote_parts: Mapping[int, MultipartPartReceipt],
    ) -> tuple[MultipartPartReceipt, ...]:
        worker_count = min(self._throughput.s3_part_concurrency, len(part_rows))
        window = min(len(part_rows), worker_count * 2)
        source_chunks = iter(
            source_store.iter_stored_archive_object(
                collection_id=collection_id,
                object=identity,
            )
        )
        source_buffer = bytearray()
        rows = iter(part_rows)
        pending: dict[Future[_CopiedPart], dict[str, object]] = {}
        committed: dict[int, MultipartPartReceipt] = {}
        exhausted = False
        retrieval_wait_seconds = 0.0
        retrieval_wait_recorded = False

        def read_part(row: Mapping[str, object]) -> tuple[bytes, int, float, float]:
            nonlocal retrieval_wait_recorded
            reserved = _part_int(row, "stored_bytes")
            byte_wait_seconds = self._resources.upload_bytes.acquire(reserved)
            try:
                source_started = time.perf_counter()
                while len(source_buffer) < reserved:
                    try:
                        chunk = bytes(next(source_chunks))
                    except StopIteration as exc:
                        raise Conflict(
                            "source archive volume ended before its part receipts"
                        ) from exc
                    if chunk:
                        source_buffer.extend(chunk)
                content = bytes(source_buffer[:reserved])
                del source_buffer[:reserved]
                source_seconds = time.perf_counter() - source_started
                queue_wait_seconds = byte_wait_seconds
                if not retrieval_wait_recorded:
                    queue_wait_seconds += retrieval_wait_seconds
                    retrieval_wait_recorded = True
                return content, reserved, queue_wait_seconds, source_seconds
            except BaseException:
                self._resources.upload_bytes.release(reserved)
                raise

        def upload_part(
            row: Mapping[str, object],
            content: bytes,
            queue_wait_seconds: float,
            source_seconds: float,
        ) -> _CopiedPart:
            number = _part_int(row, "number")
            integrity_started = time.perf_counter()
            if hashlib.sha256(content).hexdigest() != str(row["stored_sha256"]):
                raise Conflict(f"source archive volume part {number} failed verification")
            integrity_seconds = time.perf_counter() - integrity_started
            receipt = remote_parts.get(number)
            remote_seconds = 0.0
            if receipt is None:
                with self._resources.upload_requests.reserve() as request_wait_seconds:
                    queue_wait_seconds += request_wait_seconds
                    remote_started = time.perf_counter()
                    receipt = destination_object_store.upload_part(
                        upload=upload,
                        number=number,
                        content=content,
                    )
                    remote_seconds = time.perf_counter() - remote_started
            if receipt.bytes != len(content):
                raise Conflict("archive copy multipart part byte count changed")
            receipt = replace(receipt, sha256=str(row["stored_sha256"]))
            return _CopiedPart(
                receipt=receipt,
                timing=TransferTiming(
                    operation="archive_copy_part",
                    identity=f"{source.object_id}:{number}",
                    plaintext_bytes=len(content),
                    stored_bytes=len(content),
                    queue_wait_seconds=queue_wait_seconds,
                    source_seconds=source_seconds,
                    integrity_seconds=integrity_seconds,
                    crypto_seconds=0.0,
                    processing_seconds=0.0,
                    remote_seconds=remote_seconds,
                    checkpoint_seconds=0.0,
                    elapsed_seconds=(
                        queue_wait_seconds + source_seconds + integrity_seconds + remote_seconds
                    ),
                ),
            )

        with self._resources.retrieval_requests.reserve() as current_retrieval_wait_seconds:
            retrieval_wait_seconds = current_retrieval_wait_seconds
            with ThreadPoolExecutor(
                max_workers=worker_count,
                thread_name_prefix="riverhog-archive-copy-part",
            ) as executor:

                def fill() -> None:
                    nonlocal exhausted
                    while not exhausted and len(pending) < window:
                        try:
                            row = next(rows)
                        except StopIteration:
                            exhausted = True
                            if source_buffer or any(bytes(chunk) for chunk in source_chunks):
                                raise Conflict(
                                    "source archive volume has bytes beyond its part receipts"
                                ) from None
                            return
                        content, reserved, queue_wait_seconds, source_seconds = read_part(row)
                        try:
                            future = executor.submit(
                                upload_part,
                                row,
                                content,
                                queue_wait_seconds,
                                source_seconds,
                            )
                        except BaseException:
                            self._resources.upload_bytes.release(reserved)
                            raise

                        def release_buffer(
                            _future: Future[_CopiedPart],
                            amount: int = reserved,
                        ) -> None:
                            self._resources.upload_bytes.release(amount)

                        future.add_done_callback(release_buffer)
                        pending[future] = row

                fill()
                try:
                    while pending:
                        done, _ = wait(tuple(pending), return_when=FIRST_COMPLETED)
                        completed_parts: list[_CopiedPart] = []
                        for future in done:
                            row = pending.pop(future)
                            result = future.result()
                            committed[_part_int(row, "number")] = result.receipt
                            completed_parts.append(result)
                        checkpoint_started = time.perf_counter()
                        self._record_copy_part(
                            collection_id=collection_id,
                            destination_store=destination_store_name,
                            object_id=source.object_id,
                            parts=tuple(committed[number] for number in sorted(committed)),
                            total_parts=len(part_rows),
                        )
                        checkpoint_seconds = time.perf_counter() - checkpoint_started
                        checkpoint_share = checkpoint_seconds / len(completed_parts)
                        for result in completed_parts:
                            log_transfer_timing(
                                replace(
                                    result.timing,
                                    checkpoint_seconds=checkpoint_share,
                                    elapsed_seconds=(
                                        result.timing.elapsed_seconds + checkpoint_share
                                    ),
                                )
                            )
                        fill()
                except BaseException:
                    for future in pending:
                        future.cancel()
                    raise
        if set(committed) != {_part_int(row, "number") for row in part_rows}:
            raise Conflict("archive copy part receipts do not cover the volume")
        return tuple(committed[number] for number in sorted(committed))

    def _copy_small_immutable_object(
        self,
        *,
        collection_id: int,
        destination_storage_prefix: str,
        source_store: ArchiveStore,
        destination_store: ImmutableArchiveObjectStore,
        source: CollectionArchiveObjectRecord,
        identity: ArchiveObjectIdentity,
    ) -> _CopiedObject:
        started = time.perf_counter()
        reserve_bytes = min(max(1, source.stored_bytes), self._resources.upload_bytes.capacity)
        with self._resources.upload_bytes.reserve(reserve_bytes) as byte_wait:
            with self._resources.retrieval_requests.reserve() as retrieval_wait:
                source_started = time.perf_counter()
                content = b"".join(
                    source_store.iter_stored_archive_object(
                        collection_id=collection_id,
                        object=identity,
                    )
                )
                source_seconds = time.perf_counter() - source_started
            if len(content) != source.stored_bytes:
                raise Conflict("source archive artifact byte count changed")
            integrity_started = time.perf_counter()
            stored_sha256 = hashlib.sha256(content).hexdigest()
            integrity_seconds = time.perf_counter() - integrity_started
            if source.stored_sha256 and stored_sha256 != source.stored_sha256:
                raise Conflict("source archive artifact sha256 changed")
            destination_path = _destination_object_path(
                source=source,
                destination_storage_prefix=destination_storage_prefix,
            )
            content_type = {
                "manifest": "application/vnd.riverhog.collection-manifest+age",
                "proof": "application/vnd.riverhog.collection-manifest-proof+age",
                "provenance-index": "application/vnd.riverhog.provenance-index+age",
                "provenance-bundle": "application/vnd.riverhog.provenance-bundle+age",
            }[source.kind]
            with self._resources.upload_requests.reserve() as upload_wait:
                remote_started = time.perf_counter()
                receipt = destination_store.put_immutable_object(
                    object_path=destination_path,
                    content=content,
                    content_type=content_type,
                    identity_metadata={
                        "riverhog-format": archive_object_storage_format(source.kind),
                        "riverhog-plaintext-bytes": str(source.plaintext_bytes),
                        "riverhog-plaintext-sha256": source.sha256 or "",
                    },
                )
                remote_seconds = time.perf_counter() - remote_started
        if receipt.stored_sha256 != stored_sha256:
            raise Conflict("destination archive artifact sha256 changed")
        log_transfer_timing(
            TransferTiming(
                operation="archive_copy_object",
                identity=source.object_id,
                plaintext_bytes=source.plaintext_bytes,
                stored_bytes=source.stored_bytes,
                queue_wait_seconds=byte_wait + retrieval_wait + upload_wait,
                source_seconds=source_seconds,
                integrity_seconds=integrity_seconds,
                crypto_seconds=0.0,
                processing_seconds=0.0,
                remote_seconds=remote_seconds,
                checkpoint_seconds=0.0,
                elapsed_seconds=time.perf_counter() - started,
            )
        )
        return _CopiedObject(
            source.object_id,
            receipt.object_path,
            receipt.version_id,
            receipt.completed_at,
        )

    def _record_copy_part(
        self,
        *,
        collection_id: int,
        destination_store: str,
        object_id: str,
        parts: Sequence[MultipartPartReceipt],
        total_parts: int,
    ) -> None:
        with session_scope(self._session_factory) as session:
            record = session.get(
                ArchiveCopyObjectUploadRecord,
                (collection_id, destination_store, object_id),
            )
            if record is None:
                raise Conflict("archive copy upload checkpoint disappeared")
            record.multipart_parts_json = json.dumps(
                [
                    {"number": current.number, "etag": current.etag, "bytes": current.bytes}
                    for current in parts
                ],
                sort_keys=True,
                separators=(",", ":"),
            )
            record.uploaded_bytes = sum(current.bytes for current in parts)
            record.uploaded_parts = len(parts)
            record.total_parts = total_parts

    def _require_copy_active(self, collection_id: int, destination_store: str) -> None:
        with session_scope(self._session_factory) as session:
            job = session.get(ArchiveCopyJobRecord, (collection_id, destination_store))
            if job is None or job.state != "copying":
                raise Conflict("archive copy was canceled during transfer")

    def _record_completed_copy(
        self,
        session: Session,
        *,
        source: CollectionArchiveCopyRecord,
        destination_store: str,
        destination_storage_prefix: str,
        copied: Mapping[str, _CopiedObject],
    ) -> None:
        destination = session.get(
            CollectionArchiveCopyRecord,
            (source.collection_id, destination_store),
        )
        if destination is None:
            destination = CollectionArchiveCopyRecord(
                collection_id=source.collection_id,
                store=destination_store,
            )
            session.add(destination)
        destination.objects.clear()
        store_config = self._config.archive_store(destination_store)
        uploaded_at: list[str] = []
        cache_receipts: list[tuple[CollectionArchiveObjectRecord, RetrievalCacheReceipt]] = []
        cache_required = (
            self._config.retrieval_cache_new_archive_enabled
            and self._retrieval_cache is not None
            and store_config.read_mode == "restore_required"
        )
        for current in sorted(source.objects, key=lambda value: value.object_order):
            if current.kind not in _COPY_OBJECT_KINDS:
                continue
            receipt = copied.get(current.object_id)
            if receipt is None:
                raise Conflict("archive copy result omitted an immutable object")
            if current.kind in {"pack", "segment"} and cache_required:
                if receipt.retrieval_cache is None:
                    raise RuntimeError(
                        "restore-required archive copy is missing its retrieval cache receipt"
                    )
            copied_record = CollectionArchiveObjectRecord(
                collection_id=source.collection_id,
                store=destination_store,
                object_id=current.object_id,
                object_order=current.object_order,
                kind=current.kind,
                object_path=receipt.object_path,
                plaintext_bytes=current.plaintext_bytes,
                stored_bytes=current.stored_bytes,
                sha256=current.sha256,
                stored_sha256=current.stored_sha256,
                version_id=receipt.version_id,
                age_state_json=current.age_state_json,
                part_receipts_json=receipt.part_receipts_json,
                plan_sha256=current.plan_sha256,
                index_sha256=current.index_sha256,
                backend=store_config.backend,
                storage_class=store_config.storage_class,
                uploaded_at=receipt.completed_at,
                verified_at=receipt.completed_at,
            )
            for placement in current.placements:
                copied_record.placements.append(
                    CollectionArchiveFileObjectRecord(
                        collection_id=source.collection_id,
                        store=destination_store,
                        path=placement.path,
                        sequence=placement.sequence,
                        object_id=current.object_id,
                        file_offset=placement.file_offset,
                        object_offset=placement.object_offset,
                        bytes=placement.bytes,
                        member=placement.member,
                    )
                )
            destination.objects.append(copied_record)
            if receipt.retrieval_cache is not None:
                cache_receipts.append((copied_record, receipt.retrieval_cache))
            uploaded_at.append(receipt.completed_at)
        session.flush()
        cache_expires_at = format_utc_timestamp(
            utc_now() + self._config.retrieval_cache_new_archive_lease
        )
        cache_leases: list[RetrievalCacheLeaseRecord] = []
        for copied_record, cache_receipt in cache_receipts:
            if (
                cache_receipt.stored_bytes != copied_record.stored_bytes
                or len(cache_receipt.stored_sha256) != 64
            ):
                raise RuntimeError(
                    "retrieval cache receipt does not match its copied archive volume"
                )
            session.add(
                RetrievalCacheObjectRecord(
                    source_store=destination_store,
                    collection_id=source.collection_id,
                    object_id=copied_record.object_id,
                    object_path=cache_receipt.object_path,
                    version_id=cache_receipt.version_id,
                    stored_bytes=cache_receipt.stored_bytes,
                    stored_sha256=cache_receipt.stored_sha256,
                    cached_at=cache_receipt.cached_at,
                    verified_at=cache_receipt.verified_at,
                    state="ready",
                )
            )
            cache_leases.append(
                RetrievalCacheLeaseRecord(
                    owner="new-archive",
                    source_store=destination_store,
                    collection_id=source.collection_id,
                    object_id=copied_record.object_id,
                    expires_at=cache_expires_at,
                )
            )
        session.flush()
        session.add_all(cache_leases)
        if not {"manifest", "proof"}.issubset(copied):
            raise Conflict("archive copy result has no root and proof")
        destination.state = "uploaded"
        destination.archive_storage_prefix = destination_storage_prefix
        destination.backend = store_config.backend
        destination.storage_class = store_config.storage_class
        destination.last_uploaded_at = max(uploaded_at)
        destination.last_verified_at = max(uploaded_at)
        destination.failure = None

    def _record_failure(
        self,
        *,
        collection_id: int,
        destination_store: str,
        exc: Exception,
    ) -> None:
        with session_scope(self._session_factory) as session:
            job = session.get(ArchiveCopyJobRecord, (collection_id, destination_store))
            if job is None or job.state in {"canceling", "canceled"}:
                return
            job.state = "failed"
            job.next_attempt_at = None
            job.failure = f"{type(exc).__name__}: {exc}"
            self._emit(
                job,
                type="archive_copy.issue",
                terminal=True,
                details={"error": job.failure},
                session=session,
            )

    def _cleanup_source_read(self, *, collection_id: int, destination_store: str) -> None:
        with session_scope(self._session_factory) as session:
            job = session.get(ArchiveCopyJobRecord, (collection_id, destination_store))
            if job is None or job.read_requested_at is None:
                return
            source_copy = session.get(
                CollectionArchiveCopyRecord,
                (collection_id, job.source_store),
            )
            if source_copy is None or not archive_copy_is_complete(source_copy):
                return
            source_store_name = job.source_store
            data_objects = tuple(
                current
                for current in archive_copy_identity(source_copy).objects
                if current.kind in _COPY_OBJECT_KINDS
            )
        source_store = self._archive_stores.require(source_store_name).store
        try:
            source_store.cleanup_archive_objects_read(
                collection_id=collection_id,
                objects=data_objects,
            )
        except Exception:
            _LOG.exception(
                "failed to clean up archive-copy source read: collection=%s source=%s",
                collection_id,
                source_store_name,
            )

    def _cleanup_canceled_destination(
        self,
        *,
        collection_id: int,
        destination_store: str,
    ) -> None:
        with session_scope(self._session_factory) as session:
            job = session.get(ArchiveCopyJobRecord, (collection_id, destination_store))
            if job is None or job.state != "canceling":
                return
            destination_storage_prefix = job.destination_storage_prefix
        try:
            self._archive_stores.require(destination_store).store.discard_collection_archive_upload(
                archive_storage_prefix=destination_storage_prefix,
            )
        except Exception:
            _LOG.exception(
                "failed to discard canceled archive copy: collection=%s destination=%s",
                collection_id,
                destination_store,
            )
            return
        with session_scope(self._session_factory) as session:
            job = session.get(ArchiveCopyJobRecord, (collection_id, destination_store))
            if job is None or job.state != "canceling":
                return
            session.execute(
                delete(ArchiveCopyObjectUploadRecord).where(
                    ArchiveCopyObjectUploadRecord.collection_id == collection_id,
                    ArchiveCopyObjectUploadRecord.destination_store == destination_store,
                )
            )
            job.state = "canceled"
            job.completed_at = format_utc_timestamp(utc_now())
            self._emit(job, type="archive_copy.canceled", terminal=True, session=session)

    def _configured_store(self, value: str) -> str:
        try:
            return self._config.archive_store(value).name
        except ValueError as exc:
            raise BadRequest(str(exc)) from exc

    def _emit(
        self,
        job: ArchiveCopyJobRecord,
        *,
        type: str,
        terminal: bool = False,
        details: dict[str, object] | None = None,
        session: Session,
    ) -> None:
        self._lifecycle_events.emit_collection(
            type=type,
            collection_id=job.collection_id,
            details={
                "source_store": job.source_store,
                "destination_store": job.destination_store,
                "state": job.state,
                **(details or {}),
            },
            terminal=terminal,
            initiator=ApplicationPrincipal(
                app=job.initiated_by_app,
                key_id=job.initiated_by_key_id,
                access=frozenset(),
            ),
            event_context_json=job.event_context_json,
            session=session,
        )


def _select_source_copy(
    collection: CollectionRecord,
    *,
    config: RuntimeConfig,
    destination_store: str,
    source_store: str | None,
) -> CollectionArchiveCopyRecord:
    candidates = [
        copy
        for copy in collection.archive_copies
        if copy.store != destination_store and archive_copy_is_complete(copy)
    ]
    if source_store is not None:
        candidates = [copy for copy in candidates if copy.store == source_store]
    if not candidates:
        raise InvalidState(f"collection has no uploaded archive copy available: {collection.id}")
    read_rank = {store: index for index, store in enumerate(config.archive_read_order)}
    return min(
        candidates,
        key=lambda copy: (read_rank.get(copy.store, len(read_rank)), copy.store),
    )


def _required_copy(
    session: Session,
    collection_id: int,
    store: str,
) -> CollectionArchiveCopyRecord:
    copy = session.get(CollectionArchiveCopyRecord, (collection_id, store))
    if copy is None or not archive_copy_is_complete(copy):
        raise InvalidState(f"source archive copy is incomplete: {collection_id} in {store}")
    return copy


def _destination_object_path(
    *,
    source: CollectionArchiveObjectRecord,
    destination_storage_prefix: str,
) -> str:
    prefix = destination_storage_prefix.strip("/")
    if not prefix:
        raise Conflict("archive copy destination prefix is empty")
    if source.kind == "pack":
        relative = f"volumes/{source.object_id}.tar.age"
    elif source.kind == "segment":
        relative = f"volumes/{source.object_id}.bin.age"
    elif source.kind == "manifest":
        relative = "manifest.json.age"
    elif source.kind == "proof":
        relative = "manifest.json.ots.age"
    elif source.kind == "provenance-index":
        relative = "provenance/index.json.age"
    elif source.kind == "provenance-bundle":
        relative = f"provenance/{source.object_id}.tar.age"
    else:
        raise Conflict(f"archive copy object kind is not immutable: {source.kind}")
    return f"{prefix}/{relative}"


def _volume_metadata(source: CollectionArchiveObjectRecord) -> dict[str, str]:
    if source.age_state_json is None:
        raise Conflict("archive volume has no age state")
    try:
        state = UploadState.from_json_bytes(source.age_state_json)
    except (TypeError, ValueError) as exc:
        raise Conflict("archive volume age state is invalid") from exc
    if state.plaintext_size != source.plaintext_bytes:
        raise Conflict("archive volume age state size changed")
    metadata = {
        "riverhog-format": (
            PACK_VOLUME_STORAGE_FORMAT if source.kind == "pack" else RAW_VOLUME_STORAGE_FORMAT
        ),
        "riverhog-object-id": source.object_id,
        "riverhog-plaintext-bytes": str(source.plaintext_bytes),
        "riverhog-age-state-sha256": hashlib.sha256(state.to_json_bytes()).hexdigest(),
    }
    if source.kind == "pack":
        if not source.plan_sha256 or not source.index_sha256:
            raise Conflict("archive pack has no plan or index identity")
        metadata.update(
            {
                "riverhog-plan-sha256": source.plan_sha256,
                "riverhog-index-sha256": source.index_sha256,
            }
        )
    else:
        if len(source.placements) != 1:
            raise Conflict("archive segment has no unique file placement")
        placement = source.placements[0]
        metadata.update(
            {
                "riverhog-source-path-sha256": hashlib.sha256(
                    placement.path.encode("utf-8")
                ).hexdigest(),
                "riverhog-file-offset": str(placement.file_offset),
            }
        )
    return metadata


def _part_rows(value: str | None) -> list[dict[str, object]]:
    if value is None:
        raise Conflict("archive volume has no stored-part receipts")
    try:
        raw = json.loads(value)
    except json.JSONDecodeError as exc:
        raise Conflict("archive volume stored-part receipts are invalid") from exc
    if not isinstance(raw, list) or not raw:
        raise Conflict("archive volume stored-part receipts are empty")
    rows: list[dict[str, object]] = []
    expected_start = 0
    expected_keys = {
        "number",
        "plaintext_start",
        "plaintext_bytes",
        "plaintext_sha256",
        "stored_bytes",
        "stored_sha256",
        "etag",
    }
    for number, current in enumerate(raw, start=1):
        if not isinstance(current, dict) or set(current) != expected_keys:
            raise Conflict("archive volume stored-part receipt is not canonical")
        if current.get("number") != number or current.get("plaintext_start") != expected_start:
            raise Conflict("archive volume stored-part order changed")
        plaintext_bytes = current.get("plaintext_bytes")
        stored_bytes = current.get("stored_bytes")
        if (
            isinstance(plaintext_bytes, bool)
            or not isinstance(plaintext_bytes, int)
            or plaintext_bytes < 0
            or isinstance(stored_bytes, bool)
            or not isinstance(stored_bytes, int)
            or stored_bytes < 1
        ):
            raise Conflict("archive volume stored-part byte count is invalid")
        for key in ("plaintext_sha256", "stored_sha256"):
            digest = current.get(key)
            if not isinstance(digest, str) or len(digest) != 64:
                raise Conflict("archive volume stored-part digest is invalid")
        expected_start += plaintext_bytes
        rows.append(dict(current))
    return rows


def _iter_exact_parts(
    chunks: Iterable[bytes],
    parts: Sequence[Mapping[str, object]],
) -> Iterator[bytes]:
    source = iter(chunks)
    buffer = bytearray()
    for row in parts:
        needed = _part_int(row, "stored_bytes")
        while len(buffer) < needed:
            try:
                chunk = bytes(next(source))
            except StopIteration as exc:
                raise Conflict("source archive volume ended before its part receipts") from exc
            if chunk:
                buffer.extend(chunk)
        yield bytes(buffer[:needed])
        del buffer[:needed]
    if buffer or any(bytes(chunk) for chunk in source):
        raise Conflict("source archive volume has bytes beyond its part receipts")


def _part_rows_json(
    rows: Sequence[Mapping[str, object]],
    receipts: Sequence[MultipartPartReceipt],
) -> str:
    if len(rows) != len(receipts):
        raise Conflict("archive copy part receipts do not cover the volume")
    payload = []
    for row, receipt in zip(rows, receipts, strict=True):
        if (
            _part_int(row, "number") != receipt.number
            or _part_int(row, "stored_bytes") != receipt.bytes
        ):
            raise Conflict("destination archive part identity changed")
        payload.append({**row, "etag": receipt.etag})
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def _part_int(row: Mapping[str, object], key: str) -> int:
    value = row[key]
    if isinstance(value, bool) or not isinstance(value, int):
        raise Conflict("archive volume stored-part integer is invalid")
    return value


def _normalize_collection_id(value: str | int) -> int:
    try:
        return normalize_collection_id(value)
    except PathNormalizationError as exc:
        raise BadRequest(str(exc)) from exc


def _job_payload(job: ArchiveCopyJobRecord) -> dict[str, object]:
    return {
        "collection_id": job.collection_id,
        "source_store": job.source_store,
        "destination_store": job.destination_store,
        "initiated_by_app": job.initiated_by_app,
        "initiated_by_key_id": job.initiated_by_key_id,
        "state": job.state,
        "requested_at": job.requested_at,
        "ready_at": job.ready_at,
        "expires_at": job.expires_at,
        "completed_at": job.completed_at,
        "failure": job.failure,
    }


def _completed_payload(copy: CollectionArchiveCopyRecord) -> dict[str, object]:
    return {
        "collection_id": copy.collection_id,
        "source_store": None,
        "destination_store": copy.store,
        "initiated_by_app": None,
        "initiated_by_key_id": None,
        "state": "completed",
        "requested_at": None,
        "ready_at": copy.last_verified_at,
        "expires_at": None,
        "completed_at": copy.last_verified_at,
        "failure": None,
    }


def _read_hold_days(config: RuntimeConfig) -> int:
    seconds = config.retrieval_restore_hold.total_seconds()
    return max(1, int((seconds + 86_399) // 86_400))
