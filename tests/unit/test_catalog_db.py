from __future__ import annotations

from pathlib import Path

import pytest
from riverhog_core.catalog_db import (
    STATE_VERSION_TABLE,
    Base,
    create_catalog_engine,
    initialize_db,
    validate_db,
)
from sqlalchemy import inspect

from tests.unit.db_helpers import sqlite_url


def test_initialize_db_creates_current_catalog(tmp_path: Path) -> None:
    database_url = sqlite_url(tmp_path / "catalog.sqlite3")

    upgraded = initialize_db(database_url)
    validated = validate_db(database_url)

    inspector = inspect(create_catalog_engine(database_url))
    assert upgraded.condition == validated.condition == "current"
    assert upgraded.current_revision == validated.current_revision == "v1_0001"
    assert set(inspector.get_table_names()) == {*Base.metadata.tables, STATE_VERSION_TABLE}
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
        "restore_requested_at",
    }
    assert {column["name"] for column in inspector.get_columns("archive_copy_jobs")} >= {
        "initiated_by_app",
        "initiated_by_key_id",
        "event_context_json",
        "completed_at",
    }
    assert {column["name"] for column in inspector.get_columns("archive_copy_object_uploads")} >= {
        "collection_id",
        "destination_store",
        "object_id",
        "multipart_upload_id",
        "multipart_parts_json",
        "encryption_state_json",
        "cache_object_path",
        "cache_stored_sha256",
    }
    assert {column["name"] for column in inspector.get_columns("collection_proof_maturations")} == {
        "collection_id",
        "store",
        "state",
        "attempt_count",
        "next_attempt_at",
        "last_attempt_at",
        "matured_at",
        "failure",
    }
    assert {
        column["name"] for column in inspector.get_columns("collection_archive_attestations")
    } == {
        "collection_id",
        "store",
        "state",
        "attempt_count",
        "next_attempt_at",
        "last_attempt_at",
        "published_at",
        "matured_at",
        "failure",
    }
    assert {column["name"] for column in inspector.get_columns("collection_archive_objects")} >= {
        "stored_sha256"
    }
    assert {column["name"] for column in inspector.get_columns("app_keys")} == {
        "id",
        "app",
        "token_sha256",
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
        "subject",
        "event_json",
        "context_json",
        "context_expires_at",
    }
    assert {column["name"] for column in inspector.get_columns("catalog_event_tags")} == {
        "sequence",
        "phase",
        "tag_id",
    }
    assert {column["name"] for column in inspector.get_columns("retrieval_job_files")} == {
        "job_id",
        "collection_id",
        "path",
        "file_order",
    }
    collection_columns = {column["name"]: column for column in inspector.get_columns("collections")}
    assert collection_columns["content_etag"]["nullable"] is False
    assert collection_columns["record_etag"]["nullable"] is False
    assert collection_columns["metadata_revision"]["nullable"] is False
    assert collection_columns["metadata_updated_at"]["nullable"] is False
    upload_file_columns = {
        column["name"]: column for column in inspector.get_columns("collection_upload_files")
    }
    assert upload_file_columns["ingress_secret_envelope"]["nullable"] is False
    assert upload_file_columns["ingress_state_json"]["nullable"] is False


def test_create_catalog_engine_rejects_bare_database_paths(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="SQLAlchemy URL"):
        create_catalog_engine(str(tmp_path / "catalog.sqlite3"))


def test_validate_db_preserves_the_current_catalog_schema(tmp_path: Path) -> None:
    database_url = sqlite_url(tmp_path / "catalog.sqlite3")
    initialize_db(database_url)
    engine = create_catalog_engine(database_url)
    inspector = inspect(engine)
    before = {
        table: (
            tuple(column["name"] for column in inspector.get_columns(table)),
            tuple(index["name"] for index in inspector.get_indexes(table)),
        )
        for table in inspector.get_table_names()
    }

    status = validate_db(database_url)

    inspector = inspect(engine)
    after = {
        table: (
            tuple(column["name"] for column in inspector.get_columns(table)),
            tuple(index["name"] for index in inspector.get_indexes(table)),
        )
        for table in inspector.get_table_names()
    }
    assert status.condition == "current"
    assert after == before
