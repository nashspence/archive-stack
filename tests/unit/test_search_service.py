from __future__ import annotations

from pathlib import Path

from riverhog_core.catalog_db import initialize_db, make_session_factory, session_scope
from riverhog_core.catalog_models import CollectionFileRecord, CollectionRecord
from riverhog_core.runtime_config import RuntimeConfig
from riverhog_core.services.search import SqlAlchemySearchService
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


def _seed_docs_collection(sqlite_path: Path) -> None:
    session_factory = make_session_factory(sqlite_url(sqlite_path))
    with session_scope(session_factory) as session:
        session.add(CollectionRecord(id="docs"))
        session.add_all(
            [
                CollectionFileRecord(
                    collection_id="docs",
                    path="tax/2022/receipt-456.pdf",
                    bytes=21,
                    sha256="b" * 64,
                    hot=True,
                    archived=False,
                ),
                CollectionFileRecord(
                    collection_id="docs",
                    path="letters/cover.txt",
                    bytes=13,
                    sha256="a" * 64,
                    hot=True,
                    archived=False,
                ),
                CollectionFileRecord(
                    collection_id="docs",
                    path="tax/2022/invoice-123.pdf",
                    bytes=34,
                    sha256="c" * 64,
                    hot=False,
                    archived=True,
                ),
            ]
        )


def test_search_files_is_paginated_filtered_and_sorted(tmp_path: Path) -> None:
    sqlite_path = tmp_path / "state.sqlite3"
    initialize_db(sqlite_url(sqlite_path))
    _seed_docs_collection(sqlite_path)

    service = SqlAlchemySearchService(_config(sqlite_path))

    payload = service.search(
        q="tax",
        collection="docs",
        page=2,
        per_page=1,
        sort="path",
        order="asc",
    )

    assert payload["query"] == "tax"
    assert payload["collection"] == "docs"
    assert payload["page"] == 2
    assert payload["per_page"] == 1
    assert payload["total"] == 2
    assert payload["pages"] == 2
    assert payload["files"] == [
        {
            "target": "docs/tax/2022/receipt-456.pdf",
            "collection": "docs",
            "path": "tax/2022/receipt-456.pdf",
            "bytes": 21,
            "sha256": "b" * 64,
            "hot": True,
            "archived": False,
        }
    ]


def test_search_files_filters_archive_state(tmp_path: Path) -> None:
    sqlite_path = tmp_path / "state.sqlite3"
    initialize_db(sqlite_url(sqlite_path))
    _seed_docs_collection(sqlite_path)

    service = SqlAlchemySearchService(_config(sqlite_path))

    payload = service.search(
        q=None,
        collection="docs",
        archived=True,
        page=1,
        per_page=25,
        sort="target",
        order="asc",
    )

    assert payload["archived"] is True
    assert [file["target"] for file in payload["files"]] == ["docs/tax/2022/invoice-123.pdf"]
