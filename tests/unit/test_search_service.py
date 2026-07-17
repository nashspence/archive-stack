from __future__ import annotations

from pathlib import Path

from riverhog_core.catalog_db import initialize_db, make_session_factory, session_scope
from riverhog_core.catalog_models import CollectionFileRecord, CollectionRecord
from riverhog_core.runtime_config import RuntimeConfig
from riverhog_core.services.search import SqlAlchemySearchService
from tests.unit.db_helpers import sqlite_url


def _seed(path: Path) -> None:
    factory = make_session_factory(sqlite_url(path))
    with session_scope(factory) as session:
        session.add(CollectionRecord(id="2025/20250102T030405Z__docs"))
        session.add_all(
            [
                CollectionFileRecord(
                    collection_id="2025/20250102T030405Z__docs",
                    path="letters/cover.txt",
                    bytes=13,
                    sha256="a" * 64,
                    hot=True,
                ),
                CollectionFileRecord(
                    collection_id="2025/20250102T030405Z__docs",
                    path="tax/invoice.pdf",
                    bytes=34,
                    sha256="b" * 64,
                    hot=False,
                ),
                CollectionFileRecord(
                    collection_id="2025/20250102T030405Z__docs",
                    path="tax/receipt.pdf",
                    bytes=21,
                    sha256="c" * 64,
                    hot=True,
                ),
            ]
        )


def test_search_files_is_paginated_filtered_and_sorted(tmp_path: Path) -> None:
    path = tmp_path / "catalog.sqlite3"
    initialize_db(sqlite_url(path))
    _seed(path)

    payload = SqlAlchemySearchService(RuntimeConfig(database_url=sqlite_url(path))).search(
        q="tax",
        collection="2025/20250102T030405Z__docs",
        page=2,
        per_page=1,
        sort="collection_path",
        order="asc",
    )

    assert payload == {
        "query": "tax",
        "collection": "2025/20250102T030405Z__docs",
        "hot": None,
        "page": 2,
        "per_page": 1,
        "total": 2,
        "pages": 2,
        "sort": "collection_path",
        "order": "asc",
        "files": [
            {
                "logical_path": ("2025/20250102T030405Z__docs/tax/receipt.pdf"),
                "collection_id": "2025/20250102T030405Z__docs",
                "collection_path": "tax/receipt.pdf",
                "bytes": 21,
                "sha256": "c" * 64,
                "hot": True,
            }
        ],
    }


def test_search_files_filters_hot_state(tmp_path: Path) -> None:
    path = tmp_path / "catalog.sqlite3"
    initialize_db(sqlite_url(path))
    _seed(path)

    payload = SqlAlchemySearchService(RuntimeConfig(database_url=sqlite_url(path))).search(
        q=None,
        collection="2025/20250102T030405Z__docs",
        hot=False,
        page=1,
        per_page=25,
        sort="logical_path",
        order="asc",
    )

    assert [file["logical_path"] for file in payload["files"]] == [
        "2025/20250102T030405Z__docs/tax/invoice.pdf"
    ]


def test_search_files_can_return_every_database_match(tmp_path: Path) -> None:
    path = tmp_path / "catalog.sqlite3"
    initialize_db(sqlite_url(path))
    _seed(path)

    payload = SqlAlchemySearchService(RuntimeConfig(database_url=sqlite_url(path))).search(
        q="tax",
        page=9,
        per_page=1,
        sort="collection_path",
        order="asc",
        all_items=True,
    )

    assert payload["page"] == 1
    assert payload["per_page"] == 2
    assert payload["total"] == 2
    assert payload["pages"] == 1
    assert [file["collection_path"] for file in payload["files"]] == [
        "tax/invoice.pdf",
        "tax/receipt.pdf",
    ]
