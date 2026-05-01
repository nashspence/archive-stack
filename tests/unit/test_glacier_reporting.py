from __future__ import annotations

from dataclasses import replace
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
    read_finalized_image_collection_artifacts,
    read_finalized_image_coverage_parts,
)
from arc_core.runtime_config import RuntimeConfig
from arc_core.services.glacier_reporting import SqlAlchemyGlacierReportingService
from arc_core.sqlite_db import initialize_db, make_session_factory, session_scope
from tests.fixtures.crypto import FixtureRecoveryPayloadCodec
from tests.fixtures.data import (
    DOCS_COLLECTION_ID,
    DOCS_FILES,
    IMAGE_FIXTURES,
    SPLIT_IMAGE_FIXTURES,
    write_tree,
)

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
        sqlite_path=tmp_path / "state.sqlite3",
    )
    return replace(config, **overrides)


def _seed_docs_collection(config: RuntimeConfig) -> None:
    initialize_db(str(config.sqlite_path))
    session_factory = make_session_factory(str(config.sqlite_path))
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
    session_factory = make_session_factory(str(config.sqlite_path))
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
    assert report.totals.estimated_billable_bytes == 0
    assert report.totals.estimated_monthly_cost_usd == 0
    assert [image.id for image in report.images] == ["20260420T040001Z"]
    assert report.history
    assert report.billing is not None
    assert report.billing.actuals is not None
    assert report.billing.actuals.source == "unavailable"


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
    assert report.collections[0].estimated_billable_bytes == 0
    assert report.collections[0].images
    assert report.collections[0].images[0].represented_bytes > 0


def test_initialize_db_backfills_coverage_parts_for_existing_finalized_images(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    _seed_docs_collection(config)
    image_root = write_tree(tmp_path / "image-split-backfill", SPLIT_IMAGE_FIXTURES[0].files)

    session_factory = make_session_factory(str(config.sqlite_path))
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

    initialize_db(str(config.sqlite_path))
    (image_root / "DISC.yml.age").unlink()

    report = SqlAlchemyGlacierReportingService(config).get_report(collection=DOCS_COLLECTION_ID)

    assert report.collections[0].measured_storage_bytes == 0
    assert report.collections[0].images[0].represented_bytes > 0
