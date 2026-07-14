from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import inspect

from riverhog_core.catalog_db import create_catalog_engine, initialize_db
from tests.unit.db_helpers import sqlite_url

CURRENT_TABLES = {
    "archive_restore_collections",
    "archive_restores",
    "archive_usage_snapshots",
    "collection_archives",
    "collection_files",
    "collection_upload_files",
    "collection_uploads",
    "collections",
    "fetch_selectors",
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
        "paths_json",
        "archive_verification_state",
        "extraction_state",
        "materialization_state",
    }


def test_create_catalog_engine_rejects_bare_database_paths(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="SQLAlchemy URL"):
        create_catalog_engine(str(tmp_path / "catalog.sqlite3"))
