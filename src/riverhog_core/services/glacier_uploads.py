from __future__ import annotations

import json
import logging
from collections.abc import Iterator
from datetime import datetime

from sqlalchemy import or_, select
from sqlalchemy.orm import Session, sessionmaker

from riverhog_core.catalog_db import make_session_factory, session_scope
from riverhog_core.catalog_models import (
    CollectionArchiveRecord,
    CollectionFileRecord,
    CollectionRecord,
    CollectionUploadFileRecord,
    CollectionUploadRecord,
)
from riverhog_core.collection_archives import (
    CollectionArchiveExpectedFile,
    build_collection_archive_package_from_chunk_reader,
)
from riverhog_core.ports.archive_store import (
    ArchiveMultipartUploadedPart,
    ArchiveMultipartUploadState,
    ArchiveMultipartUploadTracker,
    ArchiveStore,
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
from riverhog_core.services.glacier_reporting import record_glacier_usage_snapshot
from riverhog_core.services.planning import (
    cache_collection_archive_artifacts,
    refresh_provisional_plan,
)
from riverhog_core.webhooks import (
    WebhookConfig,
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
        with session_scope(self._session_factory) as session:
            upload = session.get(CollectionUploadRecord, collection_id)
            if upload is None or upload.state != "archiving":
                return
            if not _upload_files_complete(upload.files):
                upload.state = "uploading"
                upload.archive_next_attempt_at = None
                return
            upload.archive_attempt_count = int(upload.archive_attempt_count or 0) + 1
            upload.archive_last_attempt_at = current_text
            upload.archive_next_attempt_at = current_text
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

        try:
            target_path_by_archive_path: dict[str, str] = {}
            package_files: list[CollectionArchiveExpectedFile] = []
            for path, _bytes, sha256, target_path in upload_files:
                target_path_by_archive_path[path] = target_path
                package_files.append(
                    CollectionArchiveExpectedFile(path=path, bytes=_bytes, sha256=sha256)
                )

            def _read_archive_file_chunks(path: str) -> Iterator[bytes]:
                return upload_store.iter_target(target_path_by_archive_path[path])

            package = build_collection_archive_package_from_chunk_reader(
                collection_id=collection_id,
                files=package_files,
                read_file_chunks=_read_archive_file_chunks,
                stamper=self._proof_stamper,
            )
            self._record_archive_phase(
                collection_id=collection_id,
                phase="uploading",
                updated_at=_isoformat_z(utcnow()),
            )
            receipt = self._archive_store.upload_collection_archive_package(
                collection_id=collection_id,
                package=package,
                multipart_tracker=_SqlAlchemyArchiveMultipartUploadTracker(self._session_factory),
            )
            cache_collection_archive_artifacts(
                self._config,
                collection_id=collection_id,
                manifest_bytes=package.manifest_bytes,
                proof_bytes=package.proof_bytes,
            )
        except Exception as exc:
            self._record_collection_failure(collection_id=collection_id, error=_error_text(exc))
            return

        cleanup_targets: list[str] = []
        try:
            with session_scope(self._session_factory) as session:
                upload = session.get(CollectionUploadRecord, collection_id)
                if upload is None:
                    return
                upload.archive_phase = "promoting"
                upload.archive_phase_updated_at = _isoformat_z(utcnow())
                session.flush()
                collection = CollectionRecord(id=collection_id, ingest_source=upload.ingest_source)
                session.add(collection)
                for file_record in sorted(
                    upload.files,
                    key=lambda current_file: current_file.file_order,
                ):
                    target_path = _collection_upload_target_path(collection_id, file_record.path)
                    hot_store.put_collection_file_stream(
                        collection_id,
                        file_record.path,
                        upload_store.iter_target(target_path),
                        content_length=file_record.bytes,
                    )
                    cleanup_targets.append(target_path)
                    collection.files.append(
                        CollectionFileRecord(
                            collection_id=collection_id,
                            path=file_record.path,
                            bytes=file_record.bytes,
                            sha256=file_record.sha256,
                            hot=True,
                            archived=False,
                        )
                    )
                session.add(
                    CollectionArchiveRecord(
                        collection_id=collection_id,
                        state="uploaded",
                        object_path=receipt.archive.object_path,
                        stored_bytes=receipt.archive.stored_bytes,
                        sha256=receipt.archive_sha256,
                        backend=receipt.archive.backend,
                        storage_class=receipt.archive.storage_class,
                        last_uploaded_at=receipt.archive.uploaded_at,
                        last_verified_at=receipt.archive.verified_at,
                        failure=None,
                        archive_format=receipt.archive_format,
                        compression=receipt.compression,
                        manifest_object_path=receipt.manifest.object_path,
                        manifest_sha256=receipt.manifest_sha256,
                        manifest_stored_bytes=receipt.manifest.stored_bytes,
                        manifest_uploaded_at=receipt.manifest.uploaded_at,
                        ots_object_path=receipt.proof.object_path,
                        ots_sha256=receipt.proof_sha256,
                        ots_stored_bytes=receipt.proof.stored_bytes,
                        ots_uploaded_at=receipt.proof.uploaded_at,
                    )
                )
                session.delete(upload)
                record_glacier_usage_snapshot(session, config=self._config)
        except Exception as exc:
            self._record_collection_failure(collection_id=collection_id, error=_error_text(exc))
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
                recovery_payload_codec=self._recovery_payload_codec,
            )
        except Exception:
            _LOG.exception("failed to refresh provisional plan after archiving %s", collection_id)

    def _record_collection_failure(self, *, collection_id: str, error: str) -> None:
        current = utcnow()
        current_text = _isoformat_z(current)
        notify_failure = False
        attempt_count = 0

        with session_scope(self._session_factory) as session:
            upload = session.get(CollectionUploadRecord, collection_id)
            if upload is None:
                return
            attempt_count = int(upload.archive_attempt_count or 0)
            upload.archive_failure = error
            if attempt_count < self._config.glacier_upload_retry_limit:
                upload.archive_next_attempt_at = _isoformat_z(
                    current + self._config.glacier_upload_retry_delay
                )
                upload.archive_phase = "retry_wait"
                upload.archive_phase_updated_at = current_text
                upload.state = "archiving"
                return
            upload.archive_next_attempt_at = None
            upload.archive_phase = "failed"
            upload.archive_phase_updated_at = current_text
            upload.state = "failed"
            notify_failure = True

        if notify_failure:
            self._notify_persistent_collection_failure(
                collection_id=collection_id,
                attempt_count=attempt_count,
                error=error,
                failed_at=current_text,
            )

    def _notify_persistent_collection_failure(
        self,
        *,
        collection_id: str,
        attempt_count: int,
        error: str,
        failed_at: str,
    ) -> None:
        if not self._config.glacier_failure_webhook_url:
            return
        payload = {
            "event": "collections.glacier_upload.failed",
            "collection_id": collection_id,
            "error": error,
            "attempts": attempt_count,
            "failed_at": failed_at,
            "collection_url": (
                f"{(self._config.public_base_url or '').rstrip('/')}"
                f"/v1/collection-uploads/{collection_id}"
            ),
        }
        post_webhook(
            config=WebhookConfig(
                url=self._config.glacier_failure_webhook_url,
                base_url=self._config.public_base_url or "",
            ),
            payload=payload,
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
            upload.archive_multipart_parts_json = _multipart_parts_to_json(())
            upload.archive_multipart_uploaded_bytes = 0
            upload.archive_multipart_uploaded_parts = 0
            upload.archive_multipart_total_parts = max(
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
            upload.archive_object_path = None
            upload.archive_multipart_upload_id = None
            upload.archive_multipart_part_size = None
            upload.archive_multipart_content_length = None
            upload.archive_multipart_sha256 = None
            upload.archive_multipart_parts_json = None
            upload.archive_multipart_uploaded_bytes = None
            upload.archive_multipart_uploaded_parts = None
            upload.archive_multipart_total_parts = None


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


def _upload_files_complete(file_records: list[CollectionUploadFileRecord]) -> bool:
    return bool(file_records) and all(
        file_record.uploaded_bytes >= file_record.bytes for file_record in file_records
    )


def _error_text(exc: Exception) -> str:
    detail = str(exc).strip()
    return detail or exc.__class__.__name__


def _isoformat_z(value: datetime) -> str:
    return value.strftime("%Y-%m-%dT%H:%M:%SZ")
