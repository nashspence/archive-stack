from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import timedelta
from pathlib import Path

from arc_core.catalog_models import (
    FinalizedImageRecord,
    GlacierUploadJobRecord,
    GlacierUsageSnapshotRecord,
)
from arc_core.ports.archive_store import ArchiveUploadReceipt
from arc_core.runtime_config import RuntimeConfig
from arc_core.services.glacier_uploads import (
    SqlAlchemyGlacierUploadService,
    enqueue_glacier_upload_job,
)
from arc_core.sqlite_db import initialize_db, make_session_factory, session_scope


@dataclass
class _FakeArchiveStore:
    receipt: ArchiveUploadReceipt | None = None
    error: Exception | None = None
    calls: list[tuple[str, str, Path]] | None = None

    def upload_finalized_image(
        self,
        *,
        image_id: str,
        filename: str,
        image_root: Path,
    ) -> ArchiveUploadReceipt:
        if self.calls is not None:
            self.calls.append((image_id, filename, image_root))
        if self.error is not None:
            raise self.error
        assert self.receipt is not None
        return self.receipt


@dataclass
class _CrashAfterUploadArchiveStore:
    receipt: ArchiveUploadReceipt
    upload_calls: int = 0
    total_calls: int = 0
    object_exists: bool = False

    def upload_finalized_image(
        self,
        *,
        image_id: str,
        filename: str,
        image_root: Path,
    ) -> ArchiveUploadReceipt:
        self.total_calls += 1
        if self.object_exists:
            return self.receipt
        self.upload_calls += 1
        self.object_exists = True
        raise KeyboardInterrupt("simulated process crash after remote upload")


def _config(tmp_path: Path, **overrides: object) -> RuntimeConfig:
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
        sqlite_path=tmp_path / "state.sqlite3",
    )
    return replace(config, **overrides)


def _seed_finalized_image(
    config: RuntimeConfig,
    image_root: Path,
    *,
    glacier_state: str = "pending",
    attempt_count: int = 0,
    next_attempt_at: str = "2026-04-20T04:00:00Z",
) -> None:
    initialize_db(str(config.sqlite_path))
    session_factory = make_session_factory(str(config.sqlite_path))
    with session_scope(session_factory) as session:
        session.add(
            FinalizedImageRecord(
                image_id="20260420T040001Z",
                candidate_id="img_2026-04-20_01",
                filename="20260420T040001Z.iso",
                bytes=123,
                image_root=str(image_root),
                target_bytes=123,
                required_copy_count=2,
                glacier_state=glacier_state,
            )
        )
        if attempt_count == 0:
            enqueue_glacier_upload_job(
                session,
                image_id="20260420T040001Z",
                next_attempt_at=next_attempt_at,
            )
            return
        session.add(
            GlacierUploadJobRecord(
                image_id="20260420T040001Z",
                attempt_count=attempt_count,
                next_attempt_at=next_attempt_at,
                last_attempt_at="2026-04-20T04:00:30Z",
                last_error=None,
                completed_at=None,
                failed_at=None,
            )
        )


def test_process_due_uploads_records_success_metadata(tmp_path: Path) -> None:
    image_root = tmp_path / "image-root"
    image_root.mkdir()
    config = _config(tmp_path)
    _seed_finalized_image(config, image_root)
    store = _FakeArchiveStore(
        receipt=ArchiveUploadReceipt(
            object_path="glacier/finalized-images/20260420T040001Z/20260420T040001Z.iso",
            stored_bytes=456,
            backend="s3",
            storage_class="DEEP_ARCHIVE",
            uploaded_at="2026-04-20T04:01:00Z",
            verified_at="2026-04-20T04:01:01Z",
        ),
        calls=[],
    )
    service = SqlAlchemyGlacierUploadService(config, store)

    assert service.process_due_uploads() == 1

    session_factory = make_session_factory(str(config.sqlite_path))
    with session_scope(session_factory) as session:
        image = session.get(FinalizedImageRecord, "20260420T040001Z")
        assert image is not None
        assert image.glacier_state == "uploaded"
        assert image.glacier_object_path == (
            "glacier/finalized-images/20260420T040001Z/20260420T040001Z.iso"
        )
        assert image.glacier_stored_bytes == 456
        assert image.glacier_backend == "s3"
        assert image.glacier_storage_class == "DEEP_ARCHIVE"
        assert image.glacier_last_uploaded_at == "2026-04-20T04:01:00Z"
        assert image.glacier_last_verified_at == "2026-04-20T04:01:01Z"
        assert image.glacier_failure is None
        snapshots = session.query(GlacierUsageSnapshotRecord).all()
        assert len(snapshots) == 1
        assert snapshots[0].measured_storage_bytes == 456
    assert store.calls == [
        ("20260420T040001Z", "20260420T040001Z.iso", image_root),
    ]


def test_process_due_uploads_recovers_job_left_uploading_across_restart(tmp_path: Path) -> None:
    image_root = tmp_path / "image-root"
    image_root.mkdir()
    config = _config(tmp_path)
    _seed_finalized_image(config, image_root, glacier_state="uploading", attempt_count=1)
    store = _FakeArchiveStore(
        receipt=ArchiveUploadReceipt(
            object_path="glacier/finalized-images/20260420T040001Z/20260420T040001Z.iso",
            stored_bytes=456,
            backend="s3",
            storage_class="DEEP_ARCHIVE",
            uploaded_at="2026-04-20T04:01:00Z",
            verified_at="2026-04-20T04:01:01Z",
        ),
        calls=[],
    )
    service = SqlAlchemyGlacierUploadService(config, store)

    assert service.process_due_uploads() == 1

    session_factory = make_session_factory(str(config.sqlite_path))
    with session_scope(session_factory) as session:
        image = session.get(FinalizedImageRecord, "20260420T040001Z")
        assert image is not None
        assert image.glacier_state == "uploaded"
        assert image.glacier_failure is None
    assert store.calls == [
        ("20260420T040001Z", "20260420T040001Z.iso", image_root),
    ]


def test_process_due_uploads_reconciles_completed_object_after_post_upload_crash(
    tmp_path: Path,
) -> None:
    image_root = tmp_path / "image-root"
    image_root.mkdir()
    config = _config(tmp_path)
    _seed_finalized_image(config, image_root)
    store = _CrashAfterUploadArchiveStore(
        receipt=ArchiveUploadReceipt(
            object_path="glacier/finalized-images/20260420T040001Z/20260420T040001Z.iso",
            stored_bytes=456,
            backend="s3",
            storage_class="DEEP_ARCHIVE",
            uploaded_at="2026-04-20T04:01:00Z",
            verified_at="2026-04-20T04:01:01Z",
        )
    )
    service = SqlAlchemyGlacierUploadService(config, store)

    try:
        service.process_due_uploads()
    except KeyboardInterrupt as exc:
        assert str(exc) == "simulated process crash after remote upload"
    else:  # pragma: no cover - defensive assertion
        raise AssertionError("expected simulated crash")

    session_factory = make_session_factory(str(config.sqlite_path))
    with session_scope(session_factory) as session:
        image = session.get(FinalizedImageRecord, "20260420T040001Z")
        assert image is not None
        assert image.glacier_state == "uploading"
        assert image.glacier_object_path is None
        job = session.get(GlacierUploadJobRecord, "20260420T040001Z")
        assert job is not None
        assert job.completed_at is None
        assert job.failed_at is None

    restarted_service = SqlAlchemyGlacierUploadService(config, store)
    assert restarted_service.process_due_uploads() == 1

    with session_scope(session_factory) as session:
        image = session.get(FinalizedImageRecord, "20260420T040001Z")
        assert image is not None
        assert image.glacier_state == "uploaded"
        assert image.glacier_object_path == (
            "glacier/finalized-images/20260420T040001Z/20260420T040001Z.iso"
        )
    assert store.upload_calls == 1
    assert store.total_calls == 2


def test_process_due_uploads_retries_then_marks_failed_and_notifies_webhook(
    monkeypatch,
    tmp_path: Path,
) -> None:
    image_root = tmp_path / "image-root"
    image_root.mkdir()
    config = _config(
        tmp_path,
        glacier_upload_retry_limit=2,
        glacier_upload_retry_delay=timedelta(seconds=0),
        glacier_failure_webhook_url="https://example.test/hook",
        public_base_url="https://api.test",
    )
    _seed_finalized_image(config, image_root)
    service = SqlAlchemyGlacierUploadService(
        config,
        _FakeArchiveStore(error=RuntimeError("s3 timeout")),
    )

    payloads: list[dict[str, object]] = []

    def _capture_webhook(*, config, payload):  # type: ignore[no-untyped-def]
        payloads.append(payload)

    monkeypatch.setattr("arc_core.services.glacier_uploads.post_webhook", _capture_webhook)

    assert service.process_due_uploads() == 1

    session_factory = make_session_factory(str(config.sqlite_path))
    with session_scope(session_factory) as session:
        image = session.get(FinalizedImageRecord, "20260420T040001Z")
        assert image is not None
        assert image.glacier_state == "retrying"
        assert image.glacier_failure == "s3 timeout"

    assert service.process_due_uploads() == 1

    with session_scope(session_factory) as session:
        image = session.get(FinalizedImageRecord, "20260420T040001Z")
        assert image is not None
        assert image.glacier_state == "failed"
        assert image.glacier_failure == "s3 timeout"

    assert len(payloads) == 1
    assert payloads[0]["event"] == "images.glacier_upload.failed"
    assert payloads[0]["image_id"] == "20260420T040001Z"
    assert payloads[0]["attempts"] == 2
    assert payloads[0]["error"] == "s3 timeout"
    assert payloads[0]["image_url"] == "https://api.test/v1/images/20260420T040001Z"
    assert payloads[0]["download_url"] == "https://api.test/v1/images/20260420T040001Z/iso"
