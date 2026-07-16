from __future__ import annotations

from pathlib import Path

from riverhog_core.archive_store_registry import ArchiveStoreRegistry
from riverhog_core.catalog_db import make_session_factory, session_scope
from riverhog_core.catalog_models import CollectionFileRecord, FetchFileRecord
from riverhog_core.services.fetches import SqlAlchemyFetchService
from tests.unit.archive_object_fixtures import (
    COLLECTION_ID,
    MemoryArchiveStore,
    MemoryHotStore,
    as_archive_store,
    as_hot_store,
    seed_archive_copy,
)

FILES = {"one.txt": b"first file\n", "two.txt": b"second file\n"}


def _service(path: Path, *, hot: bool):
    config, archive = seed_archive_copy(path, FILES, hot=hot)
    archive_store = MemoryArchiveStore(archive)
    hot_store = MemoryHotStore(
        {(COLLECTION_ID, name): content for name, content in FILES.items()} if hot else None
    )
    service = SqlAlchemyFetchService(
        config,
        ArchiveStoreRegistry({"deep": as_archive_store(archive_store)}),
        as_hot_store(hot_store),
    )
    return config, archive_store, hot_store, service


def test_fetch_selection_is_persisted_as_files_and_aggregated_in_sql(tmp_path: Path) -> None:
    config, _archive_store, _hot_store, service = _service(tmp_path / "catalog.sqlite3", hot=False)

    selected = service.create(
        name="one file",
        files=((COLLECTION_ID, "one.txt"),),
    )
    expanded = service.add_collections(selected.id, (COLLECTION_ID,))

    assert selected.files == 1
    assert selected.bytes == len(FILES["one.txt"])
    assert expanded.files == 2
    assert expanded.missing_bytes == sum(map(len, FILES.values()))
    with session_scope(make_session_factory(config.database_url)) as session:
        rows = session.query(FetchFileRecord).order_by(FetchFileRecord.file_order).all()
        assert [(row.path, row.file_order) for row in rows] == [
            ("one.txt", 1),
            ("two.txt", 2),
        ]


def test_file_eviction_verifies_exact_archive_coverage_then_removes_one_hot_file(
    tmp_path: Path,
) -> None:
    config, archive_store, hot_store, service = _service(tmp_path / "catalog.sqlite3", hot=True)

    result = service.evict(files=((COLLECTION_ID, "one.txt"),))

    assert result["selected_files"] == 1
    assert result["evicted_files"] == 1
    assert archive_store.verified == [("data-000000", "manifest", "proof")]
    assert hot_store.deleted == [(COLLECTION_ID, "one.txt")]
    assert (COLLECTION_ID, "two.txt") in hot_store.files
    with session_scope(make_session_factory(config.database_url)) as session:
        one = session.get(CollectionFileRecord, (COLLECTION_ID, "one.txt"))
        two = session.get(CollectionFileRecord, (COLLECTION_ID, "two.txt"))
        assert one is not None and one.hot is False
        assert two is not None and two.hot is True


def test_file_eviction_dry_run_uses_database_counts_without_mutation(tmp_path: Path) -> None:
    config, archive_store, hot_store, service = _service(tmp_path / "catalog.sqlite3", hot=True)

    result = service.evict(collections=(COLLECTION_ID,), dry_run=True)

    assert result["selected_files"] == 2
    assert result["would_evict_files"] == 2
    assert archive_store.verified == []
    assert hot_store.deleted == []
    with session_scope(make_session_factory(config.database_url)) as session:
        assert session.query(CollectionFileRecord).filter_by(hot=True).count() == 2
