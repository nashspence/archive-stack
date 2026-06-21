from __future__ import annotations

import hashlib
import sys
from collections.abc import Iterable, Iterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from riverhog_core.catalog_db import initialize_db, make_session_factory, session_scope
from riverhog_core.catalog_models import (
    ActivePinRecord,
    CollectionArchiveRecord,
    CollectionFileRecord,
    CollectionRecord,
    FileCopyRecord,
    FinalizedImageCollectionArtifactRecord,
    FinalizedImageCoveragePartRecord,
    FinalizedImageCoveredPathRecord,
    FinalizedImageRecord,
    GlacierRecoverySessionRecord,
)
from riverhog_core.collection_archives import (
    CollectionArchiveFile,
    CollectionArchivePackage,
    build_collection_archive_package,
)
from riverhog_core.domain.enums import FetchState, RecoverySessionState
from riverhog_core.domain.errors import NotFound
from riverhog_core.finalized_image_coverage import (
    read_finalized_image_collection_artifacts,
    read_finalized_image_coverage_parts,
)
from riverhog_core.ports.archive_store import ArchiveRestoreStatus
from riverhog_core.ports.hot_store import HotFileStat
from riverhog_core.runtime_config import RuntimeConfig
from riverhog_core.services import recovery_sessions as recovery_sessions_module
from riverhog_core.services.copies import SqlAlchemyCopyService
from riverhog_core.services.recovery_sessions import SqlAlchemyRecoverySessionService
from tests.fixtures.crypto import FixtureProofStamper, FixtureRecoveryPayloadCodec
from tests.fixtures.data import DOCS_FILES, IMAGE_ONE_FILES, write_tree
from tests.unit.db_helpers import sqlite_url

_PROOF_STAMPER = FixtureProofStamper()
_RECOVERY_CODEC = FixtureRecoveryPayloadCodec()


class _FakeHotStore:
    def __init__(self) -> None:
        self.puts: dict[tuple[str, str], bytes] = {}
        self.byte_put_paths: list[tuple[str, str]] = []
        self.stream_chunk_lengths: dict[tuple[str, str], list[int]] = {}

    def put_collection_file(self, collection_id: str, path: str, content: bytes) -> None:
        self.byte_put_paths.append((collection_id, path))
        self.puts[(collection_id, path)] = content

    def put_collection_file_stream(
        self,
        collection_id: str,
        path: str,
        chunks: Iterable[bytes],
        *,
        content_length: int,
    ) -> None:
        chunk_list = list(chunks)
        content = b"".join(chunk_list)
        assert len(content) == content_length
        self.stream_chunk_lengths[(collection_id, path)] = [len(chunk) for chunk in chunk_list]
        self.puts[(collection_id, path)] = content

    def get_collection_file(self, collection_id: str, path: str) -> bytes:
        assert collection_id == "docs"
        return DOCS_FILES[path]

    def stat_collection_file(self, collection_id: str, path: str) -> HotFileStat | None:
        content = self.puts.get((collection_id, path))
        if content is None:
            return None
        return HotFileStat(bytes=len(content), sha256=hashlib.sha256(content).hexdigest())


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
        if estimated_ready_at is not None:
            current = recovery_sessions_module.utcnow()
            ready_at = datetime.fromisoformat(estimated_ready_at.replace("Z", "+00:00"))
            if current < ready_at:
                return ArchiveRestoreStatus(state="requested", ready_at=estimated_ready_at)
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

    def read_restored_collection_manifest(
        self,
        *,
        collection_id: str,
        object_path: str,
    ) -> bytes:
        self.manifest_reads.append(object_path)
        return self.collection_packages[collection_id].manifest_bytes

    def read_restored_collection_manifest_proof(
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
        database_url=sqlite_url(sqlite_path),
        ots_verify_command=(sys.executable, "-m", "tests.fixtures.ots_stamp_command"),
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
    session_factory = make_session_factory(sqlite_url(sqlite_path))
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
    archive_prefix = f"glacier/archives/opaque-{package.collection_id}"
    object_path = f"{archive_prefix}/archive.tar.age"
    manifest_object_path = f"{archive_prefix}/manifest.yml.age"
    proof_object_path = f"{archive_prefix}/manifest.yml.ots.age"
    session_factory = make_session_factory(sqlite_url(sqlite_path))
    with session_scope(session_factory) as session:
        session.add(
            CollectionArchiveRecord(
                collection_id=package.collection_id,
                state="uploaded",
                object_path=object_path,
                stored_bytes=len(package.archive_bytes),
                sha256=package.archive_sha256,
                backend="s3",
                storage_class="DEEP_ARCHIVE",
                last_uploaded_at="2026-04-20T04:00:00Z",
                last_verified_at="2026-04-20T04:00:01Z",
                archive_format=package.archive_format,
                compression=package.compression,
                manifest_object_path=manifest_object_path,
                manifest_sha256=package.manifest_sha256,
                manifest_stored_bytes=len(package.manifest_bytes),
                manifest_uploaded_at="2026-04-20T04:00:00Z",
                ots_object_path=proof_object_path,
                ots_sha256=package.proof_sha256,
                ots_stored_bytes=len(package.proof_bytes),
                ots_uploaded_at="2026-04-20T04:00:00Z",
            )
        )


def _seed_collection_files(
    sqlite_path: Path,
    *,
    collection_id: str,
    files: dict[str, bytes],
    hot: bool,
    archived: bool,
) -> None:
    session_factory = make_session_factory(sqlite_url(sqlite_path))
    with session_scope(session_factory) as session:
        session.add(CollectionRecord(id=collection_id))
        for path, content in sorted(files.items()):
            session.add(
                CollectionFileRecord(
                    collection_id=collection_id,
                    path=path,
                    bytes=len(content),
                    sha256=hashlib.sha256(content).hexdigest(),
                    hot=hot,
                    archived=archived,
                )
            )


def _seed_docs_collection_archive(sqlite_path: Path) -> CollectionArchivePackage:
    package = _docs_collection_archive_package()
    _seed_collection_archive(sqlite_path, package)
    return package


def test_double_copy_loss_creates_queued_recovery_session(tmp_path: Path) -> None:
    sqlite_path = tmp_path / "state.sqlite3"
    image_root = tmp_path / "image-root"
    initialize_db(sqlite_url(sqlite_path))
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
    assert session.state == RecoverySessionState.RESTORE_REQUESTED
    assert session.notification.webhook_configured is False
    assert [str(image.id) for image in session.images] == ["20260420T040001Z"]


def test_recovery_sessions_can_be_listed_and_filtered(tmp_path: Path) -> None:
    sqlite_path = tmp_path / "state.sqlite3"
    image_root = tmp_path / "image-root"
    initialize_db(sqlite_url(sqlite_path))
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
    recovery_service.create_or_resume_for_collection("docs")

    restore_page = recovery_service.list(
        page=1,
        per_page=25,
        sort="created_at",
        order="desc",
        recovery_type="collection_restore",
        state="restore_requested",
        collection="docs",
    )
    rebuild_page = recovery_service.list(
        page=1,
        per_page=25,
        sort="id",
        order="asc",
        recovery_type="image_rebuild",
        image="20260420T040001Z",
    )

    assert restore_page.total == 1
    assert [session.id for session in restore_page.sessions] == ["rs-docs-restore-1"]
    assert restore_page.collection == "docs"
    assert rebuild_page.total == 1
    assert [session.id for session in rebuild_page.sessions] == ["rs-20260420T040001Z-rebuild-1"]


def test_recovery_ready_ttl_rounds_up_to_restore_hold_days(tmp_path: Path) -> None:
    sqlite_path = tmp_path / "state.sqlite3"
    image_root = tmp_path / "image-root"
    initialize_db(sqlite_url(sqlite_path))
    write_tree(image_root, IMAGE_ONE_FILES)
    _seed_finalized_image(sqlite_path, image_root)
    package = _seed_docs_collection_archive(sqlite_path)

    config = _config(sqlite_path, glacier_recovery_ready_ttl=timedelta(hours=25))
    copy_service = SqlAlchemyCopyService(config, _FakeHotStore())
    recovery_service = SqlAlchemyRecoverySessionService(
        config,
        _FakeArchiveStore(collection_packages={"docs": package}),
    )

    copy_service.register("20260420T040001Z", "Shelf A1", copy_id="20260420T040001Z-1")
    copy_service.register("20260420T040001Z", "Shelf B1", copy_id="20260420T040001Z-2")
    copy_service.update("20260420T040001Z", "20260420T040001Z-1", state="lost")
    copy_service.update("20260420T040001Z", "20260420T040001Z-2", state="lost")

    session = recovery_service.get_for_image("20260420T040001Z")

    with session_scope(make_session_factory(sqlite_url(sqlite_path))) as db_session:
        record = db_session.get(GlacierRecoverySessionRecord, session.id)
        assert record is not None
        assert record.hold_days == 2


def test_image_recovery_requires_uploaded_collection_archive(tmp_path: Path) -> None:
    sqlite_path = tmp_path / "state.sqlite3"
    image_root = tmp_path / "image-root"
    initialize_db(sqlite_url(sqlite_path))
    write_tree(image_root, IMAGE_ONE_FILES)
    _seed_finalized_image(sqlite_path, image_root)

    config = _config(sqlite_path)
    copy_service = SqlAlchemyCopyService(config, _FakeHotStore())
    recovery_service = SqlAlchemyRecoverySessionService(config, _FakeArchiveStore())

    copy_service.register("20260420T040001Z", "Shelf A1", copy_id="20260420T040001Z-1")
    copy_service.register("20260420T040001Z", "Shelf B1", copy_id="20260420T040001Z-2")
    copy_service.update("20260420T040001Z", "20260420T040001Z-1", state="lost")
    copy_service.update("20260420T040001Z", "20260420T040001Z-2", state="damaged")

    with pytest.raises(NotFound, match="recovery session not found"):
        recovery_service.get_for_image("20260420T040001Z")


def test_recovery_session_processes_ready_and_expired_states(
    tmp_path: Path,
    monkeypatch,
) -> None:
    sqlite_path = tmp_path / "state.sqlite3"
    image_root = tmp_path / "image-root"
    initialize_db(sqlite_url(sqlite_path))
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
    monkeypatch.setattr("riverhog_core.services.recovery_sessions.utcnow", lambda: start)
    assert recovery_service.process_due_sessions() == 1
    requested = recovery_service.get("rs-20260420T040001Z-rebuild-1")
    assert requested.state == RecoverySessionState.RESTORE_REQUESTED
    assert requested.restore_ready_at == "2026-04-20T04:00:10Z"

    monkeypatch.setattr(
        "riverhog_core.services.recovery_sessions.utcnow",
        lambda: start + timedelta(seconds=11),
    )
    assert recovery_service.process_due_sessions() == 1
    ready = recovery_service.get("rs-20260420T040001Z-rebuild-1")
    assert ready.state == RecoverySessionState.READY
    assert ready.restore_expires_at == "2026-04-20T04:00:16Z"

    monkeypatch.setattr(
        "riverhog_core.services.recovery_sessions.utcnow",
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
    initialize_db(sqlite_url(sqlite_path))
    write_tree(image_root, IMAGE_ONE_FILES)
    _seed_finalized_image(sqlite_path, image_root)
    package = _docs_collection_archive_package()
    _seed_collection_archive(sqlite_path, package)
    store = _FakeArchiveStore(collection_packages={"docs": package})
    config = _config(
        sqlite_path,
        glacier_recovery_restore_latency=timedelta(seconds=0),
        glacier_recovery_sweep_interval=timedelta(seconds=0),
        operator_webhook_url="http://example.invalid/webhooks/operator",
    )
    recovery_service = SqlAlchemyRecoverySessionService(
        config,
        store,
        recovery_payload_codec=_RECOVERY_CODEC,
    )
    payloads: list[dict[str, object]] = []

    def _post_webhook(*, config, payload):
        payloads.append(payload)

    start = datetime(2026, 4, 20, 4, 0, tzinfo=UTC)
    monkeypatch.setattr("riverhog_core.services.recovery_sessions.utcnow", lambda: start)
    monkeypatch.setattr("riverhog_core.services.recovery_sessions.post_webhook", _post_webhook)

    session = recovery_service.create_or_resume_for_collection("docs")

    assert session.state == RecoverySessionState.RESTORE_REQUESTED
    assert store.restore_requests == [
        (
            "glacier/archives/opaque-docs/archive.tar.age",
            "glacier/archives/opaque-docs/manifest.yml.age",
            "glacier/archives/opaque-docs/manifest.yml.ots.age",
        )
    ]

    assert recovery_service.process_due_sessions() == 1
    ready = recovery_service.get(session.id)
    assert ready.state == RecoverySessionState.READY

    completed = recovery_service.complete(session.id)

    assert completed.state == RecoverySessionState.COMPLETED
    assert [payload["event"] for payload in payloads] == [
        "glacier_recovery.started",
        "glacier_recovery.ready",
        "glacier_recovery.completed",
    ]
    assert [payload["type"] for payload in payloads] == [
        "collection_restore",
        "collection_restore",
        "collection_restore",
    ]
    assert store.archive_reads == ["glacier/archives/opaque-docs/archive.tar.age"]
    assert store.manifest_reads == ["glacier/archives/opaque-docs/manifest.yml.age"]
    assert store.proof_reads == ["glacier/archives/opaque-docs/manifest.yml.ots.age"]
    assert store.cleanup_requests == [
        (
            "glacier/archives/opaque-docs/archive.tar.age",
            "glacier/archives/opaque-docs/manifest.yml.age",
            "glacier/archives/opaque-docs/manifest.yml.ots.age",
        )
    ]


def test_recovery_completed_notification_retries_after_failure(
    tmp_path: Path,
    monkeypatch,
) -> None:
    sqlite_path = tmp_path / "state.sqlite3"
    image_root = tmp_path / "image-root"
    initialize_db(sqlite_url(sqlite_path))
    write_tree(image_root, IMAGE_ONE_FILES)
    _seed_finalized_image(sqlite_path, image_root)
    package = _docs_collection_archive_package()
    _seed_collection_archive(sqlite_path, package)
    store = _FakeArchiveStore(collection_packages={"docs": package})
    config = _config(
        sqlite_path,
        glacier_recovery_restore_latency=timedelta(seconds=0),
        glacier_recovery_sweep_interval=timedelta(seconds=0),
        operator_webhook_url="http://example.invalid/webhooks/operator",
        operator_webhook_retry_delay=timedelta(seconds=1),
    )
    recovery_service = SqlAlchemyRecoverySessionService(
        config,
        store,
        recovery_payload_codec=_RECOVERY_CODEC,
    )
    events: list[str] = []
    completed_failures = 0

    def _post_webhook(*, config, payload):
        nonlocal completed_failures
        events.append(str(payload["event"]))
        if payload["event"] == "glacier_recovery.completed" and completed_failures == 0:
            completed_failures += 1
            raise RuntimeError("HTTP 503")

    start = datetime(2026, 4, 20, 4, 0, tzinfo=UTC)
    monkeypatch.setattr("riverhog_core.services.recovery_sessions.utcnow", lambda: start)
    monkeypatch.setattr("riverhog_core.services.recovery_sessions.post_webhook", _post_webhook)

    session = recovery_service.create_or_resume_for_collection("docs")
    assert recovery_service.process_due_sessions() == 1
    completed = recovery_service.complete(session.id)

    assert completed.state == RecoverySessionState.COMPLETED
    assert events == [
        "glacier_recovery.started",
        "glacier_recovery.ready",
        "glacier_recovery.completed",
    ]

    monkeypatch.setattr(
        "riverhog_core.services.recovery_sessions.utcnow",
        lambda: start + timedelta(seconds=1),
    )

    assert recovery_service.process_due_sessions() == 1
    assert events == [
        "glacier_recovery.started",
        "glacier_recovery.ready",
        "glacier_recovery.completed",
        "glacier_recovery.completed",
    ]


def test_recovery_started_notification_retries_while_restore_is_pending(
    tmp_path: Path,
    monkeypatch,
) -> None:
    sqlite_path = tmp_path / "state.sqlite3"
    image_root = tmp_path / "image-root"
    initialize_db(sqlite_url(sqlite_path))
    write_tree(image_root, IMAGE_ONE_FILES)
    _seed_finalized_image(sqlite_path, image_root)
    package = _docs_collection_archive_package()
    _seed_collection_archive(sqlite_path, package)
    config = _config(
        sqlite_path,
        glacier_recovery_restore_latency=timedelta(seconds=10),
        operator_webhook_url="http://example.invalid/webhooks/operator",
        operator_webhook_retry_delay=timedelta(seconds=1),
    )
    recovery_service = SqlAlchemyRecoverySessionService(
        config,
        _FakeArchiveStore(collection_packages={"docs": package}),
        recovery_payload_codec=_RECOVERY_CODEC,
    )
    events: list[str] = []
    started_failures = 0

    def _post_webhook(*, config, payload):
        nonlocal started_failures
        events.append(str(payload["event"]))
        if payload["event"] == "glacier_recovery.started" and started_failures == 0:
            started_failures += 1
            raise RuntimeError("HTTP 503")

    start = datetime(2026, 4, 20, 4, 0, tzinfo=UTC)
    monkeypatch.setattr("riverhog_core.services.recovery_sessions.utcnow", lambda: start)
    monkeypatch.setattr("riverhog_core.services.recovery_sessions.post_webhook", _post_webhook)

    session = recovery_service.create_or_resume_for_collection("docs")
    assert session.state == RecoverySessionState.RESTORE_REQUESTED

    monkeypatch.setattr(
        "riverhog_core.services.recovery_sessions.utcnow",
        lambda: start + timedelta(seconds=1),
    )

    assert recovery_service.process_due_sessions() == 1
    assert events[:2] == [
        "glacier_recovery.started",
        "glacier_recovery.started",
    ]


def test_collection_restore_materializes_selected_files_to_hot_storage(
    tmp_path: Path,
    monkeypatch,
) -> None:
    sqlite_path = tmp_path / "state.sqlite3"
    image_root = tmp_path / "image-root"
    initialize_db(sqlite_url(sqlite_path))
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

    monkeypatch.setattr(
        "riverhog_core.services.recovery_sessions.utcnow",
        lambda: datetime(2026, 4, 20, 4, 0, tzinfo=UTC),
    )
    session = recovery_service.create_or_resume_for_collection(
        "docs",
        paths=["tax/2022/invoice-123.pdf"],
    )
    assert recovery_service.process_due_sessions() == 1

    materialized = recovery_service.get(session.id)

    assert materialized.state == RecoverySessionState.COMPLETED
    assert materialized.progress.archive_verification == "completed"
    assert materialized.progress.extraction == "completed"
    assert materialized.progress.materialization == "completed"
    assert hot_store.puts == {
        ("docs", "tax/2022/invoice-123.pdf"): DOCS_FILES["tax/2022/invoice-123.pdf"]
    }
    with session_scope(make_session_factory(sqlite_url(sqlite_path))) as db_session:
        row = db_session.get(
            CollectionFileRecord,
            {"collection_id": "docs", "path": "tax/2022/invoice-123.pdf"},
        )
        assert row is not None
        assert row.hot is True


def test_missing_pinned_hot_file_without_disc_coverage_restores_from_glacier(
    tmp_path: Path,
    monkeypatch,
) -> None:
    sqlite_path = tmp_path / "state.sqlite3"
    initialize_db(sqlite_url(sqlite_path))
    _seed_collection_files(
        sqlite_path,
        collection_id="docs",
        files=DOCS_FILES,
        hot=True,
        archived=True,
    )
    package = _docs_collection_archive_package()
    _seed_collection_archive(sqlite_path, package)
    with session_scope(make_session_factory(sqlite_url(sqlite_path))) as session:
        session.add(
            ActivePinRecord(
                target="docs/tax/2022/invoice-123.pdf",
                fetch_id="fx-1",
                fetch_order=1,
                fetch_state=FetchState.DONE.value,
            )
        )

    store = _FakeArchiveStore(collection_packages={"docs": package})
    hot_store = _FakeHotStore()
    config = _config(
        sqlite_path,
        glacier_recovery_restore_latency=timedelta(seconds=0),
        glacier_recovery_sweep_interval=timedelta(seconds=0),
    )
    recovery_service = SqlAlchemyRecoverySessionService(config, store, hot_store)
    monkeypatch.setattr(
        "riverhog_core.services.recovery_sessions.utcnow",
        lambda: datetime(2026, 4, 20, 4, 0, tzinfo=UTC),
    )

    assert recovery_service.repair_missing_pinned_hot_files(limit=10) == 1

    restored_path = "tax/2022/invoice-123.pdf"
    assert hot_store.puts == {("docs", restored_path): DOCS_FILES[restored_path]}
    assert store.restore_requests == [
        (
            "glacier/archives/opaque-docs/archive.tar.age",
            "glacier/archives/opaque-docs/manifest.yml.age",
            "glacier/archives/opaque-docs/manifest.yml.ots.age",
        )
    ]
    with session_scope(make_session_factory(sqlite_url(sqlite_path))) as session:
        row = session.get(CollectionFileRecord, {"collection_id": "docs", "path": restored_path})
        pin = session.get(ActivePinRecord, "docs/tax/2022/invoice-123.pdf")
        assert row is not None
        assert row.hot is True
        assert pin is not None
        assert pin.fetch_state == FetchState.DONE.value


def test_missing_pinned_hot_file_with_disc_coverage_waits_for_djdan_fetch(
    tmp_path: Path,
) -> None:
    sqlite_path = tmp_path / "state.sqlite3"
    initialize_db(sqlite_url(sqlite_path))
    _seed_collection_files(
        sqlite_path,
        collection_id="docs",
        files=DOCS_FILES,
        hot=True,
        archived=True,
    )
    with session_scope(make_session_factory(sqlite_url(sqlite_path))) as session:
        session.add(
            ActivePinRecord(
                target="docs/tax/2022/invoice-123.pdf",
                fetch_id="fx-1",
                fetch_order=1,
                fetch_state=FetchState.DONE.value,
            )
        )
        session.add(
            FileCopyRecord(
                collection_id="docs",
                path="tax/2022/invoice-123.pdf",
                copy_id="20260530T000000Z-1",
                volume_id="20260530T000000Z",
                location="test shelf",
                disc_path="files/000001.age",
                enc_json="{}",
            )
        )

    store = _FakeArchiveStore()
    hot_store = _FakeHotStore()
    recovery_service = SqlAlchemyRecoverySessionService(
        _config(sqlite_path),
        store,
        hot_store,
    )

    assert recovery_service.repair_missing_pinned_hot_files(limit=10) == 1

    assert store.restore_requests == []
    assert hot_store.puts == {}
    with session_scope(make_session_factory(sqlite_url(sqlite_path))) as session:
        row = session.get(
            CollectionFileRecord,
            {"collection_id": "docs", "path": "tax/2022/invoice-123.pdf"},
        )
        pin = session.get(ActivePinRecord, "docs/tax/2022/invoice-123.pdf")
        assert row is not None
        assert row.hot is False
        assert pin is not None
        assert pin.fetch_state == FetchState.WAITING_MEDIA.value


def test_collection_restore_streams_large_selected_file_to_hot_storage(
    tmp_path: Path,
    monkeypatch,
) -> None:
    sqlite_path = tmp_path / "state.sqlite3"
    initialize_db(sqlite_url(sqlite_path))
    content = (b"0123456789abcdef" * 70_000) + b"tail"
    files = {"large.bin": content}
    _seed_collection_files(
        sqlite_path,
        collection_id="docs",
        files=files,
        hot=False,
        archived=True,
    )
    package = build_collection_archive_package(
        collection_id="docs",
        files=(
            CollectionArchiveFile(
                path="large.bin",
                content=content,
                sha256=hashlib.sha256(content).hexdigest(),
            ),
        ),
        stamper=_PROOF_STAMPER,
    )
    _seed_collection_archive(sqlite_path, package)
    store = _FakeArchiveStore(collection_packages={"docs": package})
    hot_store = _FakeHotStore()
    config = _config(
        sqlite_path,
        glacier_recovery_restore_latency=timedelta(seconds=0),
        glacier_recovery_sweep_interval=timedelta(seconds=0),
    )
    recovery_service = SqlAlchemyRecoverySessionService(config, store, hot_store)

    monkeypatch.setattr(
        "riverhog_core.services.recovery_sessions.utcnow",
        lambda: datetime(2026, 4, 20, 4, 0, tzinfo=UTC),
    )
    recovery_service.create_or_resume_for_collection("docs", paths=["large.bin"])
    assert recovery_service.process_due_sessions() == 1

    key = ("docs", "large.bin")
    assert hot_store.byte_put_paths == []
    assert hot_store.puts[key] == content
    assert hot_store.stream_chunk_lengths[key]
    assert max(hot_store.stream_chunk_lengths[key]) < len(content)


def test_collection_restore_does_not_materialize_selected_file_with_bad_sha256(
    tmp_path: Path,
    monkeypatch,
) -> None:
    sqlite_path = tmp_path / "state.sqlite3"
    image_root = tmp_path / "image-root"
    initialize_db(sqlite_url(sqlite_path))
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
    hot_store = _FakeHotStore()
    config = _config(
        sqlite_path,
        glacier_recovery_restore_latency=timedelta(seconds=0),
        glacier_recovery_sweep_interval=timedelta(seconds=0),
    )
    recovery_service = SqlAlchemyRecoverySessionService(config, store, hot_store)

    monkeypatch.setattr(
        "riverhog_core.services.recovery_sessions.utcnow",
        lambda: datetime(2026, 4, 20, 4, 0, tzinfo=UTC),
    )
    recovery_service.create_or_resume_for_collection(
        "docs",
        paths=["tax/2022/invoice-123.pdf"],
    )

    with pytest.raises(ValueError, match="member sha256 mismatch"):
        recovery_service.process_due_sessions()

    assert hot_store.puts == {}


def test_collection_restore_rejects_empty_proof_before_completion(
    tmp_path: Path,
    monkeypatch,
) -> None:
    sqlite_path = tmp_path / "state.sqlite3"
    image_root = tmp_path / "image-root"
    initialize_db(sqlite_url(sqlite_path))
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
        recovery_payload_codec=_RECOVERY_CODEC,
    )

    monkeypatch.setattr(
        "riverhog_core.services.recovery_sessions.utcnow",
        lambda: datetime(2026, 4, 20, 4, 0, tzinfo=UTC),
    )
    session = recovery_service.create_or_resume_for_collection("docs")
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
    initialize_db(sqlite_url(sqlite_path))
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

    monkeypatch.setattr(
        "riverhog_core.services.recovery_sessions.utcnow",
        lambda: datetime(2026, 4, 20, 4, 0, tzinfo=UTC),
    )
    session = recovery_service.create_or_resume_for_collection("docs")
    assert recovery_service.process_due_sessions() == 1

    with pytest.raises(ValueError, match="member sha256 mismatch"):
        recovery_service.complete(session.id)
    assert store.archive_reads == ["glacier/archives/opaque-docs/archive.tar.age"]
    assert store.cleanup_requests == []


def test_image_rebuild_verifies_manifest_and_proof_before_streaming_archive(
    tmp_path: Path,
    monkeypatch,
) -> None:
    sqlite_path = tmp_path / "state.sqlite3"
    image_root = tmp_path / "image-root"
    initialize_db(sqlite_url(sqlite_path))
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
        recovery_payload_codec=_RECOVERY_CODEC,
    )

    copy_service.register("20260420T040001Z", "Shelf A1", copy_id="20260420T040001Z-1")
    copy_service.register("20260420T040001Z", "Shelf B1", copy_id="20260420T040001Z-2")
    copy_service.update("20260420T040001Z", "20260420T040001Z-1", state="lost")
    copy_service.update("20260420T040001Z", "20260420T040001Z-2", state="damaged")
    assert recovery_service.process_due_sessions() == 1

    def _fake_iso(**kwargs: object):
        yield b"rebuilt-iso"

    monkeypatch.setattr("riverhog_core.services.recovery_sessions._run_iso_from_root", _fake_iso)

    chunks = list(
        recovery_service.iter_restored_iso(
            "rs-20260420T040001Z-rebuild-1",
            "20260420T040001Z",
        )
    )

    assert chunks == [b"rebuilt-iso"]
    assert store.manifest_reads == ["glacier/archives/opaque-docs/manifest.yml.age"]
    assert store.proof_reads == ["glacier/archives/opaque-docs/manifest.yml.ots.age"]
    assert store.archive_reads == ["glacier/archives/opaque-docs/archive.tar.age"]


def test_run_iso_from_root_streams_process_stdout(
    tmp_path: Path,
    monkeypatch,
) -> None:
    image_root = tmp_path / "image-root"
    image_root.mkdir()

    class _Stdout:
        def __init__(self) -> None:
            self._chunks = [b"first", b"second", b""]
            self.closed = False

        def read(self, size: int) -> bytes:
            assert size == 1024 * 1024
            return self._chunks.pop(0)

        def close(self) -> None:
            self.closed = True

    class _Proc:
        def __init__(self, *args: object, **kwargs: object) -> None:
            _ = args, kwargs
            self.stdout = _Stdout()
            self.returncode: int | None = None

        def wait(self) -> int:
            self.returncode = 0
            return 0

        def poll(self) -> int | None:
            return self.returncode

        def kill(self) -> None:
            self.returncode = -9

    monkeypatch.setattr(recovery_sessions_module.subprocess, "Popen", _Proc)

    chunks = list(
        recovery_sessions_module._run_iso_from_root(
            image_root=image_root,
            volume_id="20260420T040001Z",
            filename="20260420T040001Z.iso",
        )
    )

    assert chunks == [b"first", b"second"]


def test_run_iso_from_root_reports_process_failure(
    tmp_path: Path,
    monkeypatch,
) -> None:
    image_root = tmp_path / "image-root"
    image_root.mkdir()

    class _Stdout:
        def read(self, size: int) -> bytes:
            _ = size
            return b""

        def close(self) -> None:
            return

    class _Proc:
        def __init__(self, *args: object, **kwargs: object) -> None:
            _ = args
            self.stdout = _Stdout()
            self.returncode: int | None = None
            kwargs["stderr"].write(b"synthetic xorriso failure")

        def wait(self) -> int:
            self.returncode = 7
            return 7

        def poll(self) -> int | None:
            return self.returncode

        def kill(self) -> None:
            self.returncode = -9

    monkeypatch.setattr(recovery_sessions_module.subprocess, "Popen", _Proc)

    with pytest.raises(RuntimeError, match="synthetic xorriso failure"):
        list(
            recovery_sessions_module._run_iso_from_root(
                image_root=image_root,
                volume_id="20260420T040001Z",
                filename="20260420T040001Z.iso",
            )
        )


def test_recovery_session_retries_initial_ready_notification_before_reminders(
    tmp_path: Path,
    monkeypatch,
) -> None:
    sqlite_path = tmp_path / "state.sqlite3"
    image_root = tmp_path / "image-root"
    initialize_db(sqlite_url(sqlite_path))
    write_tree(image_root, IMAGE_ONE_FILES)
    _seed_finalized_image(sqlite_path, image_root)
    package = _seed_docs_collection_archive(sqlite_path)

    config = _config(
        sqlite_path,
        operator_webhook_url="http://example.invalid/webhooks/operator",
        glacier_recovery_restore_latency=timedelta(seconds=10),
        operator_webhook_retry_delay=timedelta(seconds=1),
        operator_webhook_reminder_interval=timedelta(seconds=5),
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
    ready_failures = 0

    def _post_webhook(*, config, payload):
        nonlocal ready_failures
        attempts.append(str(payload["event"]))
        if payload["event"] == "glacier_recovery.ready" and ready_failures == 0:
            ready_failures += 1
            raise RuntimeError("HTTP 503")

    start = datetime(2026, 4, 20, 4, 0, tzinfo=UTC)
    monkeypatch.setattr("riverhog_core.services.recovery_sessions.utcnow", lambda: start)
    monkeypatch.setattr("riverhog_core.services.recovery_sessions.post_webhook", _post_webhook)
    assert recovery_service.process_due_sessions() == 1

    monkeypatch.setattr(
        "riverhog_core.services.recovery_sessions.utcnow",
        lambda: start + timedelta(seconds=11),
    )
    assert recovery_service.process_due_sessions() == 1

    failed_delivery = recovery_service.get("rs-20260420T040001Z-rebuild-1")
    assert failed_delivery.state == RecoverySessionState.READY
    assert failed_delivery.notification.last_notified_at is None
    assert failed_delivery.notification.reminder_count == 0
    assert attempts == ["glacier_recovery.started", "glacier_recovery.ready"]

    monkeypatch.setattr(
        "riverhog_core.services.recovery_sessions.utcnow",
        lambda: start + timedelta(seconds=12),
    )
    assert recovery_service.process_due_sessions() == 1

    retried_delivery = recovery_service.get("rs-20260420T040001Z-rebuild-1")
    assert retried_delivery.notification.last_notified_at == "2026-04-20T04:00:12Z"
    assert retried_delivery.notification.reminder_count == 0
    assert attempts == [
        "glacier_recovery.started",
        "glacier_recovery.ready",
        "glacier_recovery.ready",
    ]


def test_recovery_session_retries_initial_ready_notification_before_expiring(
    tmp_path: Path,
    monkeypatch,
) -> None:
    sqlite_path = tmp_path / "state.sqlite3"
    image_root = tmp_path / "image-root"
    initialize_db(sqlite_url(sqlite_path))
    write_tree(image_root, IMAGE_ONE_FILES)
    _seed_finalized_image(sqlite_path, image_root)
    package = _seed_docs_collection_archive(sqlite_path)

    config = _config(
        sqlite_path,
        operator_webhook_url="http://example.invalid/webhooks/operator",
        glacier_recovery_restore_latency=timedelta(seconds=10),
        glacier_recovery_ready_ttl=timedelta(seconds=12),
        operator_webhook_retry_delay=timedelta(seconds=1),
        operator_webhook_reminder_interval=timedelta(seconds=5),
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
    ready_failures = 0

    def _post_webhook(*, config, payload):
        nonlocal ready_failures
        attempts.append(str(payload["event"]))
        if payload["event"] == "glacier_recovery.ready" and ready_failures == 0:
            ready_failures += 1
            raise RuntimeError("HTTP 503")

    start = datetime(2026, 4, 20, 4, 0, tzinfo=UTC)
    monkeypatch.setattr("riverhog_core.services.recovery_sessions.utcnow", lambda: start)
    monkeypatch.setattr("riverhog_core.services.recovery_sessions.post_webhook", _post_webhook)
    assert recovery_service.process_due_sessions() == 1

    monkeypatch.setattr(
        "riverhog_core.services.recovery_sessions.utcnow",
        lambda: start + timedelta(seconds=11),
    )
    assert recovery_service.process_due_sessions() == 1

    failed_delivery = recovery_service.get("rs-20260420T040001Z-rebuild-1")
    assert failed_delivery.state == RecoverySessionState.READY
    assert failed_delivery.notification.last_notified_at is None
    assert failed_delivery.notification.next_reminder_at == "2026-04-20T04:00:12Z"
    assert failed_delivery.restore_expires_at == "2026-04-20T04:00:23Z"

    monkeypatch.setattr(
        "riverhog_core.services.recovery_sessions.utcnow",
        lambda: start + timedelta(seconds=22),
    )
    assert recovery_service.process_due_sessions() == 1

    retried_delivery = recovery_service.get("rs-20260420T040001Z-rebuild-1")
    assert retried_delivery.state == RecoverySessionState.READY
    assert retried_delivery.notification.last_notified_at == "2026-04-20T04:00:22Z"
    assert retried_delivery.notification.reminder_count == 0
    assert attempts == [
        "glacier_recovery.started",
        "glacier_recovery.ready",
        "glacier_recovery.ready",
    ]


def test_queued_recovery_session_can_group_multiple_images_before_restore_request(
    tmp_path: Path,
) -> None:
    sqlite_path = tmp_path / "state.sqlite3"
    image_root_one = tmp_path / "image-root-one"
    image_root_two = tmp_path / "image-root-two"
    initialize_db(sqlite_url(sqlite_path))
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

    assert session.state == RecoverySessionState.RESTORE_REQUESTED
    assert [str(image.id) for image in session.images] == [
        "20260420T040001Z",
        "20260420T040003Z",
    ]
    assert [str(collection.id) for collection in session.collections] == ["docs"]
