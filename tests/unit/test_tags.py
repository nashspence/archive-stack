from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from riverhog_core.app_permissions import (
    CATALOG_READ,
    COLLECTION_TAGS_MANAGE,
    COLLECTIONS_CREATE,
    KEYS_MANAGE,
    TAGS_CREATE,
    ApplicationAccess,
    ApplicationPrincipal,
)
from riverhog_core.archive_store_registry import ArchiveStoreRegistry
from riverhog_core.catalog_db import initialize_db, make_session_factory, session_scope
from riverhog_core.catalog_models import (
    ArchiveCopyRetirementRecord,
    CatalogEventTagRecord,
    CollectionDeletionRecord,
    CollectionMetadataPublicationRecord,
)
from riverhog_core.runtime_config import RuntimeConfig
from riverhog_core.services.app_keys import SqlAlchemyAppKeyService
from riverhog_core.services.archive_uploads import SqlAlchemyArchiveUploadService
from riverhog_core.services.lifecycle_events import SqlAlchemyLifecycleEventService
from riverhog_core.services.tags import SqlAlchemyTagService
from riverhog_protocol.errors import Conflict

from tests.fixtures.crypto import FixtureProofStamper
from tests.unit.archive_object_fixtures import (
    COLLECTION_ID,
    MemoryArchiveStore,
    as_archive_store,
    seed_archive_copy,
)
from tests.unit.db_helpers import sqlite_url

BOOTSTRAP = ApplicationPrincipal(
    app="bootstrap",
    key_id=None,
    access=frozenset({ApplicationAccess(KEYS_MANAGE)}),
    unrestricted_delegation=True,
)


def _services(path: Path) -> tuple[SqlAlchemyAppKeyService, SqlAlchemyTagService]:
    config = RuntimeConfig(database_url=sqlite_url(path))
    initialize_db(config.database_url)
    return SqlAlchemyAppKeyService(config), SqlAlchemyTagService(config)


def test_tag_creation_grants_only_collection_creation_access(tmp_path: Path) -> None:
    keys, tags = _services(tmp_path / "catalog.sqlite3")
    created = keys.create(
        app="munchy",
        access=(ApplicationAccess(TAGS_CREATE),),
        grantor=BOOTSTRAP,
    )
    creator = keys.authenticate(str(created["token"]))
    assert creator is not None

    tag = tags.create("camera", creator=creator)
    refreshed = keys.authenticate(str(created["token"]))

    assert tag["id"] == "camera"
    assert refreshed is not None
    assert refreshed.allows_tag(COLLECTIONS_CREATE, "camera")
    assert not refreshed.allows_tag(CATALOG_READ, "camera")


def test_tag_list_uses_catalog_scoping_and_standard_page_projection(tmp_path: Path) -> None:
    keys, tags = _services(tmp_path / "catalog.sqlite3")
    tags.create("camera", creator=BOOTSTRAP)
    tags.create("documents", creator=BOOTSTRAP)
    reader_key = keys.create(
        app="reader",
        access=(ApplicationAccess(CATALOG_READ, "tag:camera"),),
        grantor=BOOTSTRAP,
    )
    reader = keys.authenticate(str(reader_key["token"]))
    assert reader is not None

    page = tags.list(
        page=9,
        per_page=1,
        q="cam",
        sort="id",
        order="asc",
        all_items=True,
        principal=reader,
    )

    assert {key: page[key] for key in ("page", "per_page", "total", "pages")} == {
        "page": 1,
        "per_page": 1,
        "total": 1,
        "pages": 1,
    }
    assert {key: page[key] for key in ("sort", "order", "query")} == {
        "sort": "id",
        "order": "asc",
        "query": "cam",
    }
    row = page["tags"][0]
    assert row["id"] == "camera"
    assert row["created_by_app"] == "bootstrap"
    assert row["collections"] == 0


def test_collection_metadata_publication_coalesces_to_latest_revision(tmp_path: Path) -> None:
    config, _archive = seed_archive_copy(
        tmp_path / "catalog.sqlite3",
        {"document.txt": b"metadata publication\n"},
    )
    tags = SqlAlchemyTagService(config)
    tags.create("camera", creator=BOOTSTRAP)
    tags.create("reviewed", creator=BOOTSTRAP)
    manager = ApplicationPrincipal(
        app="operator",
        key_id=None,
        access=frozenset({ApplicationAccess(COLLECTION_TAGS_MANAGE)}),
    )

    assert tags.replace_collection(1, ("camera",), principal=manager)["metadata_revision"] == 2
    assert tags.replace_collection(1, ("reviewed",), principal=manager)["metadata_revision"] == 3

    events = (
        SqlAlchemyLifecycleEventService(config)
        .page(
            owner_app="operator",
            after=None,
            limit=100,
        )
        .events
    )
    assert [event.data["collection_tags"] for event in events] == [
        ["camera"],
        ["reviewed"],
    ]
    assert all(
        event.data["collection_created_at"] == "2026-07-15T00:00:00.000000Z" for event in events
    )
    assert all("tags" not in event.data for event in events)
    with session_scope(make_session_factory(config.database_url)) as session:
        assert [
            (row.sequence, row.phase, row.tag_id)
            for row in session.query(CatalogEventTagRecord).order_by(
                CatalogEventTagRecord.sequence,
                CatalogEventTagRecord.phase,
                CatalogEventTagRecord.tag_id,
            )
        ] == [
            (1, "after", "camera"),
            (1, "before", "docs"),
            (2, "after", "reviewed"),
            (2, "before", "camera"),
        ]

    archive_store = MemoryArchiveStore()
    publisher = SqlAlchemyArchiveUploadService(
        config,
        ArchiveStoreRegistry({"deep": as_archive_store(archive_store)}),
        proof_stamper=FixtureProofStamper(),
    )
    assert publisher.process_due_metadata_publications(limit=10) == 1

    assert len(archive_store.published_metadata) == 1
    collection_id, prefix, manifest_bytes = archive_store.published_metadata[0]
    manifest = yaml.safe_load(manifest_bytes)
    assert collection_id == 1
    assert prefix.endswith("opaque-docs")
    assert manifest["format"] == "riverhog-collection-metadata/v1"
    assert manifest["collection"] == 1
    assert manifest["metadata_revision"] == 3
    assert manifest["tags"] == ["reviewed"]
    with session_scope(make_session_factory(config.database_url)) as session:
        publication = session.get(CollectionMetadataPublicationRecord, (1, "deep"))
        assert publication is not None
        assert publication.state == "published"
        assert publication.desired_revision == publication.published_revision == 3


def test_startup_resumes_a_claimed_metadata_publication(tmp_path: Path) -> None:
    config, _archive = seed_archive_copy(
        tmp_path / "catalog.sqlite3",
        {"document.txt": b"metadata publication\n"},
    )
    tags = SqlAlchemyTagService(config)
    tags.create("camera", creator=BOOTSTRAP)
    manager = ApplicationPrincipal(
        app="operator",
        key_id=None,
        access=frozenset({ApplicationAccess(COLLECTION_TAGS_MANAGE)}),
    )
    tags.replace_collection(COLLECTION_ID, ("camera",), principal=manager)
    factory = make_session_factory(config.database_url)
    with session_scope(factory) as session:
        publication = session.get(CollectionMetadataPublicationRecord, (COLLECTION_ID, "deep"))
        assert publication is not None
        publication.state = "publishing"

    restarted = SqlAlchemyArchiveUploadService(
        config,
        ArchiveStoreRegistry({"deep": as_archive_store(MemoryArchiveStore())}),
        proof_stamper=FixtureProofStamper(),
    )

    assert restarted.requeue_interrupted_metadata_publications_for_startup() == 1
    with session_scope(factory) as session:
        publication = session.get(CollectionMetadataPublicationRecord, (COLLECTION_ID, "deep"))
        assert publication is not None
        assert publication.state == "pending"
        assert publication.next_attempt_at is not None


def test_collection_tag_mutation_waits_for_destructive_custody_operations(
    tmp_path: Path,
) -> None:
    config, _archive = seed_archive_copy(
        tmp_path / "catalog.sqlite3",
        {"document.txt": b"metadata publication\n"},
    )
    tags = SqlAlchemyTagService(config)
    tags.create("camera", creator=BOOTSTRAP)
    manager = ApplicationPrincipal(
        app="operator",
        key_id=None,
        access=frozenset({ApplicationAccess(COLLECTION_TAGS_MANAGE)}),
    )
    factory = make_session_factory(config.database_url)
    with session_scope(factory) as session:
        session.add(
            CollectionDeletionRecord(
                collection_id=COLLECTION_ID,
                challenge="challenge",
                plan_json="{}",
                started_at="2026-07-15T00:00:00.000000Z",
            )
        )
    with pytest.raises(Conflict, match="collection deletion is in progress"):
        tags.replace_collection(COLLECTION_ID, ("camera",), principal=manager)

    with session_scope(factory) as session:
        session.delete(session.get(CollectionDeletionRecord, COLLECTION_ID))
        session.add(
            ArchiveCopyRetirementRecord(
                collection_id=COLLECTION_ID,
                store="deep",
                challenge="challenge",
                plan_json="{}",
                started_at="2026-07-15T00:00:00.000000Z",
            )
        )
    with pytest.raises(Conflict, match="archive copy retirement is in progress"):
        tags.replace_collection(COLLECTION_ID, ("camera",), principal=manager)
