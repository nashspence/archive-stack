from __future__ import annotations

import hashlib
from collections.abc import Iterable, Iterator
from pathlib import Path

import pytest

from riverhog_core.catalog_db import initialize_db, make_session_factory, session_scope
from riverhog_core.catalog_models import (
    CollectionArchiveRecord,
    CollectionDeletionRecord,
    CollectionFileRecord,
    CollectionRecord,
)
from riverhog_core.domain.enums import FetchState
from riverhog_core.domain.errors import Conflict
from riverhog_core.ports.archive_store import (
    ArchivePackageVerificationError,
    CollectionArchivePackageIdentity,
)
from riverhog_core.ports.hot_store import HotCollectionFile, HotCollectionListing, HotFileStat
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

    def list_collection_files(self, collection_id: str) -> HotCollectionListing:
        files = tuple(
            HotCollectionFile(path=path, bytes=len(content))
            for (current_collection, path), content in sorted(self.files.items())
            if current_collection == collection_id
        )
        return HotCollectionListing(
            files=files,
            file_count=len(files),
            total_bytes=sum(file.bytes for file in files),
        )


class FakeArchiveStore:
    def __init__(self) -> None:
        self.checks: list[tuple[str, CollectionArchivePackageIdentity]] = []
        self.failure: Exception | None = None

    def verify_collection_archive_package(
        self,
        *,
        collection_id: str,
        package: CollectionArchivePackageIdentity,
    ) -> None:
        self.checks.append((collection_id, package))
        if self.failure is not None:
            raise self.failure


def _seed(path: Path, hot_store: FakeHotStore, *, archived: bool = True) -> None:
    contents = {"a.txt": b"alpha", "b.txt": b"bravo"}
    factory = make_session_factory(sqlite_url(path))
    with session_scope(factory) as session:
        session.add(CollectionRecord(id="2025/20250102T030405Z__docs"))
        for name, content in contents.items():
            hot_store.put_collection_file("2025/20250102T030405Z__docs", name, content)
            session.add(
                CollectionFileRecord(
                    collection_id="2025/20250102T030405Z__docs",
                    path=name,
                    bytes=len(content),
                    sha256=hashlib.sha256(content).hexdigest(),
                    hot=True,
                )
            )
        if archived:
            session.add(
                CollectionArchiveRecord(
                    collection_id="2025/20250102T030405Z__docs",
                    state="uploaded",
                    object_path="collections/docs/archive.tar.age",
                    stored_bytes=100,
                    sha256="a" * 64,
                    manifest_object_path="collections/docs/manifest.yml.age",
                    manifest_stored_bytes=20,
                    manifest_sha256="b" * 64,
                    ots_object_path="collections/docs/manifest.yml.ots.age",
                    ots_stored_bytes=10,
                    ots_sha256="c" * 64,
                    last_verified_at="2026-07-14T00:00:00Z",
                )
            )


def _service(
    path: Path,
    hot_store: FakeHotStore,
    archive_store: FakeArchiveStore | None = None,
) -> SqlAlchemyFetchService:
    return SqlAlchemyFetchService(
        RuntimeConfig(database_url=sqlite_url(path)),
        archive_store or FakeArchiveStore(),
        hot_store,
    )


def test_fetch_start_reports_hot_selection(tmp_path: Path) -> None:
    path = tmp_path / "catalog.sqlite3"
    initialize_db(sqlite_url(path))
    hot_store = FakeHotStore()
    _seed(path, hot_store)
    service = _service(path, hot_store)

    fetch = service.create(
        name="documents",
        collections=["2025/20250102T030405Z__docs"],
    )
    started = service.start(str(fetch.id))

    assert started.state == FetchState.DONE
    assert started.files == 2
    assert started.hot_files == 2
    assert started.missing_files == 0


def test_fetch_list_aggregates_all_collection_totals_in_the_database(tmp_path: Path) -> None:
    path = tmp_path / "catalog.sqlite3"
    initialize_db(sqlite_url(path))
    hot_store = FakeHotStore()
    _seed(path, hot_store)
    factory = make_session_factory(sqlite_url(path))
    with session_scope(factory) as session:
        session.add(CollectionRecord(id="2025/20250103T030405Z__small"))
        session.add(
            CollectionFileRecord(
                collection_id="2025/20250103T030405Z__small",
                path="small.txt",
                bytes=3,
                sha256=hashlib.sha256(b"abc").hexdigest(),
                hot=False,
            )
        )
    service = _service(path, hot_store)
    service.create(
        name="documents",
        collections=["2025/20250102T030405Z__docs"],
    )
    service.create(
        name="small",
        collections=["2025/20250103T030405Z__small"],
    )

    page = service.list(
        page=2,
        per_page=1,
        sort="bytes",
        order="desc",
        all_items=True,
    )
    filtered = service.list(
        page=1,
        per_page=25,
        q="20250103T030405Z__small",
    )

    assert page.page == 1
    assert page.per_page == 2
    assert [
        (str(fetch.id), fetch.files, fetch.bytes, fetch.hot_bytes) for fetch in page.fetches
    ] == [("fx-1", 2, 10, 10), ("fx-2", 1, 3, 0)]
    assert [str(fetch.id) for fetch in filtered.fetches] == ["fx-2"]


def test_fetch_status_and_files_share_database_projections(tmp_path: Path) -> None:
    path = tmp_path / "catalog.sqlite3"
    initialize_db(sqlite_url(path))
    hot_store = FakeHotStore()
    _seed(path, hot_store)
    service = _service(path, hot_store)
    service.create(
        name="documents",
        collections=["2025/20250102T030405Z__docs"],
    )

    status = service.status("fx-1")
    page = service.files(
        "fx-1",
        page=1,
        per_page=1,
        sort="collection_path",
        order="desc",
        q=".txt",
        hot=True,
    )

    assert status["files"] == 2
    assert status["bytes"] == 10
    assert status["collection_summaries"] == [
        {
            "collection_id": "2025/20250102T030405Z__docs",
            "files": 2,
            "bytes": 10,
            "hot_files": 2,
            "hot_bytes": 10,
            "missing_files": 0,
            "missing_bytes": 0,
        }
    ]
    assert page["total"] == 2
    assert page["pages"] == 2
    assert [file["collection_path"] for file in page["files"]] == ["b.txt"]


def test_fetch_start_queues_archive_materialization_for_missing_files(tmp_path: Path) -> None:
    path = tmp_path / "catalog.sqlite3"
    initialize_db(sqlite_url(path))
    hot_store = FakeHotStore()
    _seed(path, hot_store)
    hot_store.delete_collection_file("2025/20250102T030405Z__docs", "b.txt")
    factory = make_session_factory(sqlite_url(path))
    with session_scope(factory) as session:
        session.get(
            CollectionFileRecord, {"collection_id": "2025/20250102T030405Z__docs", "path": "b.txt"}
        ).hot = False
    service = _service(path, hot_store)

    fetch = service.create(
        name="documents",
        collections=["2025/20250102T030405Z__docs"],
    )
    started = service.start(str(fetch.id))

    assert started.state == FetchState.QUEUED_ARCHIVE
    assert started.missing_files == 1


def test_fetch_start_refuses_collection_with_active_deletion(tmp_path: Path) -> None:
    path = tmp_path / "catalog.sqlite3"
    initialize_db(sqlite_url(path))
    hot_store = FakeHotStore()
    _seed(path, hot_store)
    service = _service(path, hot_store)
    fetch = service.create(
        name="documents",
        collections=["2025/20250102T030405Z__docs"],
    )
    factory = make_session_factory(sqlite_url(path))
    with session_scope(factory) as session:
        session.add(
            CollectionDeletionRecord(
                collection_id="2025/20250102T030405Z__docs",
                challenge="delete-test",
                plan_json="{}",
                started_at="2026-07-14T00:00:00Z",
            )
        )

    with pytest.raises(Conflict, match="deletion is in progress"):
        service.start(str(fetch.id))


def test_hot_eviction_requires_complete_collection_archive_upload(tmp_path: Path) -> None:
    path = tmp_path / "catalog.sqlite3"
    initialize_db(sqlite_url(path))
    hot_store = FakeHotStore()
    _seed(path, hot_store, archived=False)

    with pytest.raises(Conflict, match="archive upload is complete"):
        _service(path, hot_store).evict(["2025/20250102T030405Z__docs"])


def test_hot_eviction_updates_store_and_catalog(tmp_path: Path) -> None:
    path = tmp_path / "catalog.sqlite3"
    initialize_db(sqlite_url(path))
    hot_store = FakeHotStore()
    archive_store = FakeArchiveStore()
    _seed(path, hot_store)

    payload = _service(path, hot_store, archive_store).evict(["2025/20250102T030405Z__docs"])

    assert payload["evicted_files"] == 2
    assert archive_store.checks[0][0] == "2025/20250102T030405Z__docs"
    assert archive_store.checks[0][1].archive.sha256 == "a" * 64
    assert archive_store.checks[0][1].manifest.sha256 == "b" * 64
    assert archive_store.checks[0][1].proof.sha256 == "c" * 64
    assert set(hot_store.deleted) == {
        ("2025/20250102T030405Z__docs", "a.txt"),
        ("2025/20250102T030405Z__docs", "b.txt"),
    }
    factory = make_session_factory(sqlite_url(path))
    with session_scope(factory) as session:
        row = session.get(
            CollectionFileRecord, {"collection_id": "2025/20250102T030405Z__docs", "path": "a.txt"}
        )
        assert row is not None and row.hot is False


def test_hot_eviction_dry_run_only_previews_selected_files(tmp_path: Path) -> None:
    path = tmp_path / "catalog.sqlite3"
    initialize_db(sqlite_url(path))
    hot_store = FakeHotStore()
    archive_store = FakeArchiveStore()
    _seed(path, hot_store)

    payload = _service(path, hot_store, archive_store).evict(
        ["2025/20250102T030405Z__docs"],
        dry_run=True,
    )

    assert payload["status"] == "would_evict"
    assert payload["would_evict_files"] == 2
    assert archive_store.checks == []
    assert set(hot_store.files) == {
        ("2025/20250102T030405Z__docs", "a.txt"),
        ("2025/20250102T030405Z__docs", "b.txt"),
    }


def test_hot_eviction_keeps_hot_files_when_remote_archive_check_fails(tmp_path: Path) -> None:
    path = tmp_path / "catalog.sqlite3"
    initialize_db(sqlite_url(path))
    hot_store = FakeHotStore()
    archive_store = FakeArchiveStore()
    archive_store.failure = ArchivePackageVerificationError("manifest checksum changed")
    _seed(path, hot_store)

    with pytest.raises(Conflict, match="does not match the upload record"):
        _service(path, hot_store, archive_store).evict(["2025/20250102T030405Z__docs"])

    assert set(hot_store.files) == {
        ("2025/20250102T030405Z__docs", "a.txt"),
        ("2025/20250102T030405Z__docs", "b.txt"),
    }
    assert hot_store.deleted == []
