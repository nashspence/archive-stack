from __future__ import annotations

import hashlib
from pathlib import Path

from arc_core.catalog_models import (
    CollectionFileRecord,
    CollectionRecord,
    FinalizedImageCollectionArtifactRecord,
    FinalizedImageCoveragePartRecord,
    FinalizedImageCoveredPathRecord,
    FinalizedImageRecord,
)
from arc_core.finalized_image_coverage import (
    build_disc_manifest_from_catalog,
    read_finalized_image_coverage_parts,
)
from arc_core.planner.manifest import MANIFEST_FILENAME
from arc_core.sqlite_db import initialize_db, make_session_factory, session_scope
from tests.fixtures.crypto import FixtureRecoveryPayloadCodec
from tests.fixtures.data import (
    ALL_COLLECTION_FILES,
    SPLIT_IMAGE_FIXTURES,
    fixture_decrypt_bytes,
    write_tree,
)

_RECOVERY_CODEC = FixtureRecoveryPayloadCodec()


def _seed_collection_state(sqlite_path: Path, *, image_root: Path) -> None:
    image = SPLIT_IMAGE_FIXTURES[0]
    session_factory = make_session_factory(str(sqlite_path))
    with session_scope(session_factory) as session:
        for collection_id in sorted({collection_id for collection_id, _ in image.covered_paths}):
            session.add(CollectionRecord(id=collection_id))
            for relpath, content in sorted(ALL_COLLECTION_FILES[collection_id].items()):
                session.add(
                    CollectionFileRecord(
                        collection_id=collection_id,
                        path=relpath,
                        bytes=len(content),
                        sha256=hashlib.sha256(content).hexdigest(),
                        hot=True,
                        archived=False,
                    )
                )

        session.add(
            FinalizedImageRecord(
                image_id=image.volume_id,
                candidate_id=image.id,
                filename=image.filename,
                bytes=image.bytes,
                image_root=str(image_root),
                target_bytes=image.bytes,
                required_copy_count=2,
            )
        )
        for collection_id, path in image.covered_paths:
            session.add(
                FinalizedImageCoveredPathRecord(
                    image_id=image.volume_id,
                    collection_id=collection_id,
                    path=path,
                )
            )
        for part in read_finalized_image_coverage_parts(image_root, _RECOVERY_CODEC):
            session.add(
                FinalizedImageCoveragePartRecord(
                    image_id=image.volume_id,
                    collection_id=part.collection_id,
                    path=part.path,
                    part_index=part.part_index,
                    part_count=part.part_count,
                    object_path=None,
                    sidecar_path=None,
                )
            )


def test_initialize_db_backfills_manifest_topology_and_rebuilds_manifest_bytes(
    tmp_path: Path,
    monkeypatch,
) -> None:
    sqlite_path = tmp_path / "state.sqlite3"
    image_root = write_tree(tmp_path / "image-root", SPLIT_IMAGE_FIXTURES[0].files)
    original_manifest = fixture_decrypt_bytes((image_root / MANIFEST_FILENAME).read_bytes())

    initialize_db(str(sqlite_path))
    _seed_collection_state(sqlite_path, image_root=image_root)

    monkeypatch.setattr(
        "arc_core.finalized_image_coverage._default_recovery_payload_codec",
        lambda: _RECOVERY_CODEC,
    )
    initialize_db(str(sqlite_path))
    (image_root / MANIFEST_FILENAME).unlink()

    session_factory = make_session_factory(str(sqlite_path))
    with session_scope(session_factory) as session:
        collection_artifacts = (
            session.query(FinalizedImageCollectionArtifactRecord)
            .filter_by(image_id=SPLIT_IMAGE_FIXTURES[0].volume_id)
            .order_by(FinalizedImageCollectionArtifactRecord.collection_id)
            .all()
        )
        coverage_parts = (
            session.query(FinalizedImageCoveragePartRecord)
            .filter_by(image_id=SPLIT_IMAGE_FIXTURES[0].volume_id)
            .order_by(
                FinalizedImageCoveragePartRecord.collection_id,
                FinalizedImageCoveragePartRecord.path,
                FinalizedImageCoveragePartRecord.part_index,
            )
            .all()
        )
        file_lookup = {
            (record.collection_id, record.path): (record.sha256, record.bytes)
            for record in session.query(CollectionFileRecord).all()
        }

    manifest_rows = [
        (row.collection_id, row.manifest_path, row.proof_path) for row in collection_artifacts
    ]
    assert manifest_rows == [("docs", "collections/000001.yml.age", "collections/000001.ots.age")]
    assert [
        (row.collection_id, row.path, row.part_index, row.object_path, row.sidecar_path)
        for row in coverage_parts
    ] == [
        (
            "docs",
            "tax/2022/invoice-123.pdf",
            0,
            "files/000001.001.age",
            "files/000001.001.yml.age",
        )
    ]

    rebuilt_manifest = build_disc_manifest_from_catalog(
        image_id=SPLIT_IMAGE_FIXTURES[0].volume_id,
        collection_artifacts=collection_artifacts,
        coverage_parts=coverage_parts,
        file_lookup=file_lookup,
    )

    assert rebuilt_manifest == original_manifest
