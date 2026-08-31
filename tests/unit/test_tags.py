from __future__ import annotations

import json
from pathlib import Path

import pytest
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
    CollectionUploadRecord,
    CollectionUploadTagRecord,
    TagRecord,
)
from riverhog_core.runtime_config import RuntimeConfig
from riverhog_core.services.app_keys import SqlAlchemyAppKeyService
from riverhog_core.services.archive_maintenance import SqlAlchemyArchiveMaintenanceService
from riverhog_core.services.lifecycle_events import SqlAlchemyLifecycleEventService
from riverhog_core.services.tags import SqlAlchemyTagService
from riverhog_protocol.errors import Conflict
from riverhog_protocol.paths import tag_set_identity

from tests.unit.archive_object_fixtures import (
    COLLECTION_ID,
    MemoryArchiveStore,
    archive_store_binding,
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
        app="stove0",
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
        page_size=25,
        position=None,
        q="cam",
        sort="id",
        order="asc",
        principal=reader,
    )
    rows = list(
        tags.iter_tags(
            q="cam",
            sort="id",
            order="asc",
            principal=reader,
        )
    )

    assert [row["id"] for row in rows] == ["camera"]
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

    assert tags.add_collection_tag(1, "camera", principal=manager)["metadata_revision"] == 2
    assert tags.remove_collection_tag(1, "docs", principal=manager)["metadata_revision"] == 3
    assert tags.add_collection_tag(1, "reviewed", principal=manager)["metadata_revision"] == 4
    assert tags.remove_collection_tag(1, "camera", principal=manager)["metadata_revision"] == 5

    events = (
        SqlAlchemyLifecycleEventService(config)
        .page(
            owner_app="operator",
            after=None,
            limit=100,
        )
        .events
    )
    assert [event.data["collection_tag_count"] for event in events] == [2, 1, 2, 1]
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
            (1, "after", "docs"),
            (1, "before", "docs"),
            (2, "after", "camera"),
            (2, "before", "camera"),
            (2, "before", "docs"),
            (3, "after", "camera"),
            (3, "after", "reviewed"),
            (3, "before", "camera"),
            (4, "after", "reviewed"),
            (4, "before", "camera"),
            (4, "before", "reviewed"),
        ]

    archive_store = MemoryArchiveStore()
    publisher = SqlAlchemyArchiveMaintenanceService(
        config,
        ArchiveStoreRegistry({"deep": archive_store_binding(archive_store)}),
    )
    assert publisher.process_due_metadata_publications(limit=10) == 1

    assert len(archive_store.published_metadata) == 1
    collection_id, prefix, manifest_bytes = archive_store.published_metadata[0]
    manifest = json.loads(manifest_bytes)
    assert collection_id == 1
    assert prefix.endswith("opaque-docs")
    assert manifest["format"] == "riverhog-collection-metadata/v1"
    assert manifest["collection"] == 1
    assert manifest["metadata_revision"] == 5
    assert manifest["tags"] == ["reviewed"]
    with session_scope(make_session_factory(config.database_url)) as session:
        publication = session.get(CollectionMetadataPublicationRecord, (1, "deep"))
        assert publication is not None
        assert publication.state == "published"
        assert publication.desired_revision == publication.published_revision == 5


def test_collection_tag_add_and_remove_are_atomic_single_assignment_operations(
    tmp_path: Path,
) -> None:
    config, _archive = seed_archive_copy(
        tmp_path / "catalog.sqlite3",
        {"document.txt": b"tag mutation\n"},
    )
    tags = SqlAlchemyTagService(config)
    tags.create("reviewed", creator=BOOTSTRAP)
    manager = ApplicationPrincipal(
        app="operator",
        key_id=None,
        access=frozenset(
            {
                ApplicationAccess(CATALOG_READ),
                ApplicationAccess(COLLECTION_TAGS_MANAGE),
            }
        ),
    )

    added = tags.add_collection_tag(COLLECTION_ID, "reviewed", principal=manager)
    assert added["tag_count"] == 2
    assert list(tags.iter_collection_tags(COLLECTION_ID, principal=manager)) == [
        {"tag": "docs"},
        {"tag": "reviewed"},
    ]
    removed = tags.remove_collection_tag(COLLECTION_ID, "docs", principal=manager)
    assert removed["tag_count"] == 1
    assert list(tags.iter_collection_tags(COLLECTION_ID, principal=manager)) == [
        {"tag": "reviewed"}
    ]
    with session_scope(make_session_factory(config.database_url)) as session:
        docs = session.get(TagRecord, "docs")
        reviewed = session.get(TagRecord, "reviewed")
        assert docs is not None and docs.collection_count == 0
        assert reviewed is not None and reviewed.collection_count == 1
    with pytest.raises(Conflict, match="already has tag"):
        tags.add_collection_tag(COLLECTION_ID, "reviewed", principal=manager)


def test_tag_deletion_plan_reports_only_bounded_catalog_dependencies(tmp_path: Path) -> None:
    path = tmp_path / "catalog.sqlite3"
    config = RuntimeConfig(database_url=sqlite_url(path))
    initialize_db(config.database_url)
    tags = SqlAlchemyTagService(config)
    keys = SqlAlchemyAppKeyService(config)
    tags.create("photos", creator=BOOTSTRAP)
    active = keys.create(
        app="reader",
        access=(ApplicationAccess(CATALOG_READ, "tag:photos"),),
        grantor=BOOTSTRAP,
    )
    with session_scope(make_session_factory(config.database_url)) as session:
        now = "2026-08-08T00:00:00.000000Z"
        upload = CollectionUploadRecord(
            idempotency_key="photos-upload",
            creation_identity_sha256="e" * 64,
            tag_set_identity=tag_set_identity(("photos",)),
            provenance_mode="omitted",
            provenance_omission_reason="fixture does not exercise source observation",
            encryption_format="age-v1-scrypt",
            passphrase_id="fixture-archive-key-v1",
            initiated_by_app="uploader",
            archive_store="archive",
            state="open",
            opened_at=now,
            last_activity_at=now,
            archive_phase="planning",
            archive_phase_updated_at=now,
            archive_storage_prefix="archives/test-upload",
            planner_checkpoint_json="{}",
            tags=[CollectionUploadTagRecord(tag_id="photos")],
        )
        session.add(upload)
        session.flush()
        upload_id = upload.collection_id

    plan = tags.plan_deletion("photos")

    assert plan["status"] == "blocked"
    assert plan["challenge"] is None
    dependencies = plan["dependencies"]
    assert dependencies["collections"]["count"] == 0
    assert dependencies["upload_sessions"] == {
        "count": 1,
        "sample": [str(upload_id)],
        "truncated": False,
    }
    assert dependencies["app_key_access"]["count"] == 1
    assert "clients, companions, and automation" in plan["warning"]

    with session_scope(make_session_factory(config.database_url)) as session:
        session.delete(session.get(CollectionUploadRecord, upload_id))
    keys.revoke(app="reader", key_id=str(active["id"]))
    ready = tags.plan_deletion("photos")
    assert ready["status"] == "ready"
    assert isinstance(ready["challenge"], str)
    assert tags.delete("photos", challenge=str(ready["challenge"])) == {
        "status": "deleted",
        "tag": "photos",
    }


def test_tag_deletion_waits_for_removed_tag_metadata_publication(tmp_path: Path) -> None:
    config, _archive = seed_archive_copy(
        tmp_path / "catalog.sqlite3",
        {"document.txt": b"metadata publication\n"},
    )
    tags = SqlAlchemyTagService(config)
    manager = ApplicationPrincipal(
        app="operator",
        key_id=None,
        access=frozenset({ApplicationAccess(COLLECTION_TAGS_MANAGE)}),
    )

    tags.remove_collection_tag(COLLECTION_ID, "docs", principal=manager)
    blocked = tags.plan_deletion("docs")
    assert blocked["dependencies"]["metadata_publications"]["count"] == 1
    assert blocked["challenge"] is None

    with session_scope(make_session_factory(config.database_url)) as session:
        publication = session.get(CollectionMetadataPublicationRecord, (COLLECTION_ID, "deep"))
        assert publication is not None
        publication.state = "published"
        publication.published_revision = publication.desired_revision
        publication.published_at = "2099-01-01T00:00:00.000000Z"
    ready = tags.plan_deletion("docs")
    assert ready["status"] == "ready"
    assert tags.delete("docs", challenge=str(ready["challenge"]))["status"] == "deleted"


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
    tags.add_collection_tag(COLLECTION_ID, "camera", principal=manager)
    tags.remove_collection_tag(COLLECTION_ID, "docs", principal=manager)
    factory = make_session_factory(config.database_url)
    with session_scope(factory) as session:
        publication = session.get(CollectionMetadataPublicationRecord, (COLLECTION_ID, "deep"))
        assert publication is not None
        publication.state = "publishing"

    restarted = SqlAlchemyArchiveMaintenanceService(
        config,
        ArchiveStoreRegistry({"deep": archive_store_binding(MemoryArchiveStore())}),
    )

    assert restarted.requeue_interrupted_metadata_publications_for_startup() == 1
    with session_scope(factory) as session:
        publication = session.get(CollectionMetadataPublicationRecord, (COLLECTION_ID, "deep"))
        assert publication is not None
        assert publication.state == "pending"
        assert publication.next_attempt_at is not None


def test_collection_tag_mutation_waits_for_destructive_archive_operations(
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
        tags.add_collection_tag(COLLECTION_ID, "camera", principal=manager)

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
        tags.add_collection_tag(COLLECTION_ID, "camera", principal=manager)
