from __future__ import annotations

from pathlib import Path

import pytest
from riverhog_core.catalog_db import Base, create_catalog_engine, initialize_db
from sqlalchemy import inspect, text

from tests.unit.db_helpers import sqlite_url


def test_initialize_db_creates_current_catalog(tmp_path: Path) -> None:
    database_url = sqlite_url(tmp_path / "catalog.sqlite3")

    initialize_db(database_url)
    initialize_db(database_url)

    inspector = inspect(create_catalog_engine(database_url))
    assert set(inspector.get_table_names()) == set(Base.metadata.tables)
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


def test_existing_catalog_is_validated_without_mutation(tmp_path: Path) -> None:
    database_url = sqlite_url(tmp_path / "catalog.sqlite3")
    initialize_db(database_url)
    engine = create_catalog_engine(database_url)
    with engine.begin() as connection:
        connection.execute(text("DROP INDEX ix_retrieval_jobs_due"))

    with pytest.raises(
        RuntimeError,
        match=r"missing index retrieval_jobs\.ix_retrieval_jobs_due",
    ):
        initialize_db(database_url)

    assert "ix_retrieval_jobs_due" not in {
        index["name"] for index in inspect(engine).get_indexes("retrieval_jobs")
    }


def test_existing_catalog_requires_current_column_contract(tmp_path: Path) -> None:
    database_url = sqlite_url(tmp_path / "catalog.sqlite3")
    engine = create_catalog_engine(database_url)
    with engine.begin() as connection:
        connection.execute(
            text(
                "CREATE TABLE collections ("
                "id TEXT PRIMARY KEY, "
                "creation_idempotency_key VARCHAR NOT NULL UNIQUE, "
                "content_etag VARCHAR(64), "
                "record_etag VARCHAR(64) NOT NULL, "
                "metadata_revision BIGINT NOT NULL, "
                "metadata_updated_at VARCHAR NOT NULL, "
                "ingest_source VARCHAR, "
                "created_by_app VARCHAR NOT NULL, "
                "created_by_key_id VARCHAR, "
                "created_at VARCHAR NOT NULL"
                ")"
            )
        )

    with pytest.raises(RuntimeError) as error:
        initialize_db(database_url)

    message = str(error.value)
    assert "column collections.id has type TEXT, expected INTEGER" in message
    assert "column collections.content_etag has nullable=True, expected False" in message
    assert set(inspect(engine).get_table_names()) == {"collections"}


def test_existing_catalog_requires_current_relational_contract(tmp_path: Path) -> None:
    database_url = sqlite_url(tmp_path / "catalog.sqlite3")
    engine = create_catalog_engine(database_url)
    with engine.begin() as connection:
        connection.execute(
            text(
                "CREATE TABLE collection_files ("
                "collection_id INTEGER NOT NULL, "
                "path VARCHAR NOT NULL, "
                "bytes BIGINT NOT NULL, "
                "sha256 VARCHAR(64) NOT NULL, "
                "PRIMARY KEY (collection_id, path)"
                ")"
            )
        )

    with pytest.raises(
        RuntimeError,
        match=r"missing foreign key on collection_files",
    ):
        initialize_db(database_url)

    assert set(inspect(engine).get_table_names()) == {"collection_files"}
