from __future__ import annotations

import base64
import hashlib
import json
import logging
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import case, or_, select
from sqlalchemy.orm import Session, sessionmaker

from riverhog_core.archive_compliance import (
    copy_counts_as_verified,
    normalize_glacier_state,
    normalize_required_copy_count,
)
from riverhog_core.catalog_db import make_session_factory, session_scope
from riverhog_core.catalog_models import (
    CollectionArchiveRecord,
    CollectionFileRecord,
    CollectionProtectionMirrorRecord,
    CollectionRecord,
    CollectionUploadFileRecord,
    CollectionUploadRecord,
    FinalizedImageCoveredPathRecord,
    FinalizedImageRecord,
    ImageCopyRecord,
)
from riverhog_core.collection_archives import (
    CollectionArchiveExpectedFile,
    CollectionArchivePackage,
    build_collection_archive_package_from_chunk_reader,
    build_collection_archive_package_from_prebuilt_artifacts,
    collection_archive_size,
    iter_collection_archive_chunks_from_reader,
)
from riverhog_core.domain.enums import GlacierState
from riverhog_core.ports.archive_store import (
    ArchiveMultipartUploadedPart,
    ArchiveMultipartUploadState,
    ArchiveMultipartUploadTracker,
    ArchiveStore,
    ArchiveUploadReceipt,
    CollectionArchiveUploadReceipt,
)
from riverhog_core.ports.hot_store import HotStore
from riverhog_core.ports.protection_mirror import ProtectionMirrorStore
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
    cache_collection_manifest_artifacts,
    refresh_provisional_plan,
)
from riverhog_core.services.protection_mirror_repair import (
    ProtectionMirrorRepairResult,
    repair_collection_hot_files_from_protection_mirror,
)
from riverhog_core.webhooks import (
    WebhookConfig,
    build_collection_lifecycle_payload,
    post_webhook,
    utcnow,
)

_LOG = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class _ProtectionMirrorRepairAttempt:
    processed: bool
    repaired: bool


class SqlAlchemyGlacierUploadService:
    def __init__(
        self,
        config: RuntimeConfig,
        archive_store: ArchiveStore,
        hot_store: HotStore | None = None,
        upload_store: UploadStore | None = None,
        protection_mirror_store: ProtectionMirrorStore | None = None,
        *,
        proof_stamper: ProofStamper | None = None,
        recovery_payload_codec: RecoveryPayloadCodec | None = None,
    ) -> None:
        self._config = config
        self._archive_store = archive_store
        self._hot_store = hot_store
        self._upload_store = upload_store
        self._protection_mirror_store = protection_mirror_store
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
        if attempted < limit:
            attempted += self._process_due_protection_mirrors(limit=limit - attempted)
        return attempted

    def repair_missing_hot_files_from_protection_mirror(
        self,
        *,
        limit: int = 1,
        force: bool = False,
    ) -> int:
        if (
            limit < 1
            or not self._config.protection_mirror_enabled
            or self._protection_mirror_store is None
            or self._hot_store is None
        ):
            return 0

        current = utcnow()
        current_text = _isoformat_z(current)
        with session_scope(self._session_factory) as session:
            _sync_protection_mirror_targets(session, current_text=current_text)
            session.flush()
            query = (
                select(CollectionProtectionMirrorRecord.collection_id)
                .where(
                    CollectionProtectionMirrorRecord.state.in_(
                        ("complete", "repairing", "repair_wait")
                    )
                )
                .order_by(
                    case(
                        (CollectionProtectionMirrorRecord.state == "repairing", 0),
                        (CollectionProtectionMirrorRecord.state == "repair_wait", 1),
                        else_=2,
                    ),
                    CollectionProtectionMirrorRecord.next_attempt_at,
                    CollectionProtectionMirrorRecord.collection_id,
                )
            )
            if not force:
                query = query.where(
                    or_(
                        CollectionProtectionMirrorRecord.next_attempt_at.is_(None),
                        CollectionProtectionMirrorRecord.next_attempt_at <= current_text,
                    )
                )
            collection_ids = list(session.scalars(query.limit(limit)).all())

        if collection_ids:
            _LOG.info(
                "protection mirror hot audit started: collections=%s force=%s",
                len(collection_ids),
                force,
            )
        processed = 0
        repaired = 0
        for collection_id in collection_ids:
            result = self._repair_one_collection_from_protection_mirror(
                collection_id=collection_id
            )
            if result.processed:
                processed += 1
            if result.repaired:
                repaired += 1
        if collection_ids:
            _LOG.info(
                "protection mirror hot audit completed: collections=%s processed=%s "
                "repaired=%s force=%s",
                len(collection_ids),
                processed,
                repaired,
                force,
            )
        return processed

    def _repair_one_collection_from_protection_mirror(
        self,
        *,
        collection_id: str,
    ) -> _ProtectionMirrorRepairAttempt:
        mirror_store = self._protection_mirror_store
        hot_store = self._hot_store
        if mirror_store is None or hot_store is None:
            return _ProtectionMirrorRepairAttempt(processed=False, repaired=False)
        self._mark_protection_mirror_repairing(collection_id=collection_id)
        try:
            result = repair_collection_hot_files_from_protection_mirror(
                session_factory=self._session_factory,
                hot_store=hot_store,
                protection_mirror_store=mirror_store,
                collection_id=collection_id,
            )
            self._mark_protection_mirror_audited(
                collection_id=collection_id,
                result=result,
            )
            if result.repaired:
                _LOG.info(
                    "protection mirror hot repair completed for %s: files=%s bytes=%s",
                    collection_id,
                    result.restored_files,
                    result.restored_bytes,
                )
            return _ProtectionMirrorRepairAttempt(processed=True, repaired=result.repaired)
        except Exception as exc:
            self._record_protection_mirror_failure(
                collection_id=collection_id,
                error=_error_text(exc),
                retry_state="repair_wait",
            )
            return _ProtectionMirrorRepairAttempt(processed=True, repaired=False)

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
        packaged_archive_size: int | None = None
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
            packaged_archive_size = _positive_int_or_none(
                upload.archive_multipart_content_length
            )
            packaged_archive_sha256 = upload.archive_multipart_sha256
            upload.archive_attempt_count = int(upload.archive_attempt_count or 0) + 1
            upload.archive_last_attempt_at = current_text
            upload.archive_next_attempt_at = current_text
            if receipt is not None:
                upload.archive_phase = "promoting"
            elif (
                manifest_bytes is not None
                and proof_bytes is not None
                and packaged_archive_size is not None
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

            def _read_archive_file_chunks(path: str) -> Iterator[bytes]:
                return upload_store.iter_target(target_path_by_archive_path[path])

            if receipt is None or manifest_bytes is None or proof_bytes is None:
                if (
                    manifest_bytes is not None
                    and proof_bytes is not None
                    and packaged_archive_size is not None
                    and packaged_archive_sha256 is not None
                ):
                    package = build_collection_archive_package_from_prebuilt_artifacts(
                        collection_id=collection_id,
                        files=package_files,
                        read_file_chunks=_read_archive_file_chunks,
                        manifest_bytes=manifest_bytes,
                        proof_bytes=proof_bytes,
                        archive_size=packaged_archive_size,
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
                        read_file_chunks=_read_archive_file_chunks,
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
            if self._protection_mirror_store is not None:
                if receipt is None:
                    raise RuntimeError("collection archive receipt was not recorded")
                if package is None:
                    package = build_collection_archive_package_from_prebuilt_artifacts(
                        collection_id=collection_id,
                        files=package_files,
                        read_file_chunks=_read_archive_file_chunks,
                        manifest_bytes=manifest_bytes,
                        proof_bytes=proof_bytes,
                        archive_size=receipt.archive.stored_bytes,
                        archive_sha256=receipt.archive_sha256,
                    )
                self._mirror_upload_package(collection_id=collection_id, package=package)
        except Exception as exc:
            self._record_collection_failure(collection_id=collection_id, error=_error_text(exc))
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
                archive_store=self._archive_store,
                protection_mirror_store=self._protection_mirror_store,
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

    def _mirror_upload_package(
        self,
        *,
        collection_id: str,
        package: CollectionArchivePackage,
    ) -> None:
        mirror_store = self._protection_mirror_store
        if mirror_store is None:
            return
        stat = mirror_store.stat_collection_archive(collection_id)
        if (
            stat is not None
            and stat.bytes == package.archive_size
            and stat.sha256 == package.archive_sha256
        ):
            self._mark_protection_mirror_complete(
                collection_id=collection_id,
                object_path=mirror_store.object_path(collection_id),
                archive_bytes=package.archive_size,
                archive_sha256=package.archive_sha256,
            )
            return
        self._mark_protection_mirror_mirroring(
            collection_id=collection_id,
            object_path=mirror_store.object_path(collection_id),
            archive_bytes=package.archive_size,
            archive_sha256=package.archive_sha256,
        )
        _LOG.info(
            "uploading collection protection mirror archive for %s: archive_bytes=%s",
            collection_id,
            package.archive_size,
        )
        mirror_store.put_collection_archive_stream_resumable(
            collection_id,
            package.iter_archive(),
            content_length=package.archive_size,
            sha256=package.archive_sha256,
            multipart_tracker=_SqlAlchemyProtectionMirrorMultipartUploadTracker(
                self._session_factory
            ),
        )
        stat = mirror_store.stat_collection_archive(collection_id)
        if (
            stat is None
            or stat.bytes != package.archive_size
            or stat.sha256 != package.archive_sha256
        ):
            raise RuntimeError(f"protection mirror archive receipt mismatch: {collection_id}")
        self._mark_protection_mirror_complete(
            collection_id=collection_id,
            object_path=mirror_store.object_path(collection_id),
            archive_bytes=package.archive_size,
            archive_sha256=package.archive_sha256,
        )

    def _process_due_protection_mirrors(self, *, limit: int) -> int:
        if (
            limit < 1
            or not self._config.protection_mirror_enabled
            or self._protection_mirror_store is None
            or self._hot_store is None
        ):
            return 0
        current = utcnow()
        current_text = _isoformat_z(current)
        with session_scope(self._session_factory) as session:
            _sync_protection_mirror_targets(session, current_text=current_text)
            session.flush()
            collection_ids = list(
                session.scalars(
                    select(CollectionProtectionMirrorRecord.collection_id)
                    .where(
                        CollectionProtectionMirrorRecord.state.in_(
                            ("pending", "mirroring", "retry_wait", "deleting")
                        )
                    )
                    .where(
                        or_(
                            CollectionProtectionMirrorRecord.next_attempt_at.is_(None),
                            CollectionProtectionMirrorRecord.next_attempt_at <= current_text,
                        )
                    )
                    .order_by(
                        case(
                            (CollectionProtectionMirrorRecord.state == "mirroring", 0),
                            (CollectionProtectionMirrorRecord.state == "deleting", 1),
                            (CollectionProtectionMirrorRecord.state == "retry_wait", 2),
                            else_=3,
                        ),
                        CollectionProtectionMirrorRecord.next_attempt_at,
                        CollectionProtectionMirrorRecord.collection_id,
                    )
                    .limit(limit)
                ).all()
            )

        processed = 0
        for collection_id in collection_ids:
            self._process_one_protection_mirror(collection_id=collection_id)
            processed += 1
        return processed

    def _process_one_protection_mirror(self, *, collection_id: str) -> None:
        mirror_store = self._protection_mirror_store
        hot_store = self._hot_store
        if mirror_store is None or hot_store is None:
            return
        with session_scope(self._session_factory) as session:
            mirror = session.get(CollectionProtectionMirrorRecord, collection_id)
            if mirror is None:
                return
            state = mirror.state
        if state == "deleting":
            try:
                mirror_store.delete_collection(collection_id)
                self._mark_protection_mirror_deleted(collection_id=collection_id)
            except Exception as exc:
                self._record_protection_mirror_failure(
                    collection_id=collection_id,
                    error=_error_text(exc),
                    keep_deleting=True,
                )
            return

        try:
            with session_scope(self._session_factory) as session:
                collection = session.get(CollectionRecord, collection_id)
                if collection is None or collection.archive is None:
                    return
                if _collection_has_required_verified_copies(session, collection_id):
                    mirror = session.get(CollectionProtectionMirrorRecord, collection_id)
                    if mirror is not None:
                        mirror.state = "deleting"
                        mirror.next_attempt_at = _isoformat_z(utcnow())
                    return
                if normalize_glacier_state(collection.archive.state) is not GlacierState.UPLOADED:
                    return
                if not collection.archive.sha256 or not collection.archive.stored_bytes:
                    raise RuntimeError(f"collection archive receipt is incomplete: {collection_id}")
                expected_files = [
                    CollectionArchiveExpectedFile(
                        path=file_record.path,
                        bytes=file_record.bytes,
                        sha256=file_record.sha256,
                    )
                    for file_record in sorted(collection.files, key=lambda record: record.path)
                ]
                archive_bytes = int(collection.archive.stored_bytes)
                archive_sha256 = collection.archive.sha256
            if collection_archive_size(expected_files) != archive_bytes:
                raise RuntimeError(
                    f"collection archive size mismatch before mirror backfill: {collection_id}"
                )
            stat = mirror_store.stat_collection_archive(collection_id)
            if stat is not None and stat.bytes == archive_bytes and stat.sha256 == archive_sha256:
                self._mark_protection_mirror_complete(
                    collection_id=collection_id,
                    object_path=mirror_store.object_path(collection_id),
                    archive_bytes=archive_bytes,
                    archive_sha256=archive_sha256,
                )
                return

            def read_file_chunks(path: str) -> Iterator[bytes]:
                return hot_store.iter_collection_file(collection_id, path)

            self._mark_protection_mirror_mirroring(
                collection_id=collection_id,
                object_path=mirror_store.object_path(collection_id),
                archive_bytes=archive_bytes,
                archive_sha256=archive_sha256,
            )
            _LOG.info(
                "backfilling collection protection mirror archive for %s: files=%s bytes=%s",
                collection_id,
                len(expected_files),
                archive_bytes,
            )
            mirror_store.put_collection_archive_stream_resumable(
                collection_id,
                iter_collection_archive_chunks_from_reader(
                    files=expected_files,
                    read_file_chunks=read_file_chunks,
                ),
                content_length=archive_bytes,
                sha256=archive_sha256,
                multipart_tracker=_SqlAlchemyProtectionMirrorMultipartUploadTracker(
                    self._session_factory
                ),
            )
            stat = mirror_store.stat_collection_archive(collection_id)
            if stat is None or stat.bytes != archive_bytes or stat.sha256 != archive_sha256:
                raise RuntimeError(f"protection mirror archive receipt mismatch: {collection_id}")
            self._mark_protection_mirror_complete(
                collection_id=collection_id,
                object_path=mirror_store.object_path(collection_id),
                archive_bytes=archive_bytes,
                archive_sha256=archive_sha256,
            )
        except Exception as exc:
            self._record_protection_mirror_failure(
                collection_id=collection_id,
                error=_error_text(exc),
            )

    def _mark_protection_mirror_mirroring(
        self,
        *,
        collection_id: str,
        object_path: str,
        archive_bytes: int,
        archive_sha256: str,
    ) -> None:
        current_text = _isoformat_z(utcnow())
        with session_scope(self._session_factory) as session:
            mirror = session.get(CollectionProtectionMirrorRecord, collection_id)
            if mirror is None:
                mirror = CollectionProtectionMirrorRecord(collection_id=collection_id)
                session.add(mirror)
            mirror.state = "mirroring"
            mirror.object_path = object_path
            mirror.archive_bytes = archive_bytes
            mirror.archive_sha256 = archive_sha256
            mirror.failure = None
            mirror.last_attempt_at = current_text
            mirror.next_attempt_at = current_text
            mirror.updated_at = current_text

    def _mark_protection_mirror_complete(
        self,
        *,
        collection_id: str,
        object_path: str,
        archive_bytes: int,
        archive_sha256: str,
    ) -> None:
        current_text = _isoformat_z(utcnow())
        with session_scope(self._session_factory) as session:
            mirror = session.get(CollectionProtectionMirrorRecord, collection_id)
            if mirror is None:
                mirror = CollectionProtectionMirrorRecord(collection_id=collection_id)
                session.add(mirror)
            mirror.state = "complete"
            mirror.object_path = object_path
            mirror.archive_bytes = archive_bytes
            mirror.archive_sha256 = archive_sha256
            mirror.failure = None
            mirror.next_attempt_at = None
            mirror.updated_at = current_text
            mirror.completed_at = current_text
            mirror.last_failure_notification_at = None

    def _mark_protection_mirror_repairing(self, *, collection_id: str) -> None:
        current_text = _isoformat_z(utcnow())
        with session_scope(self._session_factory) as session:
            mirror = session.get(CollectionProtectionMirrorRecord, collection_id)
            if mirror is None:
                return
            mirror.state = "repairing"
            mirror.failure = None
            mirror.last_attempt_at = current_text
            mirror.next_attempt_at = current_text
            mirror.updated_at = current_text

    def _mark_protection_mirror_audited(
        self,
        *,
        collection_id: str,
        result: ProtectionMirrorRepairResult,
    ) -> None:
        next_audit_at = _isoformat_z(utcnow() + self._config.protection_mirror_hot_audit_interval)
        current_text = _isoformat_z(utcnow())
        with session_scope(self._session_factory) as session:
            mirror = session.get(CollectionProtectionMirrorRecord, collection_id)
            if mirror is None:
                return
            mirror.state = "complete"
            mirror.failure = None
            mirror.next_attempt_at = next_audit_at
            mirror.updated_at = current_text
            mirror.last_failure_notification_at = None
            if result.repaired:
                mirror.last_attempt_at = current_text

    def _mark_protection_mirror_deleted(self, *, collection_id: str) -> None:
        current_text = _isoformat_z(utcnow())
        with session_scope(self._session_factory) as session:
            mirror = session.get(CollectionProtectionMirrorRecord, collection_id)
            if mirror is None:
                return
            mirror.state = "deleted"
            mirror.failure = None
            mirror.next_attempt_at = None
            mirror.updated_at = current_text
            mirror.deleted_at = current_text
            mirror.last_failure_notification_at = None
            mirror.multipart_upload_id = None
            mirror.multipart_part_size = None
            mirror.multipart_parts_json = None
            mirror.multipart_uploaded_bytes = None
            mirror.multipart_uploaded_parts = None
            mirror.multipart_total_parts = None

    def _record_protection_mirror_failure(
        self,
        *,
        collection_id: str,
        error: str,
        keep_deleting: bool = False,
        retry_state: str | None = None,
    ) -> None:
        current = utcnow()
        current_text = _isoformat_z(current)
        next_retry_at = _isoformat_z(current + self._config.glacier_upload_retry_delay)
        notify_retrying = False
        state = retry_state or ("deleting" if keep_deleting else "retry_wait")
        with session_scope(self._session_factory) as session:
            mirror = session.get(CollectionProtectionMirrorRecord, collection_id)
            if mirror is None:
                mirror = CollectionProtectionMirrorRecord(collection_id=collection_id)
                session.add(mirror)
            mirror.state = state
            mirror.failure = error
            mirror.last_attempt_at = current_text
            mirror.next_attempt_at = next_retry_at
            mirror.updated_at = current_text
            if _operator_failure_notification_due(
                mirror.last_failure_notification_at,
                current=current,
                interval=self._config.operator_failure_notification_interval,
            ):
                mirror.last_failure_notification_at = current_text
                notify_retrying = True
        _LOG.warning(
            "protection mirror work failed for %s; retrying at %s: %s",
            collection_id,
            next_retry_at,
            error,
        )
        if notify_retrying:
            self._notify_collection_protection_mirror_retrying(
                collection_id=collection_id,
                state=state,
                error=error,
                failed_at=current_text,
                next_retry_at=next_retry_at,
            )

    def _notify_collection_protection_mirror_retrying(
        self,
        *,
        collection_id: str,
        state: str,
        error: str,
        failed_at: str,
        next_retry_at: str,
    ) -> None:
        self._post_collection_operator_webhook(
            event="collections.protection_mirror_retrying",
            collection_id=collection_id,
            details={
                "mirror_state": state,
                "failed_at": failed_at,
                "next_retry_at": next_retry_at,
                "retry_delay_seconds": self._config.glacier_upload_retry_delay.total_seconds(),
                "error": error,
            },
        )

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
            upload.archive_multipart_content_length = receipt.archive.stored_bytes
            upload.archive_multipart_sha256 = receipt.archive_sha256
            upload.archive_multipart_uploaded_bytes = receipt.archive.stored_bytes
            upload.archive_multipart_uploaded_parts = upload.archive_multipart_total_parts
            upload.archive_phase = "promoting"
            upload.archive_phase_updated_at = current_text
            upload.archive_failure = None

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

    def _record_collection_failure(self, *, collection_id: str, error: str) -> None:
        current = utcnow()
        current_text = _isoformat_z(current)
        notify_retrying = False
        attempt_count = 0
        next_retry_at = _isoformat_z(current + self._config.glacier_upload_retry_delay)

        with session_scope(self._session_factory) as session:
            upload = session.get(CollectionUploadRecord, collection_id)
            if upload is None:
                return
            attempt_count = int(upload.archive_attempt_count or 0)
            upload.archive_failure = error
            upload.archive_next_attempt_at = next_retry_at
            upload.archive_phase = "retry_wait"
            upload.archive_phase_updated_at = current_text
            upload.state = "archiving"
            if _operator_failure_notification_due(
                upload.archive_last_failure_notification_at,
                current=current,
                interval=self._config.operator_failure_notification_interval,
            ):
                upload.archive_last_failure_notification_at = current_text
                notify_retrying = True

        if notify_retrying:
            self._notify_collection_archive_retrying(
                collection_id=collection_id,
                attempt_count=attempt_count,
                error=error,
                failed_at=current_text,
                next_retry_at=next_retry_at,
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
            upload.archive_multipart_upload_id = None
            upload.archive_multipart_part_size = None
            upload.archive_multipart_parts_json = None


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


class _SqlAlchemyProtectionMirrorMultipartUploadTracker(ArchiveMultipartUploadTracker):
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
            mirror = session.get(CollectionProtectionMirrorRecord, collection_id)
            if mirror is None:
                return None
            if not mirror.multipart_upload_id:
                return None
            if mirror.object_path != object_path:
                return None
            if int(mirror.multipart_part_size or 0) != part_size:
                return None
            if int(mirror.archive_bytes or 0) != content_length:
                return None
            if mirror.archive_sha256 != sha256:
                return None
            return ArchiveMultipartUploadState(
                upload_id=mirror.multipart_upload_id,
                object_path=object_path,
                part_size=part_size,
                content_length=content_length,
                sha256=sha256,
                parts=_multipart_parts_from_json(mirror.multipart_parts_json),
            )

    def save_multipart_upload(
        self,
        *,
        collection_id: str,
        state: ArchiveMultipartUploadState,
    ) -> None:
        with session_scope(self._session_factory) as session:
            mirror = session.get(CollectionProtectionMirrorRecord, collection_id)
            if mirror is None:
                mirror = CollectionProtectionMirrorRecord(collection_id=collection_id)
                session.add(mirror)
            mirror.state = "mirroring"
            mirror.object_path = state.object_path
            mirror.archive_bytes = state.content_length
            mirror.archive_sha256 = state.sha256
            mirror.multipart_upload_id = state.upload_id
            mirror.multipart_part_size = state.part_size
            mirror.multipart_parts_json = _multipart_parts_to_json(())
            mirror.multipart_uploaded_bytes = 0
            mirror.multipart_uploaded_parts = 0
            mirror.multipart_total_parts = max(
                1,
                (state.content_length + state.part_size - 1) // state.part_size,
            )
            mirror.updated_at = _isoformat_z(utcnow())

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
            mirror = session.get(CollectionProtectionMirrorRecord, collection_id)
            if mirror is None:
                return
            if mirror.multipart_upload_id != state.upload_id:
                return
            parts = _multipart_parts_from_json(mirror.multipart_parts_json)
            parts_by_number = {current.part_number: current for current in parts}
            parts_by_number[part.part_number] = part
            mirror.multipart_parts_json = _multipart_parts_to_json(
                tuple(parts_by_number[number] for number in sorted(parts_by_number))
            )
            mirror.multipart_uploaded_bytes = uploaded_bytes
            mirror.multipart_uploaded_parts = uploaded_parts
            mirror.multipart_total_parts = total_parts
            mirror.updated_at = _isoformat_z(utcnow())

    def clear_multipart_upload(
        self,
        *,
        collection_id: str,
        upload_id: str,
    ) -> None:
        with session_scope(self._session_factory) as session:
            mirror = session.get(CollectionProtectionMirrorRecord, collection_id)
            if mirror is None:
                return
            if mirror.multipart_upload_id != upload_id:
                return
            mirror.multipart_upload_id = None
            mirror.multipart_part_size = None
            mirror.multipart_parts_json = None
            mirror.multipart_uploaded_bytes = None
            mirror.multipart_uploaded_parts = None
            mirror.multipart_total_parts = None


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


def _sync_protection_mirror_targets(session: Session, *, current_text: str) -> None:
    collection_ids = list(
        session.scalars(
            select(CollectionRecord.id)
            .join(CollectionArchiveRecord)
            .where(CollectionArchiveRecord.state == GlacierState.UPLOADED.value)
            .order_by(CollectionRecord.id.asc())
        ).all()
    )
    for collection_id in collection_ids:
        mirror = session.get(CollectionProtectionMirrorRecord, collection_id)
        fully_protected = _collection_has_required_verified_copies(session, collection_id)
        if fully_protected:
            if mirror is not None and mirror.state not in {"deleted", "deleting"}:
                mirror.state = "deleting"
                mirror.next_attempt_at = current_text
                mirror.updated_at = current_text
            continue
        if mirror is None:
            session.add(
                CollectionProtectionMirrorRecord(
                    collection_id=collection_id,
                    state="pending",
                    next_attempt_at=current_text,
                    updated_at=current_text,
                )
            )
            continue
        if mirror.state == "deleted":
            mirror.state = "pending"
            mirror.next_attempt_at = current_text
            mirror.updated_at = current_text
            mirror.deleted_at = None


def _collection_has_required_verified_copies(session: Session, collection_id: str) -> bool:
    file_paths = set(
        session.scalars(
            select(CollectionFileRecord.path).where(
                CollectionFileRecord.collection_id == collection_id
            )
        ).all()
    )
    if not file_paths:
        return False
    coverage_rows = session.execute(
        select(
            FinalizedImageCoveredPathRecord.path,
            FinalizedImageCoveredPathRecord.image_id,
        ).where(FinalizedImageCoveredPathRecord.collection_id == collection_id)
    ).all()
    image_ids_by_path: dict[str, set[str]] = {path: set() for path in file_paths}
    for path, image_id in coverage_rows:
        if path in image_ids_by_path:
            image_ids_by_path[path].add(image_id)
    if any(not image_ids for image_ids in image_ids_by_path.values()):
        return False
    image_ids = sorted(
        {image_id for image_ids in image_ids_by_path.values() for image_id in image_ids}
    )
    required_by_image = {
        image.image_id: normalize_required_copy_count(image.required_copy_count)
        for image in session.scalars(
            select(FinalizedImageRecord).where(FinalizedImageRecord.image_id.in_(image_ids))
        ).all()
    }
    verified_counts: dict[str, int] = {image_id: 0 for image_id in image_ids}
    copy_rows = session.scalars(
        select(ImageCopyRecord).where(ImageCopyRecord.image_id.in_(image_ids))
    ).all()
    for copy in copy_rows:
        if copy_counts_as_verified(
            state=copy.state,
            verification_state=copy.verification_state,
        ):
            verified_counts[copy.image_id] = verified_counts.get(copy.image_id, 0) + 1
    for path_image_ids in image_ids_by_path.values():
        for image_id in path_image_ids:
            required = required_by_image.get(image_id)
            if required is None:
                return False
            if verified_counts.get(image_id, 0) < required:
                return False
    return True


def _apply_archive_receipt(
    archive: CollectionArchiveRecord,
    receipt: CollectionArchiveUploadReceipt,
) -> None:
    archive.state = "uploaded"
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


def _upload_files_complete(file_records: list[CollectionUploadFileRecord]) -> bool:
    return bool(file_records) and all(
        file_record.uploaded_bytes >= file_record.bytes for file_record in file_records
    )


def _error_text(exc: Exception) -> str:
    detail = str(exc).strip()
    return detail or exc.__class__.__name__


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
