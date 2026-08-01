from __future__ import annotations

import logging

from riverhog_protocol.errors import BadRequest, Conflict, InvalidState, NotFound
from riverhog_protocol.paths import PathNormalizationError, normalize_collection_id
from sqlalchemy import String, cast, delete, func, or_, select
from sqlalchemy.orm import Session
from time_formats import format_utc_timestamp, utc_now

from riverhog_core.app_permissions import ARCHIVES_MANAGE, ApplicationPrincipal
from riverhog_core.archive_objects import (
    CollectionArchive,
    CollectionArchiveFile,
    load_collection_archive,
)
from riverhog_core.archive_store_registry import ArchiveStoreRegistry
from riverhog_core.catalog_db import make_session_factory, session_scope
from riverhog_core.catalog_models import (
    ArchiveCopyJobRecord,
    ArchiveCopyObjectUploadRecord,
    CollectionArchiveCopyRecord,
    CollectionFileRecord,
    CollectionMetadataPublicationRecord,
    CollectionProofMaturationRecord,
    CollectionRecord,
)
from riverhog_core.collection_access import collection_access_filter
from riverhog_core.ports.archive_store import (
    ArchiveObjectIdentity,
    CollectionArchiveIdentity,
    CollectionArchiveUploadReceipt,
)
from riverhog_core.proofs import CommandProofVerifier, ProofVerifier
from riverhog_core.runtime_config import RuntimeConfig
from riverhog_core.services.archive_copy_states import (
    ARCHIVE_COPY_STATES,
    ARCHIVE_COPY_TRANSFER_STATES,
)
from riverhog_core.services.archive_records import (
    apply_archive_receipt,
    archive_copy_identity,
    archive_copy_is_complete,
    record_new_archive_cache_lease,
)
from riverhog_core.services.archive_upload_tracking import (
    SqlAlchemyArchiveMultipartUploadTracker,
)
from riverhog_core.services.collection_mutations import require_collection_archive_idle
from riverhog_core.services.lifecycle_events import (
    SqlAlchemyLifecycleEventService,
    event_context_json,
)

_LOG = logging.getLogger(__name__)
_SORT_FIELDS = {
    "collection_id",
    "source_store",
    "destination_store",
    "state",
    "requested_at",
}


class SqlAlchemyArchiveCopyService:
    def __init__(
        self,
        config: RuntimeConfig,
        archive_stores: ArchiveStoreRegistry,
        *,
        proof_verifier: ProofVerifier | None = None,
    ) -> None:
        self._config = config
        self._archive_stores = archive_stores
        self._proof_verifier = proof_verifier or CommandProofVerifier(config.ots_verify_command)
        self._session_factory = make_session_factory(config.database_url)
        self._lifecycle_events = SqlAlchemyLifecycleEventService(config)

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
        destination_archive_store = self._archive_stores.require(destination)
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
            data_objects = source_identity.data_objects
            read_requested_at = job.read_requested_at
            ready_at = job.ready_at
            expires_at = job.expires_at
            destination_storage_prefix = job.destination_storage_prefix
            job.state = "checking"
            job.next_attempt_at = None

        source_store = self._archive_stores.require(source_store_name)
        destination = self._archive_stores.require(destination_store_name)
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
                    current + self._config.retrieval_sweep_interval
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

        source_store.verify_collection_archive(
            collection_id=collection_id,
            archive=source_identity,
        )
        manifest_identity = source_identity.require_object("manifest")
        proof_identity = source_identity.require_object("proof")
        manifest_bytes = b"".join(
            source_store.iter_archive_object(
                collection_id=collection_id,
                object=manifest_identity,
            )
        )
        proof_bytes = b"".join(
            source_store.iter_archive_object(
                collection_id=collection_id,
                object=proof_identity,
            )
        )
        expected_files = self._expected_files(collection_id)
        data_by_id = {current.object_id: current for current in data_objects}
        archive = load_collection_archive(
            collection_id=collection_id,
            files=expected_files,
            manifest_bytes=manifest_bytes,
            proof_bytes=proof_bytes,
            read_object_chunks=lambda object_id: source_store.iter_archive_object(
                collection_id=collection_id,
                object=data_by_id[object_id],
            ),
            verifier=self._proof_verifier,
        )
        self._plan_destination_upload(
            collection_id=collection_id,
            destination_store=destination_store,
            archive=archive,
        )
        receipt = destination.upload_collection_archive(
            collection_id=collection_id,
            archive=archive,
            archive_storage_prefix=destination_storage_prefix,
            multipart_tracker=self._destination_upload_tracker(destination_store),
        )
        destination.verify_collection_archive(
            collection_id=collection_id,
            archive=_receipt_identity(receipt),
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
            copy = session.get(
                CollectionArchiveCopyRecord,
                (collection_id, destination_store),
            )
            if copy is None:
                copy = CollectionArchiveCopyRecord(
                    collection_id=collection_id,
                    store=destination_store,
                )
                session.add(copy)
            apply_archive_receipt(copy, receipt, archive)
            session.flush()
            record_new_archive_cache_lease(
                session,
                collection_id=collection_id,
                store=destination_store,
                receipt=receipt,
                lease=self._config.retrieval_cache_new_archive_lease,
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
                    state="pending",
                    attempt_count=0,
                    next_attempt_at=format_utc_timestamp(utc_now()),
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

    def _expected_files(self, collection_id: int) -> tuple[CollectionArchiveFile, ...]:
        with session_scope(self._session_factory) as session:
            files = session.scalars(
                select(CollectionFileRecord)
                .where(CollectionFileRecord.collection_id == collection_id)
                .order_by(CollectionFileRecord.path)
            ).all()
        return tuple(
            CollectionArchiveFile(path=file.path, bytes=file.bytes, sha256=file.sha256)
            for file in files
        )

    def _plan_destination_upload(
        self,
        *,
        collection_id: int,
        destination_store: str,
        archive: CollectionArchive,
    ) -> None:
        expected = {current.object_id: current for current in archive.data_objects}
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
                            object_path="",
                            plaintext_bytes=current.plaintext_bytes,
                            sha256=current.sha256,
                        )
                    )
                    continue
                if (
                    record.kind != current.kind
                    or record.plaintext_bytes != current.plaintext_bytes
                    or record.sha256 != current.sha256
                ):
                    raise Conflict("archive copy upload checkpoint does not match its manifest")

    def _destination_upload_tracker(
        self,
        destination_store: str,
    ) -> SqlAlchemyArchiveMultipartUploadTracker:
        def load_record(
            session: Session,
            collection_id: int,
            object_id: str,
        ) -> ArchiveCopyObjectUploadRecord | None:
            return session.get(
                ArchiveCopyObjectUploadRecord,
                (collection_id, destination_store, object_id),
            )

        def require_active(session: Session, collection_id: int) -> None:
            job = session.get(ArchiveCopyJobRecord, (collection_id, destination_store))
            if job is None or job.state != "copying":
                raise Conflict("archive copy was canceled during transfer")

        return SqlAlchemyArchiveMultipartUploadTracker(
            self._session_factory,
            load_record=load_record,
            require_active=require_active,
        )

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
            data_objects = archive_copy_identity(source_copy).data_objects
        source_store = self._archive_stores.require(source_store_name)
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
            self._archive_stores.require(destination_store).discard_collection_archive_upload(
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


def _receipt_identity(receipt: CollectionArchiveUploadReceipt) -> CollectionArchiveIdentity:
    return CollectionArchiveIdentity(
        objects=tuple(
            ArchiveObjectIdentity(
                object_id=current.object_id,
                kind=current.kind,
                object_path=current.object_path,
                plaintext_bytes=current.plaintext_bytes,
                stored_bytes=current.stored_bytes,
                sha256=current.sha256,
                stored_sha256=current.stored_sha256,
            )
            for current in receipt.objects
        )
    )


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
    return max(1, int(config.retrieval_max_lease.total_seconds() // 86400) + 1)
