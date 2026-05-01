from __future__ import annotations

from pathlib import Path

from arc_core.catalog_models import (
    CollectionArchiveRecord,
    CollectionFileRecord,
    CollectionRecord,
    FileCopyRecord,
    FinalizedImageCollectionArtifactRecord,
    FinalizedImageCoveragePartRecord,
    FinalizedImageCoveredPathRecord,
    FinalizedImageRecord,
    GlacierRecoverySessionImageRecord,
    GlacierRecoverySessionRecord,
)
from arc_core.domain.enums import CopyState
from arc_core.finalized_image_coverage import (
    read_finalized_image_collection_artifacts,
    read_finalized_image_coverage_parts,
)
from arc_core.runtime_config import RuntimeConfig
from arc_core.services.copies import SqlAlchemyCopyService
from arc_core.sqlite_db import initialize_db, make_session_factory, session_scope
from tests.fixtures.crypto import FixtureRecoveryPayloadCodec
from tests.fixtures.data import DOCS_FILES, IMAGE_ONE_FILES, write_tree

_RECOVERY_CODEC = FixtureRecoveryPayloadCodec()


class _FakeHotStore:
    def get_collection_file(self, collection_id: str, path: str) -> bytes:
        assert collection_id == "docs"
        return DOCS_FILES[path]


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
        sqlite_path=sqlite_path,
    )


def _seed_finalized_image(sqlite_path: Path, image_root: Path) -> None:
    session_factory = make_session_factory(str(sqlite_path))
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
                    archived=False,
                )
            )
        session.add(
            CollectionArchiveRecord(
                collection_id="docs",
                state="uploaded",
                object_path="glacier/collections/docs/archive.tar",
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
                required_copy_count=2,
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


def test_marking_one_confirmed_copy_lost_creates_a_fresh_replacement_slot(tmp_path: Path) -> None:
    sqlite_path = tmp_path / "state.sqlite3"
    image_root = tmp_path / "image-root"
    initialize_db(str(sqlite_path))
    write_tree(image_root, IMAGE_ONE_FILES)
    _seed_finalized_image(sqlite_path, image_root)

    service = SqlAlchemyCopyService(_config(sqlite_path), _FakeHotStore())

    initial = service.list_for_image("20260420T040001Z")
    assert [str(copy.id) for copy in initial] == ["20260420T040001Z-1", "20260420T040001Z-2"]

    service.register("20260420T040001Z", "Shelf A1", copy_id="20260420T040001Z-1")
    service.register("20260420T040001Z", "Shelf B1", copy_id="20260420T040001Z-2")

    updated = service.update("20260420T040001Z", "20260420T040001Z-1", state="lost")

    assert updated.state == CopyState.LOST
    assert [entry.event for entry in updated.history] == ["created", "registered", "state_updated"]

    copies = service.list_for_image("20260420T040001Z")
    assert [str(copy.id) for copy in copies] == [
        "20260420T040001Z-1",
        "20260420T040001Z-2",
        "20260420T040001Z-3",
    ]
    assert [copy.state for copy in copies] == [
        CopyState.LOST,
        CopyState.REGISTERED,
        CopyState.NEEDED,
    ]


def test_recovery_session_seeds_and_tops_up_replacement_slots_for_unprotected_image(
    tmp_path: Path,
) -> None:
    sqlite_path = tmp_path / "state.sqlite3"
    image_root = tmp_path / "image-root"
    initialize_db(str(sqlite_path))
    write_tree(image_root, IMAGE_ONE_FILES)
    _seed_finalized_image(sqlite_path, image_root)

    service = SqlAlchemyCopyService(_config(sqlite_path), _FakeHotStore())
    service.register("20260420T040001Z", "Shelf A1", copy_id="20260420T040001Z-1")
    service.register("20260420T040001Z", "Shelf B1", copy_id="20260420T040001Z-2")
    service.update("20260420T040001Z", "20260420T040001Z-1", state="lost")
    service.update("20260420T040001Z", "20260420T040001Z-2", state="damaged")

    session_factory = make_session_factory(str(sqlite_path))
    with session_scope(session_factory) as session:
        record = session.get(GlacierRecoverySessionRecord, "rs-20260420T040001Z-rebuild-1")
        assert record is not None
        record.state = "ready"
        record.restore_requested_at = "2026-04-20T04:00:01Z"
        record.restore_ready_at = "2026-04-20T04:00:03Z"
        record.restore_expires_at = "2026-04-20T04:10:03Z"
        assert (
            session.get(
                GlacierRecoverySessionImageRecord,
                {
                    "session_id": "rs-20260420T040001Z-rebuild-1",
                    "image_id": "20260420T040001Z",
                },
            )
            is not None
        )

    copies = service.list_for_image("20260420T040001Z")
    assert [str(copy.id) for copy in copies] == [
        "20260420T040001Z-1",
        "20260420T040001Z-2",
        "20260420T040001Z-3",
    ]
    assert [copy.state for copy in copies] == [
        CopyState.LOST,
        CopyState.DAMAGED,
        CopyState.NEEDED,
    ]

    service.register("20260420T040001Z", "Shelf C1", copy_id="20260420T040001Z-3")
    service.update(
        "20260420T040001Z",
        "20260420T040001Z-3",
        state="verified",
        verification_state="verified",
    )

    topped_up = service.list_for_image("20260420T040001Z")
    assert [str(copy.id) for copy in topped_up] == [
        "20260420T040001Z-1",
        "20260420T040001Z-2",
        "20260420T040001Z-3",
        "20260420T040001Z-4",
    ]
    assert topped_up[-1].state == CopyState.NEEDED


def test_register_uses_db_artifact_mapping_after_disc_manifest_is_removed(tmp_path: Path) -> None:
    sqlite_path = tmp_path / "state.sqlite3"
    image_root = tmp_path / "image-root"
    initialize_db(str(sqlite_path))
    write_tree(image_root, IMAGE_ONE_FILES)
    _seed_finalized_image(sqlite_path, image_root)
    (image_root / "DISC.yml.age").unlink()

    service = SqlAlchemyCopyService(_config(sqlite_path), _FakeHotStore())
    service.register("20260420T040001Z", "Shelf A1", copy_id="20260420T040001Z-1")

    session_factory = make_session_factory(str(sqlite_path))
    with session_scope(session_factory) as session:
        rows = session.query(FileCopyRecord).order_by(FileCopyRecord.disc_path).all()

    assert [(row.path, row.disc_path) for row in rows] == [
        ("tax/2022/invoice-123.pdf", "files/000001.age"),
        ("tax/2022/receipt-456.pdf", "files/000002.age"),
    ]
