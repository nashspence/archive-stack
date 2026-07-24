from __future__ import annotations

from pathlib import Path

import pytest
from riverhog_core.catalog_db import create_catalog_engine, initialize_db
from sqlalchemy import inspect, text

from tests.unit.db_helpers import sqlite_url

CURRENT_TABLES = {
    "app_key_collection_grants",
    "app_keys",
    "archive_copy_jobs",
    "archive_copy_retirements",
    "archive_download_reservations",
    "archive_download_usage",
    "archive_usage_snapshots",
    "catalog_events",
    "collection_archive_copies",
    "collection_archive_file_objects",
    "collection_archive_object_uploads",
    "collection_archive_objects",
    "collection_deletions",
    "collection_files",
    "collection_upload_files",
    "collection_uploads",
    "collections",
    "ingress_cleanup",
    "key_download_reservations",
    "key_download_usage",
    "lifecycle_events",
    "retrieval_cache_leases",
    "retrieval_cache_objects",
    "retrieval_job_files",
    "retrieval_job_objects",
    "retrieval_jobs",
}


def test_initialize_db_creates_current_catalog(tmp_path: Path) -> None:
    database_url = sqlite_url(tmp_path / "catalog.sqlite3")

    initialize_db(database_url)
    initialize_db(database_url)

    inspector = inspect(create_catalog_engine(database_url))
    assert set(inspector.get_table_names()) == CURRENT_TABLES
    assert {column["name"] for column in inspector.get_columns("archive_usage_snapshots")} == {
        "captured_at",
        "uploaded_collections",
        "measured_storage_bytes",
    }
    assert {column["name"] for column in inspector.get_columns("archive_download_usage")} == {
        "store",
        "month_started_at",
        "accounted_bytes",
        "updated_at",
    }
    assert {
        column["name"] for column in inspector.get_columns("archive_download_reservations")
    } == {
        "id",
        "store",
        "month_started_at",
        "reserved_bytes",
        "created_at",
        "expires_at",
    }
    assert {column["name"] for column in inspector.get_columns("retrieval_jobs")} >= {
        "id",
        "app",
        "state",
        "plan_etag",
    }
    assert {column["name"] for column in inspector.get_columns("app_keys")} == {
        "id",
        "app",
        "token_sha256",
        "permissions_json",
        "monthly_download_quota_bytes",
        "created_at",
        "expires_at",
        "revoked_at",
        "last_used_at",
    }
    assert {column["name"] for column in inspector.get_columns("lifecycle_events")} == {
        "sequence",
        "event_id",
        "owner_app",
        "event_json",
        "context_json",
        "context_expires_at",
    }
    assert {column["name"] for column in inspector.get_columns("retrieval_job_files")} == {
        "job_id",
        "collection_id",
        "path",
        "file_order",
    }
    collection_columns = {column["name"]: column for column in inspector.get_columns("collections")}
    assert collection_columns["manifest_etag"]["nullable"] is False
    upload_file_columns = {
        column["name"]: column for column in inspector.get_columns("collection_upload_files")
    }
    assert upload_file_columns["ingress_secret_envelope"]["nullable"] is False
    assert upload_file_columns["ingress_state_json"]["nullable"] is False


def test_create_catalog_engine_rejects_bare_database_paths(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="SQLAlchemy URL"):
        create_catalog_engine(str(tmp_path / "catalog.sqlite3"))


def test_initialize_db_requires_exact_current_schema(tmp_path: Path) -> None:
    database_url = sqlite_url(tmp_path / "catalog.sqlite3")
    initialize_db(database_url)
    engine = create_catalog_engine(database_url)
    with engine.begin() as connection:
        connection.execute(text("ALTER TABLE collection_files ADD COLUMN unexpected TEXT"))

    with pytest.raises(
        RuntimeError,
        match=r"unexpected column collection_files\.unexpected",
    ):
        initialize_db(database_url)
