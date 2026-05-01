from __future__ import annotations

import json
import subprocess
import tempfile
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import cast

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from arc_core.archive_artifacts import generate_collection_hash_artifacts
from arc_core.archive_compliance import (
    copy_counts_toward_protection,
    normalize_copy_state,
    normalize_glacier_state,
)
from arc_core.catalog_models import (
    CollectionArchiveRecord,
    CollectionFileRecord,
    CollectionRecord,
    FinalizedImageCollectionArtifactRecord,
    FinalizedImageCoveragePartRecord,
    FinalizedImageCoveredPathRecord,
    FinalizedImageRecord,
    GlacierRecoverySessionCollectionRecord,
    GlacierRecoverySessionImageRecord,
    GlacierRecoverySessionRecord,
    ImageCopyRecord,
)
from arc_core.collection_archives import (
    CollectionArchiveExpectedFile,
    iter_collection_archive_files,
    iter_verified_collection_archive_files,
    verify_collection_archive_files,
    verify_collection_archive_manifest,
    verify_collection_archive_member,
    verify_collection_archive_proof,
)
from arc_core.domain.enums import CopyState, GlacierState, RecoverySessionState
from arc_core.domain.errors import Conflict, InvalidState, NotFound
from arc_core.domain.models import (
    CollectionArchiveManifestStatus,
    GlacierArchiveStatus,
    RecoveryCostEstimate,
    RecoveryNotificationStatus,
    RecoverySessionCollection,
    RecoverySessionImage,
    RecoverySessionProgress,
    RecoverySessionSummary,
)
from arc_core.domain.types import CollectionId, ImageId
from arc_core.finalized_image_coverage import build_disc_manifest_from_catalog
from arc_core.fs_paths import normalize_relpath
from arc_core.iso.streaming import build_iso_cmd_from_root
from arc_core.planner.manifest import (
    MANIFEST_FILENAME,
    README_FILENAME,
    PlannerFileMeta,
    recovery_readme_bytes,
    sidecar_bytes,
)
from arc_core.ports.archive_store import ArchiveRestoreStatus, ArchiveStore
from arc_core.ports.hot_store import HotStore
from arc_core.proofs import CommandProofStamper, ProofStamper
from arc_core.recovery_payloads import (
    CommandAgeBatchpassRecoveryPayloadCodec,
    RecoveryPayloadCodec,
    encrypt_recovery_payload,
)
from arc_core.runtime_config import RuntimeConfig
from arc_core.services.glacier_pricing import resolve_glacier_pricing
from arc_core.sqlite_db import make_session_factory, session_scope
from arc_core.webhooks import (
    WebhookConfig,
    build_recovery_ready_payload,
    post_webhook,
    utcnow,
)

_ACTIVE_RECOVERY_STATES = {
    RecoverySessionState.PENDING_APPROVAL.value,
    RecoverySessionState.RESTORE_REQUESTED.value,
    RecoverySessionState.READY.value,
}


@dataclass(frozen=True, slots=True)
class _CollectionArchiveObjects:
    collection_id: str
    archive_object_path: str
    manifest_object_path: str
    proof_object_path: str
    manifest_sha256: str
    proof_sha256: str


class SqlAlchemyRecoverySessionService:
    def __init__(
        self,
        config: RuntimeConfig,
        archive_store: ArchiveStore,
        hot_store: HotStore | None = None,
        *,
        proof_stamper: ProofStamper | None = None,
        recovery_payload_codec: RecoveryPayloadCodec | None = None,
    ) -> None:
        self._config = config
        self._archive_store = archive_store
        self._hot_store = hot_store
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
        self._session_factory = make_session_factory(str(config.sqlite_path))

    def get(self, session_id: str) -> RecoverySessionSummary:
        with session_scope(self._session_factory) as session:
            record = session.get(GlacierRecoverySessionRecord, session_id)
            if record is None:
                raise NotFound(f"recovery session not found: {session_id}")
            return _session_summary(session, record, config=self._config)

    def get_for_collection(self, collection_id: str) -> RecoverySessionSummary:
        with session_scope(self._session_factory) as session:
            record = _latest_session_for_collection(session, collection_id)
            if record is None:
                raise NotFound(f"recovery session not found for collection: {collection_id}")
            return _session_summary(session, record, config=self._config)

    def create_or_resume_for_collection(self, collection_id: str) -> RecoverySessionSummary:
        with session_scope(self._session_factory) as session:
            collection = _require_collection(session, collection_id)
            active = _active_session_for_collection(session, collection_id)
            if active is not None:
                return _session_summary(session, active, config=self._config)
            _require_collection_archive_uploaded(collection)
            created = _create_collection_restore_session(
                session,
                config=self._config,
                collection=collection,
            )
            return _session_summary(session, created, config=self._config)

    def get_for_image(self, image_id: str) -> RecoverySessionSummary:
        with session_scope(self._session_factory) as session:
            record = _latest_session_for_image(session, image_id)
            if record is None:
                raise NotFound(f"recovery session not found for image: {image_id}")
            return _session_summary(session, record, config=self._config)

    def create_or_resume_for_image(self, image_id: str) -> RecoverySessionSummary:
        with session_scope(self._session_factory) as session:
            image = _require_image(session, image_id)
            active = _active_session_for_image(session, image_id)
            if active is not None:
                return _session_summary(session, active, config=self._config)
            _require_image_collections_archived(session, image)
            if _protected_copy_count(session, image_id) > 0:
                raise Conflict(
                    "image still has protected copies and does not require "
                    f"archive recovery: {image_id}"
                )
            reusable = _reusable_pending_approval_session(session)
            if reusable is not None:
                attached = _attach_image_to_session(
                    session,
                    record=reusable,
                    image=image,
                    config=self._config,
                )
                return _session_summary(session, attached, config=self._config)
            created = _create_recovery_session(session, config=self._config, image=image)
            return _session_summary(session, created, config=self._config)

    def approve(self, session_id: str) -> RecoverySessionSummary:
        current = utcnow()
        now = _isoformat_z(current)
        estimated_ready_at = _isoformat_z(current + self._config.glacier_recovery_restore_latency)
        with session_scope(self._session_factory) as session:
            record = session.get(GlacierRecoverySessionRecord, session_id)
            if record is None:
                raise NotFound(f"recovery session not found: {session_id}")
            if record.state == RecoverySessionState.EXPIRED.value:
                raise InvalidState(
                    "recovery session expired; re-initiate recovery to request restore"
                )
            if record.state != RecoverySessionState.PENDING_APPROVAL.value:
                raise InvalidState("recovery session is not waiting for approval")
            if (record.type or "image_rebuild") == "image_rebuild":
                _sync_session_collections_for_images(session, record)
                session.flush()
            collections = _session_collections(session, record=record)
            if not collections:
                raise InvalidState("recovery session has no collection archives to restore")
            statuses = [
                self._archive_store.request_collection_archive_restore(
                    collection_id=archive.collection_id,
                    object_path=archive.archive_object_path,
                    retrieval_tier=record.retrieval_tier,
                    hold_days=record.hold_days,
                    requested_at=now,
                    estimated_ready_at=estimated_ready_at,
                    manifest_object_path=archive.manifest_object_path,
                    proof_object_path=archive.proof_object_path,
                )
                for archive in (
                    _require_collection_archive_objects(collection)
                    for collection in collections
                )
            ]
            record.state = RecoverySessionState.RESTORE_REQUESTED.value
            record.approved_at = now
            record.restore_requested_at = now
            record.restore_ready_at = _max_timestamp(
                status.ready_at for status in statuses if status.ready_at is not None
            ) or estimated_ready_at
            record.restore_expires_at = _min_timestamp(
                status.expires_at for status in statuses if status.expires_at is not None
            )
            record.restore_next_poll_at = _isoformat_z(
                current + self._config.glacier_recovery_sweep_interval
            )
            record.latest_message = (
                "Archive restore requested; wait for the ready notification before downloading or "
                "burning replacement media."
            )
            return _session_summary(session, record, config=self._config)

    def complete(self, session_id: str) -> RecoverySessionSummary:
        now = _isoformat_z(utcnow())
        with session_scope(self._session_factory) as session:
            record = session.get(GlacierRecoverySessionRecord, session_id)
            if record is None:
                raise NotFound(f"recovery session not found: {session_id}")
            if record.state not in {
                RecoverySessionState.READY.value,
                RecoverySessionState.EXPIRED.value,
            }:
                raise InvalidState("recovery session is not ready to complete")
            collections = _session_collections(session, record=record)
            if not collections:
                raise InvalidState("recovery session has no collection archives to complete")
            if record.state == RecoverySessionState.READY.value:
                record.archive_verification_state = "in_progress"
                session.flush()
                _verify_restored_collection_archives(
                    session,
                    archive_store=self._archive_store,
                    collections=collections,
                )
                record.archive_verification_state = "completed"
            for collection in collections:
                archive = _require_collection_archive_objects(collection)
                self._archive_store.cleanup_collection_archive_restore(
                    collection_id=collection.id,
                    object_path=archive.archive_object_path,
                    manifest_object_path=archive.manifest_object_path,
                    proof_object_path=archive.proof_object_path,
                )
            record.state = RecoverySessionState.COMPLETED.value
            record.completed_at = now
            record.next_reminder_at = None
            record.restore_expires_at = now
            record.latest_message = (
                "Recovery session completed and restored ISO cleanup was recorded."
            )
            return _session_summary(session, record, config=self._config)

    def materialize_collection_files(
        self,
        session_id: str,
        collection_id: str,
        *,
        paths: Sequence[str],
    ) -> RecoverySessionSummary:
        if self._hot_store is None:
            raise InvalidState("recovery session service has no hot store for materialization")
        selected_paths = {normalize_relpath(path) for path in paths}
        if not selected_paths:
            raise InvalidState("at least one collection file path is required")
        with session_scope(self._session_factory) as session:
            record = session.get(GlacierRecoverySessionRecord, session_id)
            if record is None:
                raise NotFound(f"recovery session not found: {session_id}")
            if record.state != RecoverySessionState.READY.value:
                raise InvalidState("recovery session is not ready to materialize files")
            collection = _require_collection(session, collection_id)
            session_collection_ids = {
                current.id for current in _session_collections(session, record=record)
            }
            if collection.id not in session_collection_ids:
                raise NotFound(f"collection not found in recovery session: {collection_id}")
            expected_files = _collection_archive_expected_files(
                session,
                collection_id=collection.id,
            )
            expected_paths = {file.path for file in expected_files}
            missing_paths = sorted(selected_paths - expected_paths)
            if missing_paths:
                raise NotFound(f"collection file not found: {missing_paths[0]}")
            archive = _require_collection_archive_objects(collection)
            record.archive_verification_state = "in_progress"
            record.extraction_state = "in_progress"
            record.materialization_state = "in_progress"
            session.flush()
            manifest_bytes = self._archive_store.read_restored_collection_archive_manifest(
                collection_id=archive.collection_id,
                object_path=archive.manifest_object_path,
            )
            verify_collection_archive_manifest(
                manifest_bytes=manifest_bytes,
                expected_sha256=archive.manifest_sha256,
                collection_id=archive.collection_id,
                files=expected_files,
            )
            proof_bytes = self._archive_store.read_restored_collection_archive_proof(
                collection_id=archive.collection_id,
                object_path=archive.proof_object_path,
            )
            verify_collection_archive_proof(
                proof_bytes=proof_bytes,
                expected_sha256=archive.proof_sha256,
                manifest_bytes=manifest_bytes,
            )
            record.archive_verification_state = "completed"
            materialized: list[str] = []
            for path, content in iter_verified_collection_archive_files(
                self._archive_store.iter_restored_collection_archive(
                    collection_id=archive.collection_id,
                    object_path=archive.archive_object_path,
                ),
                files=expected_files,
                selected_paths=selected_paths,
            ):
                self._hot_store.put_collection_file(collection.id, path, content)
                row = session.get(
                    CollectionFileRecord,
                    {"collection_id": collection.id, "path": path},
                )
                if row is not None:
                    row.hot = True
                materialized.append(path)
            record.extraction_state = "completed"
            record.materialization_state = "completed"
            record.latest_message = (
                "Selected collection files were verified and materialized to hot storage."
            )
            if len(materialized) != len(selected_paths):
                missing = sorted(selected_paths - set(materialized))
                raise ValueError(f"collection archive missing selected member: {missing[0]}")
            return _session_summary(session, record, config=self._config)

    def iter_restored_iso(self, session_id: str, image_id: str) -> Iterator[bytes]:
        with session_scope(self._session_factory) as session:
            record = session.get(GlacierRecoverySessionRecord, session_id)
            if record is None:
                raise NotFound(f"recovery session not found: {session_id}")
            if record.state != RecoverySessionState.READY.value:
                raise InvalidState("recovery session is not ready for ISO download")
            images = {
                image.image_id: image
                for image in _session_images(session, record=record)
            }
            image = images.get(image_id)
            if image is None:
                raise NotFound(f"image not found in recovery session: {image_id}")
            collections = _session_collections(session, record=record)
            if (record.type or "image_rebuild") == "image_rebuild" and collections:
                collection_archives = tuple(
                    _require_collection_archive_objects(collection)
                    for collection in collections
                )
                collection_artifacts = tuple(
                    session.scalars(
                        select(FinalizedImageCollectionArtifactRecord).where(
                            FinalizedImageCollectionArtifactRecord.image_id == image_id
                        )
                    ).all()
                )
                coverage_parts = tuple(
                    session.scalars(
                        select(FinalizedImageCoveragePartRecord).where(
                            FinalizedImageCoveragePartRecord.image_id == image_id
                        )
                    ).all()
                )
                file_lookup = {
                    (file.collection_id, file.path): (file.sha256, file.bytes)
                    for file in session.scalars(select(CollectionFileRecord)).all()
                }
                return _iter_rebuilt_iso_from_collection_archives(
                    archive_store=self._archive_store,
                    image_id=image_id,
                    filename=image.filename,
                    collection_archives=collection_archives,
                    collection_artifacts=collection_artifacts,
                    coverage_parts=coverage_parts,
                    file_lookup=file_lookup,
                    proof_stamper=self._proof_stamper,
                    recovery_payload_codec=self._recovery_payload_codec,
                )
            raise InvalidState("recovery session has no collection archives to rebuild image")
        raise InvalidState("collection restore sessions do not provide ISO downloads")

    def process_due_sessions(self, *, limit: int = 100) -> int:
        if limit < 1:
            return 0

        current = utcnow()
        current_text = _isoformat_z(current)
        processed = 0
        with session_scope(self._session_factory) as session:
            due_ids = session.scalars(
                select(GlacierRecoverySessionRecord.session_id)
                .where(
                    or_(
                        (
                            (GlacierRecoverySessionRecord.state
                             == RecoverySessionState.RESTORE_REQUESTED.value)
                            & (
                                (
                                    GlacierRecoverySessionRecord.restore_next_poll_at.is_(None)
                                )
                                | (
                                    GlacierRecoverySessionRecord.restore_next_poll_at
                                    <= current_text
                                )
                                | (
                                    GlacierRecoverySessionRecord.restore_ready_at <= current_text
                                )
                            )
                        ),
                        (
                            (GlacierRecoverySessionRecord.state == RecoverySessionState.READY.value)
                            & (
                                (
                                    GlacierRecoverySessionRecord.restore_expires_at.is_not(None)
                                )
                                & (GlacierRecoverySessionRecord.restore_expires_at <= current_text)
                            )
                        ),
                        (
                            (GlacierRecoverySessionRecord.state == RecoverySessionState.READY.value)
                            & (
                                (
                                    GlacierRecoverySessionRecord.next_reminder_at.is_not(None)
                                )
                                & (GlacierRecoverySessionRecord.next_reminder_at <= current_text)
                            )
                        ),
                    )
                )
                .order_by(
                    GlacierRecoverySessionRecord.created_at,
                    GlacierRecoverySessionRecord.session_id,
                )
                .limit(limit)
            ).all()

        for session_id in due_ids:
            self._process_one(session_id=session_id)
            processed += 1
        return processed

    def _process_one(self, *, session_id: str) -> None:
        current = utcnow()
        current_text = _isoformat_z(current)
        with session_scope(self._session_factory) as session:
            record = session.get(GlacierRecoverySessionRecord, session_id)
            if record is None:
                return
            if (record.type or "image_rebuild") == "image_rebuild":
                _sync_session_collections_for_images(session, record)
                session.flush()

            if (
                record.state == RecoverySessionState.RESTORE_REQUESTED.value
            ):
                status = self._session_restore_status(session, record=record, current=current)
                if status.state == "ready":
                    record.state = RecoverySessionState.READY.value
                    record.restore_ready_at = status.ready_at or current_text
                    record.restore_expires_at = status.expires_at or _isoformat_z(
                        current + self._config.glacier_recovery_ready_ttl
                    )
                    record.restore_next_poll_at = None
                    record.latest_message = (
                        "Restored ISO data is ready; reopen the session to complete download, "
                        "verify the ISO, and burn replacement media before cleanup."
                    )
                    _notify_recovery_ready(
                        session,
                        record=record,
                        config=self._config,
                        current=current,
                        reminder=False,
                    )
                    return
                if status.state == "expired":
                    record.state = RecoverySessionState.EXPIRED.value
                    record.next_reminder_at = None
                    record.restore_next_poll_at = None
                    record.latest_message = (
                        "Restored ISO data expired and cleanup was recorded; re-initiate "
                        "recovery to request a new restore."
                    )
                    return
                record.restore_next_poll_at = _isoformat_z(
                    current + self._config.glacier_recovery_sweep_interval
                )
                record.latest_message = (
                    status.message
                    or "Archive restore is still in progress; Riverhog will poll again."
                )
                return

            if (
                record.state == RecoverySessionState.READY.value
                and record.next_reminder_at is not None
                and record.next_reminder_at <= current_text
            ):
                initial_notification_succeeded = record.last_notified_at is not None
                _notify_recovery_ready(
                    session,
                    record=record,
                    config=self._config,
                    current=current,
                    reminder=initial_notification_succeeded,
                )
                return

            if (
                record.state == RecoverySessionState.READY.value
                and record.restore_expires_at is not None
                and record.restore_expires_at <= current_text
            ):
                for collection in _session_collections(session, record=record):
                    archive = _require_collection_archive_objects(collection)
                    self._archive_store.cleanup_collection_archive_restore(
                        collection_id=collection.id,
                        object_path=archive.archive_object_path,
                        manifest_object_path=archive.manifest_object_path,
                        proof_object_path=archive.proof_object_path,
                    )
                record.state = RecoverySessionState.EXPIRED.value
                record.next_reminder_at = None
                record.restore_next_poll_at = None
                record.latest_message = (
                    "Restored ISO data expired and cleanup was recorded; re-initiate recovery to "
                    "request a new restore."
                )

    def _session_restore_status(
        self,
        session: Session,
        *,
        record: GlacierRecoverySessionRecord,
        current: datetime,
    ) -> ArchiveRestoreStatus:
        if (record.type or "image_rebuild") == "image_rebuild":
            _sync_session_collections_for_images(session, record)
            session.flush()
        collections = _session_collections(session, record=record)
        if not collections:
            return ArchiveRestoreStatus(
                state="requested",
                message="Recovery session has no collection archives to poll.",
            )
        statuses = [
            self._archive_store.get_collection_archive_restore_status(
                collection_id=archive.collection_id,
                object_path=archive.archive_object_path,
                requested_at=record.restore_requested_at or _isoformat_z(current),
                estimated_ready_at=record.restore_ready_at,
                estimated_expires_at=record.restore_expires_at,
                manifest_object_path=archive.manifest_object_path,
                proof_object_path=archive.proof_object_path,
            )
            for archive in (
                _require_collection_archive_objects(collection)
                for collection in collections
            )
        ]
        if any(status.state == "expired" for status in statuses):
            return ArchiveRestoreStatus(state="expired")
        if statuses and all(status.state == "ready" for status in statuses):
            return ArchiveRestoreStatus(
                state="ready",
                ready_at=_max_timestamp(
                    status.ready_at for status in statuses if status.ready_at is not None
                ),
                expires_at=_min_timestamp(
                    status.expires_at for status in statuses if status.expires_at is not None
                ),
            )
        return ArchiveRestoreStatus(
            state="requested",
            message="Archive restore is still in progress; Riverhog will poll again.",
        )


def ensure_glacier_recovery_session_for_image(
    session: Session,
    *,
    config: RuntimeConfig,
    image_id: str,
) -> None:
    image = session.get(FinalizedImageRecord, image_id)
    if image is None:
        return
    if not _image_collections_archived(session, image):
        return
    if _protected_copy_count(session, image_id) > 0:
        return
    if not _has_recovery_triggering_copy_history(session, image_id):
        return
    if _active_session_for_image(session, image_id) is not None:
        return
    reusable = _reusable_pending_approval_session(session)
    if reusable is not None:
        _attach_image_to_session(session, record=reusable, image=image, config=config)
        return
    _create_recovery_session(session, config=config, image=image)


def _create_recovery_session(
    session: Session,
    *,
    config: RuntimeConfig,
    image: FinalizedImageRecord,
) -> GlacierRecoverySessionRecord:
    existing_ids = session.scalars(
        select(GlacierRecoverySessionRecord.session_id)
        .join(
            GlacierRecoverySessionImageRecord,
            GlacierRecoverySessionImageRecord.session_id == GlacierRecoverySessionRecord.session_id,
        )
        .where(GlacierRecoverySessionImageRecord.image_id == image.image_id)
    ).all()
    session_id = _generated_recovery_session_id(image.image_id, existing_ids=existing_ids)
    created_at = _isoformat_z(utcnow())
    collections = [
        _require_collection(session, collection_id)
        for collection_id in _image_collection_ids(session, image.image_id)
    ]
    if not collections:
        raise InvalidState(
            f"image has no collection archives and cannot be rebuilt: {image.image_id}"
        )
    estimate = _estimate_collection_recovery_costs(config=config, collections=collections)
    warnings = _build_warnings(config=config)
    record = GlacierRecoverySessionRecord(
        session_id=session_id,
        type="image_rebuild",
        state=RecoverySessionState.PENDING_APPROVAL.value,
        created_at=created_at,
        approved_at=None,
        restore_requested_at=None,
        restore_ready_at=None,
        restore_next_poll_at=None,
        restore_expires_at=None,
        completed_at=None,
        latest_message=(
            "Approve the estimated restore cost before Riverhog requests archive restore."
        ),
        retrieval_tier=config.glacier_recovery_retrieval_tier,
        hold_days=max(int(config.glacier_recovery_ready_ttl.total_seconds() // 86400), 1),
        estimate_json=json.dumps(asdict(estimate), sort_keys=True),
        warnings_json=json.dumps(list(warnings)),
        reminder_count=0,
        next_reminder_at=None,
        last_notified_at=None,
    )
    session.add(record)
    session.flush()
    session.add(
        GlacierRecoverySessionImageRecord(
            session_id=session_id,
            image_id=image.image_id,
            image_order=0,
        )
    )
    _sync_session_collections_for_images(session, record)
    session.flush()
    return record


def _create_collection_restore_session(
    session: Session,
    *,
    config: RuntimeConfig,
    collection: CollectionRecord,
) -> GlacierRecoverySessionRecord:
    existing_ids = session.scalars(
        select(GlacierRecoverySessionRecord.session_id)
        .join(
            GlacierRecoverySessionCollectionRecord,
            GlacierRecoverySessionCollectionRecord.session_id
            == GlacierRecoverySessionRecord.session_id,
        )
        .where(GlacierRecoverySessionCollectionRecord.collection_id == collection.id)
    ).all()
    session_id = _generated_collection_restore_session_id(
        collection.id,
        existing_ids=existing_ids,
    )
    created_at = _isoformat_z(utcnow())
    estimate = _estimate_collection_recovery_costs(config=config, collections=(collection,))
    warnings = _build_warnings(config=config)
    record = GlacierRecoverySessionRecord(
        session_id=session_id,
        type="collection_restore",
        state=RecoverySessionState.PENDING_APPROVAL.value,
        created_at=created_at,
        approved_at=None,
        restore_requested_at=None,
        restore_ready_at=None,
        restore_next_poll_at=None,
        restore_expires_at=None,
        completed_at=None,
        latest_message=(
            "Approve the estimated restore cost before Riverhog requests archive restore."
        ),
        retrieval_tier=config.glacier_recovery_retrieval_tier,
        hold_days=max(int(config.glacier_recovery_ready_ttl.total_seconds() // 86400), 1),
        estimate_json=json.dumps(asdict(estimate), sort_keys=True),
        warnings_json=json.dumps(list(warnings)),
        reminder_count=0,
        next_reminder_at=None,
        last_notified_at=None,
    )
    session.add(record)
    session.flush()
    session.add(
        GlacierRecoverySessionCollectionRecord(
            session_id=session_id,
            collection_id=collection.id,
            collection_order=0,
        )
    )
    session.flush()
    return record


def _attach_image_to_session(
    session: Session,
    *,
    record: GlacierRecoverySessionRecord,
    image: FinalizedImageRecord,
    config: RuntimeConfig,
) -> GlacierRecoverySessionRecord:
    existing_image_ids = {
        row.image_id
        for row in session.scalars(
            select(GlacierRecoverySessionImageRecord).where(
                GlacierRecoverySessionImageRecord.session_id == record.session_id
            )
        ).all()
    }
    if image.image_id in existing_image_ids:
        return record
    next_order = len(existing_image_ids)
    session.add(
        GlacierRecoverySessionImageRecord(
            session_id=record.session_id,
            image_id=image.image_id,
            image_order=next_order,
        )
    )
    session.flush()
    _sync_session_collections_for_images(session, record)
    session.flush()
    _refresh_recovery_session_metadata(session, record=record, config=config)
    return record


def _require_image(session: Session, image_id: str) -> FinalizedImageRecord:
    image = cast(FinalizedImageRecord | None, session.get(FinalizedImageRecord, image_id))
    if image is None:
        raise NotFound(f"image not found: {image_id}")
    return image


def _require_collection(session: Session, collection_id: str) -> CollectionRecord:
    collection = cast(CollectionRecord | None, session.get(CollectionRecord, collection_id))
    if collection is None:
        raise NotFound(f"collection not found: {collection_id}")
    return collection


def _require_collection_archive_uploaded(collection: CollectionRecord) -> None:
    archive = collection.archive
    if archive is None or normalize_glacier_state(archive.state) != GlacierState.UPLOADED:
        raise InvalidState(
            f"collection archive is not uploaded and cannot be restored yet: {collection.id}"
        )


def _require_collection_archive_object_path(collection: CollectionRecord) -> str:
    archive = collection.archive
    if archive is None or not archive.object_path:
        raise InvalidState(
            f"collection archive object path is missing and cannot be restored: {collection.id}"
        )
    return archive.object_path


def _require_collection_archive_objects(collection: CollectionRecord) -> _CollectionArchiveObjects:
    archive = collection.archive
    if archive is None or not archive.object_path:
        raise InvalidState(
            f"collection archive object path is missing and cannot be restored: {collection.id}"
        )
    if not archive.manifest_object_path:
        raise InvalidState(
            f"collection archive manifest object path is missing and cannot be restored: "
            f"{collection.id}"
        )
    if not archive.ots_object_path:
        raise InvalidState(
            f"collection archive proof object path is missing and cannot be restored: "
            f"{collection.id}"
        )
    if not archive.manifest_sha256:
        raise InvalidState(
            f"collection archive manifest sha256 is missing and cannot be verified: "
            f"{collection.id}"
        )
    if not archive.ots_sha256:
        raise InvalidState(
            f"collection archive proof sha256 is missing and cannot be verified: {collection.id}"
        )
    return _CollectionArchiveObjects(
        collection_id=collection.id,
        archive_object_path=archive.object_path,
        manifest_object_path=archive.manifest_object_path,
        proof_object_path=archive.ots_object_path,
        manifest_sha256=archive.manifest_sha256,
        proof_sha256=archive.ots_sha256,
    )


def _iter_rebuilt_iso_from_collection_archives(
    *,
    archive_store: ArchiveStore,
    image_id: str,
    filename: str,
    collection_archives: Sequence[_CollectionArchiveObjects],
    collection_artifacts: Sequence[FinalizedImageCollectionArtifactRecord],
    coverage_parts: Sequence[FinalizedImageCoveragePartRecord],
    file_lookup: dict[tuple[str, str], tuple[str, int]],
    proof_stamper: ProofStamper,
    recovery_payload_codec: RecoveryPayloadCodec,
) -> Iterator[bytes]:
    with tempfile.TemporaryDirectory(prefix="arc-rebuilt-iso-") as tmpdir:
        work_root = Path(tmpdir)
        image_root = work_root / "image-root"
        image_root.mkdir()
        restored_files = _restore_collection_archives(
            archive_store=archive_store,
            collection_archives=collection_archives,
            work_root=work_root,
            file_lookup=file_lookup,
        )
        _write_rebuilt_collection_artifacts(
            image_root=image_root,
            collection_artifacts=collection_artifacts,
            restored_files=restored_files,
            work_root=work_root,
            proof_stamper=proof_stamper,
            recovery_payload_codec=recovery_payload_codec,
        )
        _write_rebuilt_image_payloads(
            image_root=image_root,
            coverage_parts=coverage_parts,
            restored_files=restored_files,
            file_lookup=file_lookup,
            recovery_payload_codec=recovery_payload_codec,
        )
        manifest = build_disc_manifest_from_catalog(
            image_id=image_id,
            collection_artifacts=collection_artifacts,
            coverage_parts=coverage_parts,
            file_lookup=file_lookup,
        )
        _write_image_root_file(
            image_root,
            MANIFEST_FILENAME,
            encrypt_recovery_payload(manifest, recovery_payload_codec),
        )
        _write_image_root_file(image_root, README_FILENAME, recovery_readme_bytes(image_id))
        yield from _run_iso_from_root(
            image_root=image_root,
            volume_id=image_id,
            filename=filename,
        )


def _restore_collection_archives(
    *,
    archive_store: ArchiveStore,
    collection_archives: Sequence[_CollectionArchiveObjects],
    work_root: Path,
    file_lookup: dict[tuple[str, str], tuple[str, int]],
) -> dict[tuple[str, str], bytes]:
    restored_files: dict[tuple[str, str], bytes] = {}
    for collection_archive in collection_archives:
        _verify_restored_collection_archive(
            archive_store=archive_store,
            archive=collection_archive,
            expected_files=_expected_files_from_lookup(
                file_lookup=file_lookup,
                collection_id=collection_archive.collection_id,
            ),
        )
        collection_root = work_root / "collections" / collection_archive.collection_id
        collection_root.mkdir(parents=True, exist_ok=True)
        archive_chunks = (
            archive_store.iter_restored_collection_archive(
                collection_id=collection_archive.collection_id,
                object_path=collection_archive.archive_object_path,
            )
        )
        for path, content in iter_collection_archive_files(archive_chunks):
            expected = file_lookup.get((collection_archive.collection_id, path))
            if expected is not None:
                verify_collection_archive_member(
                    path=path,
                    content=content,
                    expected_sha256=expected[0],
                )
            restored_files[(collection_archive.collection_id, path)] = content
            _write_image_root_file(collection_root, path, content)
    return restored_files


def _verify_restored_collection_archives(
    session: Session,
    *,
    archive_store: ArchiveStore,
    collections: Sequence[CollectionRecord],
) -> None:
    for collection in collections:
        archive = _require_collection_archive_objects(collection)
        _verify_restored_collection_archive(
            archive_store=archive_store,
            archive=archive,
            expected_files=_collection_archive_expected_files(
                session,
                collection_id=collection.id,
            ),
        )


def _verify_restored_collection_archive(
    *,
    archive_store: ArchiveStore,
    archive: _CollectionArchiveObjects,
    expected_files: Sequence[CollectionArchiveExpectedFile],
) -> None:
    manifest_bytes = archive_store.read_restored_collection_archive_manifest(
        collection_id=archive.collection_id,
        object_path=archive.manifest_object_path,
    )
    verify_collection_archive_manifest(
        manifest_bytes=manifest_bytes,
        expected_sha256=archive.manifest_sha256,
        collection_id=archive.collection_id,
        files=expected_files,
    )
    proof_bytes = archive_store.read_restored_collection_archive_proof(
        collection_id=archive.collection_id,
        object_path=archive.proof_object_path,
    )
    verify_collection_archive_proof(
        proof_bytes=proof_bytes,
        expected_sha256=archive.proof_sha256,
        manifest_bytes=manifest_bytes,
    )
    verify_collection_archive_files(
        chunks=archive_store.iter_restored_collection_archive(
            collection_id=archive.collection_id,
            object_path=archive.archive_object_path,
        ),
        files=expected_files,
    )


def _collection_archive_expected_files(
    session: Session,
    *,
    collection_id: str,
) -> tuple[CollectionArchiveExpectedFile, ...]:
    rows = session.scalars(
        select(CollectionFileRecord)
        .where(CollectionFileRecord.collection_id == collection_id)
        .order_by(CollectionFileRecord.path)
    ).all()
    return tuple(
        CollectionArchiveExpectedFile(
            path=row.path,
            bytes=row.bytes,
            sha256=row.sha256,
        )
        for row in rows
    )


def _expected_files_from_lookup(
    *,
    file_lookup: dict[tuple[str, str], tuple[str, int]],
    collection_id: str,
) -> tuple[CollectionArchiveExpectedFile, ...]:
    return tuple(
        CollectionArchiveExpectedFile(path=path, sha256=sha256, bytes=byte_count)
        for (current_collection_id, path), (sha256, byte_count) in sorted(
            file_lookup.items()
        )
        if current_collection_id == collection_id
    )


def _write_rebuilt_collection_artifacts(
    *,
    image_root: Path,
    collection_artifacts: Sequence[FinalizedImageCollectionArtifactRecord],
    restored_files: dict[tuple[str, str], bytes],
    work_root: Path,
    proof_stamper: ProofStamper,
    recovery_payload_codec: RecoveryPayloadCodec,
) -> None:
    collection_ids = sorted({collection_id for collection_id, _ in restored_files})
    for collection_id in collection_ids:
        collection_root = work_root / "collections" / collection_id
        artifact_root = work_root / "collection-artifacts" / collection_id
        generated = generate_collection_hash_artifacts(
            collection_id=collection_id,
            source_root=collection_root,
            artifact_root=artifact_root,
            stamper=proof_stamper,
        )
        artifact = next(
            (
                current
                for current in collection_artifacts
                if current.collection_id == collection_id
            ),
            None,
        )
        if artifact is None:
            raise InvalidState(f"image collection artifact is missing: {collection_id}")
        _write_image_root_file(
            image_root,
            artifact.manifest_path,
            encrypt_recovery_payload(
                generated.manifest_path.read_bytes(),
                recovery_payload_codec,
            ),
        )
        _write_image_root_file(
            image_root,
            artifact.proof_path,
            encrypt_recovery_payload(
                generated.proof_path.read_bytes(),
                recovery_payload_codec,
            ),
        )


def _write_rebuilt_image_payloads(
    *,
    image_root: Path,
    coverage_parts: Sequence[FinalizedImageCoveragePartRecord],
    restored_files: dict[tuple[str, str], bytes],
    file_lookup: dict[tuple[str, str], tuple[str, int]],
    recovery_payload_codec: RecoveryPayloadCodec,
) -> None:
    for part in coverage_parts:
        if part.object_path is None or part.sidecar_path is None:
            raise InvalidState(
                "finalized image coverage part is missing persisted artifact paths: "
                f"{part.collection_id}/{part.path}"
            )
        content = restored_files.get((part.collection_id, part.path))
        if content is None:
            raise InvalidState(
                f"restored collection archive is missing {part.collection_id}/{part.path}"
            )
        sha256, plaintext_bytes = file_lookup[(part.collection_id, part.path)]
        file_meta = cast(
            PlannerFileMeta,
            {
                "relpath": part.path,
                "sha256": sha256,
                "plaintext_bytes": plaintext_bytes,
                "mode": 0o644,
                "mtime": None,
                "uid": None,
                "gid": None,
            },
        )
        _write_image_root_file(
            image_root,
            part.object_path,
            encrypt_recovery_payload(
                _content_part(content, part_index=part.part_index, part_count=part.part_count),
                recovery_payload_codec,
            ),
        )
        _write_image_root_file(
            image_root,
            part.sidecar_path,
            encrypt_recovery_payload(
                sidecar_bytes(
                    file_meta,
                    collection_id=part.collection_id,
                    part_index=part.part_index,
                    part_count=part.part_count,
                ),
                recovery_payload_codec,
            ),
        )


def _content_part(content: bytes, *, part_index: int, part_count: int) -> bytes:
    if part_count < 1 or part_index < 0 or part_index >= part_count:
        raise InvalidState("invalid rebuilt image part index")
    base, remainder = divmod(len(content), part_count)
    start = part_index * base + min(part_index, remainder)
    size = base + int(part_index < remainder)
    return content[start : start + size]


def _write_image_root_file(root: Path, relpath: str, content: bytes) -> None:
    dest = root / normalize_relpath(relpath)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(content)


def _run_iso_from_root(*, image_root: Path, volume_id: str, filename: str) -> Iterator[bytes]:
    _ = filename
    proc = subprocess.run(
        build_iso_cmd_from_root(image_root=image_root, volume_id=volume_id),
        capture_output=True,
        check=False,
    )
    if proc.returncode != 0:
        detail = proc.stderr.decode("utf-8", errors="replace")[-1500:]
        raise RuntimeError(detail or f"xorriso exited {proc.returncode}")
    yield proc.stdout


def _image_collections_archived(session: Session, image: FinalizedImageRecord) -> bool:
    collection_ids = _image_collection_ids(session, image.image_id)
    if not collection_ids:
        return False
    for collection_id in collection_ids:
        collection = session.get(CollectionRecord, collection_id)
        if collection is None:
            return False
        archive = collection.archive
        if archive is None or normalize_glacier_state(archive.state) != GlacierState.UPLOADED:
            return False
    return True


def _require_image_collections_archived(session: Session, image: FinalizedImageRecord) -> None:
    if not _image_collections_archived(session, image):
        raise InvalidState(
            f"image collections are not archived and cannot be rebuilt yet: {image.image_id}"
        )


def _image_collection_ids(session: Session, image_id: str) -> list[str]:
    return sorted(
        set(
            session.scalars(
                select(FinalizedImageCoveredPathRecord.collection_id).where(
                    FinalizedImageCoveredPathRecord.image_id == image_id
                )
            ).all()
        )
    )


def _protected_copy_count(session: Session, image_id: str) -> int:
    rows = session.scalars(
        select(ImageCopyRecord.state).where(ImageCopyRecord.image_id == image_id)
    ).all()
    return sum(1 for state in rows if copy_counts_toward_protection(state))


def _has_recovery_triggering_copy_history(session: Session, image_id: str) -> bool:
    rows = session.scalars(
        select(ImageCopyRecord.state).where(ImageCopyRecord.image_id == image_id)
    ).all()
    return any(
        normalize_copy_state(state) not in {CopyState.NEEDED, CopyState.BURNING}
        for state in rows
    )


def _active_session_for_image(
    session: Session,
    image_id: str,
) -> GlacierRecoverySessionRecord | None:
    return cast(
        GlacierRecoverySessionRecord | None,
        session.scalar(
        select(GlacierRecoverySessionRecord)
        .join(
            GlacierRecoverySessionImageRecord,
            GlacierRecoverySessionImageRecord.session_id == GlacierRecoverySessionRecord.session_id,
        )
        .where(GlacierRecoverySessionImageRecord.image_id == image_id)
        .where(GlacierRecoverySessionRecord.state.in_(_ACTIVE_RECOVERY_STATES))
        .order_by(GlacierRecoverySessionRecord.created_at.desc())
        .limit(1)
    )
    )


def _active_session_for_collection(
    session: Session,
    collection_id: str,
) -> GlacierRecoverySessionRecord | None:
    return cast(
        GlacierRecoverySessionRecord | None,
        session.scalar(
        select(GlacierRecoverySessionRecord)
        .join(
            GlacierRecoverySessionCollectionRecord,
            GlacierRecoverySessionCollectionRecord.session_id
            == GlacierRecoverySessionRecord.session_id,
        )
        .where(GlacierRecoverySessionCollectionRecord.collection_id == collection_id)
        .where(GlacierRecoverySessionRecord.state.in_(_ACTIVE_RECOVERY_STATES))
        .order_by(GlacierRecoverySessionRecord.created_at.desc())
        .limit(1)
    )
    )


def _latest_session_for_image(
    session: Session,
    image_id: str,
) -> GlacierRecoverySessionRecord | None:
    return cast(
        GlacierRecoverySessionRecord | None,
        session.scalar(
        select(GlacierRecoverySessionRecord)
        .join(
            GlacierRecoverySessionImageRecord,
            GlacierRecoverySessionImageRecord.session_id == GlacierRecoverySessionRecord.session_id,
        )
        .where(GlacierRecoverySessionImageRecord.image_id == image_id)
        .order_by(GlacierRecoverySessionRecord.created_at.desc())
        .limit(1)
    )
    )


def _latest_session_for_collection(
    session: Session,
    collection_id: str,
) -> GlacierRecoverySessionRecord | None:
    return cast(
        GlacierRecoverySessionRecord | None,
        session.scalar(
        select(GlacierRecoverySessionRecord)
        .join(
            GlacierRecoverySessionCollectionRecord,
            GlacierRecoverySessionCollectionRecord.session_id
            == GlacierRecoverySessionRecord.session_id,
        )
        .where(GlacierRecoverySessionCollectionRecord.collection_id == collection_id)
        .order_by(GlacierRecoverySessionRecord.created_at.desc())
        .limit(1)
    )
    )


def _reusable_pending_approval_session(session: Session) -> GlacierRecoverySessionRecord | None:
    return cast(
        GlacierRecoverySessionRecord | None,
        session.scalar(
        select(GlacierRecoverySessionRecord)
        .where(GlacierRecoverySessionRecord.state == RecoverySessionState.PENDING_APPROVAL.value)
        .order_by(GlacierRecoverySessionRecord.created_at.desc())
        .limit(1)
    )
    )


def _session_images(
    session: Session,
    *,
    record: GlacierRecoverySessionRecord,
) -> list[FinalizedImageRecord]:
    image_rows = session.scalars(
        select(GlacierRecoverySessionImageRecord)
        .where(GlacierRecoverySessionImageRecord.session_id == record.session_id)
        .order_by(GlacierRecoverySessionImageRecord.image_order)
    ).all()
    return [_require_image(session, image_row.image_id) for image_row in image_rows]


def _session_collections(
    session: Session,
    *,
    record: GlacierRecoverySessionRecord,
) -> list[CollectionRecord]:
    collection_rows = session.scalars(
        select(GlacierRecoverySessionCollectionRecord)
        .where(GlacierRecoverySessionCollectionRecord.session_id == record.session_id)
        .order_by(GlacierRecoverySessionCollectionRecord.collection_order)
    ).all()
    return [_require_collection(session, row.collection_id) for row in collection_rows]


def _sync_session_collections_for_images(
    session: Session,
    record: GlacierRecoverySessionRecord,
) -> None:
    collection_ids: list[str] = []
    for image in _session_images(session, record=record):
        for collection_id in _image_collection_ids(session, image.image_id):
            if collection_id not in collection_ids:
                collection = session.get(CollectionRecord, collection_id)
                if (
                    collection is None
                    or collection.archive is None
                    or normalize_glacier_state(collection.archive.state)
                    != GlacierState.UPLOADED
                ):
                    continue
                collection_ids.append(collection_id)
    existing = {
        row.collection_id
        for row in session.scalars(
            select(GlacierRecoverySessionCollectionRecord).where(
                GlacierRecoverySessionCollectionRecord.session_id == record.session_id
            )
        ).all()
    }
    for index, collection_id in enumerate(collection_ids):
        if collection_id in existing:
            continue
        session.add(
            GlacierRecoverySessionCollectionRecord(
                session_id=record.session_id,
                collection_id=collection_id,
                collection_order=index,
            )
        )


def _session_summary(
    session: Session,
    record: GlacierRecoverySessionRecord,
    *,
    config: RuntimeConfig,
) -> RecoverySessionSummary:
    if (record.type or "image_rebuild") == "image_rebuild":
        _sync_session_collections_for_images(session, record)
        session.flush()
    collections: list[RecoverySessionCollection] = []
    for collection in _session_collections(session, record=record):
        archive = collection.archive
        collections.append(
            RecoverySessionCollection(
                id=CollectionId(collection.id),
                glacier=_collection_glacier_archive_status(archive),
                archive_manifest=_collection_archive_manifest_status(archive),
                stored_bytes=_collection_stored_bytes(archive),
            )
        )
    images: list[RecoverySessionImage] = []
    for image in _session_images(session, record=record):
        collection_ids = tuple(
            CollectionId(collection_id)
            for collection_id in _image_collection_ids(session, image.image_id)
        )
        images.append(
            RecoverySessionImage(
                id=ImageId(image.image_id),
                filename=image.filename,
                collection_ids=collection_ids,
                rebuild_state=_recovery_session_image_rebuild_state(record),
            )
        )
    estimate = RecoveryCostEstimate(**json.loads(record.estimate_json))
    notification = RecoveryNotificationStatus(
        webhook_configured=bool(config.glacier_recovery_webhook_url),
        reminder_count=record.reminder_count,
        next_reminder_at=record.next_reminder_at,
        last_notified_at=record.last_notified_at,
    )
    progress = RecoverySessionProgress(
        archive_verification=record.archive_verification_state or "pending",
        extraction=record.extraction_state or "pending",
        materialization=record.materialization_state or "pending",
    )
    warnings = tuple(str(item) for item in json.loads(record.warnings_json))
    return RecoverySessionSummary(
        id=record.session_id,
        type=record.type or "image_rebuild",
        state=RecoverySessionState(record.state),
        created_at=record.created_at,
        approved_at=record.approved_at,
        restore_requested_at=record.restore_requested_at,
        restore_ready_at=record.restore_ready_at,
        restore_expires_at=record.restore_expires_at,
        completed_at=record.completed_at,
        latest_message=record.latest_message,
        warnings=warnings,
        cost_estimate=estimate,
        notification=notification,
        progress=progress,
        collections=tuple(collections),
        images=tuple(images),
    )


def _collection_glacier_archive_status(
    archive: CollectionArchiveRecord | None,
) -> GlacierArchiveStatus:
    if archive is None:
        return GlacierArchiveStatus()
    return GlacierArchiveStatus(
        state=normalize_glacier_state(archive.state),
        object_path=archive.object_path,
        stored_bytes=archive.stored_bytes,
        backend=archive.backend,
        storage_class=archive.storage_class,
        last_uploaded_at=archive.last_uploaded_at,
        last_verified_at=archive.last_verified_at,
        failure=archive.failure,
    )


def _recovery_session_image_rebuild_state(record: GlacierRecoverySessionRecord) -> str:
    state = RecoverySessionState(record.state)
    if state == RecoverySessionState.PENDING_APPROVAL:
        return "pending"
    if state == RecoverySessionState.RESTORE_REQUESTED:
        return "restoring_collections"
    if state in {RecoverySessionState.READY, RecoverySessionState.COMPLETED}:
        return "ready"
    if state == RecoverySessionState.EXPIRED:
        return "failed"
    return "pending"


def _collection_archive_manifest_status(
    archive: CollectionArchiveRecord | None,
) -> CollectionArchiveManifestStatus | None:
    if archive is None:
        return None
    ots_state = "uploaded" if archive.ots_object_path else "pending"
    if normalize_glacier_state(archive.state) == GlacierState.FAILED:
        ots_state = "failed"
    return CollectionArchiveManifestStatus(
        object_path=archive.manifest_object_path,
        sha256=archive.manifest_sha256,
        ots_object_path=archive.ots_object_path,
        ots_state=ots_state,
        ots_sha256=archive.ots_sha256,
    )


def _collection_stored_bytes(archive: CollectionArchiveRecord | None) -> int:
    if archive is None:
        return 0
    return int(archive.stored_bytes or 0)


def _refresh_recovery_session_metadata(
    session: Session,
    *,
    record: GlacierRecoverySessionRecord,
    config: RuntimeConfig,
) -> None:
    collections = _session_collections(session, record=record)
    if not collections:
        raise InvalidState("recovery session has no collection archives to estimate")
    estimate = _estimate_collection_recovery_costs(config=config, collections=collections)
    record.estimate_json = json.dumps(asdict(estimate), sort_keys=True)
    record.warnings_json = json.dumps(list(_build_warnings(config=config)))
    record.hold_days = max(int(config.glacier_recovery_ready_ttl.total_seconds() // 86400), 1)
    record.retrieval_tier = config.glacier_recovery_retrieval_tier


def _estimate_collection_recovery_costs(
    *,
    config: RuntimeConfig,
    collections: Iterable[CollectionRecord],
) -> RecoveryCostEstimate:
    pricing_basis = resolve_glacier_pricing(config)
    collection_list = list(collections)
    total_bytes = sum(
        _collection_stored_bytes(collection.archive) for collection in collection_list
    )
    total_gib = Decimal(total_bytes) / Decimal(1024**3)
    hold_days = max(int(config.glacier_recovery_ready_ttl.total_seconds() // 86400), 1)
    retrieval_rate, request_rate = _retrieval_rates(config)
    retrieval_cost = _usd(total_gib * Decimal(str(retrieval_rate)))
    restore_request_count = max(len(collection_list), 1)
    request_fees = _usd(
        Decimal("0.001") * Decimal(str(request_rate)) * Decimal(restore_request_count)
    )
    temporary_storage_cost = _usd(
        total_gib
        * Decimal(str(pricing_basis.standard_storage_rate_usd_per_gib_month))
        * Decimal(hold_days)
        / Decimal(30)
    )
    return RecoveryCostEstimate(
        currency_code=pricing_basis.currency_code or "USD",
        retrieval_tier=config.glacier_recovery_retrieval_tier,
        hold_days=hold_days,
        image_count=len(collection_list),
        total_bytes=total_bytes,
        restore_request_count=restore_request_count,
        retrieval_rate_usd_per_gib=retrieval_rate,
        request_rate_usd_per_1000=request_rate,
        standard_storage_rate_usd_per_gib_month=(
            pricing_basis.standard_storage_rate_usd_per_gib_month
        ),
        retrieval_cost_usd=float(retrieval_cost),
        request_fees_usd=float(request_fees),
        temporary_storage_cost_usd=float(temporary_storage_cost),
        total_estimated_cost_usd=float(
            _usd(retrieval_cost + request_fees + temporary_storage_cost)
        ),
        assumptions=(
            "Excludes network egress or operator-local media costs.",
            "Uses the configured ready-to-download cleanup window.",
            "Assumes one archive restore request per collection.",
        ),
    )


def _build_warnings(config: RuntimeConfig) -> tuple[str, ...]:
    restore_latency = _format_timedelta(config.glacier_recovery_restore_latency)
    cleanup_window = _format_timedelta(config.glacier_recovery_ready_ttl)
    reminder = (
        "Riverhog will notify and remind the operator through the configured recovery webhook "
        "while restored ISO data is ready."
        if config.glacier_recovery_webhook_url
        else "No recovery webhook URL is configured; operators must poll the recovery session "
        "manually for readiness."
    )
    return (
        "Archive restore requests take time; the configured restore latency estimate "
        f"is {restore_latency}.",
        reminder,
        "Restored ISO data will be cleaned up after "
        f"{cleanup_window} if recovery is not completed sooner.",
    )


def _notify_recovery_ready(
    session: Session,
    *,
    record: GlacierRecoverySessionRecord,
    config: RuntimeConfig,
    current: datetime,
    reminder: bool,
) -> None:
    if not config.glacier_recovery_webhook_url:
        record.next_reminder_at = None
        return
    try:
        payload = build_recovery_ready_payload(
            config=_webhook_config(config),
            session_id=record.session_id,
            restore_expires_at=record.restore_expires_at,
            images=[
                {
                    "image_id": image.image_id,
                    "filename": _require_image(session, image.image_id).filename,
                }
                for image in session.scalars(
                    select(GlacierRecoverySessionImageRecord).where(
                        GlacierRecoverySessionImageRecord.session_id == record.session_id
                    )
                ).all()
            ],
            delivered_at=current,
            reminder_count=record.reminder_count,
            reminder=reminder,
        )
        post_webhook(
            config=_webhook_config(config),
            payload=payload,
        )
    except Exception as exc:
        record.latest_message = (
            "Ready notification failed and will retry: "
            f"{str(exc).strip() or exc.__class__.__name__}"
        )
        record.next_reminder_at = _isoformat_z(
            current + config.glacier_recovery_webhook_retry_delay
        )
        return

    record.last_notified_at = _isoformat_z(current)
    if reminder:
        record.reminder_count += 1
    interval = config.glacier_recovery_webhook_reminder_interval
    if interval.total_seconds() > 0:
        record.next_reminder_at = _isoformat_z(current + interval)
    else:
        record.next_reminder_at = None


def _webhook_config(config: RuntimeConfig) -> WebhookConfig:
    return WebhookConfig(
        url=config.glacier_recovery_webhook_url or "",
        base_url=config.public_base_url or "",
        timeout_seconds=config.glacier_recovery_webhook_timeout.total_seconds(),
        retry_seconds=config.glacier_recovery_webhook_retry_delay.total_seconds(),
        reminder_interval_seconds=config.glacier_recovery_webhook_reminder_interval.total_seconds(),
    )


def _generated_recovery_session_id(image_id: str, *, existing_ids: Sequence[str]) -> str:
    existing = set(existing_ids)
    ordinal = 1
    while True:
        candidate = f"rs-{image_id}-rebuild-{ordinal}"
        ordinal += 1
        if candidate not in existing:
            return candidate


def _generated_collection_restore_session_id(
    collection_id: str,
    *,
    existing_ids: Sequence[str],
) -> str:
    existing = set(existing_ids)
    safe_collection_id = collection_id.replace("/", "-")
    ordinal = 1
    while True:
        candidate = f"rs-{safe_collection_id}-restore-{ordinal}"
        ordinal += 1
        if candidate not in existing:
            return candidate


def _retrieval_rates(config: RuntimeConfig) -> tuple[float, float]:
    if config.glacier_recovery_retrieval_tier == "standard":
        return (
            config.glacier_standard_retrieval_rate_usd_per_gib,
            config.glacier_standard_request_rate_usd_per_1000,
        )
    return (
        config.glacier_bulk_retrieval_rate_usd_per_gib,
        config.glacier_bulk_request_rate_usd_per_1000,
    )


def _format_timedelta(value: timedelta) -> str:
    seconds = int(value.total_seconds())
    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    parts: list[str] = []
    if hours:
        parts.append(f"{hours}h")
    if minutes:
        parts.append(f"{minutes}m")
    if seconds or not parts:
        parts.append(f"{seconds}s")
    return "".join(parts)


def _isoformat_z(value: datetime) -> str:
    return value.strftime("%Y-%m-%dT%H:%M:%SZ")


def _usd(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP)


def _max_timestamp(values: Iterable[str]) -> str | None:
    value_list = list(values)
    return max(value_list) if value_list else None


def _min_timestamp(values: Iterable[str]) -> str | None:
    value_list = list(values)
    return min(value_list) if value_list else None
