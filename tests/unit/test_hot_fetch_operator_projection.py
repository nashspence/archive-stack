from __future__ import annotations

import hashlib
from pathlib import Path

from sqlalchemy import select

from riverhog_core.catalog_db import initialize_db, make_session_factory, session_scope
from riverhog_core.catalog_models import (
    CollectionFileRecord,
    CollectionRecord,
    FetchEntryRecord,
    FetchOperatorFileRecord,
    FetchOperatorSummaryRecord,
    FetchRecord,
    FetchSelectorRecord,
    FileCopyRecord,
)
from riverhog_core.runtime_config import RuntimeConfig
from riverhog_core.services.fetches import SqlAlchemyFetchService
from tests.unit.db_helpers import sqlite_url


def _config(sqlite_path: Path) -> RuntimeConfig:
    return RuntimeConfig(
        object_store="s3",
        s3_endpoint_url="http://example.invalid:9000",
        s3_region="us-east-1",
        s3_bucket="riverhog",
        s3_access_key_id="test-access",
        s3_secret_access_key="test-secret",
        s3_force_path_style=True,
        tusd_base_url="http://example.invalid:1080/files",
        tusd_hook_secret="hook-secret",
        database_url=sqlite_url(sqlite_path),
    )


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _summary_row(sqlite_path: Path, fetch_id: str) -> FetchOperatorSummaryRecord:
    session_factory = make_session_factory(sqlite_url(sqlite_path))
    with session_scope(session_factory) as session:
        row = session.get(FetchOperatorSummaryRecord, fetch_id)
        assert row is not None
        return row


def _file_rows(sqlite_path: Path, fetch_id: str) -> list[FetchOperatorFileRecord]:
    session_factory = make_session_factory(sqlite_url(sqlite_path))
    with session_scope(session_factory) as session:
        return list(
            session.scalars(
                select(FetchOperatorFileRecord)
                .where(FetchOperatorFileRecord.fetch_id == fetch_id)
                .order_by(FetchOperatorFileRecord.collection_id, FetchOperatorFileRecord.path)
            ).all()
        )


def _seed_fetch(sqlite_path: Path) -> None:
    session_factory = make_session_factory(sqlite_url(sqlite_path))
    with session_scope(session_factory) as session:
        session.add(CollectionRecord(id="docs"))
        session.add_all(
            [
                CollectionFileRecord(
                    collection_id="docs",
                    path="ready.txt",
                    bytes=50,
                    sha256=_sha256(b"ready"),
                    hot=True,
                    archived=True,
                ),
                CollectionFileRecord(
                    collection_id="docs",
                    path="missing.txt",
                    bytes=100,
                    sha256=_sha256(b"missing"),
                    hot=False,
                    archived=True,
                ),
            ]
        )
        session.add(
            FetchRecord(
                fetch_id="fx-1",
                name="Docs restore",
                fetch_order=1,
                fetch_state="queued_djdan",
            )
        )
        session.add(
            FetchSelectorRecord(
                fetch_id="fx-1",
                target="docs/",
                selector_order=1,
            )
        )


def test_fetch_operator_projection_tracks_selector_and_entry_changes(tmp_path: Path) -> None:
    sqlite_path = tmp_path / "catalog.sqlite3"
    initialize_db(sqlite_url(sqlite_path))
    _seed_fetch(sqlite_path)

    row = _summary_row(sqlite_path, "fx-1")
    assert row.fetch_id == "fx-1"
    assert row.name == "Docs restore"
    assert row.targets_text == "docs/"
    assert row.files == 2
    assert row.bytes == 150
    assert row.hot_files == 1
    assert row.hot_bytes == 50
    assert row.missing_files == 1
    assert row.missing_bytes == 100
    assert row.entries_total == 0
    files = _file_rows(sqlite_path, "fx-1")
    assert [(file.collection_id, file.path, file.hot) for file in files] == [
        ("docs", "missing.txt", False),
        ("docs", "ready.txt", True),
    ]
    assert not files[0].registered_disc_coverage

    session_factory = make_session_factory(sqlite_url(sqlite_path))
    with session_scope(session_factory) as session:
        session.add(
            FileCopyRecord(
                collection_id="docs",
                path="missing.txt",
                copy_id="copy-1",
                volume_id="BD-001",
                location="shelf",
                disc_path="docs/missing.txt",
                enc_json="{}",
            )
        )

    files = _file_rows(sqlite_path, "fx-1")
    assert files[0].path == "missing.txt"
    assert files[0].registered_disc_coverage

    with session_scope(session_factory) as session:
        session.add(
            FetchEntryRecord(
                fetch_id="fx-1",
                entry_id="e2",
                entry_order=2,
                collection_id="docs",
                path="missing.txt",
                bytes=100,
                sha256=_sha256(b"missing"),
                recovery_bytes=120,
                uploaded_bytes=30,
                upload_expires_at="2026-06-20T12:30:00Z",
                tus_url="https://uploads.test/fx-1/e2",
            )
        )

    row = _summary_row(sqlite_path, "fx-1")
    assert row.entries_total == 1
    assert row.entries_pending == 0
    assert row.entries_partial == 1
    assert row.entries_byte_complete == 0
    assert row.entries_uploaded == 0
    assert row.entry_bytes == 100
    assert row.entry_recovery_bytes == 120
    assert row.uploaded_bytes == 30
    assert row.upload_missing_bytes == 90
    assert row.upload_state_expires_at == "2026-06-20T12:30:00Z"

    with session_scope(session_factory) as session:
        entry = session.get(FetchEntryRecord, ("fx-1", "e2"))
        assert entry is not None
        entry.uploaded_bytes = 120
        entry.upload_expires_at = None

    row = _summary_row(sqlite_path, "fx-1")
    assert row.entries_partial == 0
    assert row.entries_byte_complete == 1
    assert row.upload_missing_bytes == 0
    assert row.upload_state_expires_at is None

    with session_scope(session_factory) as session:
        fetch = session.get(FetchRecord, "fx-1")
        assert fetch is not None
        fetch.fetch_state = "done"
        missing_file = session.get(CollectionFileRecord, ("docs", "missing.txt"))
        assert missing_file is not None
        missing_file.hot = True

    row = _summary_row(sqlite_path, "fx-1")
    assert row.fetch_state == "done"
    assert row.hot_files == 2
    assert row.missing_files == 0
    assert row.entries_total == 1
    assert row.entries_uploaded == 1
    assert all(file.hot for file in _file_rows(sqlite_path, "fx-1"))

    fetch_service = SqlAlchemyFetchService(_config(sqlite_path), object(), object())  # type: ignore[arg-type]
    done_summary = fetch_service.get("fx-1")
    assert done_summary.files == 2
    assert done_summary.entries_total == 2
    assert done_summary.entries_uploaded == 2
    assert done_summary.missing_bytes == 0
    done_status = fetch_service.status("fx-1")
    assert done_status["entries"] == []


def test_fetch_list_and_show_read_operator_projection(tmp_path: Path) -> None:
    sqlite_path = tmp_path / "catalog.sqlite3"
    initialize_db(sqlite_url(sqlite_path))
    _seed_fetch(sqlite_path)

    config = _config(sqlite_path)
    fetch_service = SqlAlchemyFetchService(config, object(), object())  # type: ignore[arg-type]
    page = fetch_service.list(page=1, per_page=1)
    assert page.total == 1
    assert [str(fetch.id) for fetch in page.fetches] == ["fx-1"]
    assert page.fetches[0].files == 2
    assert page.fetches[0].missing_bytes == 100

    summary = fetch_service.get("fx-1")
    assert summary.files == 2
    assert summary.entries_pending == 1
    assert summary.entries_uploaded == 1
    assert summary.missing_bytes == 100

    status = fetch_service.status("fx-1", limit=1)
    assert status["entries_total"] == 2
    assert status["entries_returned"] == 1
    assert status["entries"] == [
        {
            "id": "e1",
            "collection_id": "docs",
            "path": "missing.txt",
            "bytes": 100,
            "upload_state": "pending",
            "uploaded_bytes": 0,
            "upload_state_expires_at": None,
        }
    ]
