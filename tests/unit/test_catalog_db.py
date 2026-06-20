from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import inspect, text

from riverhog_core.catalog_db import (
    SCHEMA_BASELINE_VERSION,
    SCHEMA_LATEST_VERSION,
    create_catalog_engine,
    initialize_db,
)
from tests.unit.db_helpers import sqlite_url


def test_initialize_db_creates_current_baseline_schema(tmp_path: Path) -> None:
    database_url = sqlite_url(tmp_path / "catalog.sqlite3")

    initialize_db(database_url)
    initialize_db(database_url)

    engine = create_catalog_engine(database_url)
    inspector = inspect(engine)
    table_names = set(inspector.get_table_names())
    assert {
        "active_pins",
        "collection_archives",
        "collection_files",
        "collection_image_operator_summaries",
        "collection_operator_summaries",
        "collection_upload_files",
        "collection_uploads",
        "collections",
        "disc_operator_summaries",
        "fetch_entries",
        "file_copies",
        "finalized_image_collection_artifacts",
        "finalized_image_coverage_parts",
        "finalized_image_covered_paths",
        "finalized_images",
        "glacier_recovery_session_collections",
        "glacier_recovery_session_images",
        "glacier_recovery_sessions",
        "glacier_usage_snapshots",
        "hot_fetch_operator_summaries",
        "image_copies",
        "image_copy_events",
        "image_operator_summaries",
        "planned_candidates",
        "schema_migrations",
    }.issubset(table_names)
    assert "collection_protection_mirrors" not in table_names

    upload_columns = {column["name"] for column in inspector.get_columns("collection_uploads")}
    assert {
        "archive_encryption_state_json",
        "archive_storage_prefix",
        "collection_manifest_bytes_b64",
        "collection_manifest_proof_bytes_b64",
    }.issubset(upload_columns)

    archive_columns = {column["name"] for column in inspector.get_columns("collection_archives")}
    assert "archive_storage_prefix" in archive_columns

    index_names = {
        index["name"]
        for table in (
            "candidate_covered_paths",
            "collection_upload_files",
            "finalized_image_covered_paths",
            "finalized_image_coverage_parts",
        )
        for index in inspector.get_indexes(table)
    }
    assert {
        "ix_candidate_covered_paths_collection_path",
        "idx_collection_upload_files_collection_order",
        "ix_finalized_image_covered_paths_collection_path",
        "ix_finalized_image_coverage_parts_collection_path",
    }.issubset(index_names)

    with engine.begin() as conn:
        applied_versions = {
            row[0] for row in conn.execute(text("SELECT version FROM schema_migrations")).fetchall()
        }
    assert applied_versions == {
        SCHEMA_BASELINE_VERSION,
        2,
        3,
        4,
        5,
        6,
        SCHEMA_LATEST_VERSION,
    }


def test_initialize_db_migrates_v1_catalog_billing_columns(tmp_path: Path) -> None:
    database_url = sqlite_url(tmp_path / "catalog.sqlite3")
    engine = create_catalog_engine(database_url)
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY)"))
        conn.execute(
            text("INSERT INTO schema_migrations (version) VALUES (:v)"),
            {"v": SCHEMA_BASELINE_VERSION},
        )
        conn.execute(
            text(
                "CREATE TABLE glacier_usage_snapshots ("
                "captured_at TEXT PRIMARY KEY, "
                "uploaded_images INTEGER NOT NULL, "
                "measured_storage_bytes BIGINT NOT NULL, "
                "estimated_billable_bytes BIGINT NOT NULL, "
                "estimated_monthly_cost_usd FLOAT NOT NULL, "
                "pricing_label TEXT NOT NULL, "
                "glacier_storage_rate_usd_per_gib_month FLOAT NOT NULL, "
                "standard_storage_rate_usd_per_gib_month FLOAT NOT NULL, "
                "archived_metadata_bytes_per_object BIGINT NOT NULL, "
                "standard_metadata_bytes_per_object BIGINT NOT NULL, "
                "minimum_storage_duration_days INTEGER NOT NULL"
                ")"
            )
        )
        conn.execute(
            text(
                "CREATE TABLE glacier_recovery_sessions ("
                "session_id TEXT PRIMARY KEY, "
                "estimate_json TEXT NOT NULL"
                ")"
            )
        )

    initialize_db(database_url)

    inspector = inspect(engine)
    snapshot_columns = {
        column["name"] for column in inspector.get_columns("glacier_usage_snapshots")
    }
    recovery_columns = {
        column["name"] for column in inspector.get_columns("glacier_recovery_sessions")
    }
    assert "estimated_billable_bytes" not in snapshot_columns
    assert "estimated_monthly_cost_usd" not in snapshot_columns
    assert "pricing_label" not in snapshot_columns
    assert "estimate_json" not in recovery_columns
    with engine.begin() as conn:
        applied_versions = {
            row[0] for row in conn.execute(text("SELECT version FROM schema_migrations")).fetchall()
        }
    assert applied_versions == {
        SCHEMA_BASELINE_VERSION,
        2,
        3,
        4,
        5,
        6,
        SCHEMA_LATEST_VERSION,
    }


def test_create_catalog_engine_rejects_bare_database_paths(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="SQLAlchemy URL"):
        create_catalog_engine(str(tmp_path / "catalog.sqlite3"))


def test_initialize_db_rejects_pre_baseline_schema_markers(tmp_path: Path) -> None:
    database_url = sqlite_url(tmp_path / "catalog.sqlite3")
    engine = create_catalog_engine(database_url)
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY)"))
        conn.execute(text("INSERT INTO schema_migrations (version) VALUES (25)"))

    with pytest.raises(RuntimeError, match="pre-baseline catalog schema"):
        initialize_db(database_url)


def test_initialize_db_rejects_unversioned_existing_schema(tmp_path: Path) -> None:
    database_url = sqlite_url(tmp_path / "catalog.sqlite3")
    engine = create_catalog_engine(database_url)
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE collections (id TEXT PRIMARY KEY)"))

    with pytest.raises(RuntimeError, match="unversioned catalog schema"):
        initialize_db(database_url)
