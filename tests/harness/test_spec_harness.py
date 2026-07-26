from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest
from riverhog_api.routers.resourcesync import resourcesync_resource_list
from riverhog_core.app_permissions import CATALOG_READ, ApplicationAccess, ApplicationPrincipal
from riverhog_core.archive_store_registry import ArchiveStoreRegistry
from riverhog_core.catalog_db import make_session_factory, session_scope
from riverhog_core.catalog_models import CatalogEventRecord, CollectionRecord
from riverhog_core.ports.upload_store import UploadStore
from riverhog_core.services.archive_reporting import SqlAlchemyArchiveReportingService
from riverhog_core.services.collections import SqlAlchemyCollectionService
from riverhog_core.services.retrieval import SqlAlchemyRetrievalService
from riverhog_core.services.search import SqlAlchemySearchService
from starlette.requests import Request

from tests.fixtures.crypto import FixtureProofVerifier
from tests.unit.archive_object_fixtures import (
    COLLECTION_ID,
    MemoryArchiveStore,
    as_archive_store,
    seed_archive_copy,
)


@dataclass(frozen=True)
class Harness:
    collections: SqlAlchemyCollectionService
    search: SqlAlchemySearchService
    archive_reporting: SqlAlchemyArchiveReportingService
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
        session.add(
            CatalogEventRecord(
                change="created",
                collection_id=COLLECTION_ID,
                occurred_at="2026-07-18T00:00:00.000000Z",
                record_etag=collection.record_etag,
            )
        )
    archive_stores = ArchiveStoreRegistry({"deep": as_archive_store(MemoryArchiveStore(archive))})
    return Harness(
        collections=SqlAlchemyCollectionService(config, cast(UploadStore, object())),
        search=SqlAlchemySearchService(config),
        archive_reporting=SqlAlchemyArchiveReportingService(config),
        retrieval=SqlAlchemyRetrievalService(
            config,
            archive_stores,
            None,
            proof_verifier=FixtureProofVerifier(),
        ),
    )


def _request() -> Request:
    return Request(
        {
            "type": "http",
            "method": "GET",
            "scheme": "https",
            "server": ("riverhog.example.test", 443),
            "path": "/resourcesync/resourcelist.xml",
            "root_path": "",
            "query_string": b"",
            "headers": [],
        }
    )


def test_catalog_search_and_archive_report_share_current_identity(harness: Harness) -> None:
    collection = harness.collections.get(COLLECTION_ID)
    search = harness.search.search(
        q="readme",
        page=1,
        per_page=25,
        sort="logical_path",
        order="asc",
    )
    archive = harness.archive_reporting.get_report()
    resources = resourcesync_resource_list(
        _request(),
        ApplicationPrincipal(
            app="local",
            key_id="local-key",
            access=frozenset({ApplicationAccess(CATALOG_READ)}),
        ),
        SimpleNamespace(retrieval=harness.retrieval),
    )

    assert collection.id == COLLECTION_ID
    assert [(copy.store, copy.state.value) for copy in collection.archive_copies] == [
        ("deep", "uploaded")
    ]
    assert search["files"][0]["logical_path"] == f"{COLLECTION_ID}/readme.txt"
    assert archive.totals.uploaded_collections == 1
    assert str(COLLECTION_ID).encode() in resources.body


def test_application_retrieves_one_manifest_selected_file(harness: Harness) -> None:
    manifest, etag = harness.retrieval.collection_manifest(COLLECTION_ID)
    assert manifest["files"] == [
        {
            "path": "readme.txt",
            "bytes": len(b"current archive contract\n"),
            "sha256": hashlib.sha256(b"current archive contract\n").hexdigest(),
        }
    ]
    changes = harness.retrieval.change_list()
    assert changes["changes"][0]["etag"] == etag

    files = [(COLLECTION_ID, "readme.txt")]
    plan = harness.retrieval.plan(files)
    job = harness.retrieval.create(
        app="local",
        files=files,
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
