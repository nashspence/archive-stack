from __future__ import annotations

import base64
import json
import logging
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import case, func, or_, select, update
from sqlalchemy.orm import Session
from time_formats import format_utc_timestamp, utc_now

from riverhog_core.archive_object_paths import archive_storage_prefix_from_object_path
from riverhog_core.archive_objects import (
    CollectionArchive,
    CollectionArchiveFile,
    build_collection_archive_from_chunk_reader,
    build_collection_archive_from_manifest,
)
from riverhog_core.archive_store_registry import ArchiveStoreRegistry
from riverhog_core.catalog_db import make_session_factory, session_scope
from riverhog_core.catalog_events import record_catalog_event
from riverhog_core.catalog_models import (
    ArchiveCopyRetirementRecord,
    CollectionArchiveCopyRecord,
    CollectionArchiveObjectUploadRecord,
    CollectionDeletionRecord,
    CollectionFileRecord,
    CollectionMetadataPublicationRecord,
    CollectionProofMaturationRecord,
    CollectionRecord,
    CollectionTagRecord,
    CollectionUploadFileRecord,
    CollectionUploadRecord,
    IngressCleanupRecord,
)
from riverhog_core.collection_metadata import (
    collection_content_etag,
    collection_metadata_manifest,
    collection_record_manifest,
)
from riverhog_core.ingress_crypto import iter_ingress_plaintext
from riverhog_core.ports.archive_store import (
    ArchiveObjectUploadReceipt,
    ArchiveStore,
    CollectionArchiveUploadReceipt,
)
from riverhog_core.ports.retrieval_cache import RetrievalCacheReceipt
from riverhog_core.ports.upload_store import UploadStore
from riverhog_core.proofs import CommandProofStamper, ProofStamper
from riverhog_core.runtime_config import RuntimeConfig
from riverhog_core.services.archive_records import (
    apply_archive_receipt,
    record_new_archive_cache_lease,
)
from riverhog_core.services.archive_upload_tracking import (
    SqlAlchemyArchiveMultipartUploadTracker,
)
from riverhog_core.services.collections import (
    _collection_upload_stats,
    _collection_upload_target_path,
)
from riverhog_core.services.lifecycle_events import SqlAlchemyLifecycleEventService

_LOG = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class CollectionUploadFileEntry:
    path: str
    bytes: int
    sha256: str
    target_path: str
    ingress_upload_id: str
    ingress_secret_envelope: str
    ingress_state_json: str


@dataclass(frozen=True, slots=True)
class IngressCleanupEntry:
    target_path: str
    collection_id: int
    ingress_upload_id: str


def _load_collection_upload_object_record(
    session: Session,
    collection_id: int,
    object_id: str,
) -> CollectionArchiveObjectUploadRecord | None:
    return session.get(CollectionArchiveObjectUploadRecord, (collection_id, object_id))


def _record_collection_upload_progress(session: Session, collection_id: int) -> None:
    upload = session.get(CollectionUploadRecord, collection_id)
    if upload is None:
        return
    upload.archive_phase = "uploading"
    upload.archive_phase_updated_at = format_utc_timestamp(utc_now())


class SqlAlchemyArchiveUploadService:
    def __init__(
        self,
        config: RuntimeConfig,
        archive_stores: ArchiveStoreRegistry,
        upload_store: UploadStore | None = None,
        *,
        proof_stamper: ProofStamper | None = None,
    ) -> None:
        self._config = config
        self._archive_stores = archive_stores
        self._upload_store = upload_store
        self._proof_stamper = proof_stamper or CommandProofStamper(config.ots_stamp_command)
        self._session_factory = make_session_factory(config.database_url)
        self._lifecycle_events = SqlAlchemyLifecycleEventService(config)

    def requeue_failed_uploads_for_startup(self, *, limit: int = 100) -> int:
        if limit < 1 or self._upload_store is None:
            return 0

        current_text = format_utc_timestamp(utc_now())
        requeued = 0
        with session_scope(self._session_factory) as session:
            file_stats = (
                select(
                    CollectionUploadFileRecord.collection_id.label("collection_id"),
                    func.count(CollectionUploadFileRecord.path).label("files_total"),
                    func.coalesce(
                        func.sum(
                            case(
                                (
                                    CollectionUploadFileRecord.ingress_uploaded_bytes
                                    < CollectionUploadFileRecord.ingress_bytes,
                                    1,
                                ),
                                else_=0,
                            )
                        ),
                        0,
                    ).label("files_incomplete"),
                )
                .group_by(CollectionUploadFileRecord.collection_id)
                .subquery()
            )
            uploads = list(
                session.scalars(
                    select(CollectionUploadRecord)
                    .join(
                        file_stats,
                        file_stats.c.collection_id == CollectionUploadRecord.collection_id,
                    )
                    .where(
                        CollectionUploadRecord.state == "failed",
                        file_stats.c.files_total > 0,
                        file_stats.c.files_incomplete == 0,
                    )
                    .order_by(
                        CollectionUploadRecord.archive_phase_updated_at,
                        CollectionUploadRecord.collection_id,
                    )
                    .limit(limit)
                )
            )
            for upload in uploads:
                previous_error = upload.archive_failure
                upload.state = "archiving"
                upload.archive_phase = "retry_wait"
                upload.archive_phase_updated_at = current_text
                upload.archive_next_attempt_at = current_text
                requeued += 1
                _LOG.info(
                    "startup requeued failed collection archive upload: collection_id=%s "
                    "previous_error=%s",
                    upload.collection_id,
                    previous_error,
                )
        return requeued

    def requeue_interrupted_ingress_cleanup_for_startup(self) -> int:
        if self._upload_store is None:
            return 0
        current_text = format_utc_timestamp(utc_now())
        with session_scope(self._session_factory) as session:
            result = session.execute(
                update(IngressCleanupRecord)
                .where(IngressCleanupRecord.state == "deleting")
                .values(
                    state="pending",
                    next_attempt_at=current_text,
                    last_error="cleanup interrupted before completion",
                )
            )
            return int(getattr(result, "rowcount", 0) or 0)

    def requeue_interrupted_metadata_publications_for_startup(self) -> int:
        current_text = format_utc_timestamp(utc_now())
        with session_scope(self._session_factory) as session:
            result = session.execute(
                update(CollectionMetadataPublicationRecord)
                .where(CollectionMetadataPublicationRecord.state == "publishing")
                .values(
                    state="pending",
                    next_attempt_at=current_text,
                    failure="publication interrupted before completion",
                )
            )
            return int(getattr(result, "rowcount", 0) or 0)

    def process_due_metadata_publications(self, *, limit: int = 10) -> int:
        if limit < 1:
            return 0
        processed = 0
        for _ in range(limit):
            claimed = self._claim_due_metadata_publication()
            if claimed is None:
                break
            collection_id, store, revision = claimed
            self._publish_collection_metadata(
                collection_id=collection_id,
                store=store,
                revision=revision,
            )
            processed += 1
        return processed

    def _claim_due_metadata_publication(self) -> tuple[int, str, int] | None:
        now = format_utc_timestamp(utc_now())
        with session_scope(self._session_factory) as session:
            candidate = session.execute(
                select(
                    CollectionMetadataPublicationRecord.collection_id,
                    CollectionMetadataPublicationRecord.store,
                )
                .where(
                    CollectionMetadataPublicationRecord.state.in_(("pending", "retry_wait")),
                    CollectionMetadataPublicationRecord.next_attempt_at <= now,
                )
                .order_by(
                    CollectionMetadataPublicationRecord.next_attempt_at,
                    CollectionMetadataPublicationRecord.collection_id,
                    CollectionMetadataPublicationRecord.store,
                )
                .limit(1)
            ).one_or_none()
            if candidate is None:
                return None
            collection = session.scalar(
                select(CollectionRecord)
                .where(CollectionRecord.id == candidate.collection_id)
                .with_for_update(skip_locked=True)
            )
            if collection is None:
                return None
            publication = session.scalar(
                select(CollectionMetadataPublicationRecord)
                .where(
                    CollectionMetadataPublicationRecord.collection_id
                    == candidate.collection_id,
                    CollectionMetadataPublicationRecord.store == candidate.store,
                )
                .with_for_update()
            )
            if (
                publication is None
                or publication.state not in {"pending", "retry_wait"}
                or publication.next_attempt_at > now
            ):
                return None
            if session.get(CollectionDeletionRecord, publication.collection_id) is not None:
                return None
            if (
                session.get(
                    ArchiveCopyRetirementRecord,
                    (publication.collection_id, publication.store),
                )
                is not None
            ):
                return None
            publication.state = "publishing"
            publication.attempt_count += 1
            publication.last_attempt_at = now
            return (
                publication.collection_id,
                publication.store,
                publication.desired_revision,
            )

    def _publish_collection_metadata(
        self,
        *,
        collection_id: int,
        store: str,
        revision: int,
    ) -> None:
        try:
            with session_scope(self._session_factory) as session:
                collection = session.get(CollectionRecord, collection_id)
                copy = session.get(CollectionArchiveCopyRecord, (collection_id, store))
                publication = session.get(
                    CollectionMetadataPublicationRecord,
                    (collection_id, store),
                )
                if collection is None or copy is None or publication is None:
                    return
                if collection.metadata_revision != revision:
                    publication.state = "pending"
                    publication.next_attempt_at = format_utc_timestamp(utc_now())
                    return
                if copy.archive_storage_prefix is None:
                    raise RuntimeError("collection archive has no storage prefix")
                tags = tuple(
                    session.scalars(
                        select(CollectionTagRecord.tag_id)
                        .where(CollectionTagRecord.collection_id == collection_id)
                        .order_by(CollectionTagRecord.tag_id)
                    ).all()
                )
                manifest = collection_metadata_manifest(
                    collection_id=collection_id,
                    content_etag=collection.content_etag,
                    record_etag=collection.record_etag,
                    metadata_revision=collection.metadata_revision,
                    tags=tags,
                    updated_at=collection.metadata_updated_at,
                )
                archive_storage_prefix = copy.archive_storage_prefix

            receipt = self._archive_stores.require(store).publish_collection_metadata(
                collection_id=collection_id,
                archive_storage_prefix=archive_storage_prefix,
                manifest=manifest,
            )
            with session_scope(self._session_factory) as session:
                publication = session.get(
                    CollectionMetadataPublicationRecord,
                    (collection_id, store),
                )
                if publication is None:
                    return
                publication.published_revision = revision
                publication.object_path = receipt.object_path
                publication.version_id = receipt.version_id
                publication.stored_bytes = receipt.stored_bytes
                publication.stored_sha256 = receipt.stored_sha256
                publication.published_at = receipt.published_at
                publication.failure = None
                if publication.desired_revision == revision:
                    publication.state = "published"
                else:
                    publication.state = "pending"
                    publication.next_attempt_at = format_utc_timestamp(utc_now())
        except Exception as exc:
            _LOG.exception(
                "collection metadata manifest publication failed: collection_id=%s store=%s",
                collection_id,
                store,
            )
            with session_scope(self._session_factory) as session:
                publication = session.get(
                    CollectionMetadataPublicationRecord,
                    (collection_id, store),
                )
                if publication is None:
                    return
                delay = min(3600, 2 ** min(publication.attempt_count, 10))
                publication.state = "retry_wait"
                publication.next_attempt_at = format_utc_timestamp(
                    utc_now() + timedelta(seconds=delay)
                )
                publication.failure = str(exc)[:1000]

    def ingress_cleanup_status(self) -> dict[str, object]:
        with session_scope(self._session_factory) as session:
            counts = {
                str(state): int(count)
                for state, count in session.execute(
                    select(IngressCleanupRecord.state, func.count()).group_by(
                        IngressCleanupRecord.state
                    )
                )
            }
            oldest_created_at = session.scalar(select(func.min(IngressCleanupRecord.created_at)))
        return {
            "total": sum(counts.values()),
            "pending": counts.get("pending", 0),
            "deleting": counts.get("deleting", 0),
            "failed": counts.get("failed", 0),
            "oldest_created_at": oldest_created_at,
        }

    def process_due_ingress_cleanup(self, *, limit: int = 100) -> int:
        if limit < 1 or self._upload_store is None:
            return 0
        current_text = format_utc_timestamp(utc_now())
        with session_scope(self._session_factory) as session:
            records = list(
                session.scalars(
                    select(IngressCleanupRecord)
                    .where(
                        IngressCleanupRecord.state.in_(("pending", "failed")),
                        IngressCleanupRecord.next_attempt_at <= current_text,
                    )
                    .order_by(
                        IngressCleanupRecord.next_attempt_at,
                        IngressCleanupRecord.target_path,
                    )
                    .limit(limit)
                    .with_for_update(skip_locked=True)
                )
            )
            entries = [
                IngressCleanupEntry(
                    target_path=record.target_path,
                    collection_id=record.collection_id,
                    ingress_upload_id=record.ingress_upload_id,
                )
                for record in records
            ]
            for record in records:
                record.state = "deleting"
                record.attempt_count = int(record.attempt_count or 0) + 1
                record.last_attempt_at = current_text
                record.last_error = None

        if not entries:
            return 0
        with ThreadPoolExecutor(
            max_workers=min(self._config.ingress_cleanup_concurrency, len(entries)),
            thread_name_prefix="riverhog-ingress-cleanup",
        ) as executor:
            list(executor.map(self._process_ingress_cleanup_entry, entries))
        return len(entries)

    def _process_ingress_cleanup_entry(self, entry: IngressCleanupEntry) -> None:
        upload_store = self._upload_store
        if upload_store is None:
            return
        try:
            with session_scope(self._session_factory) as session:
                active_owner = session.scalar(
                    select(CollectionUploadFileRecord.collection_id)
                    .where(CollectionUploadFileRecord.ingress_upload_id == entry.ingress_upload_id)
                    .limit(1)
                )
            if active_owner is not None:
                raise RuntimeError(
                    "ingress upload target is still owned by an active collection upload"
                )
            upload_store.delete_target(entry.target_path)
        except Exception as exc:
            retry_at = format_utc_timestamp(utc_now() + self._config.ingress_cleanup_retry_delay)
            with session_scope(self._session_factory) as session:
                record = session.get(IngressCleanupRecord, entry.target_path)
                if record is not None:
                    record.state = "failed"
                    record.next_attempt_at = retry_at
                    record.last_error = _error_text(exc)
            _LOG.warning(
                "ingress cleanup failed; retry scheduled: collection_id=%s retry_at=%s error=%s",
                entry.collection_id,
                retry_at,
                _error_text(exc),
            )
            return

        with session_scope(self._session_factory) as session:
            record = session.get(IngressCleanupRecord, entry.target_path)
            if record is not None:
                session.delete(record)

    def abort_incomplete_multipart_uploads(
        self,
        *,
        initiated_before: datetime,
    ) -> int:
        aborted = 0
        for store_name, archive_store in self._archive_stores.items():
            try:
                store_aborted = archive_store.abort_incomplete_multipart_uploads(
                    initiated_before=initiated_before
                )
            except Exception:
                _LOG.exception(
                    "incomplete archive multipart upload sweep failed: store=%s",
                    store_name,
                )
                continue
            aborted += store_aborted
            if store_aborted:
                _LOG.warning(
                    "aborted incomplete archive multipart uploads: store=%s count=%s",
                    store_name,
                    store_aborted,
                )
        return aborted

    def process_due_uploads(self, *, limit: int = 1) -> int:
        if limit < 1:
            return 0

        current = utc_now()
        current_text = format_utc_timestamp(current)
        with session_scope(self._session_factory) as session:
            collection_ids: list[int] = []
            if self._upload_store is not None:
                collection_ids = list(
                    session.scalars(
                        select(CollectionUploadRecord.collection_id)
                        .where(CollectionUploadRecord.state == "archiving")
                        .where(
                            or_(
                                CollectionUploadRecord.archive_next_attempt_at.is_(None),
                                CollectionUploadRecord.archive_next_attempt_at <= current_text,
                            )
                        )
                        .order_by(
                            case(
                                (CollectionUploadRecord.archive_receipt_json.is_not(None), 0),
                                (CollectionUploadRecord.archive_phase == "uploading", 2),
                                (CollectionUploadRecord.archive_phase == "planned", 3),
                                (CollectionUploadRecord.archive_phase == "planning", 4),
                                else_=5,
                            ),
                            CollectionUploadRecord.archive_next_attempt_at,
                            CollectionUploadRecord.collection_id,
                        )
                        .limit(limit)
                    ).all()
                )

        attempted = 0
        for collection_id in collection_ids:
            if attempted >= limit:
                return attempted
            self._process_one_collection(collection_id=collection_id)
            attempted += 1
        return attempted

    def _process_one_collection(self, *, collection_id: int) -> None:
        if self._upload_store is None:
            return
        upload_store = self._upload_store
        current = utc_now()
        current_text = format_utc_timestamp(current)
        receipt: CollectionArchiveUploadReceipt | None = None
        manifest_bytes: bytes | None = None
        proof_bytes: bytes | None = None
        archive: CollectionArchive | None = None
        with session_scope(self._session_factory) as session:
            upload = session.get(CollectionUploadRecord, collection_id)
            if upload is None or upload.state != "archiving":
                return
            archive_store = self._archive_stores.require(upload.archive_store)
            archive_store_name = upload.archive_store
            upload_stats = _collection_upload_stats(session, collection_id)
            if (
                upload_stats["files_total"] == 0
                or upload_stats["files_uploaded"] != upload_stats["files_total"]
            ):
                upload.state = "uploading"
                upload.archive_next_attempt_at = None
                return
            receipt = _archive_receipt_from_json(upload.archive_receipt_json)
            manifest_bytes = _decode_b64(upload.collection_manifest_bytes_b64)
            proof_bytes = _decode_b64(upload.collection_manifest_proof_bytes_b64)
            archive_storage_prefix = _ensure_archive_storage_prefix(
                session,
                upload=upload,
                archive_store=archive_store,
            )
            upload.archive_attempt_count = int(upload.archive_attempt_count or 0) + 1
            upload.archive_last_attempt_at = current_text
            upload.archive_next_attempt_at = current_text
            if receipt is not None:
                upload.archive_phase = "finalizing"
            elif manifest_bytes is not None and proof_bytes is not None:
                upload.archive_phase = "planned"
            else:
                upload.archive_phase = "planning"
            upload.archive_phase_updated_at = current_text
            upload.archive_failure = None
            sorted_files = session.scalars(
                select(CollectionUploadFileRecord)
                .where(CollectionUploadFileRecord.collection_id == collection_id)
                .order_by(
                    CollectionUploadFileRecord.file_order,
                    CollectionUploadFileRecord.path,
                )
            ).all()
            upload_files = [
                CollectionUploadFileEntry(
                    path=file_record.path,
                    bytes=file_record.bytes,
                    sha256=file_record.sha256,
                    target_path=_collection_upload_target_path(file_record),
                    ingress_upload_id=file_record.ingress_upload_id,
                    ingress_secret_envelope=file_record.ingress_secret_envelope,
                    ingress_state_json=file_record.ingress_state_json,
                )
                for file_record in sorted_files
            ]
            upload_file_count = upload_stats["files_total"]
            upload_byte_count = upload_stats["bytes_total"]

        try:
            archive_details = {
                "files_total": upload_file_count,
                "files_uploaded": upload_file_count,
                "bytes_total": upload_byte_count,
                "uploaded_bytes": upload_byte_count,
            }
            target_path_by_archive_path: dict[str, str] = {}
            archive_files: list[CollectionArchiveFile] = []
            entries_by_path = {entry.path: entry for entry in upload_files}
            for entry in upload_files:
                target_path_by_archive_path[entry.path] = entry.target_path
                archive_files.append(
                    CollectionArchiveFile(
                        path=entry.path,
                        bytes=entry.bytes,
                        sha256=entry.sha256,
                    )
                )

            def _read_archive_file_chunks(
                path: str,
                offset: int = 0,
                size: int | None = None,
            ) -> Iterator[bytes]:
                entry = entries_by_path[path]
                return iter_ingress_plaintext(
                    self._config,
                    upload_store,
                    target_path=target_path_by_archive_path[path],
                    collection_id=collection_id,
                    path=path,
                    plaintext_bytes=entry.bytes,
                    secret_envelope=entry.ingress_secret_envelope,
                    state_json=entry.ingress_state_json,
                    offset=offset,
                    size=size,
                )

            if manifest_bytes is not None and proof_bytes is not None:
                archive = build_collection_archive_from_manifest(
                    collection_id=collection_id,
                    files=archive_files,
                    read_file_chunks=lambda path: _read_archive_file_chunks(path),
                    read_file_chunks_range=_read_archive_file_chunks,
                    manifest_bytes=manifest_bytes,
                    proof_bytes=proof_bytes,
                )
            else:
                _LOG.info(
                    "building collection archive for %s: files=%s payload_bytes=%s",
                    collection_id,
                    upload_file_count,
                    upload_byte_count,
                )
                archive = build_collection_archive_from_chunk_reader(
                    collection_id=collection_id,
                    files=archive_files,
                    read_file_chunks=lambda path: _read_archive_file_chunks(path),
                    read_file_chunks_range=_read_archive_file_chunks,
                    max_plaintext_object_bytes=archive_store.max_plaintext_object_bytes(),
                    stamper=self._proof_stamper,
                )
                _LOG.info(
                    "collection archive built for %s: files=%s payload_bytes=%s objects=%s",
                    collection_id,
                    upload_file_count,
                    upload_byte_count,
                    len(archive.data_objects),
                )
                manifest_bytes = archive.manifest_bytes
                proof_bytes = archive.proof_bytes
                self._record_planned_archive(
                    collection_id=collection_id,
                    archive=archive,
                    manifest_bytes=manifest_bytes,
                    proof_bytes=proof_bytes,
                )
            if receipt is None:
                self._record_archive_phase(
                    collection_id=collection_id,
                    phase="uploading",
                    updated_at=format_utc_timestamp(utc_now()),
                )
                _LOG.info(
                    "uploading collection archive for %s: objects=%s",
                    collection_id,
                    len(archive.data_objects),
                )
                receipt = archive_store.upload_collection_archive(
                    collection_id=collection_id,
                    archive=archive,
                    archive_storage_prefix=archive_storage_prefix,
                    multipart_tracker=SqlAlchemyArchiveMultipartUploadTracker(
                        self._session_factory,
                        load_record=_load_collection_upload_object_record,
                        record_progress=_record_collection_upload_progress,
                    ),
                )
                self._record_completed_archive(
                    collection_id=collection_id,
                    receipt=receipt,
                    manifest_bytes=manifest_bytes,
                    proof_bytes=proof_bytes,
                )
            if manifest_bytes is None or proof_bytes is None:
                raise RuntimeError("collection archive artifacts were not recorded")
        except Exception as exc:
            error = _error_text(exc)
            if _archive_failure_is_retryable(exc):
                _LOG.exception(
                    "collection archive planning/upload failed for %s; scheduling retry: %s",
                    collection_id,
                    error,
                )
                self._record_collection_failure(
                    collection_id=collection_id,
                    error=error,
                    retryable=True,
                )
            else:
                _LOG.exception(
                    "collection archive planning/upload failed permanently for %s: %s",
                    collection_id,
                    error,
                )
                self._record_collection_failure(
                    collection_id=collection_id,
                    error=error,
                    retryable=False,
                )
            return

        try:
            if receipt is None:
                raise RuntimeError("collection archive receipt was not recorded")
            self._finalize_archived_collection(
                collection_id=collection_id,
                receipt=receipt,
                archive=archive,
                upload_files=upload_files,
                event_details={
                    **archive_details,
                    "archive_storage_prefix": archive_storage_prefix,
                    "archive_objects": len(receipt.objects),
                    "archive_total_bytes": sum(current.stored_bytes for current in receipt.objects),
                    "archive_store": archive_store_name,
                },
            )
        except Exception as exc:
            error = _error_text(exc)
            if _archive_failure_is_retryable(exc):
                _LOG.exception(
                    "collection archive finalization failed for %s; scheduling retry: %s",
                    collection_id,
                    error,
                )
                self._record_collection_failure(
                    collection_id=collection_id,
                    error=error,
                    retryable=True,
                )
            else:
                _LOG.exception(
                    "collection archive finalization failed permanently for %s: %s",
                    collection_id,
                    error,
                )
                self._record_collection_failure(
                    collection_id=collection_id,
                    error=error,
                    retryable=False,
                )
            return

    def _finalize_archived_collection(
        self,
        *,
        collection_id: int,
        receipt: CollectionArchiveUploadReceipt,
        archive: CollectionArchive,
        upload_files: list[CollectionUploadFileEntry],
        event_details: dict[str, object],
    ) -> None:
        with session_scope(self._session_factory) as session:
            upload = session.get(CollectionUploadRecord, collection_id)
            if upload is None:
                return
            upload.archive_phase = "finalizing"
            upload.archive_phase_updated_at = format_utc_timestamp(utc_now())
            session.flush()
            file_entries = [(row.path, row.bytes, row.sha256) for row in upload_files]
            tags = tuple(json.loads(upload.tags_json))
            content_etag = collection_content_etag(file_entries)
            now = format_utc_timestamp(utc_now())
            _manifest, record_etag = collection_record_manifest(
                collection_id=collection_id,
                content_etag=content_etag,
                metadata_revision=1,
                tags=tags,
                files=file_entries,
            )
            collection = session.get(CollectionRecord, collection_id)
            if collection is None:
                collection = CollectionRecord(
                    id=collection_id,
                    creation_idempotency_key=upload.idempotency_key,
                    content_etag=content_etag,
                    record_etag=record_etag,
                    metadata_revision=1,
                    metadata_updated_at=now,
                    ingest_source=upload.ingest_source,
                    created_by_app=upload.initiated_by_app,
                    created_by_key_id=upload.initiated_by_key_id,
                    created_at=upload.opened_at or now,
                )
                session.add(collection)
                session.flush()
                for tag in tags:
                    collection.tags.append(
                        CollectionTagRecord(
                            collection_id=collection_id,
                            tag_id=tag,
                            assigned_by_app=upload.initiated_by_app,
                            assigned_by_key_id=upload.initiated_by_key_id,
                            assigned_at=now,
                        )
                    )
            elif collection.content_etag != content_etag:
                raise RuntimeError("immutable collection content changed")
            existing_paths = {file_record.path for file_record in collection.files}
            for entry in upload_files:
                if entry.path in existing_paths:
                    continue
                collection.files.append(
                    CollectionFileRecord(
                        collection_id=collection_id,
                        path=entry.path,
                        bytes=entry.bytes,
                        sha256=entry.sha256,
                    )
                )
            copy = session.get(
                CollectionArchiveCopyRecord,
                (collection_id, upload.archive_store),
            )
            if copy is None:
                copy = CollectionArchiveCopyRecord(
                    collection_id=collection_id,
                    store=upload.archive_store,
                )
                session.add(copy)
            apply_archive_receipt(copy, receipt, archive)
            session.flush()
            session.merge(
                CollectionMetadataPublicationRecord(
                    collection_id=collection_id,
                    store=upload.archive_store,
                    desired_revision=collection.metadata_revision,
                    state="pending",
                    attempt_count=0,
                    next_attempt_at=now,
                )
            )
            session.merge(
                CollectionProofMaturationRecord(
                    collection_id=collection_id,
                    store=upload.archive_store,
                    state="pending",
                    attempt_count=0,
                    next_attempt_at=now,
                )
            )
            record_new_archive_cache_lease(
                session,
                collection_id=collection_id,
                store=upload.archive_store,
                receipt=receipt,
                lease=self._config.retrieval_cache_new_archive_lease,
            )
            record_catalog_event(
                session,
                change="created",
                collection_id=collection_id,
                occurred_at=now,
                record_etag=record_etag,
                before_tags=(),
                after_tags=tags,
            )
            self._lifecycle_events.emit_collection(
                type="collection.finalized",
                collection_id=collection_id,
                details=event_details,
                terminal=True,
                session=session,
            )
            cleanup_created_at = format_utc_timestamp(utc_now())
            for entry in upload_files:
                session.add(
                    IngressCleanupRecord(
                        target_path=entry.target_path,
                        collection_id=collection_id,
                        ingress_upload_id=entry.ingress_upload_id,
                        state="pending",
                        attempt_count=0,
                        created_at=cleanup_created_at,
                        next_attempt_at=cleanup_created_at,
                    )
                )
            session.delete(upload)

    def _record_completed_archive(
        self,
        *,
        collection_id: int,
        receipt: CollectionArchiveUploadReceipt,
        manifest_bytes: bytes,
        proof_bytes: bytes,
    ) -> None:
        current_text = format_utc_timestamp(utc_now())
        with session_scope(self._session_factory) as session:
            upload = session.get(CollectionUploadRecord, collection_id)
            if upload is None:
                return
            upload.archive_receipt_json = _archive_receipt_to_json(receipt)
            upload.collection_manifest_bytes_b64 = _encode_b64(manifest_bytes)
            upload.collection_manifest_proof_bytes_b64 = _encode_b64(proof_bytes)
            upload.archive_storage_prefix = archive_storage_prefix_from_object_path(
                receipt.require_object("manifest").object_path
            )
            upload.archive_phase = "finalizing"
            upload.archive_phase_updated_at = current_text
            upload.archive_failure = None

    def _record_planned_archive(
        self,
        *,
        collection_id: int,
        archive: CollectionArchive,
        manifest_bytes: bytes,
        proof_bytes: bytes,
    ) -> None:
        current_text = format_utc_timestamp(utc_now())
        with session_scope(self._session_factory) as session:
            upload = session.get(CollectionUploadRecord, collection_id)
            if upload is None:
                return
            upload.collection_manifest_bytes_b64 = _encode_b64(manifest_bytes)
            upload.collection_manifest_proof_bytes_b64 = _encode_b64(proof_bytes)
            upload.archive_objects.clear()
            for current in archive.data_objects:
                upload.archive_objects.append(
                    CollectionArchiveObjectUploadRecord(
                        collection_id=collection_id,
                        object_id=current.object_id,
                        kind=current.kind,
                        object_path="",
                        plaintext_bytes=current.plaintext_bytes,
                        sha256=current.sha256,
                    )
                )
            upload.archive_phase = "planned"
            upload.archive_phase_updated_at = current_text
            upload.archive_failure = None

    def _record_collection_failure(
        self,
        *,
        collection_id: int,
        error: str,
        retryable: bool,
    ) -> None:
        current = utc_now()
        current_text = format_utc_timestamp(current)
        emit_event = False
        attempt_count = 0
        next_retry_at = (
            format_utc_timestamp(current + self._config.archive_upload_retry_delay)
            if retryable
            else None
        )

        with session_scope(self._session_factory) as session:
            upload = session.get(CollectionUploadRecord, collection_id)
            if upload is None:
                return
            attempt_count = int(upload.archive_attempt_count or 0)
            previous_state = upload.state
            previous_phase = upload.archive_phase
            previous_failure = upload.archive_failure
            upload.archive_failure = error
            upload.archive_next_attempt_at = next_retry_at
            upload.archive_phase = "retry_wait" if retryable else "failed"
            upload.archive_phase_updated_at = current_text
            upload.state = "archiving" if retryable else "failed"
            if retryable:
                emit_event = True
            elif (
                previous_state != "failed"
                or previous_phase != "failed"
                or previous_failure != error
            ):
                emit_event = True

        if retryable:
            _LOG.warning(
                "collection archive retry scheduled for %s: attempts=%s next_retry_at=%s error=%s",
                collection_id,
                attempt_count,
                next_retry_at,
                error,
            )
        else:
            _LOG.error(
                "collection archive marked failed for %s: attempts=%s error=%s",
                collection_id,
                attempt_count,
                error,
            )

        if retryable and emit_event:
            self._emit_collection_archive_retry_scheduled(
                collection_id=collection_id,
                attempt_count=attempt_count,
                error=error,
                failed_at=current_text,
                next_retry_at=next_retry_at or "",
            )
        if not retryable and emit_event:
            self._emit_collection_archive_failed(
                collection_id=collection_id,
                attempt_count=attempt_count,
                error=error,
                failed_at=current_text,
            )

    def _emit_collection_archive_retry_scheduled(
        self,
        *,
        collection_id: int,
        attempt_count: int,
        error: str,
        failed_at: str,
        next_retry_at: str,
    ) -> None:
        self._lifecycle_events.emit_collection(
            type="collection.archive_retry_scheduled",
            collection_id=collection_id,
            details={
                "attempts": attempt_count,
                "failed_at": failed_at,
                "next_retry_at": next_retry_at,
                "retry_delay_seconds": self._config.archive_upload_retry_delay.total_seconds(),
                "error": error,
            },
        )

    def _emit_collection_archive_failed(
        self,
        *,
        collection_id: int,
        attempt_count: int,
        error: str,
        failed_at: str,
    ) -> None:
        self._lifecycle_events.emit_collection(
            type="collection.archive_failed",
            collection_id=collection_id,
            details={
                "attempts": attempt_count,
                "failed_at": failed_at,
                "error": error,
            },
        )

    def _record_archive_phase(
        self,
        *,
        collection_id: int,
        phase: str,
        updated_at: str,
    ) -> None:
        with session_scope(self._session_factory) as session:
            upload = session.get(CollectionUploadRecord, collection_id)
            if upload is None:
                return
            upload.archive_phase = phase
            upload.archive_phase_updated_at = updated_at


def _ensure_archive_storage_prefix(
    session: Session,
    *,
    upload: CollectionUploadRecord,
    archive_store: ArchiveStore,
) -> str:
    if upload.archive_storage_prefix:
        return upload.archive_storage_prefix.strip("/")
    prefix = archive_store.new_collection_archive_storage_prefix()
    upload.archive_storage_prefix = prefix
    return prefix


def _archive_receipt_to_json(receipt: CollectionArchiveUploadReceipt) -> str:
    payload = {"objects": [_archive_upload_receipt_to_payload(item) for item in receipt.objects]}
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def _archive_receipt_from_json(raw: str | None) -> CollectionArchiveUploadReceipt | None:
    if not raw:
        return None
    try:
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            return None
        return CollectionArchiveUploadReceipt(
            objects=tuple(
                _archive_upload_receipt_from_payload(item) for item in payload["objects"]
            ),
        )
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        return None


def _archive_upload_receipt_to_payload(
    receipt: ArchiveObjectUploadReceipt,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "object_id": receipt.object_id,
        "kind": receipt.kind,
        "object_path": receipt.object_path,
        "plaintext_bytes": receipt.plaintext_bytes,
        "stored_bytes": receipt.stored_bytes,
        "sha256": receipt.sha256,
        "stored_sha256": receipt.stored_sha256,
        "backend": receipt.backend,
        "storage_class": receipt.storage_class,
        "uploaded_at": receipt.uploaded_at,
        "verified_at": receipt.verified_at,
    }
    if receipt.retrieval_cache is not None:
        payload["retrieval_cache"] = {
            "object_path": receipt.retrieval_cache.object_path,
            "version_id": receipt.retrieval_cache.version_id,
            "stored_bytes": receipt.retrieval_cache.stored_bytes,
            "stored_sha256": receipt.retrieval_cache.stored_sha256,
            "cached_at": receipt.retrieval_cache.cached_at,
            "verified_at": receipt.retrieval_cache.verified_at,
        }
    return payload


def _archive_upload_receipt_from_payload(payload: object) -> ArchiveObjectUploadReceipt:
    if not isinstance(payload, dict):
        raise ValueError("archive upload receipt payload must be an object")
    cache_payload = payload.get("retrieval_cache")
    retrieval_cache = None
    if isinstance(cache_payload, dict):
        retrieval_cache = RetrievalCacheReceipt(
            object_path=str(cache_payload["object_path"]),
            version_id=(
                str(cache_payload["version_id"])
                if cache_payload.get("version_id") is not None
                else None
            ),
            stored_bytes=int(cache_payload["stored_bytes"]),
            stored_sha256=str(cache_payload["stored_sha256"]),
            cached_at=str(cache_payload["cached_at"]),
            verified_at=str(cache_payload["verified_at"]),
        )
    return ArchiveObjectUploadReceipt(
        object_id=str(payload["object_id"]),
        kind=str(payload["kind"]),
        object_path=str(payload["object_path"]),
        plaintext_bytes=int(payload["plaintext_bytes"]),
        stored_bytes=int(payload["stored_bytes"]),
        sha256=str(payload["sha256"]),
        stored_sha256=str(payload["stored_sha256"]),
        backend=str(payload["backend"]),
        storage_class=str(payload["storage_class"]),
        uploaded_at=str(payload["uploaded_at"]),
        verified_at=str(payload["verified_at"]) if payload.get("verified_at") is not None else None,
        retrieval_cache=retrieval_cache,
    )


def _encode_b64(content: bytes) -> str:
    return base64.b64encode(content).decode("ascii")


def _decode_b64(raw: str | None) -> bytes | None:
    if raw is None:
        return None
    try:
        return base64.b64decode(raw.encode("ascii"), validate=True)
    except (ValueError, UnicodeEncodeError):
        return None


def _positive_int_or_none(value: int | None) -> int | None:
    if value is None or value <= 0:
        return None
    return int(value)


def enqueue_collection_archive_upload(
    session: Session,
    *,
    collection_id: int,
    next_attempt_at: str,
) -> None:
    upload = session.get(CollectionUploadRecord, collection_id)
    if upload is None:
        return
    upload.state = "archiving"
    upload.archive_next_attempt_at = next_attempt_at


def _error_text(exc: Exception) -> str:
    detail = str(exc).strip()
    return detail or exc.__class__.__name__


def _archive_failure_is_retryable(exc: Exception) -> bool:
    if isinstance(exc, ValueError):
        return False
    return True
