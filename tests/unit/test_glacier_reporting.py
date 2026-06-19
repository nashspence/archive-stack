from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from riverhog_core.catalog_db import initialize_db, make_session_factory, session_scope
from riverhog_core.catalog_models import (
    CollectionArchiveRecord,
    CollectionFileRecord,
    CollectionRecord,
    CollectionUploadFileRecord,
    CollectionUploadRecord,
    FinalizedImageCollectionArtifactRecord,
    FinalizedImageCoveragePartRecord,
    FinalizedImageCoveredPathRecord,
    FinalizedImageRecord,
)
from riverhog_core.finalized_image_coverage import (
    read_finalized_image_collection_artifacts,
    read_finalized_image_coverage_parts,
)
from riverhog_core.runtime_config import RuntimeConfig
from riverhog_core.services.glacier_reporting import SqlAlchemyGlacierReportingService
from tests.fixtures.crypto import FixtureRecoveryPayloadCodec
from tests.fixtures.data import (
    DOCS_COLLECTION_ID,
    DOCS_FILES,
    IMAGE_FIXTURES,
    SPLIT_IMAGE_FIXTURES,
    write_tree,
)
from tests.unit.db_helpers import sqlite_url

_RECOVERY_CODEC = FixtureRecoveryPayloadCodec()


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
        database_url=sqlite_url(tmp_path / "state.sqlite3"),
    )
    return replace(config, **overrides)


def _seed_docs_collection(config: RuntimeConfig) -> None:
    initialize_db(config.database_url)
    session_factory = make_session_factory(config.database_url)
    with session_scope(session_factory) as session:
        session.add(CollectionRecord(id=DOCS_COLLECTION_ID))
        for path, content in sorted(DOCS_FILES.items()):
            session.add(
                CollectionFileRecord(
                    collection_id=DOCS_COLLECTION_ID,
                    path=path,
                    bytes=len(content),
                    sha256="a" * 64,
                    hot=True,
                    archived=False,
                )
            )


def _seed_uploaded_image(
    config: RuntimeConfig,
    *,
    image_id: str,
    candidate_id: str,
    filename: str,
    image_root: Path,
    bytes_total: int,
    covered_paths: tuple[tuple[str, str], ...],
) -> None:
    session_factory = make_session_factory(config.database_url)
    with session_scope(session_factory) as session:
        session.add(
            FinalizedImageRecord(
                image_id=image_id,
                candidate_id=candidate_id,
                filename=filename,
                bytes=bytes_total,
                image_root=str(image_root),
                target_bytes=bytes_total,
                required_copy_count=2,
            )
        )
        for collection_id, path in covered_paths:
            session.add(
                FinalizedImageCoveredPathRecord(
                    image_id=image_id,
                    collection_id=collection_id,
                    path=path,
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


def test_get_report_does_not_count_finalized_images_in_glacier_totals(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    _seed_docs_collection(config)
    image_root = write_tree(tmp_path / "image-1", IMAGE_FIXTURES[0].files)
    _seed_uploaded_image(
        config,
        image_id="20260420T040001Z",
        candidate_id=IMAGE_FIXTURES[0].id,
        filename=IMAGE_FIXTURES[0].filename,
        image_root=image_root,
        bytes_total=IMAGE_FIXTURES[0].bytes,
        covered_paths=IMAGE_FIXTURES[0].covered_paths,
    )

    report = SqlAlchemyGlacierReportingService(config).get_report()

    assert report.scope == "all"
    assert report.totals.measured_storage_bytes == 0
    assert [image.id for image in report.images] == ["20260420T040001Z"]
    assert report.history


def test_get_report_does_not_derive_collection_usage_from_finalized_image_coverage(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    _seed_docs_collection(config)
    image_root = write_tree(tmp_path / "image-split", SPLIT_IMAGE_FIXTURES[0].files)
    _seed_uploaded_image(
        config,
        image_id="20260420T040003Z",
        candidate_id=SPLIT_IMAGE_FIXTURES[0].id,
        filename=SPLIT_IMAGE_FIXTURES[0].filename,
        image_root=image_root,
        bytes_total=SPLIT_IMAGE_FIXTURES[0].bytes,
        covered_paths=SPLIT_IMAGE_FIXTURES[0].covered_paths,
    )

    report = SqlAlchemyGlacierReportingService(config).get_report(collection=DOCS_COLLECTION_ID)

    assert report.scope == "collection"
    assert [collection.id for collection in report.collections] == [DOCS_COLLECTION_ID]
    assert report.collections[0].measured_storage_bytes == 0
    assert report.collections[0].images
    assert report.collections[0].images[0].represented_bytes > 0


def test_get_report_counts_manifest_and_proof_in_measured_storage(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    _seed_docs_collection(config)
    session_factory = make_session_factory(config.database_url)
    with session_scope(session_factory) as session:
        session.add(
            CollectionArchiveRecord(
                collection_id=DOCS_COLLECTION_ID,
                state="uploaded",
                object_path=f"glacier/archives/opaque-{DOCS_COLLECTION_ID}/archive.tar.age",
                stored_bytes=1000,
                sha256="a" * 64,
                backend="aws",
                storage_class="DEEP_ARCHIVE",
                archive_format="tar",
                compression="none",
                manifest_object_path=(
                    f"glacier/archives/opaque-{DOCS_COLLECTION_ID}/manifest.yml.age"
                ),
                manifest_sha256="b" * 64,
                manifest_stored_bytes=200,
                ots_object_path=(
                    f"glacier/archives/opaque-{DOCS_COLLECTION_ID}/manifest.yml.ots.age"
                ),
                ots_sha256="c" * 64,
                ots_stored_bytes=20,
            )
        )

    report = SqlAlchemyGlacierReportingService(config).get_report(collection=DOCS_COLLECTION_ID)

    collection = report.collections[0]
    assert collection.measured_storage_bytes == 1220
    assert collection.collection_manifest is not None
    assert collection.collection_manifest.object_path.endswith("/manifest.yml.age")
    assert collection.collection_manifest.ots_object_path.endswith("/manifest.yml.ots.age")


def test_get_report_ignores_terminal_upload_sessions(tmp_path: Path) -> None:
    config = _config(tmp_path)
    initialize_db(config.database_url)
    session_factory = make_session_factory(config.database_url)
    with session_scope(session_factory) as session:
        for state in ("canceled", "expired", "uploading"):
            collection_id = f"2026/20260613T000000Z__{state}-upload"
            session.add(
                CollectionUploadRecord(
                    collection_id=collection_id,
                    ingest_source="/archive",
                    state=state,
                    opened_at="2026-06-13T00:00:00Z",
                    last_activity_at="2026-06-13T00:00:00Z",
                    closed_at=(
                        "2026-06-13T00:01:00Z"
                        if state in {"canceled", "expired"}
                        else None
                    ),
                )
            )
            session.add(
                CollectionUploadFileRecord(
                    collection_id=collection_id,
                    path="file.txt",
                    file_order=0,
                    bytes=123,
                    sha256="a" * 64,
                    uploaded_bytes=123,
                )
            )

    report = SqlAlchemyGlacierReportingService(config).get_report()

    assert [str(collection.id) for collection in report.collections] == [
        "2026/20260613T000000Z__uploading-upload"
    ]
    assert report.collections[0].bytes == 123


def test_initialize_db_backfills_coverage_parts_for_existing_finalized_images(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    _seed_docs_collection(config)
    image_root = write_tree(tmp_path / "image-split-backfill", SPLIT_IMAGE_FIXTURES[0].files)

    session_factory = make_session_factory(config.database_url)
    with session_scope(session_factory) as session:
        session.add(
            FinalizedImageRecord(
                image_id="20260420T040003Z",
                candidate_id=SPLIT_IMAGE_FIXTURES[0].id,
                filename=SPLIT_IMAGE_FIXTURES[0].filename,
                bytes=SPLIT_IMAGE_FIXTURES[0].bytes,
                image_root=str(image_root),
                target_bytes=SPLIT_IMAGE_FIXTURES[0].bytes,
                required_copy_count=2,
            )
        )
        for collection_id, path in SPLIT_IMAGE_FIXTURES[0].covered_paths:
            session.add(
                FinalizedImageCoveredPathRecord(
                    image_id="20260420T040003Z",
                    collection_id=collection_id,
                    path=path,
                )
            )

    initialize_db(config.database_url)
    (image_root / "DISC.yml.age").unlink()

    report = SqlAlchemyGlacierReportingService(config).get_report(collection=DOCS_COLLECTION_ID)

    assert report.collections[0].measured_storage_bytes == 0
    assert report.collections[0].images[0].represented_bytes > 0
