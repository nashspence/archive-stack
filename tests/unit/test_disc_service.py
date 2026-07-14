from __future__ import annotations

from collections.abc import Iterable
from dataclasses import replace
from pathlib import Path

import pytest

from riverhog_core.catalog_db import initialize_db, make_session_factory, session_scope
from riverhog_core.catalog_models import (
    ArchiveRestoreImageRecord,
    ArchiveRestoreRecord,
    CollectionArchiveRecord,
    CollectionFileRecord,
    CollectionRecord,
    FileDiscRecord,
    FinalizedImageCollectionArtifactRecord,
    FinalizedImageCoveragePartRecord,
    FinalizedImageCoveredPathRecord,
    FinalizedImageRecord,
)
from riverhog_core.domain.enums import DiscState, VerificationState
from riverhog_core.domain.errors import InvalidState
from riverhog_core.finalized_image_coverage import (
    read_finalized_image_collection_artifacts,
    read_finalized_image_coverage_parts,
)
from riverhog_core.runtime_config import RuntimeConfig
from riverhog_core.services.discs import SqlAlchemyDiscService
from tests.fixtures.crypto import FixtureRecoveryPayloadCodec
from tests.fixtures.data import DOCS_FILES, IMAGE_ONE_FILES, write_tree
from tests.unit.db_helpers import sqlite_url

_RECOVERY_CODEC = FixtureRecoveryPayloadCodec()


class _FakeHotStore:
    def put_collection_file(self, collection_id: str, path: str, content: bytes) -> None:
        raise NotImplementedError

    def put_collection_file_stream(
        self,
        collection_id: str,
        path: str,
        chunks: Iterable[bytes],
        *,
        content_length: int,
    ) -> None:
        raise NotImplementedError

    def get_collection_file(self, collection_id: str, path: str) -> bytes:
        assert collection_id == "docs"
        return DOCS_FILES[path]

    def has_collection_file(self, collection_id: str, path: str) -> bool:
        raise NotImplementedError

    def delete_collection_file(self, collection_id: str, path: str) -> None:
        raise NotImplementedError

    def list_collection_files(self, collection_id: str) -> list[tuple[str, int]]:
        raise NotImplementedError


def _config(sqlite_path: Path) -> RuntimeConfig:
    return RuntimeConfig(
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
    )


def _seed_finalized_image(sqlite_path: Path, image_root: Path) -> None:
    session_factory = make_session_factory(sqlite_url(sqlite_path))
    with session_scope(session_factory) as session:
        session.add(CollectionRecord(id="docs"))
        for relative_path, content in DOCS_FILES.items():
            session.add(
                CollectionFileRecord(
                    collection_id="docs",
                    path=relative_path,
                    bytes=len(content),
                    sha256="a" * 64,
                    hot=True,
                )
            )
        session.add(
            CollectionArchiveRecord(
                collection_id="docs",
                state="uploaded",
                object_path="archive/archives/opaque-docs/archive.tar.age",
                stored_bytes=123,
                backend="s3",
                storage_class="DEEP_ARCHIVE",
            )
        )

        session.add(
            FinalizedImageRecord(
                image_id="20260420T040001Z",
                candidate_id="img_2026-04-20_01",
                filename="20260420T040001Z.iso",
                bytes=sum(len(content) for content in DOCS_FILES.values()),
                image_root=str(image_root),
                target_bytes=10_000,
                required_disc_count=2,
            )
        )
        for relative_path in (
            "tax/2022/invoice-123.pdf",
            "tax/2022/receipt-456.pdf",
        ):
            session.add(
                FinalizedImageCoveredPathRecord(
                    image_id="20260420T040001Z",
                    collection_id="docs",
                    path=relative_path,
                )
            )
        for artifact in read_finalized_image_collection_artifacts(image_root, _RECOVERY_CODEC):
            session.add(
                FinalizedImageCollectionArtifactRecord(
                    image_id="20260420T040001Z",
                    collection_id=artifact.collection_id,
                    manifest_path=artifact.manifest_path,
                    proof_path=artifact.proof_path,
                )
            )
        for part in read_finalized_image_coverage_parts(image_root, _RECOVERY_CODEC):
            session.add(
                FinalizedImageCoveragePartRecord(
                    image_id="20260420T040001Z",
                    collection_id=part.collection_id,
                    path=part.path,
                    part_index=part.part_index,
                    part_count=part.part_count,
                    object_path=part.object_path,
                    sidecar_path=part.sidecar_path,
                )
            )


def test_marking_one_confirmed_disc_lost_creates_a_fresh_replacement_slot(tmp_path: Path) -> None:
    sqlite_path = tmp_path / "state.sqlite3"
    image_root = tmp_path / "image-root"
    initialize_db(sqlite_url(sqlite_path))
    write_tree(image_root, IMAGE_ONE_FILES)
    _seed_finalized_image(sqlite_path, image_root)

    service = SqlAlchemyDiscService(_config(sqlite_path), _FakeHotStore())

    initial = service.list_for_image("20260420T040001Z")
    assert [str(disc.disc_id) for disc in initial] == ["20260420T040001Z-1", "20260420T040001Z-2"]

    service.register("20260420T040001Z", "Shelf A1", disc_id="20260420T040001Z-1")
    service.register("20260420T040001Z", "Shelf B1", disc_id="20260420T040001Z-2")

    updated = service.update("20260420T040001Z", "20260420T040001Z-1", state="lost")

    assert updated.state == DiscState.LOST
    assert [entry.event for entry in updated.history] == ["created", "registered", "state_updated"]

    discs = service.list_for_image("20260420T040001Z")
    assert [str(disc.disc_id) for disc in discs] == [
        "20260420T040001Z-1",
        "20260420T040001Z-2",
        "20260420T040001Z-3",
    ]
    assert [disc.state for disc in discs] == [
        DiscState.LOST,
        DiscState.REGISTERED,
        DiscState.NEEDED,
    ]


def test_archive_restore_seeds_and_tops_up_replacement_slots_for_none_image(
    tmp_path: Path,
) -> None:
    sqlite_path = tmp_path / "state.sqlite3"
    image_root = tmp_path / "image-root"
    initialize_db(sqlite_url(sqlite_path))
    write_tree(image_root, IMAGE_ONE_FILES)
    _seed_finalized_image(sqlite_path, image_root)

    service = SqlAlchemyDiscService(_config(sqlite_path), _FakeHotStore())
    service.register("20260420T040001Z", "Shelf A1", disc_id="20260420T040001Z-1")
    service.register("20260420T040001Z", "Shelf B1", disc_id="20260420T040001Z-2")
    service.update("20260420T040001Z", "20260420T040001Z-1", state="lost")
    service.update("20260420T040001Z", "20260420T040001Z-2", state="damaged")

    session_factory = make_session_factory(sqlite_url(sqlite_path))
    with session_scope(session_factory) as session:
        record = session.get(ArchiveRestoreRecord, "ar-20260420T040001Z-rebuild-1")
        assert record is not None
        record.state = "ready"
        record.requested_at = "2026-04-20T04:00:01Z"
        record.ready_at = "2026-04-20T04:00:03Z"
        record.expires_at = "2026-04-20T04:10:03Z"
        assert (
            session.get(
                ArchiveRestoreImageRecord,
                {
                    "restore_id": "ar-20260420T040001Z-rebuild-1",
                    "image_id": "20260420T040001Z",
                },
            )
            is not None
        )

    discs = service.list_for_image("20260420T040001Z")
    assert [str(disc.disc_id) for disc in discs] == [
        "20260420T040001Z-1",
        "20260420T040001Z-2",
        "20260420T040001Z-3",
    ]
    assert [disc.state for disc in discs] == [
        DiscState.LOST,
        DiscState.DAMAGED,
        DiscState.NEEDED,
    ]

    service.register("20260420T040001Z", "Shelf C1", disc_id="20260420T040001Z-3")
    service.update(
        "20260420T040001Z",
        "20260420T040001Z-3",
        state="verified",
        verification_state="verified",
    )

    topped_up = service.list_for_image("20260420T040001Z")
    assert [str(disc.disc_id) for disc in topped_up] == [
        "20260420T040001Z-1",
        "20260420T040001Z-2",
        "20260420T040001Z-3",
        "20260420T040001Z-4",
    ]
    assert topped_up[-1].state == DiscState.NEEDED


def test_register_uses_db_artifact_mapping_after_disc_manifest_is_removed(tmp_path: Path) -> None:
    sqlite_path = tmp_path / "state.sqlite3"
    image_root = tmp_path / "image-root"
    initialize_db(sqlite_url(sqlite_path))
    write_tree(image_root, IMAGE_ONE_FILES)
    _seed_finalized_image(sqlite_path, image_root)
    (image_root / "DISC.yml.age").unlink()

    service = SqlAlchemyDiscService(_config(sqlite_path), _FakeHotStore())
    service.register("20260420T040001Z", "Shelf A1", disc_id="20260420T040001Z-1")

    session_factory = make_session_factory(sqlite_url(sqlite_path))
    with session_scope(session_factory) as session:
        rows = session.query(FileDiscRecord).order_by(FileDiscRecord.disc_path).all()

    assert [(row.path, row.disc_path) for row in rows] == [
        ("tax/2022/invoice-123.pdf", "files/000001.age"),
        ("tax/2022/receipt-456.pdf", "files/000002.age"),
    ]
    assert [(row.recovery_bytes, row.recovery_sha256) for row in rows] == [
        (len((image_root / "files/000001.age").read_bytes()), None),
        (len((image_root / "files/000002.age").read_bytes()), None),
    ]


def test_verified_update_after_registration_does_not_resync_disc_rows(tmp_path: Path) -> None:
    sqlite_path = tmp_path / "state.sqlite3"
    image_root = tmp_path / "image-root"
    initialize_db(sqlite_url(sqlite_path))
    write_tree(image_root, IMAGE_ONE_FILES)
    _seed_finalized_image(sqlite_path, image_root)

    service = SqlAlchemyDiscService(_config(sqlite_path), _FakeHotStore())
    service.register("20260420T040001Z", "Shelf A1", disc_id="20260420T040001Z-1")
    (image_root / "files/000001.age").unlink()

    updated = service.update(
        "20260420T040001Z",
        "20260420T040001Z-1",
        state="verified",
        verification_state="verified",
    )

    assert updated.state == DiscState.VERIFIED
    assert updated.verification_state == VerificationState.VERIFIED
    session_factory = make_session_factory(sqlite_url(sqlite_path))
    with session_scope(session_factory) as session:
        rows = session.query(FileDiscRecord).order_by(FileDiscRecord.disc_path).all()

    assert [(row.path, row.disc_path) for row in rows] == [
        ("tax/2022/invoice-123.pdf", "files/000001.age"),
        ("tax/2022/receipt-456.pdf", "files/000002.age"),
    ]


def test_register_synchronously_writes_recovery_index(tmp_path: Path) -> None:
    sqlite_path = tmp_path / "state.sqlite3"
    image_root = tmp_path / "image-root"
    initialize_db(sqlite_url(sqlite_path))
    write_tree(image_root, IMAGE_ONE_FILES)
    _seed_finalized_image(sqlite_path, image_root)

    service = SqlAlchemyDiscService(_config(sqlite_path), _FakeHotStore())
    summary = service.register("20260420T040001Z", "Shelf A1", disc_id="20260420T040001Z-1")

    assert summary.state == DiscState.REGISTERED
    session_factory = make_session_factory(sqlite_url(sqlite_path))
    with session_scope(session_factory) as session:
        rows = session.query(FileDiscRecord).order_by(FileDiscRecord.disc_path).all()

    assert [(row.path, row.disc_path, row.location) for row in rows] == [
        ("tax/2022/invoice-123.pdf", "files/000001.age", "Shelf A1"),
        ("tax/2022/receipt-456.pdf", "files/000002.age", "Shelf A1"),
    ]


def test_recovery_index_sync_rolls_back_and_can_be_retried(tmp_path: Path) -> None:
    sqlite_path = tmp_path / "state.sqlite3"
    image_root = tmp_path / "image-root"
    initialize_db(sqlite_url(sqlite_path))
    write_tree(image_root, IMAGE_ONE_FILES)
    _seed_finalized_image(sqlite_path, image_root)

    service = SqlAlchemyDiscService(_config(sqlite_path), _FakeHotStore())
    service.register("20260420T040001Z", "Shelf A1", disc_id="20260420T040001Z-1")

    missing_payload = image_root / "files/000002.age"
    original_payload = missing_payload.read_bytes()
    missing_payload.unlink()

    with pytest.raises(InvalidState):
        service.update("20260420T040001Z", "20260420T040001Z-1", location="Shelf B1")

    session_factory = make_session_factory(sqlite_url(sqlite_path))
    with session_scope(session_factory) as session:
        rows = session.query(FileDiscRecord).order_by(FileDiscRecord.disc_path).all()
    assert [(row.disc_path, row.location) for row in rows] == [
        ("files/000001.age", "Shelf A1"),
        ("files/000002.age", "Shelf A1"),
    ]

    missing_payload.write_bytes(original_payload)
    service.update("20260420T040001Z", "20260420T040001Z-1", location="Shelf B1")
    with session_scope(session_factory) as session:
        rows = session.query(FileDiscRecord).order_by(FileDiscRecord.disc_path).all()
    assert [(row.disc_path, row.location) for row in rows] == [
        ("files/000001.age", "Shelf B1"),
        ("files/000002.age", "Shelf B1"),
    ]


def test_notify_label_needed_sends_best_effort_operator_webhook(
    tmp_path: Path,
    monkeypatch,
) -> None:
    sqlite_path = tmp_path / "state.sqlite3"
    image_root = tmp_path / "image-root"
    initialize_db(sqlite_url(sqlite_path))
    write_tree(image_root, IMAGE_ONE_FILES)
    _seed_finalized_image(sqlite_path, image_root)
    config = replace(
        _config(sqlite_path),
        operator_webhook_url="https://example.test/hook",
        public_base_url="https://api.test",
    )
    sent: list[dict[str, object]] = []

    def fake_post_webhook(*, config, payload):
        sent.append(payload)

    monkeypatch.setattr("riverhog_core.services.discs.post_webhook", fake_post_webhook)

    service = SqlAlchemyDiscService(config, _FakeHotStore())
    disc = service.notify_label_needed("20260420T040001Z", "20260420T040001Z-1")

    assert disc.disc_id == "20260420T040001Z-1"
    assert sent == [
        {
            "event": "images.disc_label_needed",
            "type": "disc_lifecycle",
            "image_id": "20260420T040001Z",
            "disc_id": "20260420T040001Z-1",
            "label_text": "20260420T040001Z-1",
            "delivered_at": sent[0]["delivered_at"],
            "operator_urgency": "time_sensitive",
            "operator_action": "label the physical disc exactly as label_text",
            "image_url": "https://api.test/v1/images/20260420T040001Z",
            "notification": {
                "title": "👨🏻‍🎤 20260420T040001Z-1",
                "body": (
                    "That burn verified clean! Label the disc exactly, then tell me where it lives."
                ),
            },
        }
    ]
