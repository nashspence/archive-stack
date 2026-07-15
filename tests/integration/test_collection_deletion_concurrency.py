from __future__ import annotations

import hashlib
import os
import threading
from collections.abc import Callable, Iterator
from typing import Any, cast

import pytest
from sqlalchemy import event, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from riverhog_core.catalog_db import (
    Base,
    create_catalog_engine,
    initialize_db,
    make_session_factory,
    session_scope,
)
from riverhog_core.catalog_models import (
    ArchiveRestoreRecord,
    CollectionArchiveRecord,
    CollectionDeletionRecord,
    CollectionFileRecord,
    CollectionRecord,
    FetchRecord,
)
from riverhog_core.domain.enums import FetchState
from riverhog_core.domain.errors import Conflict
from riverhog_core.ports.archive_store import ArchiveRestoreStatus
from riverhog_core.runtime_config import RuntimeConfig
from riverhog_core.services.archive_restores import SqlAlchemyArchiveRestoreService
from riverhog_core.services.collection_deletions import SqlAlchemyCollectionDeletionService
from riverhog_core.services.fetches import SqlAlchemyFetchService
from tests.fixtures.crypto import FixtureProofVerifier

pytestmark = pytest.mark.integration

_WAIT_SECONDS = 10.0


class BlockingHotStore:
    def __init__(self, *, hot: bool) -> None:
        self.files = (
            {("2025/20250102T030405Z__docs", "document.txt"): b"archived document"} if hot else {}
        )
        self.delete_started = threading.Event()
        self.allow_delete = threading.Event()

    def list_collection_files(self, collection_id: str) -> list[tuple[str, int]]:
        return [
            (path, len(content))
            for (current_collection, path), content in sorted(self.files.items())
            if current_collection == collection_id
        ]

    def delete_collection_file(self, collection_id: str, path: str) -> None:
        self.delete_started.set()
        if not self.allow_delete.wait(_WAIT_SECONDS):
            raise RuntimeError("timed out waiting to finish the synthetic hot-object deletion")
        self.files.pop((collection_id, path), None)


class FakeArchiveStore:
    def __init__(self) -> None:
        self.objects = {
            "archive/archives/opaque-docs/archive.tar.age",
            "archive/archives/opaque-docs/manifest.yml.age",
            "archive/archives/opaque-docs/manifest.yml.ots.age",
        }
        self.catalog_entries: list[dict[str, object]] | None = None

    def delete_collection_archive_package(
        self,
        *,
        collection_id: str,
        object_path: str,
        manifest_object_path: str,
        proof_object_path: str,
    ) -> None:
        assert collection_id == "2025/20250102T030405Z__docs"
        for path in (object_path, manifest_object_path, proof_object_path):
            self.objects.discard(path)

    def publish_restore_catalog(
        self,
        *,
        entries: list[dict[str, object]],
        generated_at: str,
    ) -> None:
        assert generated_at.endswith("Z")
        self.catalog_entries = entries

    def request_collection_archive_restore(self, **_: object) -> ArchiveRestoreStatus:
        return ArchiveRestoreStatus(state="requested")

    def get_collection_archive_restore_status(self, **_: object) -> ArchiveRestoreStatus:
        return ArchiveRestoreStatus(state="requested")


class FakeUploadStore:
    def cancel_upload(self, tus_url: str) -> None:
        raise AssertionError(tus_url)

    def delete_target(self, target_path: str) -> None:
        raise AssertionError(target_path)


@pytest.fixture
def database_url() -> Iterator[str]:
    value = os.getenv("RIVERHOG_TEST_POSTGRES_URL", "").strip()
    if not value:
        pytest.skip("RIVERHOG_TEST_POSTGRES_URL is required")
    engine = create_catalog_engine(value)
    Base.metadata.drop_all(engine)
    engine.dispose()
    initialize_db(value)
    try:
        yield value
    finally:
        engine = create_catalog_engine(value)
        Base.metadata.drop_all(engine)
        engine.dispose()


def _seed(database_url: str, *, hot: bool) -> None:
    content = b"archived document"
    factory = make_session_factory(database_url)
    with session_scope(factory) as session:
        session.add(CollectionRecord(id="2025/20250102T030405Z__docs"))
        session.add(
            CollectionFileRecord(
                collection_id="2025/20250102T030405Z__docs",
                path="document.txt",
                bytes=len(content),
                sha256=hashlib.sha256(content).hexdigest(),
                hot=hot,
            )
        )
        session.add(
            CollectionArchiveRecord(
                collection_id="2025/20250102T030405Z__docs",
                state="uploaded",
                archive_storage_prefix="archive/archives/opaque-docs",
                object_path="archive/archives/opaque-docs/archive.tar.age",
                stored_bytes=100,
                sha256="a" * 64,
                manifest_object_path="archive/archives/opaque-docs/manifest.yml.age",
                manifest_sha256="b" * 64,
                manifest_stored_bytes=20,
                ots_object_path="archive/archives/opaque-docs/manifest.yml.ots.age",
                ots_sha256="c" * 64,
                ots_stored_bytes=10,
                last_verified_at="2026-07-14T00:00:00Z",
            )
        )


def _services(
    database_url: str,
    *,
    hot: bool,
) -> tuple[
    SqlAlchemyCollectionDeletionService,
    SqlAlchemyFetchService,
    SqlAlchemyArchiveRestoreService,
    BlockingHotStore,
]:
    _seed(database_url, hot=hot)
    config = RuntimeConfig(database_url=database_url)
    hot_store = BlockingHotStore(hot=hot)
    archive_store = FakeArchiveStore()
    return (
        SqlAlchemyCollectionDeletionService(
            config,
            cast(Any, archive_store),
            cast(Any, hot_store),
            cast(Any, FakeUploadStore()),
        ),
        SqlAlchemyFetchService(
            config,
            cast(Any, archive_store),
            cast(Any, hot_store),
        ),
        SqlAlchemyArchiveRestoreService(
            config,
            cast(Any, archive_store),
            cast(Any, hot_store),
            proof_verifier=FixtureProofVerifier(),
        ),
        hot_store,
    )


def _thread_call(
    call: Callable[[], object],
    *,
    results: list[object],
    errors: list[BaseException],
    done: threading.Event,
) -> None:
    try:
        results.append(call())
    except BaseException as exc:
        errors.append(exc)
    finally:
        done.set()


def _join_started(thread: threading.Thread) -> None:
    if thread.ident is not None:
        thread.join(_WAIT_SECONDS)


def _service_engine(service: object) -> Engine:
    factory = cast(Any, service)._session_factory
    return cast(Engine, factory.kw["bind"])


def _observe_for_update(attempted: threading.Event) -> Callable[..., None]:
    def observe(
        _: object,
        __: object,
        statement: str,
        ___: object,
        ____: object,
        _____: object,
    ) -> None:
        if "FOR UPDATE" in statement.upper():
            attempted.set()

    return observe


def _start_work(
    kind: str,
    *,
    fetch_service: SqlAlchemyFetchService,
    restore_service: SqlAlchemyArchiveRestoreService,
    fetch_id: str | None,
) -> object:
    if kind == "fetch":
        assert fetch_id is not None
        return fetch_service.start(fetch_id)
    assert kind == "restore"
    return restore_service.create_or_resume_for_collection("2025/20250102T030405Z__docs")


@pytest.mark.parametrize("work_kind", ["fetch", "restore"])
def test_deletion_lock_blocks_new_collection_work(
    database_url: str,
    work_kind: str,
) -> None:
    deletion, fetches, restores, hot_store = _services(database_url, hot=True)
    fetch_id = (
        str(
            fetches.create(
                name="documents",
                collections=["2025/20250102T030405Z__docs"],
            ).id
        )
        if work_kind == "fetch"
        else None
    )
    challenge = str(deletion.plan("2025/20250102T030405Z__docs")["challenge"])
    marker_flushed = threading.Event()
    allow_marker_commit = threading.Event()
    work_lock_attempted = threading.Event()
    deletion_done = threading.Event()
    work_done = threading.Event()
    deletion_results: list[object] = []
    deletion_errors: list[BaseException] = []
    work_results: list[object] = []
    work_errors: list[BaseException] = []

    def block_marker_commit(session: Session, _: object) -> None:
        if not any(isinstance(record, CollectionDeletionRecord) for record in session.new):
            return
        marker_flushed.set()
        if not allow_marker_commit.wait(_WAIT_SECONDS):
            raise RuntimeError("timed out waiting to commit the collection deletion marker")

    work_service = fetches if work_kind == "fetch" else restores
    observe_work_lock = _observe_for_update(work_lock_attempted)
    event.listen(Session, "after_flush", block_marker_commit)
    event.listen(_service_engine(work_service), "before_cursor_execute", observe_work_lock)
    deletion_thread = threading.Thread(
        target=_thread_call,
        args=(lambda: deletion.delete("2025/20250102T030405Z__docs", challenge=challenge),),
        kwargs={
            "results": deletion_results,
            "errors": deletion_errors,
            "done": deletion_done,
        },
    )
    work_thread = threading.Thread(
        target=_thread_call,
        args=(
            lambda: _start_work(
                work_kind,
                fetch_service=fetches,
                restore_service=restores,
                fetch_id=fetch_id,
            ),
        ),
        kwargs={"results": work_results, "errors": work_errors, "done": work_done},
    )
    try:
        deletion_thread.start()
        assert marker_flushed.wait(_WAIT_SECONDS)
        work_thread.start()
        assert work_lock_attempted.wait(_WAIT_SECONDS)
        assert not work_done.wait(0.1)
        allow_marker_commit.set()
        assert hot_store.delete_started.wait(_WAIT_SECONDS)
        assert work_done.wait(_WAIT_SECONDS)
        assert work_results == []
        assert len(work_errors) == 1
        assert isinstance(work_errors[0], Conflict)
        assert "deletion is in progress" in str(work_errors[0])
    finally:
        allow_marker_commit.set()
        hot_store.allow_delete.set()
        _join_started(deletion_thread)
        _join_started(work_thread)
        event.remove(Session, "after_flush", block_marker_commit)
        event.remove(_service_engine(work_service), "before_cursor_execute", observe_work_lock)

    assert not deletion_thread.is_alive()
    assert not work_thread.is_alive()
    assert deletion_errors == []
    assert deletion_results
    assert cast(dict[str, object], deletion_results[0])["status"] == "deleted"
    factory = make_session_factory(database_url)
    with session_scope(factory) as session:
        assert session.get(CollectionRecord, "2025/20250102T030405Z__docs") is None
        assert session.get(CollectionDeletionRecord, "2025/20250102T030405Z__docs") is None
        if fetch_id is not None:
            fetch = session.get(FetchRecord, fetch_id)
            assert fetch is not None and fetch.fetch_state == FetchState.DRAFT.value
        assert session.scalar(select(ArchiveRestoreRecord)) is None


@pytest.mark.parametrize("work_kind", ["fetch", "restore"])
def test_active_collection_work_prevents_deletion_from_beginning(
    database_url: str,
    work_kind: str,
) -> None:
    deletion, fetches, restores, hot_store = _services(database_url, hot=False)
    fetch_id = (
        str(
            fetches.create(
                name="documents",
                collections=["2025/20250102T030405Z__docs"],
            ).id
        )
        if work_kind == "fetch"
        else None
    )
    challenge = str(deletion.plan("2025/20250102T030405Z__docs")["challenge"])
    work_flushed = threading.Event()
    allow_work_commit = threading.Event()
    deletion_lock_attempted = threading.Event()
    work_done = threading.Event()
    deletion_done = threading.Event()
    work_results: list[object] = []
    work_errors: list[BaseException] = []
    deletion_results: list[object] = []
    deletion_errors: list[BaseException] = []

    def block_work_commit(session: Session, _: object) -> None:
        fetch_started = any(
            isinstance(record, FetchRecord)
            and record.fetch_state == FetchState.QUEUED_ARCHIVE.value
            for record in session.dirty
        )
        restore_started = any(isinstance(record, ArchiveRestoreRecord) for record in session.new)
        if not (fetch_started or restore_started):
            return
        work_flushed.set()
        if not allow_work_commit.wait(_WAIT_SECONDS):
            raise RuntimeError("timed out waiting to commit the collection work")

    observe_deletion_lock = _observe_for_update(deletion_lock_attempted)
    event.listen(Session, "after_flush", block_work_commit)
    event.listen(_service_engine(deletion), "before_cursor_execute", observe_deletion_lock)
    work_thread = threading.Thread(
        target=_thread_call,
        args=(
            lambda: _start_work(
                work_kind,
                fetch_service=fetches,
                restore_service=restores,
                fetch_id=fetch_id,
            ),
        ),
        kwargs={"results": work_results, "errors": work_errors, "done": work_done},
    )
    deletion_thread = threading.Thread(
        target=_thread_call,
        args=(lambda: deletion.delete("2025/20250102T030405Z__docs", challenge=challenge),),
        kwargs={
            "results": deletion_results,
            "errors": deletion_errors,
            "done": deletion_done,
        },
    )
    try:
        work_thread.start()
        assert work_flushed.wait(_WAIT_SECONDS)
        deletion_thread.start()
        assert deletion_lock_attempted.wait(_WAIT_SECONDS)
        assert not deletion_done.wait(0.1)
        allow_work_commit.set()
        assert work_done.wait(_WAIT_SECONDS)
        assert deletion_done.wait(_WAIT_SECONDS)
    finally:
        allow_work_commit.set()
        hot_store.allow_delete.set()
        _join_started(work_thread)
        _join_started(deletion_thread)
        event.remove(Session, "after_flush", block_work_commit)
        event.remove(_service_engine(deletion), "before_cursor_execute", observe_deletion_lock)

    assert not work_thread.is_alive()
    assert not deletion_thread.is_alive()
    assert work_errors == []
    assert len(work_results) == 1
    assert deletion_results == []
    assert len(deletion_errors) == 1
    assert isinstance(deletion_errors[0], Conflict)
    assert "plan changed" in str(deletion_errors[0])
    factory = make_session_factory(database_url)
    with session_scope(factory) as session:
        assert session.get(CollectionRecord, "2025/20250102T030405Z__docs") is not None
        assert session.get(CollectionDeletionRecord, "2025/20250102T030405Z__docs") is None
        if fetch_id is not None:
            fetch = session.get(FetchRecord, fetch_id)
            assert fetch is not None and fetch.fetch_state == FetchState.QUEUED_ARCHIVE.value
        else:
            restore = session.scalar(select(ArchiveRestoreRecord))
            assert restore is not None and restore.state == "requested"
