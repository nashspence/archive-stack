from __future__ import annotations

from pathlib import Path

from riverhog_core.app_permissions import CATALOG_READ, ApplicationAccess, ApplicationPrincipal
from riverhog_core.catalog_db import initialize_db, make_session_factory, session_scope
from riverhog_core.catalog_models import (
    CollectionFileRecord,
    CollectionRecord,
    CollectionSlugRecord,
)
from riverhog_core.runtime_config import RuntimeConfig
from riverhog_core.services.search import SqlAlchemySearchService

from tests.unit.db_helpers import sqlite_url


def _seed(path: Path) -> None:
    factory = make_session_factory(sqlite_url(path))
    with session_scope(factory) as session:
        session.add(
            CollectionSlugRecord(
                id="docs",
                created_by_app="fixture",
                created_at="2026-01-01T00:00:00.000000Z",
            )
        )
        session.add(
            CollectionRecord(
                id="docs/20250102T030405Z",
                slug="docs",
                manifest_etag="0" * 64,
            )
        )
        session.add_all(
            [
                CollectionFileRecord(
                    collection_id="docs/20250102T030405Z",
                    path="letters/cover.txt",
                    bytes=13,
                    sha256="a" * 64,
                ),
                CollectionFileRecord(
                    collection_id="docs/20250102T030405Z",
                    path="tax/invoice.pdf",
                    bytes=34,
                    sha256="b" * 64,
                ),
                CollectionFileRecord(
                    collection_id="docs/20250102T030405Z",
                    path="tax/receipt.pdf",
                    bytes=21,
                    sha256="c" * 64,
                ),
            ]
        )


def test_search_files_is_paginated_filtered_and_sorted(tmp_path: Path) -> None:
    path = tmp_path / "catalog.sqlite3"
    initialize_db(sqlite_url(path))
    _seed(path)

    payload = SqlAlchemySearchService(RuntimeConfig(database_url=sqlite_url(path))).search(
        q="tax",
        collection="docs/20250102T030405Z",
        page=2,
        per_page=1,
        sort="collection_path",
        order="asc",
    )

    assert payload == {
        "query": "tax",
        "collection": "docs/20250102T030405Z",
        "page": 2,
        "per_page": 1,
        "total": 2,
        "pages": 2,
        "sort": "collection_path",
        "order": "asc",
        "files": [
            {
                "logical_path": ("docs/20250102T030405Z/tax/receipt.pdf"),
                "collection_id": "docs/20250102T030405Z",
                "collection_path": "tax/receipt.pdf",
                "bytes": 21,
                "sha256": "c" * 64,
            }
        ],
    }


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


def test_search_applies_slug_grants_in_the_database(tmp_path: Path) -> None:
    path = tmp_path / "catalog.sqlite3"
    initialize_db(sqlite_url(path))
    _seed(path)
    principal = ApplicationPrincipal(
        app="reader",
        key_id="reader-key",
        access=frozenset({ApplicationAccess(CATALOG_READ, "slug:other")}),
    )

    payload = SqlAlchemySearchService(RuntimeConfig(database_url=sqlite_url(path))).search(
        q=None,
        page=1,
        per_page=25,
        sort="logical_path",
        order="asc",
        principal=principal,
    )

    assert payload["total"] == 0
    assert payload["files"] == []
