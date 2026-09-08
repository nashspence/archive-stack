from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

import pytest
from riverhog_core.app_permissions import CATALOG_READ, ApplicationAccess, ApplicationPrincipal
from riverhog_core.archive_store_registry import ArchiveStoreRegistry
from riverhog_core.catalog_db import make_session_factory, session_scope
from riverhog_core.catalog_events import record_catalog_event
from riverhog_core.catalog_models import CollectionRecord
from riverhog_core.services.archive_stores import SqlAlchemyArchiveStoreService
from riverhog_core.services.catalog_sync import SqlAlchemyCatalogSyncService
from riverhog_core.services.collections import SqlAlchemyCollectionService
from riverhog_core.services.retrieval import SqlAlchemyRetrievalService
from riverhog_core.services.search import SqlAlchemySearchService

from tests.unit.archive_object_fixtures import (
    COLLECTION_ID,
    MemoryArchiveStore,
    archive_store_binding,
    seed_archive_copy,
)


@dataclass(frozen=True)
class Harness:
    collections: SqlAlchemyCollectionService
    search: SqlAlchemySearchService
    archive_stores: SqlAlchemyArchiveStoreService
    catalog_sync: SqlAlchemyCatalogSyncService
    retrieval: SqlAlchemyRetrievalService


@pytest.fixture
def harness(tmp_path: Path) -> Harness:
    content = b"current archive contract\n"
    config, archive = seed_archive_copy(
        tmp_path / "catalog.sqlite3",
        {"readme.txt": content},
    )
    factory = make_session_factory(config.database_url)
    with session_scope(factory) as session:
        collection = session.get(CollectionRecord, COLLECTION_ID)
        assert collection is not None
        record_catalog_event(
            session,
            change="created",
            collection_id=COLLECTION_ID,
            occurred_at="2026-07-18T00:00:00.000000Z",
            inventory_identity=collection.inventory_identity,
            before_tags=(),
            after_tags=(),
        )
    memory_store = MemoryArchiveStore(archive)
    archive_stores = ArchiveStoreRegistry({"deep": archive_store_binding(memory_store)})
    return Harness(
        collections=SqlAlchemyCollectionService(config),
        search=SqlAlchemySearchService(config),
        archive_stores=SqlAlchemyArchiveStoreService(config, archive_stores),
        catalog_sync=SqlAlchemyCatalogSyncService(config),
        retrieval=SqlAlchemyRetrievalService(
            config,
            archive_stores,
            None,
        ),
    )


def test_catalog_search_and_archive_store_share_current_identity(harness: Harness) -> None:
    collection = harness.collections.get(COLLECTION_ID)
    copies = harness.collections.list_archive_copies(
        COLLECTION_ID,
        page_size=25,
        position=None,
    )
    search = harness.search.search(
        q="readme",
        page_size=25,
        position=None,
        sort="file_ref",
        order="asc",
    )
    archive = harness.archive_stores.get("deep")
    principal = ApplicationPrincipal(
        app="local",
        key_id="local-key",
        access=frozenset({ApplicationAccess(CATALOG_READ)}),
    )
    checkpoint = harness.catalog_sync.checkpoint(principal=principal)
    catalog = harness.catalog_sync.collections(
        cursor=checkpoint.catalog_cursor,
        limit=100,
        principal=principal,
    )

    assert collection.id == COLLECTION_ID
    assert collection.archive_copy_count == 1
    copy_rows = copies["copies"]
    assert isinstance(copy_rows, list)
    assert [(copy["store"], copy["state"]) for copy in copy_rows] == [("deep", "uploaded")]
    assert search["files"][0]["file_ref"] == f"{COLLECTION_ID}/readme.txt"
    assert archive.collections == 1
    assert [item.collection_id for item in catalog.collections] == [COLLECTION_ID]


def test_application_retrieves_one_manifest_selected_file(harness: Harness) -> None:
    header, files, etag, file_count, file_bytes = harness.retrieval.collection_inventory(
        COLLECTION_ID
    )
    assert header.collection == COLLECTION_ID
    assert file_count == 1
    assert file_bytes == len(b"current archive contract\n")
    assert [(item.path, item.bytes, item.sha256) for item in files] == [
        (
            "readme.txt",
            len(b"current archive contract\n"),
            hashlib.sha256(b"current archive contract\n").hexdigest(),
        )
    ]
    checkpoint = harness.catalog_sync.checkpoint(
        principal=ApplicationPrincipal(
            app="local",
            key_id="local-key",
            access=frozenset({ApplicationAccess(CATALOG_READ)}),
        )
    )
    catalog = harness.catalog_sync.collections(
        cursor=checkpoint.catalog_cursor,
        limit=100,
        principal=ApplicationPrincipal(
            app="local",
            key_id="local-key",
            access=frozenset({ApplicationAccess(CATALOG_READ)}),
        ),
    )
    assert catalog.collections[0].content_identity == header.content_identity
    assert len(etag) == 64

    files = [(COLLECTION_ID, "readme.txt")]
    plan = harness.retrieval.plan(files)
    job = harness.retrieval.create(
        app="local",
        plan_id=str(plan["id"]),
        plan_etag=str(plan["etag"]),
    )
    chunks, byte_count, sha256 = harness.retrieval.content(
        app="local",
        job_id=str(job["id"]),
        collection_id=COLLECTION_ID,
        path="readme.txt",
    )

    content = b"".join(chunks)
    assert job["state"] == "ready"
    assert byte_count == len(content)
    assert sha256 == hashlib.sha256(content).hexdigest()
    assert content == b"current archive contract\n"
    assert harness.retrieval.acknowledge(app="local", job_id=str(job["id"]))["state"] == (
        "completed"
    )
