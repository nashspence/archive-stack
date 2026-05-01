from __future__ import annotations

import hashlib
from collections.abc import Iterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from arc_core.catalog_models import (
    CollectionArchiveRecord,
    CollectionFileRecord,
    CollectionRecord,
    FinalizedImageCollectionArtifactRecord,
    FinalizedImageCoveragePartRecord,
    FinalizedImageCoveredPathRecord,
    FinalizedImageRecord,
)
from arc_core.collection_archives import (
    CollectionArchiveFile,
    CollectionArchivePackage,
    build_collection_archive_package,
)
from arc_core.domain.enums import RecoverySessionState
from arc_core.domain.errors import InvalidState
from arc_core.finalized_image_coverage import (
    read_finalized_image_collection_artifacts,
    read_finalized_image_coverage_parts,
)
from arc_core.ports.archive_store import ArchiveRestoreStatus
from arc_core.runtime_config import RuntimeConfig
from arc_core.services.copies import SqlAlchemyCopyService
from arc_core.services.recovery_sessions import SqlAlchemyRecoverySessionService
from arc_core.sqlite_db import initialize_db, make_session_factory, session_scope
from tests.fixtures.crypto import FixtureProofStamper, FixtureRecoveryPayloadCodec
from tests.fixtures.data import DOCS_FILES, IMAGE_ONE_FILES, write_tree

_PROOF_STAMPER = FixtureProofStamper()
_RECOVERY_CODEC = FixtureRecoveryPayloadCodec()


class _FakeHotStore:
    def __init__(self) -> None:
        self.puts: dict[tuple[str, str], bytes] = {}

    def put_collection_file(self, collection_id: str, path: str, content: bytes) -> None:
        self.puts[(collection_id, path)] = content

    def get_collection_file(self, collection_id: str, path: str) -> bytes:
        assert collection_id == "docs"
        return DOCS_FILES[path]


class _FakeArchiveStore:
    def __init__(
        self,
        *,
        collection_packages: dict[str, CollectionArchivePackage] | None = None,
    ) -> None:
        self.restore_requests: list[str | tuple[str, str, str]] = []
        self.cleanup_requests: list[str | tuple[str, str, str]] = []
        self.archive_reads: list[str] = []
        self.manifest_reads: list[str] = []
        self.proof_reads: list[str] = []
        self.collection_packages = collection_packages or {}

    def request_collection_archive_restore(
        self,
        *,
        collection_id: str,
        object_path: str,
        retrieval_tier: str,
        hold_days: int,
        requested_at: str,
        estimated_ready_at: str,
        manifest_object_path: str | None = None,
        proof_object_path: str | None = None,
    ) -> ArchiveRestoreStatus:
        assert retrieval_tier
        assert hold_days > 0
        assert requested_at
        assert manifest_object_path is not None
        assert proof_object_path is not None
        self.restore_requests.append((object_path, manifest_object_path, proof_object_path))
        return ArchiveRestoreStatus(state="requested", ready_at=estimated_ready_at)

    def get_collection_archive_restore_status(
        self,
        *,
        collection_id: str,
        object_path: str,
        requested_at: str,
        estimated_ready_at: str | None,
        estimated_expires_at: str | None,
        manifest_object_path: str | None = None,
        proof_object_path: str | None = None,
    ) -> ArchiveRestoreStatus:
        assert collection_id in self.collection_packages
        assert object_path
        assert requested_at
        assert manifest_object_path is not None
        assert proof_object_path is not None
        return ArchiveRestoreStatus(state="ready", ready_at=estimated_ready_at)

    def iter_restored_collection_archive(
        self,
        *,
        collection_id: str,
        object_path: str,
    ) -> Iterator[bytes]:
        self.archive_reads.append(object_path)
        archive_bytes = self.collection_packages[collection_id].archive_bytes
        for offset in range(0, len(archive_bytes), 7):
            yield archive_bytes[offset : offset + 7]

    def read_restored_collection_archive_manifest(
        self,
        *,
        collection_id: str,
        object_path: str,
    ) -> bytes:
        self.manifest_reads.append(object_path)
        return self.collection_packages[collection_id].manifest_bytes

    def read_restored_collection_archive_proof(
        self,
        *,
        collection_id: str,
        object_path: str,
    ) -> bytes:
        self.proof_reads.append(object_path)
        return self.collection_packages[collection_id].proof_bytes

    def cleanup_collection_archive_restore(
        self,
        *,
        collection_id: str,
        object_path: str,
        manifest_object_path: str | None = None,
        proof_object_path: str | None = None,
    ) -> None:
        assert manifest_object_path is not None
        assert proof_object_path is not None
        self.cleanup_requests.append((object_path, manifest_object_path, proof_object_path))


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
                        sha256=hashlib.sha256(content).hexdigest(),
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
        for artifact in read_finalized_image_collection_artifacts(image_root, _RECOVERY_CODEC):
            session.add(
                FinalizedImageCollectionArtifactRecord(
                    image_id=image_id,
                    collection_id=artifact.collection_id,
                    manifest_path=artifact.manifest_path,
                    proof_path=artifact.proof_path,
                )
            )
        for part in read_finalized_image_coverage_parts(image_root, _RECOVERY_CODEC):
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


def _docs_collection_archive_package() -> CollectionArchivePackage:
    return build_collection_archive_package(
        collection_id="docs",
        files=tuple(
            CollectionArchiveFile(
                path=path,
                content=content,
                sha256=hashlib.sha256(content).hexdigest(),
            )
            for path, content in sorted(DOCS_FILES.items())
        ),
        stamper=_PROOF_STAMPER,
    )


def _seed_collection_archive(sqlite_path: Path, package: CollectionArchivePackage) -> None:
    session_factory = make_session_factory(str(sqlite_path))
    with session_scope(session_factory) as session:
        session.add(
            CollectionArchiveRecord(
                collection_id=package.collection_id,
                state="uploaded",
                object_path=f"glacier/collections/{package.collection_id}/archive.tar",
                stored_bytes=len(package.archive_bytes),
                sha256=package.archive_sha256,
                backend="s3",
                storage_class="DEEP_ARCHIVE",
                last_uploaded_at="2026-04-20T04:00:00Z",
                last_verified_at="2026-04-20T04:00:01Z",
                archive_format=package.archive_format,
                compression=package.compression,
                manifest_object_path=f"glacier/collections/{package.collection_id}/manifest.yml",
                manifest_sha256=package.manifest_sha256,
                manifest_stored_bytes=len(package.manifest_bytes),
                manifest_uploaded_at="2026-04-20T04:00:00Z",
                ots_object_path=f"glacier/collections/{package.collection_id}/manifest.yml.ots",
                ots_sha256=package.proof_sha256,
                ots_stored_bytes=len(package.proof_bytes),
                ots_uploaded_at="2026-04-20T04:00:00Z",
            )
        )


def _seed_docs_collection_archive(sqlite_path: Path) -> CollectionArchivePackage:
    package = _docs_collection_archive_package()
    _seed_collection_archive(sqlite_path, package)
    return package


def test_double_copy_loss_creates_pending_recovery_session(tmp_path: Path) -> None:
    sqlite_path = tmp_path / "state.sqlite3"
    image_root = tmp_path / "image-root"
    initialize_db(str(sqlite_path))
    write_tree(image_root, IMAGE_ONE_FILES)
    _seed_finalized_image(sqlite_path, image_root)
    package = _seed_docs_collection_archive(sqlite_path)

    config = _config(sqlite_path)
    copy_service = SqlAlchemyCopyService(config, _FakeHotStore())
    recovery_service = SqlAlchemyRecoverySessionService(
        config,
        _FakeArchiveStore(collection_packages={"docs": package}),
    )

    copy_service.register("20260420T040001Z", "Shelf A1", copy_id="20260420T040001Z-1")
    copy_service.register("20260420T040001Z", "Shelf B1", copy_id="20260420T040001Z-2")
    copy_service.update("20260420T040001Z", "20260420T040001Z-1", state="lost")
    copy_service.update("20260420T040001Z", "20260420T040001Z-2", state="damaged")

    session = recovery_service.get_for_image("20260420T040001Z")

    assert session.id == "rs-20260420T040001Z-rebuild-1"
    assert session.state == RecoverySessionState.PENDING_APPROVAL
    assert session.cost_estimate.total_estimated_cost_usd > 0
    assert session.notification.webhook_configured is False
    assert [str(image.id) for image in session.images] == ["20260420T040001Z"]


def test_image_recovery_requires_uploaded_collection_archive(tmp_path: Path) -> None:
    sqlite_path = tmp_path / "state.sqlite3"
    image_root = tmp_path / "image-root"
    initialize_db(str(sqlite_path))
    write_tree(image_root, IMAGE_ONE_FILES)
    _seed_finalized_image(sqlite_path, image_root)

    config = _config(sqlite_path)
    recovery_service = SqlAlchemyRecoverySessionService(config, _FakeArchiveStore())

    with pytest.raises(InvalidState, match="image collections are not archived"):
        recovery_service.create_or_resume_for_image("20260420T040001Z")


def test_recovery_session_processes_ready_and_expired_states(
    tmp_path: Path,
    monkeypatch,
) -> None:
    sqlite_path = tmp_path / "state.sqlite3"
    image_root = tmp_path / "image-root"
    initialize_db(str(sqlite_path))
    write_tree(image_root, IMAGE_ONE_FILES)
    _seed_finalized_image(sqlite_path, image_root)
    package = _seed_docs_collection_archive(sqlite_path)

    config = _config(
        sqlite_path,
        glacier_recovery_restore_latency=timedelta(seconds=10),
        glacier_recovery_ready_ttl=timedelta(seconds=5),
    )
    copy_service = SqlAlchemyCopyService(config, _FakeHotStore())
    recovery_service = SqlAlchemyRecoverySessionService(
        config,
        _FakeArchiveStore(collection_packages={"docs": package}),
    )

    copy_service.register("20260420T040001Z", "Shelf A1", copy_id="20260420T040001Z-1")
    copy_service.register("20260420T040001Z", "Shelf B1", copy_id="20260420T040001Z-2")
    copy_service.update("20260420T040001Z", "20260420T040001Z-1", state="lost")
    copy_service.update("20260420T040001Z", "20260420T040001Z-2", state="lost")

    start = datetime(2026, 4, 20, 4, 0, tzinfo=UTC)
    monkeypatch.setattr("arc_core.services.recovery_sessions.utcnow", lambda: start)
    approved = recovery_service.approve("rs-20260420T040001Z-rebuild-1")
    assert approved.state == RecoverySessionState.RESTORE_REQUESTED
    assert approved.restore_ready_at == "2026-04-20T04:00:10Z"

    monkeypatch.setattr(
        "arc_core.services.recovery_sessions.utcnow",
        lambda: start + timedelta(seconds=11),
    )
    assert recovery_service.process_due_sessions() == 1
    ready = recovery_service.get("rs-20260420T040001Z-rebuild-1")
    assert ready.state == RecoverySessionState.READY
    assert ready.restore_expires_at == "2026-04-20T04:00:16Z"

    monkeypatch.setattr(
        "arc_core.services.recovery_sessions.utcnow",
        lambda: start + timedelta(seconds=17),
    )
    assert recovery_service.process_due_sessions() == 1
    expired = recovery_service.get("rs-20260420T040001Z-rebuild-1")
    assert expired.state == RecoverySessionState.EXPIRED

    completed = recovery_service.complete("rs-20260420T040001Z-rebuild-1")
    assert completed.state == RecoverySessionState.COMPLETED


def test_collection_restore_requests_and_verifies_manifest_and_proof(
    tmp_path: Path,
    monkeypatch,
) -> None:
    sqlite_path = tmp_path / "state.sqlite3"
    image_root = tmp_path / "image-root"
    initialize_db(str(sqlite_path))
    write_tree(image_root, IMAGE_ONE_FILES)
    _seed_finalized_image(sqlite_path, image_root)
    package = _docs_collection_archive_package()
    _seed_collection_archive(sqlite_path, package)
    store = _FakeArchiveStore(collection_packages={"docs": package})
    config = _config(
        sqlite_path,
        glacier_recovery_restore_latency=timedelta(seconds=0),
        glacier_recovery_sweep_interval=timedelta(seconds=0),
    )
    recovery_service = SqlAlchemyRecoverySessionService(
        config,
        store,
        proof_stamper=_PROOF_STAMPER,
        recovery_payload_codec=_RECOVERY_CODEC,
    )

    session = recovery_service.create_or_resume_for_collection("docs")
    start = datetime(2026, 4, 20, 4, 0, tzinfo=UTC)
    monkeypatch.setattr("arc_core.services.recovery_sessions.utcnow", lambda: start)
    approved = recovery_service.approve(session.id)

    assert approved.state == RecoverySessionState.RESTORE_REQUESTED
    assert store.restore_requests == [
        (
            "glacier/collections/docs/archive.tar",
            "glacier/collections/docs/manifest.yml",
            "glacier/collections/docs/manifest.yml.ots",
        )
    ]

    assert recovery_service.process_due_sessions() == 1
    ready = recovery_service.get(session.id)
    assert ready.state == RecoverySessionState.READY

    completed = recovery_service.complete(session.id)

    assert completed.state == RecoverySessionState.COMPLETED
    assert store.archive_reads == ["glacier/collections/docs/archive.tar"]
    assert store.manifest_reads == ["glacier/collections/docs/manifest.yml"]
    assert store.proof_reads == ["glacier/collections/docs/manifest.yml.ots"]
    assert store.cleanup_requests == [
        (
            "glacier/collections/docs/archive.tar",
            "glacier/collections/docs/manifest.yml",
            "glacier/collections/docs/manifest.yml.ots",
        )
    ]


def test_collection_restore_materializes_selected_files_to_hot_storage(
    tmp_path: Path,
    monkeypatch,
) -> None:
    sqlite_path = tmp_path / "state.sqlite3"
    image_root = tmp_path / "image-root"
    initialize_db(str(sqlite_path))
    write_tree(image_root, IMAGE_ONE_FILES)
    _seed_finalized_image(sqlite_path, image_root)
    package = _docs_collection_archive_package()
    _seed_collection_archive(sqlite_path, package)
    store = _FakeArchiveStore(collection_packages={"docs": package})
    hot_store = _FakeHotStore()
    config = _config(
        sqlite_path,
        glacier_recovery_restore_latency=timedelta(seconds=0),
        glacier_recovery_sweep_interval=timedelta(seconds=0),
    )
    recovery_service = SqlAlchemyRecoverySessionService(config, store, hot_store)

    session = recovery_service.create_or_resume_for_collection("docs")
    monkeypatch.setattr(
        "arc_core.services.recovery_sessions.utcnow",
        lambda: datetime(2026, 4, 20, 4, 0, tzinfo=UTC),
    )
    recovery_service.approve(session.id)
    assert recovery_service.process_due_sessions() == 1

    materialized = recovery_service.materialize_collection_files(
        session.id,
        "docs",
        paths=["tax/2022/invoice-123.pdf"],
    )

    assert materialized.progress.archive_verification == "completed"
    assert materialized.progress.extraction == "completed"
    assert materialized.progress.materialization == "completed"
    assert hot_store.puts == {
        ("docs", "tax/2022/invoice-123.pdf"): DOCS_FILES["tax/2022/invoice-123.pdf"]
    }
    with session_scope(make_session_factory(str(sqlite_path))) as db_session:
        row = db_session.get(
            CollectionFileRecord,
            {"collection_id": "docs", "path": "tax/2022/invoice-123.pdf"},
        )
        assert row is not None
        assert row.hot is True


def test_collection_restore_rejects_empty_proof_before_completion(
    tmp_path: Path,
    monkeypatch,
) -> None:
    sqlite_path = tmp_path / "state.sqlite3"
    image_root = tmp_path / "image-root"
    initialize_db(str(sqlite_path))
    write_tree(image_root, IMAGE_ONE_FILES)
    _seed_finalized_image(sqlite_path, image_root)
    package = _docs_collection_archive_package()
    bad_proof = b""
    bad_package = replace(
        package,
        proof_bytes=bad_proof,
        proof_sha256=hashlib.sha256(bad_proof).hexdigest(),
    )
    _seed_collection_archive(sqlite_path, bad_package)
    store = _FakeArchiveStore(collection_packages={"docs": bad_package})
    config = _config(
        sqlite_path,
        glacier_recovery_restore_latency=timedelta(seconds=0),
        glacier_recovery_sweep_interval=timedelta(seconds=0),
    )
    recovery_service = SqlAlchemyRecoverySessionService(
        config,
        store,
        proof_stamper=_PROOF_STAMPER,
        recovery_payload_codec=_RECOVERY_CODEC,
    )

    session = recovery_service.create_or_resume_for_collection("docs")
    monkeypatch.setattr(
        "arc_core.services.recovery_sessions.utcnow",
        lambda: datetime(2026, 4, 20, 4, 0, tzinfo=UTC),
    )
    recovery_service.approve(session.id)
    assert recovery_service.process_due_sessions() == 1

    with pytest.raises(ValueError, match="proof is empty"):
        recovery_service.complete(session.id)
    assert store.cleanup_requests == []


def test_collection_restore_rejects_corrupt_archive_before_completion(
    tmp_path: Path,
    monkeypatch,
) -> None:
    sqlite_path = tmp_path / "state.sqlite3"
    image_root = tmp_path / "image-root"
    initialize_db(str(sqlite_path))
    write_tree(image_root, IMAGE_ONE_FILES)
    _seed_finalized_image(sqlite_path, image_root)
    package = _docs_collection_archive_package()
    corrupt_archive_bytes = package.archive_bytes.replace(
        b"invoice 123 contents\n",
        b"invoice 123 contentx\n",
        1,
    )
    corrupt_package = replace(
        package,
        archive_size=len(corrupt_archive_bytes),
        archive_sha256=hashlib.sha256(corrupt_archive_bytes).hexdigest(),
        _archive_chunks=lambda: iter((corrupt_archive_bytes,)),
    )
    _seed_collection_archive(sqlite_path, corrupt_package)
    store = _FakeArchiveStore(collection_packages={"docs": corrupt_package})
    config = _config(
        sqlite_path,
        glacier_recovery_restore_latency=timedelta(seconds=0),
        glacier_recovery_sweep_interval=timedelta(seconds=0),
    )
    recovery_service = SqlAlchemyRecoverySessionService(config, store)

    session = recovery_service.create_or_resume_for_collection("docs")
    monkeypatch.setattr(
        "arc_core.services.recovery_sessions.utcnow",
        lambda: datetime(2026, 4, 20, 4, 0, tzinfo=UTC),
    )
    recovery_service.approve(session.id)
    assert recovery_service.process_due_sessions() == 1

    with pytest.raises(ValueError, match="member sha256 mismatch"):
        recovery_service.complete(session.id)
    assert store.archive_reads == ["glacier/collections/docs/archive.tar"]
    assert store.cleanup_requests == []


def test_image_rebuild_verifies_manifest_and_proof_before_streaming_archive(
    tmp_path: Path,
    monkeypatch,
) -> None:
    sqlite_path = tmp_path / "state.sqlite3"
    image_root = tmp_path / "image-root"
    initialize_db(str(sqlite_path))
    write_tree(image_root, IMAGE_ONE_FILES)
    _seed_finalized_image(sqlite_path, image_root)
    package = _docs_collection_archive_package()
    _seed_collection_archive(sqlite_path, package)
    store = _FakeArchiveStore(collection_packages={"docs": package})

    config = _config(
        sqlite_path,
        glacier_recovery_restore_latency=timedelta(seconds=0),
        glacier_recovery_sweep_interval=timedelta(seconds=0),
    )
    copy_service = SqlAlchemyCopyService(config, _FakeHotStore())
    recovery_service = SqlAlchemyRecoverySessionService(
        config,
        store,
        proof_stamper=_PROOF_STAMPER,
        recovery_payload_codec=_RECOVERY_CODEC,
    )

    copy_service.register("20260420T040001Z", "Shelf A1", copy_id="20260420T040001Z-1")
    copy_service.register("20260420T040001Z", "Shelf B1", copy_id="20260420T040001Z-2")
    copy_service.update("20260420T040001Z", "20260420T040001Z-1", state="lost")
    copy_service.update("20260420T040001Z", "20260420T040001Z-2", state="damaged")
    recovery_service.approve("rs-20260420T040001Z-rebuild-1")
    assert recovery_service.process_due_sessions() == 1

    def _fake_iso(**kwargs: object):
        yield b"rebuilt-iso"

    monkeypatch.setattr("arc_core.services.recovery_sessions._run_iso_from_root", _fake_iso)

    chunks = list(
        recovery_service.iter_restored_iso(
            "rs-20260420T040001Z-rebuild-1",
            "20260420T040001Z",
        )
    )

    assert chunks == [b"rebuilt-iso"]
    assert store.manifest_reads == ["glacier/collections/docs/manifest.yml"]
    assert store.proof_reads == ["glacier/collections/docs/manifest.yml.ots"]


def test_recovery_session_retries_initial_ready_notification_before_reminders(
    tmp_path: Path,
    monkeypatch,
) -> None:
    sqlite_path = tmp_path / "state.sqlite3"
    image_root = tmp_path / "image-root"
    initialize_db(str(sqlite_path))
    write_tree(image_root, IMAGE_ONE_FILES)
    _seed_finalized_image(sqlite_path, image_root)
    package = _seed_docs_collection_archive(sqlite_path)

    config = _config(
        sqlite_path,
        glacier_recovery_webhook_url="http://example.invalid/webhooks/recovery",
        glacier_recovery_restore_latency=timedelta(seconds=10),
        glacier_recovery_webhook_retry_delay=timedelta(seconds=1),
        glacier_recovery_webhook_reminder_interval=timedelta(seconds=5),
    )
    copy_service = SqlAlchemyCopyService(config, _FakeHotStore())
    recovery_service = SqlAlchemyRecoverySessionService(
        config,
        _FakeArchiveStore(collection_packages={"docs": package}),
    )

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
    recovery_service.approve("rs-20260420T040001Z-rebuild-1")

    monkeypatch.setattr(
        "arc_core.services.recovery_sessions.utcnow",
        lambda: start + timedelta(seconds=11),
    )
    assert recovery_service.process_due_sessions() == 1

    failed_delivery = recovery_service.get("rs-20260420T040001Z-rebuild-1")
    assert failed_delivery.state == RecoverySessionState.READY
    assert failed_delivery.notification.last_notified_at is None
    assert failed_delivery.notification.reminder_count == 0
    assert attempts == ["images.rebuild_ready"]

    monkeypatch.setattr(
        "arc_core.services.recovery_sessions.utcnow",
        lambda: start + timedelta(seconds=12),
    )
    assert recovery_service.process_due_sessions() == 1

    retried_delivery = recovery_service.get("rs-20260420T040001Z-rebuild-1")
    assert retried_delivery.notification.last_notified_at == "2026-04-20T04:00:12Z"
    assert retried_delivery.notification.reminder_count == 0
    assert attempts == ["images.rebuild_ready", "images.rebuild_ready"]


def test_recovery_session_retries_initial_ready_notification_before_expiring(
    tmp_path: Path,
    monkeypatch,
) -> None:
    sqlite_path = tmp_path / "state.sqlite3"
    image_root = tmp_path / "image-root"
    initialize_db(str(sqlite_path))
    write_tree(image_root, IMAGE_ONE_FILES)
    _seed_finalized_image(sqlite_path, image_root)
    package = _seed_docs_collection_archive(sqlite_path)

    config = _config(
        sqlite_path,
        glacier_recovery_webhook_url="http://example.invalid/webhooks/recovery",
        glacier_recovery_restore_latency=timedelta(seconds=10),
        glacier_recovery_ready_ttl=timedelta(seconds=12),
        glacier_recovery_webhook_retry_delay=timedelta(seconds=1),
        glacier_recovery_webhook_reminder_interval=timedelta(seconds=5),
    )
    copy_service = SqlAlchemyCopyService(config, _FakeHotStore())
    recovery_service = SqlAlchemyRecoverySessionService(
        config,
        _FakeArchiveStore(collection_packages={"docs": package}),
    )

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
    recovery_service.approve("rs-20260420T040001Z-rebuild-1")

    monkeypatch.setattr(
        "arc_core.services.recovery_sessions.utcnow",
        lambda: start + timedelta(seconds=11),
    )
    assert recovery_service.process_due_sessions() == 1

    failed_delivery = recovery_service.get("rs-20260420T040001Z-rebuild-1")
    assert failed_delivery.state == RecoverySessionState.READY
    assert failed_delivery.notification.last_notified_at is None
    assert failed_delivery.notification.next_reminder_at == "2026-04-20T04:00:12Z"
    assert failed_delivery.restore_expires_at == "2026-04-20T04:00:23Z"

    monkeypatch.setattr(
        "arc_core.services.recovery_sessions.utcnow",
        lambda: start + timedelta(seconds=22),
    )
    assert recovery_service.process_due_sessions() == 1

    retried_delivery = recovery_service.get("rs-20260420T040001Z-rebuild-1")
    assert retried_delivery.state == RecoverySessionState.READY
    assert retried_delivery.notification.last_notified_at == "2026-04-20T04:00:22Z"
    assert retried_delivery.notification.reminder_count == 0
    assert attempts == ["images.rebuild_ready", "images.rebuild_ready"]


def test_pending_recovery_session_can_group_multiple_images_before_approval(
    tmp_path: Path,
) -> None:
    sqlite_path = tmp_path / "state.sqlite3"
    image_root_one = tmp_path / "image-root-one"
    image_root_two = tmp_path / "image-root-two"
    initialize_db(str(sqlite_path))
    write_tree(image_root_one, IMAGE_ONE_FILES)
    write_tree(image_root_two, IMAGE_ONE_FILES)
    _seed_finalized_image(sqlite_path, image_root_one)
    _seed_finalized_image(
        sqlite_path,
        image_root_two,
        image_id="20260420T040003Z",
        candidate_id="img_2026-04-20_03",
        filename="20260420T040003Z.iso",
    )
    package = _seed_docs_collection_archive(sqlite_path)

    config = _config(sqlite_path)
    copy_service = SqlAlchemyCopyService(config, _FakeHotStore())
    recovery_service = SqlAlchemyRecoverySessionService(
        config,
        _FakeArchiveStore(collection_packages={"docs": package}),
    )

    copy_service.register("20260420T040001Z", "Shelf A1", copy_id="20260420T040001Z-1")
    copy_service.register("20260420T040001Z", "Shelf B1", copy_id="20260420T040001Z-2")
    copy_service.register("20260420T040003Z", "Shelf C1", copy_id="20260420T040003Z-1")
    copy_service.register("20260420T040003Z", "Shelf D1", copy_id="20260420T040003Z-2")

    copy_service.update("20260420T040001Z", "20260420T040001Z-1", state="lost")
    copy_service.update("20260420T040001Z", "20260420T040001Z-2", state="damaged")
    copy_service.update("20260420T040003Z", "20260420T040003Z-1", state="lost")
    copy_service.update("20260420T040003Z", "20260420T040003Z-2", state="damaged")

    session = recovery_service.get("rs-20260420T040001Z-rebuild-1")

    assert session.state == RecoverySessionState.PENDING_APPROVAL
    assert [str(image.id) for image in session.images] == [
        "20260420T040001Z",
        "20260420T040003Z",
    ]
    assert session.cost_estimate.image_count == 1
    assert session.cost_estimate.restore_request_count == 1
