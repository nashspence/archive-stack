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
    FetchCollectionRecord,
    FetchRecord,
)
from riverhog_core.collection_archives import (
    CollectionArchiveFile,
    build_collection_archive_package,
)
from riverhog_core.domain.enums import ArchiveRestoreState
from riverhog_core.domain.errors import Conflict
from riverhog_core.ports.archive_store import ArchiveRestoreStatus
from riverhog_core.ports.hot_store import HotFileStat
from riverhog_core.runtime_config import RuntimeConfig
from riverhog_core.services.archive_restores import SqlAlchemyArchiveRestoreService
from tests.fixtures.crypto import FixtureProofStamper, FixtureProofVerifier
from tests.unit.db_helpers import sqlite_url


class FakeHotStore:
    def __init__(self) -> None:
        self.files: dict[tuple[str, str], bytes] = {}

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

    def stat_collection_file(self, collection_id: str, path: str) -> HotFileStat | None:
        content = self.files.get((collection_id, path))
        if content is None:
            return None
        return HotFileStat(bytes=len(content), sha256=hashlib.sha256(content).hexdigest())

    def list_collection_files(self, collection_id: str) -> list[tuple[str, int]]:
        return [
            (path, len(content))
            for (current_collection, path), content in sorted(self.files.items())
            if current_collection == collection_id
        ]


class FakeArchiveStore:
    def __init__(self, *, ready: bool = True) -> None:
        content = b"archived document"
        self.package = build_collection_archive_package(
            collection_id="2025/20250102T030405Z__docs",
            files=(
                CollectionArchiveFile(
                    path="document.txt",
                    content=content,
                    sha256=hashlib.sha256(content).hexdigest(),
                ),
            ),
            stamper=FixtureProofStamper(),
        )
        self.ready = ready
        self.cleaned = 0

    def request_collection_archive_restore(self, **_: object) -> ArchiveRestoreStatus:
        return ArchiveRestoreStatus(state="ready" if self.ready else "requested")

    def get_collection_archive_restore_status(self, **_: object) -> ArchiveRestoreStatus:
        return ArchiveRestoreStatus(state="ready" if self.ready else "requested")

    def iter_restored_collection_archive(self, **_: object) -> Iterator[bytes]:
        yield self.package.archive_bytes

    def read_restored_collection_manifest(self, **_: object) -> bytes:
        return self.package.manifest_bytes

    def read_restored_collection_manifest_proof(self, **_: object) -> bytes:
        return self.package.proof_bytes

    def cleanup_collection_archive_restore(self, **_: object) -> None:
        self.cleaned += 1


def _seed(path: Path, store: FakeArchiveStore) -> None:
    content = b"archived document"
    factory = make_session_factory(sqlite_url(path))
    with session_scope(factory) as session:
        session.add(CollectionRecord(id="2025/20250102T030405Z__docs"))
        session.add(
            CollectionFileRecord(
                collection_id="2025/20250102T030405Z__docs",
                path="document.txt",
                bytes=len(content),
                sha256=hashlib.sha256(content).hexdigest(),
                hot=False,
            )
        )
        session.add(
            CollectionArchiveRecord(
                collection_id="2025/20250102T030405Z__docs",
                state="uploaded",
                object_path="collections/docs/archive.tar.age",
                stored_bytes=len(store.package.archive_bytes),
                sha256=store.package.archive_sha256,
                manifest_object_path="collections/docs/manifest.yml",
                manifest_sha256=store.package.manifest_sha256,
                ots_object_path="collections/docs/manifest.yml.ots",
                ots_sha256=store.package.proof_sha256,
                last_verified_at="2026-07-14T00:00:00Z",
            )
        )


def _service(
    path: Path,
    archive_store: FakeArchiveStore,
    hot_store: FakeHotStore,
) -> SqlAlchemyArchiveRestoreService:
    return SqlAlchemyArchiveRestoreService(
        RuntimeConfig(database_url=sqlite_url(path)),
        archive_store,
        hot_store,
        proof_verifier=FixtureProofVerifier(),
    )


def test_archive_restore_verifies_and_materializes_collection(tmp_path: Path) -> None:
    path = tmp_path / "catalog.sqlite3"
    initialize_db(sqlite_url(path))
    archive_store = FakeArchiveStore()
    hot_store = FakeHotStore()
    _seed(path, archive_store)

    summary = _service(path, archive_store, hot_store).create_or_resume_for_collection(
        "2025/20250102T030405Z__docs"
    )

    assert summary.state == ArchiveRestoreState.COMPLETED
    assert summary.progress.archive_verification == "completed"
    assert summary.progress.materialization == "completed"
    assert hot_store.files[("2025/20250102T030405Z__docs", "document.txt")] == b"archived document"
    assert archive_store.cleaned == 1


def test_archive_restore_can_be_canceled_while_retrieval_is_pending(tmp_path: Path) -> None:
    path = tmp_path / "catalog.sqlite3"
    initialize_db(sqlite_url(path))
    archive_store = FakeArchiveStore(ready=False)
    hot_store = FakeHotStore()
    _seed(path, archive_store)
    service = _service(path, archive_store, hot_store)

    pending = service.create_or_resume_for_collection("2025/20250102T030405Z__docs")
    canceled = service.cancel(pending.id)

    assert pending.state == ArchiveRestoreState.REQUESTED
    assert canceled.state == ArchiveRestoreState.CANCELED
    assert archive_store.cleaned == 1


def test_archive_restore_refuses_collection_with_active_deletion(tmp_path: Path) -> None:
    path = tmp_path / "catalog.sqlite3"
    initialize_db(sqlite_url(path))
    archive_store = FakeArchiveStore()
    hot_store = FakeHotStore()
    _seed(path, archive_store)
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
        _service(path, archive_store, hot_store).create_or_resume_for_collection(
            "2025/20250102T030405Z__docs"
        )


def test_archive_restore_list_for_fetch_is_database_paginated(tmp_path: Path) -> None:
    path = tmp_path / "catalog.sqlite3"
    initialize_db(sqlite_url(path))
    archive_store = FakeArchiveStore()
    hot_store = FakeHotStore()
    _seed(path, archive_store)
    service = _service(path, archive_store, hot_store)
    restored = service.create_or_resume_for_collection("2025/20250102T030405Z__docs")
    factory = make_session_factory(sqlite_url(path))
    with session_scope(factory) as session:
        session.add(
            FetchRecord(
                fetch_id="fx-1",
                name="documents",
                fetch_order=1,
                fetch_state="done",
            )
        )
        session.add(
            FetchCollectionRecord(
                fetch_id="fx-1",
                collection_id="2025/20250102T030405Z__docs",
                collection_order=1,
            )
        )

    page = service.list_for_fetch(
        "fx-1",
        page=1,
        per_page=1,
        sort="created_at",
        order="desc",
    )

    assert page.total == 1
    assert page.pages == 1
    assert [item.id for item in page.restores] == [restored.id]
