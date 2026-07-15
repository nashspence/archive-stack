from __future__ import annotations

from pathlib import Path
from typing import cast

from riverhog_core.catalog_db import initialize_db, make_session_factory, session_scope
from riverhog_core.catalog_models import (
    CollectionArchiveRecord,
    CollectionFileRecord,
    CollectionRecord,
)
from riverhog_core.ports.hot_store import HotStore
from riverhog_core.ports.upload_store import UploadStore
from riverhog_core.runtime_config import RuntimeConfig
from riverhog_core.services.collections import SqlAlchemyCollectionService
from tests.unit.db_helpers import sqlite_url


class UnusedStore:
    def __getattr__(self, name: str) -> object:
        raise AssertionError(f"store method should not be used: {name}")


def _service(path: Path) -> SqlAlchemyCollectionService:
    store = UnusedStore()
    return SqlAlchemyCollectionService(
        RuntimeConfig(database_url=sqlite_url(path)),
        cast(HotStore, store),
        cast(UploadStore, store),
    )


def _seed(path: Path) -> None:
    factory = make_session_factory(sqlite_url(path))
    with session_scope(factory) as session:
        for collection_id, size, hot in (
            ("2025/20250101T000000Z__alpha", 10, True),
            ("2025/20250102T000000Z__beta", 20, False),
        ):
            session.add(CollectionRecord(id=collection_id))
            session.add(
                CollectionFileRecord(
                    collection_id=collection_id,
                    path="file.txt",
                    bytes=size,
                    sha256=("a" if collection_id.endswith("__alpha") else "b") * 64,
                    hot=hot,
                )
            )
        session.add(
            CollectionArchiveRecord(
                collection_id="2025/20250101T000000Z__alpha",
                state="uploaded",
                object_path="collections/alpha/archive.tar.age",
                stored_bytes=14,
                last_verified_at="2026-07-14T00:00:00Z",
            )
        )


def test_collection_summary_reports_hot_and_archive_state(tmp_path: Path) -> None:
    path = tmp_path / "catalog.sqlite3"
    initialize_db(sqlite_url(path))
    _seed(path)

    summary = _service(path).get("2025/20250101T000000Z__alpha")

    assert summary.files == 1
    assert summary.bytes == 10
    assert summary.hot_bytes == 10
    assert summary.archive.state.value == "uploaded"


def test_collection_list_sorts_current_catalog_fields(tmp_path: Path) -> None:
    path = tmp_path / "catalog.sqlite3"
    initialize_db(sqlite_url(path))
    _seed(path)

    page = _service(path).list(
        page=1,
        per_page=25,
        q=None,
        sort="bytes",
        order="desc",
    )

    assert [str(collection.id) for collection in page.collections] == [
        "2025/20250102T000000Z__beta",
        "2025/20250101T000000Z__alpha",
    ]


def test_collection_list_returns_all_database_summaries_in_one_page(tmp_path: Path) -> None:
    path = tmp_path / "catalog.sqlite3"
    initialize_db(sqlite_url(path))
    _seed(path)

    page = _service(path).list(
        page=2,
        per_page=1,
        q="2025/",
        sort="id",
        order="asc",
        all_items=True,
    )

    assert page.page == 1
    assert page.per_page == 2
    assert page.pages == 1
    assert [
        (str(collection.id), collection.files, collection.bytes, collection.hot_bytes)
        for collection in page.collections
    ] == [
        ("2025/20250101T000000Z__alpha", 1, 10, 10),
        ("2025/20250102T000000Z__beta", 1, 20, 0),
    ]
