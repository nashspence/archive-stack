from __future__ import annotations

from pathlib import Path

from riverhog_core.archive_store_registry import ArchiveStoreRegistry
from riverhog_core.catalog_db import make_session_factory, session_scope
from riverhog_core.catalog_models import (
    ArchiveRestoreFileRecord,
    ArchiveRestoreObjectRecord,
    ArchiveRestoreRecord,
    CollectionFileRecord,
    FetchFileRecord,
    FetchRecord,
)
from riverhog_core.services.archive_restores import SqlAlchemyArchiveRestoreService
from tests.fixtures.crypto import FixtureProofVerifier
from tests.unit.archive_object_fixtures import (
    COLLECTION_ID,
    MemoryArchiveStore,
    MemoryHotStore,
    as_archive_store,
    as_hot_store,
    seed_archive_copy,
)

FILES = {"one.txt": b"first file\n", "two.txt": b"second file\n"}


def _service(path: Path, *, ready: bool = True):
    config, archive = seed_archive_copy(path, FILES, hot=False)
    archive_store = MemoryArchiveStore(archive, ready=ready)
    hot_store = MemoryHotStore()
    service = SqlAlchemyArchiveRestoreService(
        config,
        ArchiveStoreRegistry({"deep": as_archive_store(archive_store)}, default_store="deep"),
        as_hot_store(hot_store),
        proof_verifier=FixtureProofVerifier(),
    )
    return config, archive_store, hot_store, service


def _seed_fetch(config, path: str = "one.txt") -> None:
    with session_scope(make_session_factory(config.database_url)) as session:
        session.add(
            FetchRecord(
                fetch_id="fx-1",
                name="selected",
                fetch_order=1,
                fetch_state="queued_archive",
            )
        )
        session.add(
            FetchFileRecord(
                fetch_id="fx-1",
                collection_id=COLLECTION_ID,
                path=path,
                file_order=1,
            )
        )


def _seed_second_fetch(config, path: str = "two.txt") -> None:
    with session_scope(make_session_factory(config.database_url)) as session:
        session.add(
            FetchRecord(
                fetch_id="fx-2",
                name="other selection",
                fetch_order=2,
                fetch_state="queued_archive",
            )
        )
        session.add(
            FetchFileRecord(
                fetch_id="fx-2",
                collection_id=COLLECTION_ID,
                path=path,
                file_order=1,
            )
        )


def test_restore_fetch_materializes_only_the_selected_file(tmp_path: Path) -> None:
    config, archive_store, hot_store, service = _service(tmp_path / "catalog.sqlite3")
    _seed_fetch(config)

    page = service.create_or_resume_for_fetch("fx-1")

    assert page.total == 1
    assert page.restores[0].state.value == "completed"
    assert hot_store.files == {(COLLECTION_ID, "one.txt"): FILES["one.txt"]}
    assert archive_store.prepared == [("data-000000",)]
    assert archive_store.read == ["manifest", "proof", "data-000000"]
    with session_scope(make_session_factory(config.database_url)) as session:
        one = session.get(CollectionFileRecord, (COLLECTION_ID, "one.txt"))
        two = session.get(CollectionFileRecord, (COLLECTION_ID, "two.txt"))
        assert one is not None and one.hot is True
        assert two is not None and two.hot is False
        restore = session.query(ArchiveRestoreRecord).one()
        files = session.query(ArchiveRestoreFileRecord).filter_by(restore_id=restore.restore_id)
        objects = session.query(ArchiveRestoreObjectRecord).filter_by(restore_id=restore.restore_id)
        assert [row.path for row in files] == ["one.txt"]
        assert [row.object_id for row in objects] == ["data-000000"]


def test_restore_waits_on_only_the_objects_mapped_to_selected_files(tmp_path: Path) -> None:
    config, archive_store, hot_store, service = _service(tmp_path / "catalog.sqlite3", ready=False)
    _seed_fetch(config, "two.txt")

    page = service.create_or_resume_for_fetch("fx-1")

    assert page.restores[0].state.value == "requested"
    assert archive_store.prepared == [("data-000000",)]
    assert hot_store.files == {}


def test_concurrent_fetches_keep_distinct_file_selections(tmp_path: Path) -> None:
    config, _archive_store, _hot_store, service = _service(
        tmp_path / "catalog.sqlite3", ready=False
    )
    _seed_fetch(config, "one.txt")
    _seed_second_fetch(config, "two.txt")

    service.create_or_resume_for_fetch("fx-1")
    service.create_or_resume_for_fetch("fx-2")

    with session_scope(make_session_factory(config.database_url)) as session:
        restores = session.query(ArchiveRestoreRecord).order_by(ArchiveRestoreRecord.restore_id)
        assert restores.count() == 2
        assert sorted(
            (row.restore_id, row.path) for row in session.query(ArchiveRestoreFileRecord).all()
        ) == sorted(
            (
                (restores[0].restore_id, "one.txt"),
                (restores[1].restore_id, "two.txt"),
            )
        )
