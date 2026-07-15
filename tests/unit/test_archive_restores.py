from __future__ import annotations

import hashlib
from collections.abc import Iterable, Iterator
from dataclasses import replace
from pathlib import Path

import pytest

from riverhog_core.archive_store_registry import ArchiveStoreRegistry
from riverhog_core.catalog_db import initialize_db, make_session_factory, session_scope
from riverhog_core.catalog_models import (
    CollectionArchiveCopyRecord,
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
from riverhog_core.ports.archive_store import ArchiveReadStatus
from riverhog_core.ports.hot_store import HotCollectionFile, HotCollectionListing, HotFileStat
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
        self.prepared = 0
        self.cleaned = 0

    def prepare_collection_archive_read(self, **_: object) -> ArchiveReadStatus:
        self.prepared += 1
        return ArchiveReadStatus(state="ready" if self.ready else "requested")

    def get_collection_archive_read_status(self, **_: object) -> ArchiveReadStatus:
        return ArchiveReadStatus(state="ready" if self.ready else "requested")

    def iter_collection_archive(self, **_: object) -> Iterator[bytes]:
        yield self.package.archive_bytes

    def read_collection_manifest(self, **_: object) -> bytes:
        return self.package.manifest_bytes

    def read_collection_manifest_proof(self, **_: object) -> bytes:
        return self.package.proof_bytes

    def cleanup_collection_archive_read(self, **_: object) -> None:
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
            CollectionArchiveCopyRecord(
                collection_id="2025/20250102T030405Z__docs",
                store="deep",
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
        ArchiveStoreRegistry({"deep": archive_store}, default_store="deep"),
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


def test_archive_restore_uses_the_first_available_store_in_read_order(tmp_path: Path) -> None:
    path = tmp_path / "catalog.sqlite3"
    initialize_db(sqlite_url(path))
    deep_store = FakeArchiveStore()
    b2_store = FakeArchiveStore()
    hot_store = FakeHotStore()
    _seed(path, deep_store)
    factory = make_session_factory(sqlite_url(path))
    with session_scope(factory) as session:
        deep_copy = session.get(
            CollectionArchiveCopyRecord,
            ("2025/20250102T030405Z__docs", "deep"),
        )
        assert deep_copy is not None
        session.add(
            CollectionArchiveCopyRecord(
                collection_id=deep_copy.collection_id,
                store="b2",
                state=deep_copy.state,
                object_path="collections/docs-b2/archive.tar.age",
                stored_bytes=deep_copy.stored_bytes,
                sha256=deep_copy.sha256,
                manifest_object_path="collections/docs-b2/manifest.yml",
                manifest_sha256=deep_copy.manifest_sha256,
                ots_object_path="collections/docs-b2/manifest.yml.ots",
                ots_sha256=deep_copy.ots_sha256,
                last_verified_at=deep_copy.last_verified_at,
            )
        )
    config = RuntimeConfig(database_url=sqlite_url(path))
    b2_config = replace(
        config.archive_store("deep"),
        name="b2",
        backend="b2",
        storage_class="STANDARD",
    )
    config = replace(
        config,
        archive_read_order=("b2", "deep"),
        archive_stores={"deep": config.archive_store("deep"), "b2": b2_config},
    )
    service = SqlAlchemyArchiveRestoreService(
        config,
        ArchiveStoreRegistry(
            {"deep": deep_store, "b2": b2_store},
            default_store="deep",
        ),
        hot_store,
        proof_verifier=FixtureProofVerifier(),
    )

    summary = service.create_or_resume_for_collection("2025/20250102T030405Z__docs")

    assert summary.collections[0].archive_copy.store == "b2"
    assert b2_store.prepared == 1
    assert b2_store.cleaned == 1
    assert deep_store.prepared == 0


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
