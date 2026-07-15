from __future__ import annotations

import base64
import hashlib
import json
import logging
from collections.abc import Iterator
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from datetime import datetime

from sqlalchemy import case, func, or_, select
from sqlalchemy.orm import Session, sessionmaker

from riverhog_core.archive_object_paths import archive_storage_prefix_from_object_path
from riverhog_core.archive_store_registry import ArchiveStoreRegistry
from riverhog_core.catalog_db import make_session_factory, session_scope
from riverhog_core.catalog_models import (
    CollectionArchiveCopyRecord,
    CollectionFileRecord,
    CollectionRecord,
    CollectionUploadFileRecord,
    CollectionUploadRecord,
)
from riverhog_core.collection_archives import (
    CollectionArchiveExpectedFile,
    CollectionArchivePackage,
    build_collection_archive_package_from_chunk_reader,
    build_collection_archive_package_from_prebuilt_artifacts,
    collection_archive_size,
)
from riverhog_core.operator_reminders import operator_reminder_due
from riverhog_core.ports.archive_store import (
    ArchiveMultipartUploadedPart,
    ArchiveMultipartUploadState,
    ArchiveMultipartUploadTracker,
    ArchiveStore,
    ArchiveUploadReceipt,
    CollectionArchiveUploadReceipt,
)
from riverhog_core.ports.hot_store import HotStore
from riverhog_core.ports.upload_store import UploadStore
from riverhog_core.proofs import CommandProofStamper, ProofStamper
from riverhog_core.runtime_config import RuntimeConfig
from riverhog_core.services.archive_catalog import publish_archive_restore_catalog
from riverhog_core.services.archive_records import apply_archive_receipt
from riverhog_core.services.archive_reporting import record_archive_usage_snapshot
from riverhog_core.services.collections import (
    _collection_upload_stats,
    _collection_upload_target_path,
)
from riverhog_core.services.notification_routing import (
    decode_collection_notify_json,
    post_collection_operator_webhook,
)
from riverhog_core.webhooks import post_webhook, utcnow

_LOG = logging.getLogger(__name__)

CollectionUploadFileEntry = tuple[str, int, str, str]


class SqlAlchemyArchiveUploadService:
    def __init__(
        self,
        config: RuntimeConfig,
        archive_stores: ArchiveStoreRegistry,
        hot_store: HotStore | None = None,
        upload_store: UploadStore | None = None,
        *,
        proof_stamper: ProofStamper | None = None,
    ) -> None:
        self._config = config
        self._archive_stores = archive_stores
        self._hot_store = hot_store
        self._upload_store = upload_store
        self._proof_stamper = proof_stamper or CommandProofStamper(config.ots_stamp_command)
        self._session_factory = make_session_factory(config.database_url)

    def requeue_failed_uploads_for_startup(self, *, limit: int = 100) -> int:
        if limit < 1 or self._upload_store is None:
            return 0

        current_text = _isoformat_z(utcnow())
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
                                    CollectionUploadFileRecord.uploaded_bytes
                                    < CollectionUploadFileRecord.bytes,
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

    def publish_restore_catalog(self) -> int:
        return self._publish_restore_catalog()

    def process_due_uploads(self, *, limit: int = 1) -> int:
        if limit < 1:
            return 0

        current = utcnow()
        current_text = _isoformat_z(current)
        with session_scope(self._session_factory) as session:
            collection_ids: list[str] = []
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
                                (
                                    CollectionUploadRecord.archive_multipart_uploaded_parts > 0,
                                    1,
                                ),
                                (
                                    CollectionUploadRecord.archive_multipart_uploaded_bytes > 0,
                                    1,
                                ),
                                (
                                    CollectionUploadRecord.archive_multipart_upload_id.is_not(None),
                                    2,
                                ),
                                (CollectionUploadRecord.archive_phase == "uploading", 2),
                                (CollectionUploadRecord.archive_phase == "packaged", 3),
                                (CollectionUploadRecord.archive_phase == "packaging", 4),
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

    def _process_one_collection(self, *, collection_id: str) -> None:
        if self._upload_store is None:
            return
        upload_store = self._upload_store
        current = utcnow()
        current_text = _isoformat_z(current)
        receipt: CollectionArchiveUploadReceipt | None = None
        manifest_bytes: bytes | None = None
        proof_bytes: bytes | None = None
        packaged_archive_sha256: str | None = None
        package: CollectionArchivePackage | None = None
        retain_hot = True
        with session_scope(self._session_factory) as session:
            upload = session.get(CollectionUploadRecord, collection_id)
            if upload is None or upload.state != "archiving":
                return
            retain_hot = upload.retain_hot
            archive_store = self._archive_stores.require(upload.archive_store)
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
            packaged_archive_sha256 = upload.archive_multipart_sha256
            archive_storage_prefix = _ensure_archive_storage_prefix(
                session,
                upload=upload,
                archive_store=archive_store,
            )
            upload.archive_attempt_count = int(upload.archive_attempt_count or 0) + 1
            upload.archive_last_attempt_at = current_text
            upload.archive_next_attempt_at = current_text
            if receipt is not None:
                upload.archive_phase = "materializing_hot" if retain_hot else "finalizing"
            elif (
                manifest_bytes is not None
                and proof_bytes is not None
                and packaged_archive_sha256 is not None
            ):
                upload.archive_phase = "packaged"
            else:
                upload.archive_phase = "packaging"
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
                (
                    file_record.path,
                    file_record.bytes,
                    file_record.sha256,
                    _collection_upload_target_path(collection_id, file_record.path),
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
            package_files: list[CollectionArchiveExpectedFile] = []
            for path, _bytes, sha256, target_path in upload_files:
                target_path_by_archive_path[path] = target_path
                package_files.append(
                    CollectionArchiveExpectedFile(path=path, bytes=_bytes, sha256=sha256)
                )

            def _read_archive_file_chunks(
                path: str,
                offset: int = 0,
                size: int | None = None,
            ) -> Iterator[bytes]:
                return upload_store.iter_target(
                    target_path_by_archive_path[path],
                    offset=offset,
                    size=size,
                )

            if receipt is None or manifest_bytes is None or proof_bytes is None:
                if (
                    manifest_bytes is not None
                    and proof_bytes is not None
                    and packaged_archive_sha256 is not None
                ):
                    package = build_collection_archive_package_from_prebuilt_artifacts(
                        collection_id=collection_id,
                        files=package_files,
                        read_file_chunks=lambda path: _read_archive_file_chunks(path),
                        read_file_chunks_range=_read_archive_file_chunks,
                        manifest_bytes=manifest_bytes,
                        proof_bytes=proof_bytes,
                        archive_size=collection_archive_size(package_files),
                        archive_sha256=packaged_archive_sha256,
                    )
                else:
                    _LOG.info(
                        "building collection archive package for %s: files=%s payload_bytes=%s",
                        collection_id,
                        upload_file_count,
                        upload_byte_count,
                    )
                    package = build_collection_archive_package_from_chunk_reader(
                        collection_id=collection_id,
                        files=package_files,
                        read_file_chunks=lambda path: _read_archive_file_chunks(path),
                        read_file_chunks_range=_read_archive_file_chunks,
                        stamper=self._proof_stamper,
                    )
                    _LOG.info(
                        "collection archive package built for %s: files=%s payload_bytes=%s "
                        "archive_bytes=%s",
                        collection_id,
                        upload_file_count,
                        upload_byte_count,
                        package.archive_size,
                    )
                    manifest_bytes = package.manifest_bytes
                    proof_bytes = package.proof_bytes
                    self._record_packaged_archive(
                        collection_id=collection_id,
                        package=package,
                        manifest_bytes=manifest_bytes,
                        proof_bytes=proof_bytes,
                    )
                manifest_bytes = package.manifest_bytes
                proof_bytes = package.proof_bytes
                if receipt is None:
                    self._record_archive_phase(
                        collection_id=collection_id,
                        phase="uploading",
                        updated_at=_isoformat_z(utcnow()),
                    )
                    _LOG.info(
                        "uploading collection archive package for %s: archive_bytes=%s",
                        collection_id,
                        package.archive_size,
                    )
                    receipt = archive_store.upload_collection_archive_package(
                        collection_id=collection_id,
                        package=package,
                        archive_storage_prefix=archive_storage_prefix,
                        multipart_tracker=_SqlAlchemyArchiveMultipartUploadTracker(
                            self._session_factory
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
                    "collection archive packaging/upload failed for %s; scheduling retry: %s",
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
                    "collection archive packaging/upload failed permanently for %s: %s",
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
            if retain_hot:
                self._materialize_collection_files(
                    collection_id=collection_id,
                    upload_files=upload_files,
                )
            else:
                self._delete_collection_upload_targets(
                    collection_id=collection_id,
                    upload_files=upload_files,
                )
            self._finalize_archived_collection(
                collection_id=collection_id,
                receipt=receipt,
                upload_files=upload_files,
            )
            self._publish_restore_catalog()
            self._post_collection_operator_webhook(
                event="collections.finalized",
                collection_id=collection_id,
                details={
                    **archive_details,
                    "archive_object_path": receipt.archive.object_path,
                    "archive_total_bytes": receipt.archive.stored_bytes,
                    "archive_sha256": receipt.archive_sha256,
                    "archive_store": upload.archive_store,
                    "retain_hot": retain_hot,
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

    def _materialize_one_collection_file(
        self,
        *,
        collection_id: str,
        path: str,
        target_path: str,
        byte_count: int,
        sha256: str,
    ) -> None:
        hot_store = self._hot_store
        upload_store = self._upload_store
        if hot_store is None or upload_store is None:
            raise RuntimeError("hot storage is unavailable for retained-hot upload")
        if _hot_file_matches(
            hot_store,
            collection_id=collection_id,
            path=path,
            expected_bytes=byte_count,
            expected_sha256=sha256,
        ):
            self._mark_collection_upload_file_materialized(collection_id=collection_id, path=path)
            return

        digest = hashlib.sha256()

        def digesting_chunks() -> Iterator[bytes]:
            for chunk in upload_store.iter_target(target_path):
                digest.update(chunk)
                yield chunk

        resumable_put = getattr(hot_store, "put_collection_file_stream_resumable", None)
        if callable(resumable_put):
            resumable_put(
                collection_id,
                path,
                digesting_chunks(),
                content_length=byte_count,
                sha256=sha256,
                multipart_tracker=_SqlAlchemyHotMultipartUploadTracker(
                    self._session_factory,
                    path=path,
                ),
            )
        else:
            hot_store.put_collection_file_stream(
                collection_id,
                path,
                digesting_chunks(),
                content_length=byte_count,
                sha256=sha256,
            )
        if digest.hexdigest() != sha256:
            hot_store.delete_collection_file(collection_id, path)
            raise ValueError(
                f"materialized hot collection file sha256 mismatch: {collection_id}/{path}"
            )
        if not _hot_file_matches(
            hot_store,
            collection_id=collection_id,
            path=path,
            expected_bytes=byte_count,
            expected_sha256=sha256,
        ):
            raise ValueError(
                f"materialized hot collection file metadata mismatch: {collection_id}/{path}"
            )
        self._mark_collection_upload_file_materialized(collection_id=collection_id, path=path)

    def _materialize_collection_files(
        self,
        *,
        collection_id: str,
        upload_files: list[CollectionUploadFileEntry],
    ) -> None:
        remaining = self._unmaterialized_collection_upload_files(
            collection_id=collection_id,
            upload_files=upload_files,
        )
        if not remaining:
            return
        self._record_archive_phase(
            collection_id=collection_id,
            phase="materializing_hot",
            updated_at=_isoformat_z(utcnow()),
        )
        max_workers = min(self._config.hot_materialization_concurrency, len(remaining))
        _LOG.info(
            "materializing collection files in hot storage for %s: files=%s concurrency=%s",
            collection_id,
            len(remaining),
            max_workers,
        )
        if max_workers == 1:
            for path, _bytes, sha256, target_path in remaining:
                self._materialize_collection_file_and_cleanup(
                    collection_id=collection_id,
                    path=path,
                    target_path=target_path,
                    byte_count=_bytes,
                    sha256=sha256,
                )
            return

        executor = ThreadPoolExecutor(
            max_workers=max_workers,
            thread_name_prefix="riverhog-hot-materialize",
        )
        futures: list[Future[None]] = []
        try:
            for path, _bytes, sha256, target_path in remaining:
                futures.append(
                    executor.submit(
                        self._materialize_collection_file_and_cleanup,
                        collection_id=collection_id,
                        path=path,
                        target_path=target_path,
                        byte_count=_bytes,
                        sha256=sha256,
                    )
                )
            for future in as_completed(futures):
                future.result()
        except Exception:
            for future in futures:
                future.cancel()
            raise
        finally:
            executor.shutdown(wait=True, cancel_futures=True)

    def _materialize_collection_file_and_cleanup(
        self,
        *,
        collection_id: str,
        path: str,
        target_path: str,
        byte_count: int,
        sha256: str,
    ) -> None:
        self._materialize_one_collection_file(
            collection_id=collection_id,
            path=path,
            target_path=target_path,
            byte_count=byte_count,
            sha256=sha256,
        )
        self._delete_upload_target(
            collection_id=collection_id,
            target_path=target_path,
        )

    def _delete_collection_upload_targets(
        self,
        *,
        collection_id: str,
        upload_files: list[CollectionUploadFileEntry],
    ) -> None:
        for _path, _bytes, _sha256, target_path in upload_files:
            self._delete_upload_target(
                collection_id=collection_id,
                target_path=target_path,
            )

    def _unmaterialized_collection_upload_files(
        self,
        *,
        collection_id: str,
        upload_files: list[CollectionUploadFileEntry],
    ) -> list[CollectionUploadFileEntry]:
        with session_scope(self._session_factory) as session:
            materialized_paths = set(
                session.scalars(
                    select(CollectionUploadFileRecord.path).where(
                        CollectionUploadFileRecord.collection_id == collection_id,
                        CollectionUploadFileRecord.hot_materialized_at.is_not(None),
                    )
                ).all()
            )
        return [entry for entry in upload_files if entry[0] not in materialized_paths]

    def _delete_upload_target(self, *, collection_id: str, target_path: str) -> None:
        upload_store = self._upload_store
        if upload_store is None:
            return
        try:
            upload_store.delete_target(target_path)
        except Exception:
            _LOG.warning(
                "failed to delete collection upload target for %s: %s",
                collection_id,
                target_path,
                exc_info=True,
            )

    def _mark_collection_upload_file_materialized(self, *, collection_id: str, path: str) -> None:
        with session_scope(self._session_factory) as session:
            upload = session.get(CollectionUploadRecord, collection_id)
            file_record = session.get(CollectionUploadFileRecord, (collection_id, path))
            if upload is None or file_record is None:
                return
            current_text = _isoformat_z(utcnow())
            file_record.hot_materialized_at = current_text
            upload.archive_phase = "materializing_hot"
            upload.archive_phase_updated_at = current_text
            upload.archive_failure = None

    def _finalize_archived_collection(
        self,
        *,
        collection_id: str,
        receipt: CollectionArchiveUploadReceipt,
        upload_files: list[tuple[str, int, str, str]],
    ) -> None:
        with session_scope(self._session_factory) as session:
            upload = session.get(CollectionUploadRecord, collection_id)
            if upload is None:
                return
            upload.archive_phase = "finalizing"
            upload.archive_phase_updated_at = _isoformat_z(utcnow())
            session.flush()
            if upload.retain_hot:
                unmaterialized = [
                    file_record.path
                    for file_record in upload.files
                    if file_record.hot_materialized_at is None
                ]
                if unmaterialized:
                    raise RuntimeError(f"collection has unmaterialized files: {unmaterialized[0]}")
            collection = session.get(CollectionRecord, collection_id)
            if collection is None:
                collection = CollectionRecord(
                    id=collection_id,
                    ingest_source=upload.ingest_source,
                    notify_json=upload.notify_json,
                )
                session.add(collection)
                session.flush()
            elif not collection.notify_json and upload.notify_json:
                collection.notify_json = upload.notify_json
            existing_paths = {file_record.path for file_record in collection.files}
            for path, _bytes, sha256, _target_path in upload_files:
                if path in existing_paths:
                    continue
                collection.files.append(
                    CollectionFileRecord(
                        collection_id=collection_id,
                        path=path,
                        bytes=_bytes,
                        sha256=sha256,
                        hot=upload.retain_hot,
                    )
                )
            archive = session.get(
                CollectionArchiveCopyRecord,
                (collection_id, upload.archive_store),
            )
            if archive is None:
                archive = CollectionArchiveCopyRecord(
                    collection_id=collection_id,
                    store=upload.archive_store,
                )
                session.add(archive)
            apply_archive_receipt(archive, receipt)
            session.delete(upload)
            record_archive_usage_snapshot(session, config=self._config)

    def _record_completed_archive(
        self,
        *,
        collection_id: str,
        receipt: CollectionArchiveUploadReceipt,
        manifest_bytes: bytes,
        proof_bytes: bytes,
    ) -> None:
        current_text = _isoformat_z(utcnow())
        with session_scope(self._session_factory) as session:
            upload = session.get(CollectionUploadRecord, collection_id)
            if upload is None:
                return
            upload.archive_receipt_json = _archive_receipt_to_json(receipt)
            upload.collection_manifest_bytes_b64 = _encode_b64(manifest_bytes)
            upload.collection_manifest_proof_bytes_b64 = _encode_b64(proof_bytes)
            upload.archive_object_path = receipt.archive.object_path
            upload.archive_storage_prefix = archive_storage_prefix_from_object_path(
                receipt.archive.object_path
            )
            upload.archive_multipart_content_length = receipt.archive.stored_bytes
            upload.archive_multipart_sha256 = receipt.archive_sha256
            upload.archive_multipart_uploaded_bytes = receipt.archive.stored_bytes
            upload.archive_multipart_uploaded_parts = upload.archive_multipart_total_parts
            upload.archive_phase = "materializing_hot" if upload.retain_hot else "finalizing"
            upload.archive_phase_updated_at = current_text
            upload.archive_failure = None

    def _publish_restore_catalog(self) -> int:
        published = 0
        for store_name, archive_store in self._archive_stores.items():
            try:
                published += publish_archive_restore_catalog(
                    store_name=store_name,
                    archive_store=archive_store,
                    session_factory=self._session_factory,
                )
            except Exception:
                _LOG.warning(
                    "failed to publish encrypted archive catalog for %s",
                    store_name,
                    exc_info=True,
                )
        return published

    def _record_packaged_archive(
        self,
        *,
        collection_id: str,
        package: CollectionArchivePackage,
        manifest_bytes: bytes,
        proof_bytes: bytes,
    ) -> None:
        current_text = _isoformat_z(utcnow())
        with session_scope(self._session_factory) as session:
            upload = session.get(CollectionUploadRecord, collection_id)
            if upload is None:
                return
            upload.collection_manifest_bytes_b64 = _encode_b64(manifest_bytes)
            upload.collection_manifest_proof_bytes_b64 = _encode_b64(proof_bytes)
            upload.archive_multipart_content_length = package.archive_size
            upload.archive_multipart_sha256 = package.archive_sha256
            upload.archive_multipart_uploaded_bytes = upload.archive_multipart_uploaded_bytes or 0
            upload.archive_multipart_uploaded_parts = upload.archive_multipart_uploaded_parts or 0
            upload.archive_multipart_total_parts = upload.archive_multipart_total_parts or 0
            upload.archive_phase = "packaged"
            upload.archive_phase_updated_at = current_text
            upload.archive_failure = None

    def _post_collection_operator_webhook(
        self,
        *,
        event: str,
        collection_id: str,
        details: dict[str, object] | None = None,
    ) -> None:
        post_collection_operator_webhook(
            config=self._config,
            event=event,
            collection_id=collection_id,
            details=details,
            notify=self._collection_notify_config(collection_id),
            post=post_webhook,
            log=_LOG,
        )

    def _collection_notify_config(self, collection_id: str) -> dict[str, object] | None:
        with session_scope(self._session_factory) as session:
            upload = session.get(CollectionUploadRecord, collection_id)
            if upload is not None:
                raw = upload.notify_json
            else:
                collection = session.get(CollectionRecord, collection_id)
                raw = collection.notify_json if collection is not None else None
        return decode_collection_notify_json(raw, log=_LOG)

    def _record_collection_failure(
        self,
        *,
        collection_id: str,
        error: str,
        retryable: bool,
    ) -> None:
        current = utcnow()
        current_text = _isoformat_z(current)
        notify_operator = False
        attempt_count = 0
        next_retry_at = (
            _isoformat_z(current + self._config.archive_upload_retry_delay) if retryable else None
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
            if retryable and _operator_failure_notification_due(
                upload.archive_last_failure_notification_at,
                current=current,
                config=self._config,
            ):
                upload.archive_last_failure_notification_at = current_text
                notify_operator = True
            if not retryable and (
                previous_state != "failed"
                or previous_phase != "failed"
                or previous_failure != error
            ):
                upload.archive_last_failure_notification_at = current_text
                notify_operator = True

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

        if retryable and notify_operator:
            self._notify_collection_archive_retrying(
                collection_id=collection_id,
                attempt_count=attempt_count,
                error=error,
                failed_at=current_text,
                next_retry_at=next_retry_at or "",
            )
        if not retryable and notify_operator:
            self._notify_collection_archive_failed(
                collection_id=collection_id,
                attempt_count=attempt_count,
                error=error,
                failed_at=current_text,
            )

    def _notify_collection_archive_retrying(
        self,
        *,
        collection_id: str,
        attempt_count: int,
        error: str,
        failed_at: str,
        next_retry_at: str,
    ) -> None:
        self._post_collection_operator_webhook(
            event="collections.archive_retrying",
            collection_id=collection_id,
            details={
                "attempts": attempt_count,
                "failed_at": failed_at,
                "next_retry_at": next_retry_at,
                "retry_delay_seconds": self._config.archive_upload_retry_delay.total_seconds(),
                "error": error,
            },
        )

    def _notify_collection_archive_failed(
        self,
        *,
        collection_id: str,
        attempt_count: int,
        error: str,
        failed_at: str,
    ) -> None:
        self._post_collection_operator_webhook(
            event="collections.archive_failed",
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
        collection_id: str,
        phase: str,
        updated_at: str,
    ) -> None:
        with session_scope(self._session_factory) as session:
            upload = session.get(CollectionUploadRecord, collection_id)
            if upload is None:
                return
            upload.archive_phase = phase
            upload.archive_phase_updated_at = updated_at


class _SqlAlchemyArchiveMultipartUploadTracker(ArchiveMultipartUploadTracker):
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    def load_multipart_upload(
        self,
        *,
        collection_id: str,
        object_path: str,
        part_size: int,
        content_length: int,
        sha256: str,
    ) -> ArchiveMultipartUploadState | None:
        with session_scope(self._session_factory) as session:
            upload = session.get(CollectionUploadRecord, collection_id)
            if upload is None:
                return None
            if not upload.archive_multipart_upload_id:
                return None
            if upload.archive_object_path != object_path:
                return None
            if int(upload.archive_multipart_part_size or 0) != part_size:
                return None
            if int(upload.archive_multipart_content_length or 0) != content_length:
                return None
            if upload.archive_multipart_sha256 != sha256:
                return None
            return ArchiveMultipartUploadState(
                upload_id=upload.archive_multipart_upload_id,
                object_path=object_path,
                part_size=part_size,
                content_length=content_length,
                sha256=sha256,
                total_parts=upload.archive_multipart_total_parts,
                encryption_state_json=upload.archive_encryption_state_json,
                parts=_multipart_parts_from_json(upload.archive_multipart_parts_json),
            )

    def save_multipart_upload(
        self,
        *,
        collection_id: str,
        state: ArchiveMultipartUploadState,
    ) -> None:
        with session_scope(self._session_factory) as session:
            upload = session.get(CollectionUploadRecord, collection_id)
            if upload is None:
                return
            upload.archive_object_path = state.object_path
            upload.archive_multipart_upload_id = state.upload_id
            upload.archive_multipart_part_size = state.part_size
            upload.archive_multipart_content_length = state.content_length
            upload.archive_multipart_sha256 = state.sha256
            upload.archive_encryption_state_json = state.encryption_state_json
            upload.archive_multipart_parts_json = _multipart_parts_to_json(())
            upload.archive_multipart_uploaded_bytes = 0
            upload.archive_multipart_uploaded_parts = 0
            upload.archive_multipart_total_parts = state.total_parts or max(
                1,
                (state.content_length + state.part_size - 1) // state.part_size,
            )
            upload.archive_phase = "uploading"
            upload.archive_phase_updated_at = _isoformat_z(utcnow())

    def record_multipart_upload_progress(
        self,
        *,
        collection_id: str,
        state: ArchiveMultipartUploadState,
        part: ArchiveMultipartUploadedPart,
        uploaded_bytes: int,
        uploaded_parts: int,
        total_parts: int,
    ) -> None:
        with session_scope(self._session_factory) as session:
            upload = session.get(CollectionUploadRecord, collection_id)
            if upload is None:
                return
            if upload.archive_multipart_upload_id != state.upload_id:
                return
            parts = _multipart_parts_from_json(upload.archive_multipart_parts_json)
            parts_by_number = {current.part_number: current for current in parts}
            parts_by_number[part.part_number] = part
            upload.archive_multipart_parts_json = _multipart_parts_to_json(
                tuple(parts_by_number[number] for number in sorted(parts_by_number))
            )
            upload.archive_multipart_uploaded_bytes = uploaded_bytes
            upload.archive_multipart_uploaded_parts = uploaded_parts
            upload.archive_multipart_total_parts = total_parts
            upload.archive_phase = "uploading"
            upload.archive_phase_updated_at = _isoformat_z(utcnow())

    def clear_multipart_upload(
        self,
        *,
        collection_id: str,
        upload_id: str,
    ) -> None:
        with session_scope(self._session_factory) as session:
            upload = session.get(CollectionUploadRecord, collection_id)
            if upload is None:
                return
            if upload.archive_multipart_upload_id != upload_id:
                return
            upload.archive_multipart_upload_id = None
            upload.archive_multipart_part_size = None
            upload.archive_multipart_parts_json = None
            upload.archive_encryption_state_json = None


class _SqlAlchemyHotMultipartUploadTracker(ArchiveMultipartUploadTracker):
    def __init__(self, session_factory: sessionmaker[Session], *, path: str) -> None:
        self._session_factory = session_factory
        self._path = path

    def load_multipart_upload(
        self,
        *,
        collection_id: str,
        object_path: str,
        part_size: int,
        content_length: int,
        sha256: str,
    ) -> ArchiveMultipartUploadState | None:
        with session_scope(self._session_factory) as session:
            file_record = session.get(CollectionUploadFileRecord, (collection_id, self._path))
            if file_record is None:
                return None
            if not file_record.hot_multipart_upload_id:
                return None
            if int(file_record.hot_multipart_part_size or 0) != part_size:
                return None
            if int(file_record.bytes) != content_length:
                return None
            if file_record.sha256 != sha256:
                return None
            return ArchiveMultipartUploadState(
                upload_id=file_record.hot_multipart_upload_id,
                object_path=object_path,
                part_size=part_size,
                content_length=content_length,
                sha256=sha256,
                parts=_multipart_parts_from_json(file_record.hot_multipart_parts_json),
            )

    def save_multipart_upload(
        self,
        *,
        collection_id: str,
        state: ArchiveMultipartUploadState,
    ) -> None:
        with session_scope(self._session_factory) as session:
            file_record = session.get(CollectionUploadFileRecord, (collection_id, self._path))
            if file_record is None:
                return
            file_record.hot_multipart_upload_id = state.upload_id
            file_record.hot_multipart_part_size = state.part_size
            file_record.hot_multipart_parts_json = _multipart_parts_to_json(())
            file_record.hot_multipart_uploaded_bytes = 0
            file_record.hot_multipart_uploaded_parts = 0
            file_record.hot_multipart_total_parts = max(
                1,
                (state.content_length + state.part_size - 1) // state.part_size,
            )

    def record_multipart_upload_progress(
        self,
        *,
        collection_id: str,
        state: ArchiveMultipartUploadState,
        part: ArchiveMultipartUploadedPart,
        uploaded_bytes: int,
        uploaded_parts: int,
        total_parts: int,
    ) -> None:
        with session_scope(self._session_factory) as session:
            file_record = session.get(CollectionUploadFileRecord, (collection_id, self._path))
            if file_record is None:
                return
            if file_record.hot_multipart_upload_id != state.upload_id:
                return
            parts = _multipart_parts_from_json(file_record.hot_multipart_parts_json)
            parts_by_number = {current.part_number: current for current in parts}
            parts_by_number[part.part_number] = part
            file_record.hot_multipart_parts_json = _multipart_parts_to_json(
                tuple(parts_by_number[number] for number in sorted(parts_by_number))
            )
            file_record.hot_multipart_uploaded_bytes = uploaded_bytes
            file_record.hot_multipart_uploaded_parts = uploaded_parts
            file_record.hot_multipart_total_parts = total_parts

    def clear_multipart_upload(
        self,
        *,
        collection_id: str,
        upload_id: str,
    ) -> None:
        with session_scope(self._session_factory) as session:
            file_record = session.get(CollectionUploadFileRecord, (collection_id, self._path))
            if file_record is None:
                return
            if file_record.hot_multipart_upload_id != upload_id:
                return
            file_record.hot_multipart_upload_id = None
            file_record.hot_multipart_part_size = None
            file_record.hot_multipart_parts_json = None
            file_record.hot_multipart_uploaded_bytes = None
            file_record.hot_multipart_uploaded_parts = None
            file_record.hot_multipart_total_parts = None


def _multipart_parts_from_json(raw: str | None) -> tuple[ArchiveMultipartUploadedPart, ...]:
    if not raw:
        return ()
    payload = json.loads(raw)
    if not isinstance(payload, list):
        return ()
    parts: list[ArchiveMultipartUploadedPart] = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        parts.append(
            ArchiveMultipartUploadedPart(
                part_number=int(item["part_number"]),
                etag=str(item["etag"]),
                size=int(item["size"]),
            )
        )
    return tuple(sorted(parts, key=lambda part: part.part_number))


def _multipart_parts_to_json(parts: tuple[ArchiveMultipartUploadedPart, ...]) -> str:
    return json.dumps(
        [{"part_number": part.part_number, "etag": part.etag, "size": part.size} for part in parts],
        separators=(",", ":"),
    )


def _hot_file_matches(
    hot_store: HotStore,
    *,
    collection_id: str,
    path: str,
    expected_bytes: int,
    expected_sha256: str,
) -> bool:
    stat = hot_store.stat_collection_file(collection_id, path)
    if stat is None:
        return False
    if stat.bytes != expected_bytes:
        return False
    if stat.sha256 is not None:
        return stat.sha256 == expected_sha256

    digest = hashlib.sha256()
    byte_count = 0
    for chunk in hot_store.iter_collection_file(collection_id, path):
        digest.update(chunk)
        byte_count += len(chunk)
    return byte_count == expected_bytes and digest.hexdigest() == expected_sha256


def _ensure_archive_storage_prefix(
    session: Session,
    *,
    upload: CollectionUploadRecord,
    archive_store: ArchiveStore,
) -> str:
    if upload.archive_storage_prefix:
        return upload.archive_storage_prefix.strip("/")
    prefix = archive_storage_prefix_from_object_path(upload.archive_object_path)
    if prefix is None:
        prefix = archive_store.new_collection_archive_storage_prefix()
    upload.archive_storage_prefix = prefix
    return prefix

def _archive_receipt_to_json(receipt: CollectionArchiveUploadReceipt) -> str:
    payload = {
        "archive": _archive_upload_receipt_to_payload(receipt.archive),
        "manifest": _archive_upload_receipt_to_payload(receipt.manifest),
        "proof": _archive_upload_receipt_to_payload(receipt.proof),
        "archive_sha256": receipt.archive_sha256,
        "manifest_sha256": receipt.manifest_sha256,
        "proof_sha256": receipt.proof_sha256,
        "archive_format": receipt.archive_format,
        "compression": receipt.compression,
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def _archive_receipt_from_json(raw: str | None) -> CollectionArchiveUploadReceipt | None:
    if not raw:
        return None
    try:
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            return None
        return CollectionArchiveUploadReceipt(
            archive=_archive_upload_receipt_from_payload(payload["archive"]),
            manifest=_archive_upload_receipt_from_payload(payload["manifest"]),
            proof=_archive_upload_receipt_from_payload(payload["proof"]),
            archive_sha256=str(payload["archive_sha256"]),
            manifest_sha256=str(payload["manifest_sha256"]),
            proof_sha256=str(payload["proof_sha256"]),
            archive_format=str(payload["archive_format"]),
            compression=str(payload["compression"]),
        )
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        return None


def _archive_upload_receipt_to_payload(receipt: ArchiveUploadReceipt) -> dict[str, object]:
    return {
        "object_path": receipt.object_path,
        "stored_bytes": receipt.stored_bytes,
        "backend": receipt.backend,
        "storage_class": receipt.storage_class,
        "uploaded_at": receipt.uploaded_at,
        "verified_at": receipt.verified_at,
    }


def _archive_upload_receipt_from_payload(payload: object) -> ArchiveUploadReceipt:
    if not isinstance(payload, dict):
        raise ValueError("archive upload receipt payload must be an object")
    return ArchiveUploadReceipt(
        object_path=str(payload["object_path"]),
        stored_bytes=int(payload["stored_bytes"]),
        backend=str(payload["backend"]),
        storage_class=str(payload["storage_class"]),
        uploaded_at=str(payload["uploaded_at"]),
        verified_at=str(payload["verified_at"]) if payload.get("verified_at") is not None else None,
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
    collection_id: str,
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


def _operator_failure_notification_due(
    last_notified_at: str | None,
    *,
    current: datetime,
    config: RuntimeConfig,
) -> bool:
    if last_notified_at is None:
        return True
    try:
        previous = datetime.fromisoformat(last_notified_at.replace("Z", "+00:00"))
    except ValueError:
        return True
    return operator_reminder_due(
        last_sent_at=previous,
        current=current,
        interval=config.operator_webhook_reminder_interval,
        reminder_time=config.operator_webhook_reminder_time,
        reminder_timezone=config.operator_webhook_reminder_timezone,
    )


def _isoformat_z(value: datetime) -> str:
    return value.strftime("%Y-%m-%dT%H:%M:%SZ")
