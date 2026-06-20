from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import pytest

from riverhog_core.catalog_db import initialize_db, make_session_factory, session_scope
from riverhog_core.catalog_models import (
    ActivePinRecord,
    CollectionFileRecord,
    CollectionRecord,
    FetchEntryRecord,
    HotFetchOperatorSummaryRecord,
)
from riverhog_core.runtime_config import RuntimeConfig
from riverhog_core.services import fetches as fetches_service
from riverhog_core.services import pins as pins_service
from riverhog_core.services.fetches import SqlAlchemyFetchService
from riverhog_core.services.pins import SqlAlchemyPinService
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


def _summary_row(sqlite_path: Path, target: str) -> HotFetchOperatorSummaryRecord:
    session_factory = make_session_factory(sqlite_url(sqlite_path))
    with session_scope(session_factory) as session:
        row = session.get(HotFetchOperatorSummaryRecord, target)
        assert row is not None
        return row


def _seed_pin(sqlite_path: Path) -> None:
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
            ActivePinRecord(
                target="docs/",
                fetch_id="fx-1",
                fetch_order=1,
                fetch_state="waiting_media",
            )
        )


def test_hot_fetch_operator_projection_tracks_pin_and_entry_changes(tmp_path: Path) -> None:
    sqlite_path = tmp_path / "catalog.sqlite3"
    initialize_db(sqlite_url(sqlite_path))
    _seed_pin(sqlite_path)

    row = _summary_row(sqlite_path, "docs/")
    assert row.fetch_id == "fx-1"
    assert row.files == 2
    assert row.bytes == 150
    assert row.hot_files == 1
    assert row.hot_bytes == 50
    assert row.missing_files == 1
    assert row.missing_bytes == 100
    assert row.entries_total == 0

    session_factory = make_session_factory(sqlite_url(sqlite_path))
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

    row = _summary_row(sqlite_path, "docs/")
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

    row = _summary_row(sqlite_path, "docs/")
    assert row.entries_partial == 0
    assert row.entries_byte_complete == 1
    assert row.upload_missing_bytes == 0
    assert row.upload_state_expires_at is None

    with session_scope(session_factory) as session:
        pin = session.get(ActivePinRecord, "docs/")
        assert pin is not None
        pin.fetch_state = "done"
        missing_file = session.get(CollectionFileRecord, ("docs", "missing.txt"))
        assert missing_file is not None
        missing_file.hot = True

    row = _summary_row(sqlite_path, "docs/")
    assert row.fetch_state == "done"
    assert row.hot_files == 2
    assert row.missing_files == 0
    assert row.entries_total == 1
    assert row.entries_uploaded == 1

    fetch_service = SqlAlchemyFetchService(_config(sqlite_path), object(), object())  # type: ignore[arg-type]
    done_summary = fetch_service.get("fx-1")
    assert done_summary.files == 2
    assert done_summary.entries_total == 2
    assert done_summary.entries_uploaded == 2
    assert done_summary.missing_bytes == 0
    done_status = fetch_service.status("fx-1")
    assert done_status["entries"] == []


def test_hot_list_and_show_read_operator_projection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sqlite_path = tmp_path / "catalog.sqlite3"
    initialize_db(sqlite_url(sqlite_path))
    _seed_pin(sqlite_path)

    def fail_if_old_target_stats_path_is_used(*_args: object, **_kwargs: object) -> Any:
        raise AssertionError("hot operator views should read the projection")

    monkeypatch.setattr(
        pins_service,
        "selected_collection_file_stats",
        fail_if_old_target_stats_path_is_used,
        raising=False,
    )
    monkeypatch.setattr(
        fetches_service,
        "_summary_from_target_stats",
        fail_if_old_target_stats_path_is_used,
    )

    config = _config(sqlite_path)
    pin_service = SqlAlchemyPinService(config, object(), object())  # type: ignore[arg-type]
    page = pin_service.list_pins(page=1, per_page=1)
    assert page.total == 1
    assert [pin.target for pin in page.pins] == ["docs/"]
    assert page.pins[0].fetch.files == 2
    assert page.pins[0].fetch.missing_bytes == 100

    fetch_service = SqlAlchemyFetchService(config, object(), object())  # type: ignore[arg-type]
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
