from __future__ import annotations

import json
import logging
import subprocess
import tempfile
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from math import ceil
from pathlib import Path
from typing import cast

from sqlalchemy import or_, select
from sqlalchemy.orm import Session, sessionmaker

from riverhog_core.archive_compliance import (
    copy_counts_toward_protection,
    normalize_copy_state,
    normalize_glacier_state,
)
from riverhog_core.catalog_db import make_session_factory, session_scope
from riverhog_core.catalog_models import (
    ActivePinRecord,
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
from riverhog_core.collection_archives import (
    CollectionArchiveExpectedFile,
    iter_verified_collection_archive_file_chunks,
    verify_collection_archive_files,
    verify_collection_manifest,
    verify_collection_manifest_proof,
)
from riverhog_core.domain.enums import CopyState, FetchState, GlacierState, RecoverySessionState
from riverhog_core.domain.errors import Conflict, InvalidState, NotFound
from riverhog_core.domain.models import (
    CollectionManifestStatus,
    GlacierArchiveStatus,
    RecoveryNotificationStatus,
    RecoverySessionCollection,
    RecoverySessionImage,
    RecoverySessionProgress,
    RecoverySessionSummary,
)
from riverhog_core.domain.types import CollectionId, ImageId
from riverhog_core.finalized_image_coverage import build_disc_manifest_from_catalog
from riverhog_core.fs_paths import normalize_relpath
from riverhog_core.iso.streaming import build_iso_cmd_from_root
from riverhog_core.planner.manifest import (
    MANIFEST_FILENAME,
    README_FILENAME,
    PlannerFileMeta,
    recovery_readme_bytes,
    sidecar_bytes,
)
from riverhog_core.ports.archive_store import ArchiveRestoreStatus, ArchiveStore
from riverhog_core.ports.hot_store import HotStore
from riverhog_core.proofs import (
    CommandProofVerifier,
    ProofVerifier,
)
from riverhog_core.recovery_payloads import (
    CommandAgeBatchpassRecoveryPayloadCodec,
    RecoveryPayloadCodec,
    encrypt_recovery_payload,
)
from riverhog_core.runtime_config import RuntimeConfig
from riverhog_core.services.compliance import file_has_registered_disc_coverage
from riverhog_core.services.target_selection import selected_collection_files
from riverhog_core.webhooks import (
    WebhookConfig,
    build_recovery_completed_payload,
    build_recovery_ready_payload,
    build_recovery_started_payload,
    post_webhook,
    utcnow,
)

_LOG = logging.getLogger(__name__)

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


@dataclass(frozen=True, slots=True)
class _RestoredCollectionArtifact:
    manifest_bytes: bytes
    proof_bytes: bytes


class SqlAlchemyRecoverySessionService:
    def __init__(
        self,
        config: RuntimeConfig,
        archive_store: ArchiveStore,
        hot_store: HotStore | None = None,
        *,
        proof_verifier: ProofVerifier | None = None,
        recovery_payload_codec: RecoveryPayloadCodec | None = None,
    ) -> None:
        self._config = config
        self._archive_store = archive_store
        self._hot_store = hot_store
        self._proof_verifier = proof_verifier or CommandProofVerifier(config.ots_verify_command)
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
                    _require_collection_archive_objects(collection) for collection in collections
                )
            ]
            record.state = RecoverySessionState.RESTORE_REQUESTED.value
            record.approved_at = now
            record.restore_requested_at = now
            record.restore_ready_at = (
                _max_timestamp(
                    status.ready_at for status in statuses if status.ready_at is not None
                )
                or estimated_ready_at
            )
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
            _notify_recovery_started(
                session,
                record=record,
                config=self._config,
                current=current,
            )
            return _session_summary(session, record, config=self._config)

    def complete(self, session_id: str) -> RecoverySessionSummary:
        current = utcnow()
        now = _isoformat_z(current)
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
                    proof_verifier=self._proof_verifier,
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
            _notify_recovery_completed(
                session,
                record=record,
                config=self._config,
                current=current,
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
            manifest_bytes = self._archive_store.read_restored_collection_manifest(
                collection_id=archive.collection_id,
                object_path=archive.manifest_object_path,
            )
            verify_collection_manifest(
                manifest_bytes=manifest_bytes,
                expected_sha256=archive.manifest_sha256,
                collection_id=archive.collection_id,
                files=expected_files,
            )
            proof_bytes = self._archive_store.read_restored_collection_manifest_proof(
                collection_id=archive.collection_id,
                object_path=archive.proof_object_path,
            )
            verify_collection_manifest_proof(
                proof_bytes=proof_bytes,
                expected_sha256=archive.proof_sha256,
                manifest_bytes=manifest_bytes,
                verifier=self._proof_verifier,
            )
            record.archive_verification_state = "completed"
            materialized: list[str] = []
            archive_chunks = self._archive_store.iter_restored_collection_archive(
                collection_id=archive.collection_id,
                object_path=archive.archive_object_path,
            )
            for (
                path,
                content_chunks,
                content_length,
            ) in iter_verified_collection_archive_file_chunks(
                archive_chunks,
                files=expected_files,
                selected_paths=selected_paths,
            ):
                self._hot_store.put_collection_file_stream(
                    collection.id,
                    path,
                    content_chunks,
                    content_length=content_length,
                )
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
            images = {image.image_id: image for image in _session_images(session, record=record)}
            image = images.get(image_id)
            if image is None:
                raise NotFound(f"image not found in recovery session: {image_id}")
            collections = _session_collections(session, record=record)
            if (record.type or "image_rebuild") == "image_rebuild" and collections:
                collection_archives = tuple(
                    _require_collection_archive_objects(collection) for collection in collections
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
                    proof_verifier=self._proof_verifier,
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
        started_next = GlacierRecoverySessionRecord.started_notification_next_attempt_at
        completed_next = GlacierRecoverySessionRecord.completed_notification_next_attempt_at
        with session_scope(self._session_factory) as session:
            due_ids = session.scalars(
                select(GlacierRecoverySessionRecord.session_id)
                .where(
                    or_(
                        (
                            (
                                GlacierRecoverySessionRecord.state
                                == RecoverySessionState.RESTORE_REQUESTED.value
                            )
                            & (
                                (GlacierRecoverySessionRecord.restore_next_poll_at.is_(None))
                                | (
                                    GlacierRecoverySessionRecord.restore_next_poll_at
                                    <= current_text
                                )
                                | (GlacierRecoverySessionRecord.restore_ready_at <= current_text)
                            )
                        ),
                        (
                            (
                                GlacierRecoverySessionRecord.state
                                == RecoverySessionState.RESTORE_REQUESTED.value
                            )
                            & started_next.is_not(None)
                            & (started_next <= current_text)
                        ),
                        (
                            (GlacierRecoverySessionRecord.state == RecoverySessionState.READY.value)
                            & (
                                (GlacierRecoverySessionRecord.restore_expires_at.is_not(None))
                                & (GlacierRecoverySessionRecord.restore_expires_at <= current_text)
                            )
                        ),
                        (
                            (GlacierRecoverySessionRecord.state == RecoverySessionState.READY.value)
                            & (
                                (GlacierRecoverySessionRecord.next_reminder_at.is_not(None))
                                & (GlacierRecoverySessionRecord.next_reminder_at <= current_text)
                            )
                        ),
                        (
                            (
                                GlacierRecoverySessionRecord.state
                                == RecoverySessionState.COMPLETED.value
                            )
                            & (completed_next.is_not(None))
                            & (completed_next <= current_text)
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

    def repair_missing_pinned_hot_files(self, *, limit: int = 100) -> int:
        if limit < 1 or self._hot_store is None:
            return 0
        hot_store = self._hot_store

        glacier_restore_paths: dict[str, set[str]] = {}
        operator_fetches = 0
        missing_count = 0
        with session_scope(self._session_factory) as session:
            pins = session.scalars(
                select(ActivePinRecord).order_by(ActivePinRecord.fetch_order)
            ).all()
            for pin in pins:
                if missing_count >= limit:
                    break
                selected = _selected_pin_files(session, pin.target)
                listed_hot_files: dict[str, dict[str, int]] = {}
                missing_for_pin: list[CollectionFileRecord] = []
                for file_record in selected:
                    if missing_count >= limit:
                        break
                    if _hot_file_available_for_audit(
                        hot_store,
                        file_record,
                        selected_count=len(selected),
                        listed_hot_files=listed_hot_files,
                    ):
                        if not file_record.hot:
                            file_record.hot = True
                        continue
                    file_record.hot = False
                    missing_for_pin.append(file_record)
                    missing_count += 1
                    if file_has_registered_disc_coverage(
                        session,
                        collection_id=file_record.collection_id,
                        path=file_record.path,
                    ):
                        operator_fetches += 1
                    else:
                        glacier_restore_paths.setdefault(
                            file_record.collection_id,
                            set(),
                        ).add(file_record.path)
                if missing_for_pin:
                    pin.fetch_state = FetchState.WAITING_MEDIA.value
                elif pin.fetch_state != FetchState.DONE.value:
                    pin.fetch_state = FetchState.DONE.value

        if missing_count:
            _LOG.info(
                "pinned hot-file audit found missing files: total=%s "
                "operator_fetch_files=%s glacier_restore_collections=%s",
                missing_count,
                operator_fetches,
                len(glacier_restore_paths),
            )

        restored_collections = 0
        for collection_id, paths in sorted(glacier_restore_paths.items()):
            if self._restore_missing_pinned_files_from_glacier(
                collection_id=collection_id,
                paths=sorted(paths),
            ):
                restored_collections += 1

        if restored_collections:
            with session_scope(self._session_factory) as session:
                _sync_pin_states_after_hot_repair(session, hot_store=hot_store)
        return missing_count

    def _restore_missing_pinned_files_from_glacier(
        self,
        *,
        collection_id: str,
        paths: Sequence[str],
    ) -> bool:
        if self._hot_store is None:
            return False
        hot_store = self._hot_store
        if not paths:
            return False
        try:
            summary = self.create_or_resume_for_collection(collection_id)
            if summary.state == RecoverySessionState.PENDING_APPROVAL:
                _LOG.info(
                    "requesting automatic Glacier restore for missing pinned files: "
                    "collection=%s files=%s",
                    collection_id,
                    len(paths),
                )
                summary = self.approve(summary.id)
            if summary.state == RecoverySessionState.RESTORE_REQUESTED:
                self._process_one(session_id=summary.id)
                summary = self.get(summary.id)
            if summary.state == RecoverySessionState.READY:
                missing_paths = _missing_hot_paths(
                    self._session_factory,
                    hot_store,
                    collection_id=collection_id,
                    paths=paths,
                )
                if not missing_paths:
                    return False
                _LOG.info(
                    "materializing missing pinned files from restored Glacier archive: "
                    "collection=%s files=%s",
                    collection_id,
                    len(missing_paths),
                )
                self.materialize_collection_files(
                    summary.id,
                    collection_id,
                    paths=missing_paths,
                )
                self.complete(summary.id)
                return True
            _LOG.info(
                "automatic Glacier restore is pending for missing pinned files: "
                "collection=%s session=%s state=%s",
                collection_id,
                summary.id,
                summary.state.value,
            )
            return False
        except Exception:
            _LOG.exception(
                "automatic Glacier restore for missing pinned files failed: collection=%s",
                collection_id,
            )
            return False

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

            if record.state == RecoverySessionState.COMPLETED.value:
                _notify_recovery_completed(
                    session,
                    record=record,
                    config=self._config,
                    current=current,
                )
                return

            if record.state == RecoverySessionState.RESTORE_REQUESTED.value:
                _notify_recovery_started(
                    session,
                    record=record,
                    config=self._config,
                    current=current,
                )
                status = self._session_restore_status(session, record=record, current=current)
                if status.state == "ready":
                    record.state = RecoverySessionState.READY.value
                    record.restore_ready_at = status.ready_at or current_text
                    record.restore_expires_at = status.expires_at or _isoformat_z(
                        current + self._config.glacier_recovery_ready_ttl
                    )
                    record.restore_next_poll_at = None
                    if (record.type or "image_rebuild") == "image_rebuild":
                        record.latest_message = (
                            "Restored ISO data is ready; reopen the session to complete "
                            "download, verify the ISO, and burn replacement media before "
                            "cleanup."
                        )
                    else:
                        record.latest_message = (
                            "Restored collection archive is ready; Riverhog can materialize "
                            "missing pinned files."
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
                _require_collection_archive_objects(collection) for collection in collections
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


def _selected_pin_files(session: Session, raw_target: str) -> list[CollectionFileRecord]:
    return selected_collection_files(session, raw_target, missing_ok=True)


def _hot_file_available_for_audit(
    hot_store: HotStore,
    file_record: CollectionFileRecord,
    *,
    selected_count: int,
    listed_hot_files: dict[str, dict[str, int]],
) -> bool:
    if selected_count <= 1:
        return _hot_file_available(hot_store, file_record)
    listing = listed_hot_files.get(file_record.collection_id)
    if listing is None:
        try:
            listing = {
                path: int(byte_count)
                for path, byte_count in hot_store.list_collection_files(file_record.collection_id)
            }
        except Exception:
            return _hot_file_available(hot_store, file_record)
        listed_hot_files[file_record.collection_id] = listing
    return listing.get(file_record.path) == int(file_record.bytes)


def _hot_file_available(hot_store: HotStore, file_record: CollectionFileRecord) -> bool:
    try:
        stat = hot_store.stat_collection_file(file_record.collection_id, file_record.path)
    except FileNotFoundError:
        return False
    if stat is None:
        return False
    if int(stat.bytes) != int(file_record.bytes):
        return False
    if stat.sha256 is not None and stat.sha256 != file_record.sha256:
        return False
    return True


def _missing_hot_paths(
    session_factory: sessionmaker[Session],
    hot_store: HotStore,
    *,
    collection_id: str,
    paths: Sequence[str],
) -> list[str]:
    missing: list[str] = []
    with session_scope(session_factory) as session:
        for path in paths:
            file_record = session.get(
                CollectionFileRecord,
                {"collection_id": collection_id, "path": path},
            )
            if file_record is None:
                continue
            if not _hot_file_available(hot_store, file_record):
                missing.append(path)
    return missing


def _sync_pin_states_after_hot_repair(session: Session, *, hot_store: HotStore) -> None:
    pins = session.scalars(select(ActivePinRecord).order_by(ActivePinRecord.fetch_order)).all()
    for pin in pins:
        selected = _selected_pin_files(session, pin.target)
        if selected and all(
            _hot_file_available(hot_store, file_record) for file_record in selected
        ):
            pin.fetch_state = FetchState.DONE.value


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
            "Approve the archive restore before Riverhog requests archived collection data."
        ),
        retrieval_tier=config.glacier_recovery_retrieval_tier,
        hold_days=_restore_hold_days(config),
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
            "Approve the archive restore before Riverhog requests archived collection data."
        ),
        retrieval_tier=config.glacier_recovery_retrieval_tier,
        hold_days=_restore_hold_days(config),
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
            f"collection manifest object path is missing and cannot be restored: {collection.id}"
        )
    if not archive.ots_object_path:
        raise InvalidState(
            f"collection manifest proof object path is missing and cannot be restored: "
            f"{collection.id}"
        )
    if not archive.manifest_sha256:
        raise InvalidState(
            f"collection manifest sha256 is missing and cannot be verified: {collection.id}"
        )
    if not archive.ots_sha256:
        raise InvalidState(
            f"collection manifest proof sha256 is missing and cannot be verified: {collection.id}"
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
    proof_verifier: ProofVerifier,
    recovery_payload_codec: RecoveryPayloadCodec,
) -> Iterator[bytes]:
    with tempfile.TemporaryDirectory(prefix="riverhog-rebuilt-iso-") as tmpdir:
        work_root = Path(tmpdir)
        image_root = work_root / "image-root"
        image_root.mkdir()
        restored_artifacts = _restore_collection_artifacts(
            archive_store=archive_store,
            collection_archives=collection_archives,
            file_lookup=file_lookup,
            proof_verifier=proof_verifier,
        )
        _write_rebuilt_collection_artifacts(
            image_root=image_root,
            collection_artifacts=collection_artifacts,
            restored_artifacts=restored_artifacts,
            recovery_payload_codec=recovery_payload_codec,
        )
        _write_rebuilt_image_payloads_from_collection_archives(
            image_root=image_root,
            archive_store=archive_store,
            collection_archives=collection_archives,
            coverage_parts=coverage_parts,
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


def _restore_collection_artifacts(
    *,
    archive_store: ArchiveStore,
    collection_archives: Sequence[_CollectionArchiveObjects],
    file_lookup: dict[tuple[str, str], tuple[str, int]],
    proof_verifier: ProofVerifier,
) -> dict[str, _RestoredCollectionArtifact]:
    restored_artifacts: dict[str, _RestoredCollectionArtifact] = {}
    for collection_archive in collection_archives:
        restored_artifacts[collection_archive.collection_id] = (
            _verify_restored_collection_manifest_and_proof(
                archive_store=archive_store,
                archive=collection_archive,
                expected_files=_expected_files_from_lookup(
                    file_lookup=file_lookup,
                    collection_id=collection_archive.collection_id,
                ),
                proof_verifier=proof_verifier,
            )
        )
    return restored_artifacts


def _verify_restored_collection_archives(
    session: Session,
    *,
    archive_store: ArchiveStore,
    collections: Sequence[CollectionRecord],
    proof_verifier: ProofVerifier,
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
            proof_verifier=proof_verifier,
        )


def _verify_restored_collection_archive(
    *,
    archive_store: ArchiveStore,
    archive: _CollectionArchiveObjects,
    expected_files: Sequence[CollectionArchiveExpectedFile],
    proof_verifier: ProofVerifier,
) -> _RestoredCollectionArtifact:
    artifact = _verify_restored_collection_manifest_and_proof(
        archive_store=archive_store,
        archive=archive,
        expected_files=expected_files,
        proof_verifier=proof_verifier,
    )
    verify_collection_archive_files(
        chunks=archive_store.iter_restored_collection_archive(
            collection_id=archive.collection_id,
            object_path=archive.archive_object_path,
        ),
        files=expected_files,
    )
    return artifact


def _verify_restored_collection_manifest_and_proof(
    *,
    archive_store: ArchiveStore,
    archive: _CollectionArchiveObjects,
    expected_files: Sequence[CollectionArchiveExpectedFile],
    proof_verifier: ProofVerifier,
) -> _RestoredCollectionArtifact:
    manifest_bytes = archive_store.read_restored_collection_manifest(
        collection_id=archive.collection_id,
        object_path=archive.manifest_object_path,
    )
    verify_collection_manifest(
        manifest_bytes=manifest_bytes,
        expected_sha256=archive.manifest_sha256,
        collection_id=archive.collection_id,
        files=expected_files,
    )
    proof_bytes = archive_store.read_restored_collection_manifest_proof(
        collection_id=archive.collection_id,
        object_path=archive.proof_object_path,
    )
    verify_collection_manifest_proof(
        proof_bytes=proof_bytes,
        expected_sha256=archive.proof_sha256,
        manifest_bytes=manifest_bytes,
        verifier=proof_verifier,
    )
    return _RestoredCollectionArtifact(manifest_bytes=manifest_bytes, proof_bytes=proof_bytes)


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
        for (current_collection_id, path), (sha256, byte_count) in sorted(file_lookup.items())
        if current_collection_id == collection_id
    )


def _write_rebuilt_collection_artifacts(
    *,
    image_root: Path,
    collection_artifacts: Sequence[FinalizedImageCollectionArtifactRecord],
    restored_artifacts: dict[str, _RestoredCollectionArtifact],
    recovery_payload_codec: RecoveryPayloadCodec,
) -> None:
    for artifact in sorted(collection_artifacts, key=lambda current: current.collection_id):
        restored = restored_artifacts.get(artifact.collection_id)
        if restored is None:
            raise InvalidState(
                f"restored collection artifacts are missing: {artifact.collection_id}"
            )
        _write_image_root_file(
            image_root,
            artifact.manifest_path,
            encrypt_recovery_payload(
                restored.manifest_bytes,
                recovery_payload_codec,
            ),
        )
        _write_image_root_file(
            image_root,
            artifact.proof_path,
            encrypt_recovery_payload(
                restored.proof_bytes,
                recovery_payload_codec,
            ),
        )


def _write_rebuilt_image_payloads_from_collection_archives(
    *,
    image_root: Path,
    archive_store: ArchiveStore,
    collection_archives: Sequence[_CollectionArchiveObjects],
    coverage_parts: Sequence[FinalizedImageCoveragePartRecord],
    file_lookup: dict[tuple[str, str], tuple[str, int]],
    recovery_payload_codec: RecoveryPayloadCodec,
) -> None:
    parts_by_file: dict[tuple[str, str], list[FinalizedImageCoveragePartRecord]] = {}
    for part in coverage_parts:
        if part.object_path is None or part.sidecar_path is None:
            raise InvalidState(
                "finalized image coverage part is missing persisted artifact paths: "
                f"{part.collection_id}/{part.path}"
            )
        parts_by_file.setdefault((part.collection_id, part.path), []).append(part)

    written: set[tuple[str, str, int, int]] = set()
    for archive in collection_archives:
        selected_paths = {
            path for collection_id, path in parts_by_file if collection_id == archive.collection_id
        }
        archive_chunks = archive_store.iter_restored_collection_archive(
            collection_id=archive.collection_id,
            object_path=archive.archive_object_path,
        )
        expected_files = _expected_files_from_lookup(
            file_lookup=file_lookup,
            collection_id=archive.collection_id,
        )
        for path, content_chunks, _content_length in iter_verified_collection_archive_file_chunks(
            archive_chunks,
            files=expected_files,
            selected_paths=selected_paths,
        ):
            content = b"".join(content_chunks)
            for part in sorted(
                parts_by_file.get((archive.collection_id, path), ()),
                key=lambda current: (current.part_count, current.part_index),
            ):
                sha256, plaintext_bytes = file_lookup[(part.collection_id, part.path)]
                file_meta = cast(
                    PlannerFileMeta,
                    {
                        "relpath": part.path,
                        "sha256": sha256,
                        "plaintext_bytes": plaintext_bytes,
                    },
                )
                _write_rebuilt_image_part(
                    image_root=image_root,
                    part=part,
                    content=content,
                    file_meta=file_meta,
                    recovery_payload_codec=recovery_payload_codec,
                )
                written.add(
                    (
                        part.collection_id,
                        part.path,
                        int(part.part_index),
                        int(part.part_count),
                    )
                )

    expected = {
        (
            part.collection_id,
            part.path,
            int(part.part_index),
            int(part.part_count),
        )
        for part in coverage_parts
    }
    missing = sorted(expected - written)
    if missing:
        collection_id, path, part_index, part_count = missing[0]
        raise InvalidState(
            "restored collection archive is missing finalized image part: "
            f"{collection_id}/{path} part {part_index + 1} of {part_count}"
        )


def _write_rebuilt_image_part(
    *,
    image_root: Path,
    part: FinalizedImageCoveragePartRecord,
    content: bytes,
    file_meta: PlannerFileMeta,
    recovery_payload_codec: RecoveryPayloadCodec,
) -> None:
    if part.object_path is None or part.sidecar_path is None:
        raise InvalidState(
            "finalized image coverage part is missing persisted artifact paths: "
            f"{part.collection_id}/{part.path}"
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
    cmd = build_iso_cmd_from_root(image_root=image_root, volume_id=volume_id)
    with tempfile.TemporaryFile() as stderr:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=stderr)
        assert proc.stdout is not None
        try:
            while True:
                chunk = proc.stdout.read(1024 * 1024)
                if not chunk:
                    break
                yield chunk
            returncode = proc.wait()
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()
            proc.stdout.close()
        if returncode != 0:
            stderr.seek(0, 2)
            size = stderr.tell()
            stderr.seek(max(size - 1500, 0))
            detail = stderr.read().decode("utf-8", errors="replace")
            raise RuntimeError(detail or f"xorriso exited {returncode}")


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
        normalize_copy_state(state) not in {CopyState.NEEDED, CopyState.BURNING} for state in rows
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
                GlacierRecoverySessionImageRecord.session_id
                == GlacierRecoverySessionRecord.session_id,
            )
            .where(GlacierRecoverySessionImageRecord.image_id == image_id)
            .where(GlacierRecoverySessionRecord.state.in_(_ACTIVE_RECOVERY_STATES))
            .order_by(GlacierRecoverySessionRecord.created_at.desc())
            .limit(1)
        ),
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
        ),
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
                GlacierRecoverySessionImageRecord.session_id
                == GlacierRecoverySessionRecord.session_id,
            )
            .where(GlacierRecoverySessionImageRecord.image_id == image_id)
            .order_by(GlacierRecoverySessionRecord.created_at.desc())
            .limit(1)
        ),
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
        ),
    )


def _reusable_pending_approval_session(session: Session) -> GlacierRecoverySessionRecord | None:
    return cast(
        GlacierRecoverySessionRecord | None,
        session.scalar(
            select(GlacierRecoverySessionRecord)
            .where(
                GlacierRecoverySessionRecord.state == RecoverySessionState.PENDING_APPROVAL.value
            )
            .order_by(GlacierRecoverySessionRecord.created_at.desc())
            .limit(1)
        ),
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
                    or normalize_glacier_state(collection.archive.state) != GlacierState.UPLOADED
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
                collection_manifest=_collection_manifest_status(archive),
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
    notification = RecoveryNotificationStatus(
        webhook_configured=bool(config.operator_webhook_url),
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


def _collection_manifest_status(
    archive: CollectionArchiveRecord | None,
) -> CollectionManifestStatus | None:
    if archive is None:
        return None
    ots_state = "uploaded" if archive.ots_object_path else "pending"
    if normalize_glacier_state(archive.state) == GlacierState.FAILED:
        ots_state = "failed"
    return CollectionManifestStatus(
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
        raise InvalidState("recovery session has no collection archives")
    record.warnings_json = json.dumps(list(_build_warnings(config=config)))
    record.hold_days = _restore_hold_days(config)
    record.retrieval_tier = config.glacier_recovery_retrieval_tier


def _build_warnings(config: RuntimeConfig) -> tuple[str, ...]:
    restore_latency = _format_timedelta(config.glacier_recovery_restore_latency)
    cleanup_window = _format_timedelta(config.glacier_recovery_ready_ttl)
    reminder = (
        "Riverhog will notify and remind the operator through the configured operator webhook "
        "as Glacier recovery starts, becomes ready, and completes."
        if config.operator_webhook_url
        else "No operator webhook URL is configured; operators must poll the recovery session "
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
    if not config.operator_webhook_url:
        record.next_reminder_at = None
        return
    try:
        webhook_config = _webhook_config(config)
        payload = build_recovery_ready_payload(
            config=webhook_config,
            session_id=record.session_id,
            recovery_type=_recovery_type(record),
            restore_expires_at=record.restore_expires_at,
            images=_recovery_image_payload(session, record),
            collections=_recovery_collection_payload(session, record),
            delivered_at=current,
            reminder_count=record.reminder_count,
            reminder=reminder,
        )
        post_webhook(
            config=webhook_config,
            payload=payload,
        )
    except Exception as exc:
        record.latest_message = (
            "Ready notification failed and will retry: "
            f"{str(exc).strip() or exc.__class__.__name__}"
        )
        record.next_reminder_at = _isoformat_z(current + config.operator_webhook_retry_delay)
        return

    record.last_notified_at = _isoformat_z(current)
    if reminder:
        record.reminder_count += 1
    interval = config.operator_webhook_reminder_interval
    if interval.total_seconds() > 0:
        record.next_reminder_at = _isoformat_z(current + interval)
    else:
        record.next_reminder_at = None


def _notify_recovery_started(
    session: Session,
    *,
    record: GlacierRecoverySessionRecord,
    config: RuntimeConfig,
    current: datetime,
) -> None:
    current_text = _isoformat_z(current)
    if not config.operator_webhook_url:
        record.started_notification_next_attempt_at = None
        record.started_notification_failure = None
        return
    if (
        record.started_notification_sent_at is not None
        and record.started_notification_next_attempt_at is None
    ):
        return
    if (
        record.started_notification_next_attempt_at is not None
        and record.started_notification_next_attempt_at > current_text
    ):
        return

    webhook_config = _webhook_config(config)
    try:
        post_webhook(
            config=webhook_config,
            payload=build_recovery_started_payload(
                config=webhook_config,
                session_id=record.session_id,
                recovery_type=_recovery_type(record),
                retrieval_tier=record.retrieval_tier,
                estimated_ready_at=record.restore_ready_at,
                images=_recovery_image_payload(session, record),
                collections=_recovery_collection_payload(session, record),
                delivered_at=current,
            ),
        )
    except Exception as exc:
        record.started_notification_failure = str(exc).strip() or exc.__class__.__name__
        record.started_notification_next_attempt_at = _isoformat_z(
            current + config.operator_webhook_retry_delay
        )
        return

    record.started_notification_sent_at = current_text
    record.started_notification_next_attempt_at = None
    record.started_notification_failure = None


def _notify_recovery_completed(
    session: Session,
    *,
    record: GlacierRecoverySessionRecord,
    config: RuntimeConfig,
    current: datetime,
) -> None:
    current_text = _isoformat_z(current)
    if not config.operator_webhook_url:
        record.completed_notification_next_attempt_at = None
        record.completed_notification_failure = None
        return
    if (
        record.completed_notification_sent_at is not None
        and record.completed_notification_next_attempt_at is None
    ):
        return
    if (
        record.completed_notification_next_attempt_at is not None
        and record.completed_notification_next_attempt_at > current_text
    ):
        return

    webhook_config = _webhook_config(config)
    try:
        post_webhook(
            config=webhook_config,
            payload=build_recovery_completed_payload(
                config=webhook_config,
                session_id=record.session_id,
                recovery_type=_recovery_type(record),
                images=_recovery_image_payload(session, record),
                collections=_recovery_collection_payload(session, record),
                delivered_at=current,
            ),
        )
    except Exception as exc:
        record.completed_notification_failure = str(exc).strip() or exc.__class__.__name__
        record.completed_notification_next_attempt_at = _isoformat_z(
            current + config.operator_webhook_retry_delay
        )
        return

    record.completed_notification_sent_at = current_text
    record.completed_notification_next_attempt_at = None
    record.completed_notification_failure = None


def _recovery_type(record: GlacierRecoverySessionRecord) -> str:
    return record.type or "image_rebuild"


def _recovery_image_payload(
    session: Session,
    record: GlacierRecoverySessionRecord,
) -> list[dict[str, str]]:
    rows = session.scalars(
        select(GlacierRecoverySessionImageRecord).where(
            GlacierRecoverySessionImageRecord.session_id == record.session_id
        )
    ).all()
    return [
        {
            "image_id": row.image_id,
            "filename": _require_image(session, row.image_id).filename,
        }
        for row in rows
    ]


def _recovery_collection_payload(
    session: Session,
    record: GlacierRecoverySessionRecord,
) -> list[dict[str, str]]:
    rows = session.scalars(
        select(GlacierRecoverySessionCollectionRecord).where(
            GlacierRecoverySessionCollectionRecord.session_id == record.session_id
        )
    ).all()
    return [{"collection_id": row.collection_id} for row in rows]


def _webhook_config(config: RuntimeConfig) -> WebhookConfig:
    return WebhookConfig(
        url=config.operator_webhook_url or "",
        base_url=config.public_base_url or "",
        timeout_seconds=config.operator_webhook_timeout.total_seconds(),
        retry_seconds=config.operator_webhook_retry_delay.total_seconds(),
        reminder_interval_seconds=config.operator_webhook_reminder_interval.total_seconds(),
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


def _restore_hold_days(config: RuntimeConfig) -> int:
    return max(ceil(config.glacier_recovery_ready_ttl.total_seconds() / 86400), 1)


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


def _max_timestamp(values: Iterable[str]) -> str | None:
    value_list = list(values)
    return max(value_list) if value_list else None


def _min_timestamp(values: Iterable[str]) -> str | None:
    value_list = list(values)
    return min(value_list) if value_list else None
