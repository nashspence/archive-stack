from __future__ import annotations

import logging

from riverhog_protocol.errors import BadRequest, Conflict, InvalidState, NotFound
from riverhog_protocol.paths import PathNormalizationError, normalize_collection_id
from sqlalchemy import or_, select
from sqlalchemy.orm import Session
from time_formats import format_utc_timestamp, utc_now

from riverhog_core.archive_objects import CollectionArchiveFile, load_collection_archive
from riverhog_core.archive_store_registry import ArchiveStoreRegistry
from riverhog_core.catalog_db import make_session_factory, session_scope
from riverhog_core.catalog_models import (
    ArchiveCopyJobRecord,
    CollectionArchiveCopyRecord,
    CollectionFileRecord,
    CollectionMetadataPublicationRecord,
    CollectionProofMaturationRecord,
    CollectionRecord,
)
from riverhog_core.ports.archive_store import (
    ArchiveObjectIdentity,
    CollectionArchiveIdentity,
    CollectionArchiveUploadReceipt,
)
from riverhog_core.proofs import CommandProofVerifier, ProofVerifier
from riverhog_core.runtime_config import RuntimeConfig
from riverhog_core.services.archive_records import (
    apply_archive_receipt,
    archive_copy_identity,
    archive_copy_is_complete,
)
from riverhog_core.services.archive_reporting import record_archive_usage_snapshot
from riverhog_core.services.collection_custody import require_collection_custody_idle

_LOG = logging.getLogger(__name__)


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

    def requeue_interrupted_copies_for_startup(self, *, limit: int = 100) -> int:
        if limit < 1:
            return 0
        current_text = format_utc_timestamp(utc_now())
        with session_scope(self._session_factory) as session:
            jobs = session.scalars(
                select(ArchiveCopyJobRecord)
                .where(ArchiveCopyJobRecord.state == "copying")
                .order_by(ArchiveCopyJobRecord.requested_at)
                .limit(limit)
                .with_for_update()
            ).all()
            for job in jobs:
                job.state = "requested"
                job.next_attempt_at = current_text
            return len(jobs)

    def create_or_resume(
        self,
        collection_id: int,
        *,
        destination_store: str,
        source_store: str | None = None,
    ) -> dict[str, object]:
        normalized_collection_id = _normalize_collection_id(collection_id)
        destination = self._configured_store(destination_store)
        source = self._configured_store(source_store) if source_store is not None else None
        if source == destination:
            raise BadRequest("archive copy source and destination stores must differ")
        destination_archive_store = self._archive_stores.require(destination)
        current_text = format_utc_timestamp(utc_now())
        with session_scope(self._session_factory) as session:
            require_collection_custody_idle(session, normalized_collection_id)
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
                    state="requested",
                    requested_at=current_text,
                    next_attempt_at=current_text,
                )
                session.add(job)
            elif job.state not in {"requested", "waiting", "copying"}:
                job.source_store = source_copy.store
                job.state = "requested"
                job.next_attempt_at = current_text
                job.failure = None
            return _job_payload(job)

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
            source_store = self._archive_stores.require(job.source_store)
            destination = self._archive_stores.require(job.destination_store)
            data_objects = source_identity.data_objects
            if job.read_requested_at is None:
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
                job.read_requested_at = current_text
            else:
                status = source_store.get_archive_objects_read_status(
                    collection_id=collection_id,
                    objects=data_objects,
                    requested_at=job.read_requested_at,
                    estimated_ready_at=job.ready_at,
                    estimated_expires_at=job.expires_at,
                )
            job.ready_at = status.ready_at or job.ready_at
            job.expires_at = status.expires_at or job.expires_at
            if status.state == "expired":
                job.state = "requested"
                job.read_requested_at = None
                job.ready_at = None
                job.expires_at = None
                job.next_attempt_at = current_text
                return
            if status.state != "ready":
                job.state = "waiting"
                job.next_attempt_at = format_utc_timestamp(
                    current + self._config.retrieval_sweep_interval
                )
                return
            job.state = "copying"
            job.next_attempt_at = None
            destination_storage_prefix = job.destination_storage_prefix

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
        receipt = destination.upload_collection_archive(
            collection_id=collection_id,
            archive=archive,
            archive_storage_prefix=destination_storage_prefix,
        )
        destination.verify_collection_archive(
            collection_id=collection_id,
            archive=_receipt_identity(receipt),
        )
        with session_scope(self._session_factory) as session:
            job = session.get(ArchiveCopyJobRecord, (collection_id, destination_store))
            if job is None:
                raise Conflict("archive copy job disappeared during transfer")
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
            session.delete(job)
            record_archive_usage_snapshot(session, config=self._config)
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

    def _record_failure(
        self,
        *,
        collection_id: int,
        destination_store: str,
        exc: Exception,
    ) -> None:
        with session_scope(self._session_factory) as session:
            job = session.get(ArchiveCopyJobRecord, (collection_id, destination_store))
            if job is None:
                return
            job.state = "failed"
            job.next_attempt_at = None
            job.failure = f"{type(exc).__name__}: {exc}"

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
            source_store = self._archive_stores.require(job.source_store)
            try:
                source_store.cleanup_archive_objects_read(
                    collection_id=collection_id,
                    objects=archive_copy_identity(source_copy).data_objects,
                )
            except Exception:
                _LOG.exception(
                    "failed to clean up archive-copy source read: collection=%s source=%s",
                    collection_id,
                    job.source_store,
                )

    def _configured_store(self, value: str) -> str:
        try:
            return self._config.archive_store(value).name
        except ValueError as exc:
            raise BadRequest(str(exc)) from exc


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
        "state": job.state,
        "requested_at": job.requested_at,
        "ready_at": job.ready_at,
        "expires_at": job.expires_at,
        "failure": job.failure,
    }


def _completed_payload(copy: CollectionArchiveCopyRecord) -> dict[str, object]:
    return {
        "collection_id": copy.collection_id,
        "source_store": None,
        "destination_store": copy.store,
        "state": "completed",
        "requested_at": None,
        "ready_at": copy.last_verified_at,
        "expires_at": None,
        "failure": None,
    }


def _read_hold_days(config: RuntimeConfig) -> int:
    return max(1, int(config.retrieval_max_lease.total_seconds() // 86400) + 1)
