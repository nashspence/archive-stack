from __future__ import annotations

import hashlib
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import cast

import pytest
from fastapi.testclient import TestClient

from riverhog_api.app import create_app
from riverhog_api.deps import ServiceContainer
from riverhog_core.catalog_db import initialize_db, make_session_factory, session_scope
from riverhog_core.catalog_models import (
    CollectionArchiveRecord,
    CollectionFileRecord,
    CollectionRecord,
)
from riverhog_core.ports.archive_store import ArchiveStore
from riverhog_core.ports.hot_store import HotFileStat
from riverhog_core.ports.upload_store import UploadStore
from riverhog_core.runtime_config import RuntimeConfig
from riverhog_core.services.archive_reporting import SqlAlchemyArchiveReportingService
from riverhog_core.services.archive_restores import SqlAlchemyArchiveRestoreService
from riverhog_core.services.archive_uploads import SqlAlchemyArchiveUploadService
from riverhog_core.services.collection_deletions import SqlAlchemyCollectionDeletionService
from riverhog_core.services.collections import SqlAlchemyCollectionService
from riverhog_core.services.fetches import SqlAlchemyFetchService
from riverhog_core.services.files import SqlAlchemyFileService
from riverhog_core.services.search import SqlAlchemySearchService
from tests.fixtures.crypto import FixtureProofVerifier
from tests.unit.db_helpers import sqlite_url


class MemoryHotStore:
    def __init__(self) -> None:
        self.files: dict[tuple[str, str], bytes] = {}

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
        self.files.pop((collection_id, path), None)

    def list_collection_files(self, collection_id: str) -> list[tuple[str, int]]:
        return [
            (path, len(content))
            for (current_collection, path), content in sorted(self.files.items())
            if current_collection == collection_id
        ]


class IdleArchiveUploadService:
    def requeue_failed_uploads_for_startup(self, *, limit: int = 100) -> int:
        _ = limit
        return 0

    def publish_restore_catalog(self) -> int:
        return 0

    def process_due_uploads(self, *, limit: int = 1) -> int:
        _ = limit
        return 0


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TestClient]:
    path = tmp_path / "catalog.sqlite3"
    database_url = sqlite_url(path)
    initialize_db(database_url)
    config = RuntimeConfig(database_url=database_url)
    hot_store = MemoryHotStore()
    content = b"current archive contract\n"
    hot_store.put_collection_file("docs", "readme.txt", content)
    factory = make_session_factory(database_url)
    with session_scope(factory) as session:
        session.add(CollectionRecord(id="docs"))
        session.add(
            CollectionFileRecord(
                collection_id="docs",
                path="readme.txt",
                bytes=len(content),
                sha256=hashlib.sha256(content).hexdigest(),
                hot=True,
            )
        )
        session.add(
            CollectionArchiveRecord(
                collection_id="docs",
                state="uploaded",
                object_path="collections/docs/archive.tar.age",
                stored_bytes=100,
                sha256="a" * 64,
                manifest_object_path="collections/docs/manifest.yml",
                manifest_sha256="b" * 64,
                manifest_stored_bytes=20,
                ots_object_path="collections/docs/manifest.yml.ots",
                ots_sha256="c" * 64,
                ots_stored_bytes=10,
                last_verified_at="2026-07-14T00:00:00Z",
            )
        )

    unused = cast(object, object())
    container = ServiceContainer(
        collections=SqlAlchemyCollectionService(
            config,
            hot_store,
            cast(UploadStore, unused),
        ),
        collection_deletions=cast(SqlAlchemyCollectionDeletionService, unused),
        search=SqlAlchemySearchService(config),
        archive_uploads=cast(SqlAlchemyArchiveUploadService, IdleArchiveUploadService()),
        archive_reporting=SqlAlchemyArchiveReportingService(config),
        archive_restores=SqlAlchemyArchiveRestoreService(
            config,
            cast(ArchiveStore, unused),
            hot_store,
            proof_verifier=FixtureProofVerifier(),
        ),
        fetches=SqlAlchemyFetchService(config, hot_store),
        files=SqlAlchemyFileService(config, hot_store),
    )
    app = create_app(
        container=container,
        upload_expiry_reaper_interval=3600,
        archive_upload_reaper_interval=3600,
        archive_restore_reaper_interval=3600,
    )
    with TestClient(app) as test_client:
        yield test_client


def test_collection_search_and_archive_report_share_current_identity(
    client: TestClient,
) -> None:
    collection = client.get("/v1/collections/docs").json()
    search = client.get("/v1/search", params={"q": "readme"}).json()
    archive = client.get("/v1/archive").json()

    assert collection["id"] == "docs"
    assert collection["archive"]["state"] == "uploaded"
    assert search["files"][0]["target"] == "docs/readme.txt"
    assert archive["totals"]["uploaded_collections"] == 1
    assert archive["totals"]["measured_storage_bytes"] == 130


def test_hot_fetch_completes_when_selected_files_are_available(client: TestClient) -> None:
    created = client.post(
        "/v1/fetches",
        json={"name": "documentation", "targets": ["docs/"]},
    ).json()
    started = client.post(f"/v1/fetches/{created['id']}/start").json()
    status = client.get(f"/v1/fetches/{created['id']}/status").json()
    content = client.get("/v1/files/docs/readme.txt/content")

    assert started["state"] == "done"
    assert status["hot_files"] == 1
    assert status["missing_files"] == 0
    assert status["next_action"]["action"] == "none"
    assert content.content == b"current archive contract\n"
