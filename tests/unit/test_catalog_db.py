from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import inspect, text

from riverhog_core.catalog_db import create_catalog_engine, initialize_db
from tests.unit.db_helpers import sqlite_url

CURRENT_TABLES = {
    "archive_copy_jobs",
    "archive_copy_retirements",
    "archive_restore_files",
    "archive_restore_objects",
    "archive_restores",
    "archive_usage_snapshots",
    "collection_archive_copies",
    "collection_archive_file_objects",
    "collection_archive_object_uploads",
    "collection_archive_objects",
    "collection_deletions",
    "collection_files",
    "collection_upload_files",
    "collection_uploads",
    "collections",
    "fetch_files",
    "fetches",
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
    assert {column["name"] for column in inspector.get_columns("archive_restores")} >= {
        "restore_id",
        "state",
        "archive_verification_state",
        "extraction_state",
        "materialization_state",
    }
    assert {column["name"] for column in inspector.get_columns("fetch_files")} == {
        "fetch_id",
        "collection_id",
        "path",
        "file_order",
    }
    fetch_columns = {
        column["name"]: column for column in inspector.get_columns("fetches")
    }
    assert set(fetch_columns) == {"id", "label", "state"}
    assert fetch_columns["id"]["type"].python_type is int
    assert fetch_columns["label"]["nullable"] is True
    upload_columns = {
        column["name"]: column for column in inspector.get_columns("collection_uploads")
    }
    assert upload_columns["retain_hot"]["default"] in {"1", "true"}


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
