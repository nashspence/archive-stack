from __future__ import annotations

from pathlib import Path

from arc_core.catalog_models import CollectionFileRecord, CollectionRecord
from arc_core.runtime_config import RuntimeConfig
from arc_core.services.files import SqlAlchemyFileService
from arc_core.sqlite_db import initialize_db, make_session_factory, session_scope


class _FakeHotStore:
    def put_collection_file(self, collection_id: str, path: str, content: bytes) -> None:
        raise NotImplementedError

    def get_collection_file(self, collection_id: str, path: str) -> bytes:
        raise NotImplementedError

    def has_collection_file(self, collection_id: str, path: str) -> bool:
        raise NotImplementedError

    def delete_collection_file(self, collection_id: str, path: str) -> None:
        raise NotImplementedError

    def list_collection_files(self, collection_id: str) -> list[tuple[str, int]]:
        raise NotImplementedError


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
        sqlite_path=sqlite_path,
    )


def _seed_docs_collection(sqlite_path: Path) -> None:
    session_factory = make_session_factory(str(sqlite_path))
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
                    bytes=21,
                    sha256="c" * 64,
                    hot=False,
                    archived=True,
                ),
            ]
        )


def test_list_collection_files_is_paginated_and_sorted(tmp_path: Path) -> None:
    sqlite_path = tmp_path / "state.sqlite3"
    initialize_db(str(sqlite_path))
    _seed_docs_collection(sqlite_path)

    service = SqlAlchemyFileService(_config(sqlite_path), _FakeHotStore())

    payload = service.list_collection_files("docs", page=2, per_page=2)

    assert payload["collection_id"] == "docs"
    assert payload["page"] == 2
    assert payload["per_page"] == 2
    assert payload["total"] == 3
    assert payload["pages"] == 2
    assert payload["files"] == [
        {
            "path": "tax/2022/receipt-456.pdf",
            "bytes": 21,
            "hot": True,
            "archived": False,
        }
    ]


def test_query_by_target_is_paginated_and_reports_canonical_target(tmp_path: Path) -> None:
    sqlite_path = tmp_path / "state.sqlite3"
    initialize_db(str(sqlite_path))
    _seed_docs_collection(sqlite_path)

    service = SqlAlchemyFileService(_config(sqlite_path), _FakeHotStore())

    payload = service.query_by_target("docs/", page=1, per_page=2)

    assert payload["target"] == "docs/"
    assert payload["page"] == 1
    assert payload["per_page"] == 2
    assert payload["total"] == 3
    assert payload["pages"] == 2
    assert [record["target"] for record in payload["files"]] == [
        "docs/letters/cover.txt",
        "docs/tax/2022/invoice-123.pdf",
    ]
