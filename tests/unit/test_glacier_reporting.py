from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from arc_core.catalog_models import (
    CollectionFileRecord,
    CollectionRecord,
    FinalizedImageCoveredPathRecord,
    FinalizedImageRecord,
)
from arc_core.runtime_config import RuntimeConfig
from arc_core.services.glacier_reporting import SqlAlchemyGlacierReportingService
from arc_core.sqlite_db import initialize_db, make_session_factory, session_scope
from tests.fixtures.data import (
    DOCS_COLLECTION_ID,
    DOCS_FILES,
    IMAGE_FIXTURES,
    SPLIT_FILE_PARTS,
    SPLIT_IMAGE_FIXTURES,
    write_tree,
)


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
    stored_bytes: int,
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
                glacier_state="uploaded",
                glacier_object_path=f"glacier/finalized-images/{image_id}/{image_id}.iso",
                glacier_stored_bytes=stored_bytes,
                glacier_backend="s3",
                glacier_storage_class="DEEP_ARCHIVE",
                glacier_last_uploaded_at="2026-04-28T00:00:00Z",
                glacier_last_verified_at="2026-04-28T00:00:00Z",
                glacier_failure=None,
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


def test_get_report_returns_totals_and_image_costs(tmp_path: Path) -> None:
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
        stored_bytes=8200,
        covered_paths=IMAGE_FIXTURES[0].covered_paths,
    )

    report = SqlAlchemyGlacierReportingService(config).get_report()

    assert report.scope == "all"
    assert report.totals.measured_storage_bytes == 8200
    assert report.totals.estimated_billable_bytes == 8200 + 32768 + 8192
    assert report.totals.estimated_monthly_cost_usd > 0
    assert [image.id for image in report.images] == ["20260420T040001Z"]
    assert report.history
    assert report.billing is not None
    assert report.billing.source == "unavailable"


def test_get_report_uses_manifest_part_lengths_for_collection_filter(tmp_path: Path) -> None:
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
        stored_bytes=5100,
        covered_paths=SPLIT_IMAGE_FIXTURES[0].covered_paths,
    )

    report = SqlAlchemyGlacierReportingService(config).get_report(collection=DOCS_COLLECTION_ID)

    assert report.scope == "collection"
    assert [collection.id for collection in report.collections] == [DOCS_COLLECTION_ID]
    assert report.collections[0].represented_bytes == len(SPLIT_FILE_PARTS[0])
    assert report.collections[0].attribution_state == "derived"
    assert report.collections[0].derived_stored_bytes > 0
