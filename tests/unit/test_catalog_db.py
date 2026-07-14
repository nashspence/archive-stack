from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import inspect

from riverhog_core.catalog_db import create_catalog_engine, initialize_db
from tests.unit.db_helpers import sqlite_url


def test_initialize_db_creates_current_baseline_schema(tmp_path: Path) -> None:
    database_url = sqlite_url(tmp_path / "catalog.sqlite3")

    initialize_db(database_url)
    initialize_db(database_url)

    engine = create_catalog_engine(database_url)
    inspector = inspect(engine)
    table_names = set(inspector.get_table_names())
    assert {
        "collection_archives",
        "collection_files",
        "collection_image_operator_summaries",
        "collection_operator_summaries",
        "collection_upload_files",
        "collection_uploads",
        "collections",
        "disc_operator_summaries",
        "fetch_entries",
        "fetch_operator_files",
        "fetch_operator_summaries",
        "fetch_selectors",
        "fetches",
        "file_discs",
        "finalized_image_collection_artifacts",
        "finalized_image_coverage_parts",
        "finalized_image_covered_paths",
        "finalized_images",
        "archive_restore_collections",
        "archive_restore_images",
        "archive_restores",
        "archive_usage_snapshots",
        "image_discs",
        "image_disc_events",
        "image_operator_summaries",
        "planned_candidates",
    }.issubset(table_names)

    collection_columns = {column["name"] for column in inspector.get_columns("collections")}
    assert "notify_json" in collection_columns

    upload_columns = {column["name"] for column in inspector.get_columns("collection_uploads")}
    assert {
        "archive_encryption_state_json",
        "archive_storage_prefix",
        "collection_manifest_bytes_b64",
        "collection_manifest_proof_bytes_b64",
        "notify_json",
    }.issubset(upload_columns)

    archive_columns = {column["name"] for column in inspector.get_columns("collection_archives")}
    assert "archive_storage_prefix" in archive_columns

    index_names = {
        index["name"]
        for table in (
            "candidate_covered_paths",
            "collection_upload_files",
            "fetch_operator_files",
            "finalized_image_covered_paths",
            "finalized_image_coverage_parts",
            "archive_restores",
        )
        for index in inspector.get_indexes(table)
    }
    assert {
        "ix_candidate_covered_paths_collection_path",
        "idx_collection_upload_files_collection_order",
        "ix_fetch_operator_files_bytes",
        "ix_fetch_operator_files_path",
        "ix_finalized_image_covered_paths_collection_path",
        "ix_finalized_image_coverage_parts_collection_path",
        "ix_archive_restores_state_created",
        "ix_archive_restores_type_state_created",
    }.issubset(index_names)


def test_create_catalog_engine_rejects_bare_database_paths(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="SQLAlchemy URL"):
        create_catalog_engine(str(tmp_path / "catalog.sqlite3"))
