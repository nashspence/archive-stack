from __future__ import annotations

import base64
import hashlib
import json
import logging
import secrets
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

from sqlalchemy import case, func, or_, select
from sqlalchemy.orm import Session, sessionmaker

from riverhog_core.archive_object_paths import archive_storage_prefix_from_object_path
from riverhog_core.catalog_db import make_session_factory, session_scope
from riverhog_core.catalog_models import (
    ActivePinRecord,
    CollectionArchiveRecord,
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
from riverhog_core.domain.enums import FetchState
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
from riverhog_core.recovery_payloads import (
    CommandAgeBatchpassRecoveryPayloadCodec,
    RecoveryPayloadCodec,
)
from riverhog_core.runtime_config import RuntimeConfig
from riverhog_core.services.collections import _collection_upload_target_path
from riverhog_core.services.compliance import collection_is_fully_compliant
from riverhog_core.services.glacier_reporting import record_glacier_usage_snapshot
from riverhog_core.services.planning import (
    cache_collection_manifest_artifacts,
    refresh_provisional_plan,
)
from riverhog_core.webhooks import (
    WebhookConfig,
    build_collection_lifecycle_payload,
    post_webhook,
    utcnow,
)

_LOG = logging.getLogger(__name__)


class SqlAlchemyGlacierUploadService:
    def __init__(
        self,
        config: RuntimeConfig,
        archive_store: ArchiveStore,
        hot_store: HotStore | None = None,
        upload_store: UploadStore | None = None,
        *,
        proof_stamper: ProofStamper | None = None,
        recovery_payload_codec: RecoveryPayloadCodec | None = None,
    ) -> None:
        self._config = config
        self._archive_store = archive_store
        self._hot_store = hot_store
        self._upload_store = upload_store
        self._proof_stamper = proof_stamper or CommandProofStamper(config.ots_stamp_command)
        self._recovery_payload_codec = (
            recovery_payload_codec
            or CommandAgeBatchpassRecoveryPayloadCodec(
                command=config.recovery_payload_command,
                passphrase=config.recovery_payload_passphrase,
                work_factor=config.recovery_payload_work_factor,
                max_work_factor=config.recovery_payload_max_work_factor,
            )
        )
        self._session_factory = make_session_factory(config.database_url)

    def requeue_failed_uploads_for_startup(self, *, limit: int = 100) -> int:
        if limit < 1 or self._hot_store is None or self._upload_store is None:
            return 0

        current_text = _isoformat_z(utcnow())
        requeued = 0
        with session_scope(self._session_factory) as session:
            uploads = list(
                session.scalars(
                    select(CollectionUploadRecord)
                    .where(CollectionUploadRecord.state == "failed")
                    .order_by(
                        CollectionUploadRecord.archive_phase_updated_at,
                        CollectionUploadRecord.collection_id,
                    )
                    .limit(limit)
                )
            )
            for upload in uploads:
                if not _upload_files_complete(upload.files):
                    continue
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

    def publish_recovery_catalog(self) -> int:
        return self._publish_recovery_catalog()

    def process_due_uploads(self, *, limit: int = 1) -> int:
        if limit < 1:
            return 0

        current = utcnow()
        current_text = _isoformat_z(current)
        with session_scope(self._session_factory) as session:
            collection_ids: list[str] = []
            if self._hot_store is not None and self._upload_store is not None:
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
                                    CollectionUploadRecord.archive_multipart_upload_id.is_not(
                                        None
                                    ),
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
        if self._hot_store is None or self._upload_store is None:
            return
        hot_store = self._hot_store
        upload_store = self._upload_store
        current = utcnow()
        current_text = _isoformat_z(current)
        receipt: CollectionArchiveUploadReceipt | None = None
        manifest_bytes: bytes | None = None
        proof_bytes: bytes | None = None
        packaged_archive_sha256: str | None = None
        package: CollectionArchivePackage | None = None
        with session_scope(self._session_factory) as session:
            upload = session.get(CollectionUploadRecord, collection_id)
            if upload is None or upload.state != "archiving":
                return
            if not _upload_files_complete(upload.files):
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
                config=self._config,
            )
            upload.archive_attempt_count = int(upload.archive_attempt_count or 0) + 1
            upload.archive_last_attempt_at = current_text
            upload.archive_next_attempt_at = current_text
            if receipt is not None:
                upload.archive_phase = "promoting"
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
            sorted_files = sorted(
                upload.files,
                key=lambda current_file: current_file.file_order,
            )
            upload_files = [
                (
                    file_record.path,
                    file_record.bytes,
                    file_record.sha256,
                    _collection_upload_target_path(collection_id, file_record.path),
                )
                for file_record in sorted_files
            ]
            upload_file_count = len(upload_files)
            upload_byte_count = sum(_bytes for _, _bytes, _, _ in upload_files)

        try:
            archive_details = _upload_files_webhook_details(upload_files)
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
                    receipt = self._archive_store.upload_collection_archive_package(
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
            cache_collection_manifest_artifacts(
                self._config,
                collection_id=collection_id,
                manifest_bytes=manifest_bytes,
                proof_bytes=proof_bytes,
            )
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

        cleanup_targets: list[str] = []
        try:
            if receipt is None:
                raise RuntimeError("collection archive receipt was not recorded")
            for path, _bytes, sha256, target_path in upload_files:
                self._promote_one_collection_file(
                    collection_id=collection_id,
                    path=path,
                    target_path=target_path,
                    byte_count=_bytes,
                    sha256=sha256,
                )
                cleanup_targets.append(target_path)
            self._finalize_archived_collection(
                collection_id=collection_id,
                receipt=receipt,
                upload_files=upload_files,
            )
            self._publish_recovery_catalog()
            self._post_collection_operator_webhook(
                event="collections.finalized",
                collection_id=collection_id,
                details={
                    **archive_details,
                    "archive_object_path": receipt.archive.object_path,
                    "archive_total_bytes": receipt.archive.stored_bytes,
                    "archive_sha256": receipt.archive_sha256,
                },
            )
        except Exception as exc:
            error = _error_text(exc)
            if _archive_failure_is_retryable(exc):
                _LOG.exception(
                    "collection archive promotion/finalization failed for %s; scheduling retry: %s",
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
                    "collection archive promotion/finalization failed permanently for %s: %s",
                    collection_id,
                    error,
                )
                self._record_collection_failure(
                    collection_id=collection_id,
                    error=error,
                    retryable=False,
                )
            return

        for target_path in cleanup_targets:
            try:
                upload_store.delete_target(target_path)
            except Exception:
                _LOG.warning(
                    "failed to delete staged collection upload target after finalizing %s: %s",
                    collection_id,
                    target_path,
                    exc_info=True,
                )

        try:
            refresh_provisional_plan(
                config=self._config,
                hot_store=hot_store,
                archive_store=self._archive_store,
                recovery_payload_codec=self._recovery_payload_codec,
            )
        except Exception:
            _LOG.exception("failed to refresh provisional plan after archiving %s", collection_id)
            self._post_collection_operator_webhook(
                event="collections.planner_failed",
                collection_id=collection_id,
                details=archive_details,
            )

    def _promote_one_collection_file(
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
            return
        self._record_archive_phase(
            collection_id=collection_id,
            phase="promoting",
            updated_at=_isoformat_z(utcnow()),
        )
        if _hot_file_matches(
            hot_store,
            collection_id=collection_id,
            path=path,
            expected_bytes=byte_count,
            expected_sha256=sha256,
        ):
            self._mark_collection_upload_file_promoted(collection_id=collection_id, path=path)
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
            raise ValueError(f"promoted collection file sha256 mismatch: {collection_id}/{path}")
        if not _hot_file_matches(
            hot_store,
            collection_id=collection_id,
            path=path,
            expected_bytes=byte_count,
            expected_sha256=sha256,
        ):
            raise ValueError(f"promoted collection file metadata mismatch: {collection_id}/{path}")
        self._mark_collection_upload_file_promoted(collection_id=collection_id, path=path)

    def _mark_collection_upload_file_promoted(self, *, collection_id: str, path: str) -> None:
        with session_scope(self._session_factory) as session:
            upload = session.get(CollectionUploadRecord, collection_id)
            file_record = session.get(CollectionUploadFileRecord, (collection_id, path))
            if upload is None or file_record is None:
                return
            current_text = _isoformat_z(utcnow())
            file_record.hot_promoted_at = current_text
            upload.archive_phase = "promoting"
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
            unpromoted = [
                file_record.path
                for file_record in upload.files
                if file_record.hot_promoted_at is None
            ]
            if unpromoted:
                raise RuntimeError(f"collection has unpromoted files: {unpromoted[0]}")
            collection = session.get(CollectionRecord, collection_id)
            if collection is None:
                collection = CollectionRecord(id=collection_id, ingest_source=upload.ingest_source)
                session.add(collection)
                session.flush()
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
                        hot=True,
                        archived=False,
                    )
                )
            archive = session.get(CollectionArchiveRecord, collection_id)
            if archive is None:
                archive = CollectionArchiveRecord(collection_id=collection_id)
                session.add(archive)
            _apply_archive_receipt(archive, receipt)
            _ensure_default_hot_pin(session, collection_id=collection_id)
            session.delete(upload)
            record_glacier_usage_snapshot(session, config=self._config)

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
            upload.archive_phase = "promoting"
            upload.archive_phase_updated_at = current_text
            upload.archive_failure = None

    def _publish_recovery_catalog(self) -> int:
        publish = getattr(self._archive_store, "publish_recovery_catalog", None)
        if not callable(publish):
            return 0
        generated_at = _isoformat_z(utcnow())
        with session_scope(self._session_factory) as session:
            archives = list(
                session.scalars(
                    select(CollectionArchiveRecord)
                    .where(CollectionArchiveRecord.state == "uploaded")
                    .where(CollectionArchiveRecord.object_path.is_not(None))
                    .order_by(CollectionArchiveRecord.collection_id)
                )
            )
            entries = [
                {
                    "collection_id": archive.collection_id,
                    "archive_storage_prefix": archive.archive_storage_prefix
                    or archive_storage_prefix_from_object_path(archive.object_path),
                    "archive_key": archive.object_path,
                    "manifest_key": archive.manifest_object_path,
                    "proof_key": archive.ots_object_path,
                    "archive_stored_bytes": archive.stored_bytes,
                    "archive_plaintext_sha256": archive.sha256,
                    "manifest_sha256": archive.manifest_sha256,
                    "proof_sha256": archive.ots_sha256,
                    "backend": archive.backend,
                    "archive_storage_class": archive.storage_class,
                    "archive_format": archive.archive_format,
                    "compression": archive.compression,
                    "uploaded_at": archive.last_uploaded_at,
                    "verified_at": archive.last_verified_at,
                }
                for archive in archives
            ]
        try:
            publish(entries=entries, generated_at=generated_at)
        except Exception:
            _LOG.warning("failed to publish encrypted archive recovery catalog", exc_info=True)
            return 0
        return len(entries)

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
        if not self._config.operator_webhook_url:
            return
        try:
            webhook_config = WebhookConfig(
                url=self._config.operator_webhook_url,
                base_url=self._config.public_base_url or "",
                timeout_seconds=self._config.operator_webhook_timeout.total_seconds(),
            )
            payload = build_collection_lifecycle_payload(
                config=webhook_config,
                event=event,
                collection_id=collection_id,
                delivered_at=utcnow(),
                details=details,
            )
            post_webhook(config=webhook_config, payload=payload)
        except Exception:
            _LOG.warning("failed to deliver %s webhook for %s", event, collection_id, exc_info=True)

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
            _isoformat_z(current + self._config.glacier_upload_retry_delay)
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
            if retryable and _operator_failure_notification_due(
                upload.archive_last_failure_notification_at,
                current=current,
                interval=self._config.operator_failure_notification_interval,
            ):
                upload.archive_last_failure_notification_at = current_text
                notify_operator = True
            if (
                not retryable
                and (
                    previous_state != "failed"
                    or previous_phase != "failed"
                    or previous_failure != error
                )
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
                "retry_delay_seconds": self._config.glacier_upload_retry_delay.total_seconds(),
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


def _upload_files_webhook_details(
    upload_files: list[tuple[str, int, str, str]],
) -> dict[str, object]:
    return {
        "files_total": len(upload_files),
        "files_uploaded": len(upload_files),
        "bytes_total": sum(file_record[1] for file_record in upload_files),
        "uploaded_bytes": sum(file_record[1] for file_record in upload_files),
    }


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


def _apply_archive_receipt(
    archive: CollectionArchiveRecord,
    receipt: CollectionArchiveUploadReceipt,
) -> None:
    archive.state = "uploaded"
    archive.archive_storage_prefix = archive_storage_prefix_from_object_path(
        receipt.archive.object_path
    )
    archive.object_path = receipt.archive.object_path
    archive.stored_bytes = receipt.archive.stored_bytes
    archive.sha256 = receipt.archive_sha256
    archive.backend = receipt.archive.backend
    archive.storage_class = receipt.archive.storage_class
    archive.last_uploaded_at = receipt.archive.uploaded_at
    archive.last_verified_at = receipt.archive.verified_at
    archive.failure = None
    archive.archive_format = receipt.archive_format
    archive.compression = receipt.compression
    archive.manifest_object_path = receipt.manifest.object_path
    archive.manifest_sha256 = receipt.manifest_sha256
    archive.manifest_stored_bytes = receipt.manifest.stored_bytes
    archive.manifest_uploaded_at = receipt.manifest.uploaded_at
    archive.ots_object_path = receipt.proof.object_path
    archive.ots_sha256 = receipt.proof_sha256
    archive.ots_stored_bytes = receipt.proof.stored_bytes
    archive.ots_uploaded_at = receipt.proof.uploaded_at


def _ensure_archive_storage_prefix(
    session: Session,
    *,
    upload: CollectionUploadRecord,
    config: RuntimeConfig,
) -> str:
    if upload.archive_storage_prefix:
        return upload.archive_storage_prefix.strip("/")
    prefix = archive_storage_prefix_from_object_path(upload.archive_object_path)
    if prefix is None:
        prefix = _mint_archive_storage_prefix(session, config=config)
    upload.archive_storage_prefix = prefix
    return prefix


def _mint_archive_storage_prefix(session: Session, *, config: RuntimeConfig) -> str:
    for _ in range(16):
        prefix = f"{config.glacier_prefix}/archives/{secrets.token_hex(16)}"
        upload_exists = session.scalar(
            select(CollectionUploadRecord.collection_id).where(
                CollectionUploadRecord.archive_storage_prefix == prefix
            )
        )
        archive_exists = session.scalar(
            select(CollectionArchiveRecord.collection_id).where(
                CollectionArchiveRecord.archive_storage_prefix == prefix
            )
        )
        if upload_exists is None and archive_exists is None:
            return prefix
    raise RuntimeError("failed to mint a unique archive storage prefix")


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


def _ensure_default_hot_pin(session: Session, *, collection_id: str) -> None:
    if collection_is_fully_compliant(session, collection_id=collection_id):
        return
    target = f"{collection_id}/"
    if session.get(ActivePinRecord, target) is not None:
        return
    fetch_order = int(session.scalar(select(func.max(ActivePinRecord.fetch_order))) or 0) + 1
    session.add(
        ActivePinRecord(
            target=target,
            fetch_id=f"fx-{fetch_order}",
            fetch_order=fetch_order,
            fetch_state=FetchState.DONE.value,
        )
    )


def _upload_files_complete(file_records: list[CollectionUploadFileRecord]) -> bool:
    return bool(file_records) and all(
        file_record.uploaded_bytes >= file_record.bytes for file_record in file_records
    )


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
    interval: timedelta,
) -> bool:
    if last_notified_at is None:
        return True
    if interval.total_seconds() <= 0:
        return True
    try:
        previous = datetime.fromisoformat(last_notified_at.replace("Z", "+00:00"))
    except ValueError:
        return True
    return current.astimezone(UTC) - previous.astimezone(UTC) >= interval


def _isoformat_z(value: datetime) -> str:
    return value.strftime("%Y-%m-%dT%H:%M:%SZ")
