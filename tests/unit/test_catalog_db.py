from __future__ import annotations

import gc
import weakref
from pathlib import Path
from unittest.mock import Mock

import pytest
from riverhog_core.catalog_db import (
    STATE_VERSION_TABLE,
    Base,
    create_catalog_engine,
    dispose_session_factory,
    initialize_db,
    make_session_factory,
    validate_db,
)
from riverhog_core.state_migrations.versions import v1_0008, v1_0010
from riverhog_protocol import (
    CollectionUploadCreationIdentityDocument,
    CollectionUploadCreationIdentityPayload,
)
from sqlalchemy import inspect

from tests.unit.db_helpers import sqlite_url


def test_v1_0008_hard_cut_preserves_resumable_checkpoint_identity() -> None:
    old_part = {
        "number": 1,
        "plaintext_start": 0,
        "plaintext_bytes": 3,
        "plaintext_sha256": "a" * 64,
        "stored_bytes": 4,
        "stored_sha256": "b" * 64,
        "etag": '"provider-token"',
    }
    checkpoint = {
        "schema": "pack-upload-checkpoint/v1",
        "upload_id": "provider-write-token",
        "age_state": {},
        "parts": [old_part],
        "completed": {
            "version_id": "provider-revision",
            "etag": '"provider-entity"',
            "bytes": 4,
            "completed_at": "2026-08-25T00:00:00Z",
            "retrieval_cache": {
                "object_path": "objects/item",
                "version_id": "cache-revision",
            },
        },
    }

    upgraded = v1_0008._checkpoint(checkpoint)

    assert upgraded["write_token"] == "provider-write-token"
    assert upgraded["archive_parts"] == [
        {key: value for key, value in old_part.items() if key != "etag"}
    ]
    assert upgraded["write_segments"] == [
        {
            "number": 1,
            "segment_token": '"provider-token"',
            "bytes": 4,
            "sha256": "b" * 64,
        }
    ]
    assert upgraded["completed"]["revision"] == "provider-revision"
    assert upgraded["completed"]["entity_token"] == '"provider-entity"'
    assert upgraded["completed"]["retrieval_cache"]["revision"] == "cache-revision"


def test_v1_0010_backfill_uses_the_upload_creation_identity_encoding() -> None:
    payload = CollectionUploadCreationIdentityPayload(
        tags=("archive", "camera"),
        ingest_source="fixture",
        archive_store="primary",
        event_context={"device": "fixture"},
        provenance_mode="omitted",
        provenance_omission_reason="fixture omission",
        custody_mode="custody-transfer",
    )
    document = payload.model_dump(mode="json", exclude_none=True)

    assert v1_0010._canonical_sha256(document) == (
        CollectionUploadCreationIdentityDocument.seal(payload).creation_identity_sha256
    )


def test_initialize_db_creates_current_catalog(tmp_path: Path) -> None:
    database_url = sqlite_url(tmp_path / "catalog.sqlite3")

    upgraded = initialize_db(database_url)
    validated = validate_db(database_url)

    inspector = inspect(create_catalog_engine(database_url))
    assert upgraded.condition == validated.condition == "current"
    assert upgraded.current_revision == validated.current_revision == "v1_0010"
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
    assert {column["name"] for column in inspector.get_columns("archive_copy_object_uploads")} == {
        "collection_id",
        "destination_store",
        "object_id",
        "kind",
        "object_path",
        "plaintext_bytes",
        "sha256",
        "write_token",
        "expected_stored_bytes",
        "write_segments_json",
        "uploaded_bytes",
        "uploaded_segments",
        "total_segments",
    }
    assert {
        column["name"] for column in inspector.get_columns("collection_archive_object_uploads")
    } >= {"uploaded_units", "total_units"}
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
        "archive_parts_json",
        "revision",
        "stored_sha256",
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
    assert collection_columns["creation_identity_sha256"]["nullable"] is False
    assert collection_columns["creation_custody_mode"]["nullable"] is False
    assert collection_columns["content_identity"]["nullable"] is False
    assert collection_columns["record_etag"]["nullable"] is False
    assert collection_columns["metadata_revision"]["nullable"] is False
    assert collection_columns["metadata_updated_at"]["nullable"] is False
    upload_file_columns = {
        column["name"]: column for column in inspector.get_columns("collection_upload_files")
    }
    assert upload_file_columns["raw_digest_manifest_json"]["nullable"] is True
    assert upload_file_columns["raw_part_plaintext_bytes"]["nullable"] is True
    upload_volume_columns = {
        column["name"]: column
        for column in inspector.get_columns("collection_archive_object_uploads")
    }
    assert upload_volume_columns["plan_json"]["nullable"] is False
    assert upload_volume_columns["checkpoint_json"]["nullable"] is True
    assert upload_volume_columns["sealed_receipt_json"]["nullable"] is True
    upload_columns = {
        column["name"]: column for column in inspector.get_columns("collection_uploads")
    }
    assert upload_columns["creation_identity_sha256"]["nullable"] is False
    for name in (
        "state",
        "opened_at",
        "last_activity_at",
        "archive_phase",
        "archive_phase_updated_at",
        "archive_attempt_count",
        "archive_storage_prefix",
        "planner_checkpoint_json",
    ):
        assert upload_columns[name]["nullable"] is False


def test_create_catalog_engine_rejects_bare_database_paths(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="SQLAlchemy URL"):
        create_catalog_engine(str(tmp_path / "catalog.sqlite3"))


def test_session_factory_disposes_its_owned_engine_when_released(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = create_catalog_engine(sqlite_url(tmp_path / "catalog.sqlite3"))
    dispose = Mock(wraps=engine.dispose)
    monkeypatch.setattr(engine, "dispose", dispose)
    monkeypatch.setattr("riverhog_core.catalog_db.create_catalog_engine", lambda _: engine)

    session_factory = make_session_factory("sqlite+pysqlite://")
    reference = weakref.ref(session_factory)
    del session_factory
    gc.collect()

    assert reference() is None
    dispose.assert_called_once_with()


def test_session_factory_can_be_disposed_explicitly(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = create_catalog_engine(sqlite_url(tmp_path / "catalog.sqlite3"))
    dispose = Mock(wraps=engine.dispose)
    monkeypatch.setattr(engine, "dispose", dispose)
    monkeypatch.setattr("riverhog_core.catalog_db.create_catalog_engine", lambda _: engine)
    session_factory = make_session_factory("sqlite+pysqlite://")

    dispose_session_factory(session_factory)

    dispose.assert_called_once_with()


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
