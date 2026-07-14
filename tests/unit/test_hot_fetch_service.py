from __future__ import annotations

import hashlib
from collections.abc import Iterable, Iterator
from pathlib import Path

import pytest

from riverhog_core.catalog_db import initialize_db, make_session_factory, session_scope
from riverhog_core.catalog_models import (
    CollectionArchiveRecord,
    CollectionFileRecord,
    CollectionRecord,
)
from riverhog_core.domain.enums import FetchState
from riverhog_core.domain.errors import Conflict
from riverhog_core.ports.hot_store import HotFileStat
from riverhog_core.runtime_config import RuntimeConfig
from riverhog_core.services.fetches import SqlAlchemyFetchService
from tests.unit.db_helpers import sqlite_url


class FakeHotStore:
    def __init__(self) -> None:
        self.files: dict[tuple[str, str], bytes] = {}
        self.deleted: list[tuple[str, str]] = []

    def put_collection_file(self, collection_id: str, path: str, content: bytes) -> None:
        self.files[(collection_id, path)] = content

    def put_collection_file_stream(
        self,
        collection_id: str,
        path: str,
        chunks: Iterable[bytes],
        *,
        content_length: int,
        sha256: str | None = None,
    ) -> None:
        _ = sha256
        content = b"".join(chunks)
        assert len(content) == content_length
        self.files[(collection_id, path)] = content

    def get_collection_file(self, collection_id: str, path: str) -> bytes:
        return self.files[(collection_id, path)]

    def iter_collection_file(
        self,
        collection_id: str,
        path: str,
        *,
        offset: int = 0,
        size: int | None = None,
    ) -> Iterator[bytes]:
        content = self.get_collection_file(collection_id, path)
        yield content[offset:] if size is None else content[offset : offset + size]

    def stat_collection_file(self, collection_id: str, path: str) -> HotFileStat | None:
        content = self.files.get((collection_id, path))
        if content is None:
            return None
        return HotFileStat(bytes=len(content), sha256=hashlib.sha256(content).hexdigest())

    def has_collection_file(self, collection_id: str, path: str) -> bool:
        return (collection_id, path) in self.files

    def delete_collection_file(self, collection_id: str, path: str) -> None:
        self.deleted.append((collection_id, path))
        self.files.pop((collection_id, path), None)

    def list_collection_files(self, collection_id: str) -> list[tuple[str, int]]:
        return [
            (path, len(content))
            for (current_collection, path), content in sorted(self.files.items())
            if current_collection == collection_id
        ]


def _seed(path: Path, hot_store: FakeHotStore, *, archived: bool = True) -> None:
    contents = {"a.txt": b"alpha", "b.txt": b"bravo"}
    factory = make_session_factory(sqlite_url(path))
    with session_scope(factory) as session:
        session.add(CollectionRecord(id="docs"))
        for name, content in contents.items():
            hot_store.put_collection_file("docs", name, content)
            session.add(
                CollectionFileRecord(
                    collection_id="docs",
                    path=name,
                    bytes=len(content),
                    sha256=hashlib.sha256(content).hexdigest(),
                    hot=True,
                )
            )
        if archived:
            session.add(
                CollectionArchiveRecord(
                    collection_id="docs",
                    state="uploaded",
                    object_path="collections/docs/archive.tar.age",
                    sha256="a" * 64,
                    last_verified_at="2026-07-14T00:00:00Z",
                )
            )


def _service(path: Path, hot_store: FakeHotStore) -> SqlAlchemyFetchService:
    return SqlAlchemyFetchService(RuntimeConfig(database_url=sqlite_url(path)), hot_store)


def test_fetch_start_reports_hot_selection(tmp_path: Path) -> None:
    path = tmp_path / "catalog.sqlite3"
    initialize_db(sqlite_url(path))
    hot_store = FakeHotStore()
    _seed(path, hot_store)
    service = _service(path, hot_store)

    fetch = service.create(name="documents", targets=["docs/"])
    started = service.start(str(fetch.id))

    assert started.state == FetchState.DONE
    assert started.files == 2
    assert started.hot_files == 2
    assert started.missing_files == 0


def test_fetch_start_queues_archive_materialization_for_missing_files(tmp_path: Path) -> None:
    path = tmp_path / "catalog.sqlite3"
    initialize_db(sqlite_url(path))
    hot_store = FakeHotStore()
    _seed(path, hot_store)
    hot_store.delete_collection_file("docs", "b.txt")
    factory = make_session_factory(sqlite_url(path))
    with session_scope(factory) as session:
        session.get(CollectionFileRecord, {"collection_id": "docs", "path": "b.txt"}).hot = False
    service = _service(path, hot_store)

    fetch = service.create(name="documents", targets=["docs/"])
    started = service.start(str(fetch.id))

    assert started.state == FetchState.QUEUED_ARCHIVE
    assert started.missing_files == 1


def test_hot_eviction_requires_verified_collection_archive(tmp_path: Path) -> None:
    path = tmp_path / "catalog.sqlite3"
    initialize_db(sqlite_url(path))
    hot_store = FakeHotStore()
    _seed(path, hot_store, archived=False)

    with pytest.raises(Conflict, match="collection archive is verified"):
        _service(path, hot_store).evict(["docs/a.txt"])


def test_hot_eviction_updates_store_and_catalog(tmp_path: Path) -> None:
    path = tmp_path / "catalog.sqlite3"
    initialize_db(sqlite_url(path))
    hot_store = FakeHotStore()
    _seed(path, hot_store)

    payload = _service(path, hot_store).evict(["docs/a.txt"])

    assert payload["evicted_files"] == 1
    assert hot_store.deleted[-1] == ("docs", "a.txt")
    factory = make_session_factory(sqlite_url(path))
    with session_scope(factory) as session:
        row = session.get(CollectionFileRecord, {"collection_id": "docs", "path": "a.txt"})
        assert row is not None and row.hot is False
