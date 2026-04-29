from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from arc_core.catalog_models import (
    CollectionFileRecord,
    CollectionRecord,
    FinalizedImageCollectionArtifactRecord,
    FinalizedImageCoveragePartRecord,
    FinalizedImageCoveredPathRecord,
    FinalizedImageRecord,
)
from arc_core.domain.enums import RecoverySessionState
from arc_core.domain.errors import NotFound
from arc_core.finalized_image_coverage import (
    read_finalized_image_collection_artifacts,
    read_finalized_image_coverage_parts,
)
from arc_core.ports.archive_store import ArchiveUploadReceipt
from arc_core.runtime_config import RuntimeConfig
from arc_core.services.copies import SqlAlchemyCopyService
from arc_core.services.glacier_uploads import (
    SqlAlchemyGlacierUploadService,
    enqueue_glacier_upload_job,
)
from arc_core.services.recovery_sessions import SqlAlchemyRecoverySessionService
from arc_core.sqlite_db import initialize_db, make_session_factory, session_scope
from tests.fixtures.data import DOCS_FILES, IMAGE_ONE_FILES, write_tree


class _FakeHotStore:
    def get_collection_file(self, collection_id: str, path: str) -> bytes:
        assert collection_id == "docs"
        return DOCS_FILES[path]


class _FakeArchiveStore:
    def upload_finalized_image(
        self,
        *,
        image_id: str,
        filename: str,
        image_root: Path,
    ) -> ArchiveUploadReceipt:
        assert image_id == "20260420T040001Z"
        assert filename == "20260420T040001Z.iso"
        assert image_root.exists()
        return ArchiveUploadReceipt(
            object_path="glacier/finalized-images/20260420T040001Z/20260420T040001Z.iso",
            stored_bytes=1234,
            backend="s3",
            storage_class="DEEP_ARCHIVE",
            uploaded_at="2026-04-20T04:10:00Z",
            verified_at="2026-04-20T04:11:00Z",
        )


def _config(sqlite_path: Path, **overrides: object) -> RuntimeConfig:
    config = RuntimeConfig(
        object_store="s3",
        s3_endpoint_url="http://example.invalid:9000",
        s3_region="us-east-1",
        s3_bucket="riverhog",
        s3_access_key_id="test-access",
        s3_secret_access_key="test-secret",
        s3_force_path_style=True,
        tusd_base_url="http://example.invalid:1080/files",
        tusd_hook_secret="hook-secret",
        sqlite_path=sqlite_path,
    )
    return replace(config, **overrides)


def _seed_finalized_image(
    sqlite_path: Path,
    image_root: Path,
    *,
    image_id: str = "20260420T040001Z",
    candidate_id: str = "img_2026-04-20_01",
    filename: str = "20260420T040001Z.iso",
    glacier_state: str,
) -> None:
    session_factory = make_session_factory(str(sqlite_path))
    with session_scope(session_factory) as session:
        if session.get(CollectionRecord, "docs") is None:
            session.add(CollectionRecord(id="docs"))
        for relative_path, content in DOCS_FILES.items():
            if (
                session.get(
                    CollectionFileRecord,
                    {"collection_id": "docs", "path": relative_path},
                )
                is None
            ):
                session.add(
                    CollectionFileRecord(
                        collection_id="docs",
                        path=relative_path,
                        bytes=len(content),
                        sha256="a" * 64,
                        hot=True,
                        archived=False,
                    )
                )

        session.add(
            FinalizedImageRecord(
                image_id=image_id,
                candidate_id=candidate_id,
                filename=filename,
                bytes=sum(len(content) for content in DOCS_FILES.values()),
                image_root=str(image_root),
                target_bytes=10_000,
                required_copy_count=2,
                glacier_state=glacier_state,
            )
        )
        for relative_path in (
            "tax/2022/invoice-123.pdf",
            "tax/2022/receipt-456.pdf",
        ):
            session.add(
                FinalizedImageCoveredPathRecord(
                    image_id=image_id,
                    collection_id="docs",
                    path=relative_path,
                )
            )
        for artifact in read_finalized_image_collection_artifacts(image_root):
            session.add(
                FinalizedImageCollectionArtifactRecord(
                    image_id=image_id,
                    collection_id=artifact.collection_id,
                    manifest_path=artifact.manifest_path,
                    proof_path=artifact.proof_path,
                )
            )
        for part in read_finalized_image_coverage_parts(image_root):
            session.add(
                FinalizedImageCoveragePartRecord(
                    image_id=image_id,
                    collection_id=part.collection_id,
                    path=part.path,
                    part_index=part.part_index,
                    part_count=part.part_count,
                    object_path=part.object_path,
                    sidecar_path=part.sidecar_path,
                )
            )


def test_double_copy_loss_creates_pending_recovery_session(tmp_path: Path) -> None:
    sqlite_path = tmp_path / "state.sqlite3"
    image_root = tmp_path / "image-root"
    initialize_db(str(sqlite_path))
    write_tree(image_root, IMAGE_ONE_FILES)
    _seed_finalized_image(sqlite_path, image_root, glacier_state="uploaded")

    config = _config(sqlite_path)
    copy_service = SqlAlchemyCopyService(config, _FakeHotStore())
    recovery_service = SqlAlchemyRecoverySessionService(config)

    copy_service.register("20260420T040001Z", "Shelf A1", copy_id="20260420T040001Z-1")
    copy_service.register("20260420T040001Z", "Shelf B1", copy_id="20260420T040001Z-2")
    copy_service.update("20260420T040001Z", "20260420T040001Z-1", state="lost")
    copy_service.update("20260420T040001Z", "20260420T040001Z-2", state="damaged")

    session = recovery_service.get_for_image("20260420T040001Z")

    assert session.id == "rs-20260420T040001Z-1"
    assert session.state == RecoverySessionState.PENDING_APPROVAL
    assert session.cost_estimate.total_estimated_cost_usd > 0
    assert session.notification.webhook_configured is False
    assert [str(image.id) for image in session.images] == ["20260420T040001Z"]


def test_recovery_session_processes_ready_and_expired_states(
    tmp_path: Path,
    monkeypatch,
) -> None:
    sqlite_path = tmp_path / "state.sqlite3"
    image_root = tmp_path / "image-root"
    initialize_db(str(sqlite_path))
    write_tree(image_root, IMAGE_ONE_FILES)
    _seed_finalized_image(sqlite_path, image_root, glacier_state="uploaded")

    config = _config(
        sqlite_path,
        glacier_recovery_restore_latency=timedelta(seconds=10),
        glacier_recovery_ready_ttl=timedelta(seconds=5),
    )
    copy_service = SqlAlchemyCopyService(config, _FakeHotStore())
    recovery_service = SqlAlchemyRecoverySessionService(config)

    copy_service.register("20260420T040001Z", "Shelf A1", copy_id="20260420T040001Z-1")
    copy_service.register("20260420T040001Z", "Shelf B1", copy_id="20260420T040001Z-2")
    copy_service.update("20260420T040001Z", "20260420T040001Z-1", state="lost")
    copy_service.update("20260420T040001Z", "20260420T040001Z-2", state="lost")

    start = datetime(2026, 4, 20, 4, 0, tzinfo=UTC)
    monkeypatch.setattr("arc_core.services.recovery_sessions.utcnow", lambda: start)
    approved = recovery_service.approve("rs-20260420T040001Z-1")
    assert approved.state == RecoverySessionState.RESTORE_REQUESTED
    assert approved.restore_ready_at == "2026-04-20T04:00:10Z"

    monkeypatch.setattr(
        "arc_core.services.recovery_sessions.utcnow",
        lambda: start + timedelta(seconds=11),
    )
    assert recovery_service.process_due_sessions() == 1
    ready = recovery_service.get("rs-20260420T040001Z-1")
    assert ready.state == RecoverySessionState.READY
    assert ready.restore_expires_at == "2026-04-20T04:00:16Z"

    monkeypatch.setattr(
        "arc_core.services.recovery_sessions.utcnow",
        lambda: start + timedelta(seconds=17),
    )
    assert recovery_service.process_due_sessions() == 1
    expired = recovery_service.get("rs-20260420T040001Z-1")
    assert expired.state == RecoverySessionState.EXPIRED

    completed = recovery_service.complete("rs-20260420T040001Z-1")
    assert completed.state == RecoverySessionState.COMPLETED


def test_recovery_session_retries_initial_ready_notification_before_reminders(
    tmp_path: Path,
    monkeypatch,
) -> None:
    sqlite_path = tmp_path / "state.sqlite3"
    image_root = tmp_path / "image-root"
    initialize_db(str(sqlite_path))
    write_tree(image_root, IMAGE_ONE_FILES)
    _seed_finalized_image(sqlite_path, image_root, glacier_state="uploaded")

    config = _config(
        sqlite_path,
        glacier_recovery_webhook_url="http://example.invalid/webhooks/recovery",
        glacier_recovery_restore_latency=timedelta(seconds=10),
        glacier_recovery_webhook_retry_delay=timedelta(seconds=1),
        glacier_recovery_webhook_reminder_interval=timedelta(seconds=5),
    )
    copy_service = SqlAlchemyCopyService(config, _FakeHotStore())
    recovery_service = SqlAlchemyRecoverySessionService(config)

    copy_service.register("20260420T040001Z", "Shelf A1", copy_id="20260420T040001Z-1")
    copy_service.register("20260420T040001Z", "Shelf B1", copy_id="20260420T040001Z-2")
    copy_service.update("20260420T040001Z", "20260420T040001Z-1", state="lost")
    copy_service.update("20260420T040001Z", "20260420T040001Z-2", state="lost")

    attempts: list[str] = []

    def _post_webhook(*, config, payload):
        attempts.append(str(payload["event"]))
        if len(attempts) == 1:
            raise RuntimeError("HTTP 503")

    start = datetime(2026, 4, 20, 4, 0, tzinfo=UTC)
    monkeypatch.setattr("arc_core.services.recovery_sessions.utcnow", lambda: start)
    monkeypatch.setattr("arc_core.services.recovery_sessions.post_webhook", _post_webhook)
    recovery_service.approve("rs-20260420T040001Z-1")

    monkeypatch.setattr(
        "arc_core.services.recovery_sessions.utcnow",
        lambda: start + timedelta(seconds=11),
    )
    assert recovery_service.process_due_sessions() == 1

    failed_delivery = recovery_service.get("rs-20260420T040001Z-1")
    assert failed_delivery.state == RecoverySessionState.READY
    assert failed_delivery.notification.last_notified_at is None
    assert failed_delivery.notification.reminder_count == 0
    assert attempts == ["images.recovery_ready"]

    monkeypatch.setattr(
        "arc_core.services.recovery_sessions.utcnow",
        lambda: start + timedelta(seconds=12),
    )
    assert recovery_service.process_due_sessions() == 1

    retried_delivery = recovery_service.get("rs-20260420T040001Z-1")
    assert retried_delivery.notification.last_notified_at == "2026-04-20T04:00:12Z"
    assert retried_delivery.notification.reminder_count == 0
    assert attempts == ["images.recovery_ready", "images.recovery_ready"]


def test_recovery_session_retries_initial_ready_notification_before_expiring(
    tmp_path: Path,
    monkeypatch,
) -> None:
    sqlite_path = tmp_path / "state.sqlite3"
    image_root = tmp_path / "image-root"
    initialize_db(str(sqlite_path))
    write_tree(image_root, IMAGE_ONE_FILES)
    _seed_finalized_image(sqlite_path, image_root, glacier_state="uploaded")

    config = _config(
        sqlite_path,
        glacier_recovery_webhook_url="http://example.invalid/webhooks/recovery",
        glacier_recovery_restore_latency=timedelta(seconds=10),
        glacier_recovery_ready_ttl=timedelta(seconds=12),
        glacier_recovery_webhook_retry_delay=timedelta(seconds=1),
        glacier_recovery_webhook_reminder_interval=timedelta(seconds=5),
    )
    copy_service = SqlAlchemyCopyService(config, _FakeHotStore())
    recovery_service = SqlAlchemyRecoverySessionService(config)

    copy_service.register("20260420T040001Z", "Shelf A1", copy_id="20260420T040001Z-1")
    copy_service.register("20260420T040001Z", "Shelf B1", copy_id="20260420T040001Z-2")
    copy_service.update("20260420T040001Z", "20260420T040001Z-1", state="lost")
    copy_service.update("20260420T040001Z", "20260420T040001Z-2", state="lost")

    attempts: list[str] = []

    def _post_webhook(*, config, payload):
        attempts.append(str(payload["event"]))
        if len(attempts) == 1:
            raise RuntimeError("HTTP 503")

    start = datetime(2026, 4, 20, 4, 0, tzinfo=UTC)
    monkeypatch.setattr("arc_core.services.recovery_sessions.utcnow", lambda: start)
    monkeypatch.setattr("arc_core.services.recovery_sessions.post_webhook", _post_webhook)
    recovery_service.approve("rs-20260420T040001Z-1")

    monkeypatch.setattr(
        "arc_core.services.recovery_sessions.utcnow",
        lambda: start + timedelta(seconds=11),
    )
    assert recovery_service.process_due_sessions() == 1

    failed_delivery = recovery_service.get("rs-20260420T040001Z-1")
    assert failed_delivery.state == RecoverySessionState.READY
    assert failed_delivery.notification.last_notified_at is None
    assert failed_delivery.notification.next_reminder_at == "2026-04-20T04:00:12Z"
    assert failed_delivery.restore_expires_at == "2026-04-20T04:00:23Z"

    monkeypatch.setattr(
        "arc_core.services.recovery_sessions.utcnow",
        lambda: start + timedelta(seconds=22),
    )
    assert recovery_service.process_due_sessions() == 1

    retried_delivery = recovery_service.get("rs-20260420T040001Z-1")
    assert retried_delivery.state == RecoverySessionState.READY
    assert retried_delivery.notification.last_notified_at == "2026-04-20T04:00:22Z"
    assert retried_delivery.notification.reminder_count == 0
    assert attempts == ["images.recovery_ready", "images.recovery_ready"]


def test_glacier_upload_completion_backfills_recovery_session_for_unprotected_image(
    tmp_path: Path,
    monkeypatch,
) -> None:
    sqlite_path = tmp_path / "state.sqlite3"
    image_root = tmp_path / "image-root"
    initialize_db(str(sqlite_path))
    write_tree(image_root, IMAGE_ONE_FILES)
    _seed_finalized_image(sqlite_path, image_root, glacier_state="pending")

    config = _config(sqlite_path)
    copy_service = SqlAlchemyCopyService(config, _FakeHotStore())
    upload_service = SqlAlchemyGlacierUploadService(config, _FakeArchiveStore())
    recovery_service = SqlAlchemyRecoverySessionService(config)

    copy_service.register("20260420T040001Z", "Shelf A1", copy_id="20260420T040001Z-1")
    copy_service.register("20260420T040001Z", "Shelf B1", copy_id="20260420T040001Z-2")
    copy_service.update("20260420T040001Z", "20260420T040001Z-1", state="lost")
    copy_service.update("20260420T040001Z", "20260420T040001Z-2", state="damaged")

    session_factory = make_session_factory(str(sqlite_path))
    with session_scope(session_factory) as session:
        enqueue_glacier_upload_job(
            session,
            image_id="20260420T040001Z",
            next_attempt_at="2026-04-20T04:00:00Z",
        )

    monkeypatch.setattr(
        "arc_core.services.glacier_uploads.record_glacier_usage_snapshot",
        lambda session, config: None,
    )

    assert upload_service.process_due_uploads() == 1
    session = recovery_service.get_for_image("20260420T040001Z")
    assert session.state == RecoverySessionState.PENDING_APPROVAL


def test_glacier_upload_completion_does_not_create_recovery_session_for_ordinary_burn_backlog(
    tmp_path: Path,
    monkeypatch,
) -> None:
    sqlite_path = tmp_path / "state.sqlite3"
    image_root = tmp_path / "image-root"
    initialize_db(str(sqlite_path))
    write_tree(image_root, IMAGE_ONE_FILES)
    _seed_finalized_image(sqlite_path, image_root, glacier_state="pending")

    config = _config(sqlite_path)
    upload_service = SqlAlchemyGlacierUploadService(config, _FakeArchiveStore())
    recovery_service = SqlAlchemyRecoverySessionService(config)

    session_factory = make_session_factory(str(sqlite_path))
    with session_scope(session_factory) as session:
        enqueue_glacier_upload_job(
            session,
            image_id="20260420T040001Z",
            next_attempt_at="2026-04-20T04:00:00Z",
        )

    monkeypatch.setattr(
        "arc_core.services.glacier_uploads.record_glacier_usage_snapshot",
        lambda session, config: None,
    )

    assert upload_service.process_due_uploads() == 1
    with pytest.raises(NotFound, match="recovery session not found for image"):
        recovery_service.get_for_image("20260420T040001Z")


def test_pending_recovery_session_can_group_multiple_images_before_approval(
    tmp_path: Path,
) -> None:
    sqlite_path = tmp_path / "state.sqlite3"
    image_root_one = tmp_path / "image-root-one"
    image_root_two = tmp_path / "image-root-two"
    initialize_db(str(sqlite_path))
    write_tree(image_root_one, IMAGE_ONE_FILES)
    write_tree(image_root_two, IMAGE_ONE_FILES)
    _seed_finalized_image(sqlite_path, image_root_one, glacier_state="uploaded")
    _seed_finalized_image(
        sqlite_path,
        image_root_two,
        image_id="20260420T040003Z",
        candidate_id="img_2026-04-20_03",
        filename="20260420T040003Z.iso",
        glacier_state="uploaded",
    )

    config = _config(sqlite_path)
    copy_service = SqlAlchemyCopyService(config, _FakeHotStore())
    recovery_service = SqlAlchemyRecoverySessionService(config)

    copy_service.register("20260420T040001Z", "Shelf A1", copy_id="20260420T040001Z-1")
    copy_service.register("20260420T040001Z", "Shelf B1", copy_id="20260420T040001Z-2")
    copy_service.register("20260420T040003Z", "Shelf C1", copy_id="20260420T040003Z-1")
    copy_service.register("20260420T040003Z", "Shelf D1", copy_id="20260420T040003Z-2")

    copy_service.update("20260420T040001Z", "20260420T040001Z-1", state="lost")
    copy_service.update("20260420T040001Z", "20260420T040001Z-2", state="damaged")
    copy_service.update("20260420T040003Z", "20260420T040003Z-1", state="lost")
    copy_service.update("20260420T040003Z", "20260420T040003Z-2", state="damaged")

    session = recovery_service.get("rs-20260420T040001Z-1")

    assert session.state == RecoverySessionState.PENDING_APPROVAL
    assert [str(image.id) for image in session.images] == [
        "20260420T040001Z",
        "20260420T040003Z",
    ]
    assert session.cost_estimate.image_count == 2
    assert session.cost_estimate.restore_request_count == 2
