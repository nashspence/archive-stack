from __future__ import annotations

import hashlib
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path

import pytest
from pydantic import TypeAdapter, ValidationError
from riverhog_age import decrypt_age_scrypt
from riverhog_application_access import ALL_RESOURCES
from riverhog_core.app_permissions import (
    CATALOG_READ,
    COLLECTION_DESCRIPTIONS_MANAGE,
    ApplicationAccess,
    ApplicationPrincipal,
)
from riverhog_core.archive_store_registry import ArchiveStoreRegistry
from riverhog_core.catalog_db import initialize_db, make_session_factory, session_scope
from riverhog_core.catalog_events import record_catalog_event
from riverhog_core.catalog_models import (
    CatalogEventRecord,
    CollectionArchiveCopyRecord,
    CollectionArchiveObjectRecord,
    CollectionDescriptionPublicationRecord,
    CollectionFileRecord,
    CollectionMutableDocumentPublicationAttemptRecord,
    CollectionMutableDocumentReclamationRecord,
    CollectionRecord,
    CollectionTagMembershipRecord,
    CollectionTagRecord,
    RetrievalCacheObjectRecord,
)
from riverhog_core.ports.archive_store import CollectionDescriptionReceipt
from riverhog_core.runtime_config import (
    DEV_ARCHIVE_PASSPHRASE,
    DEV_ARCHIVE_PASSPHRASE_ID,
    RuntimeConfig,
)
from riverhog_core.services.catalog_sync import SqlAlchemyCatalogSyncService
from riverhog_core.services.collection_descriptions import (
    SqlAlchemyCollectionDescriptionService,
)
from riverhog_core.services.collections import SqlAlchemyCollectionService
from riverhog_core.services.search import SqlAlchemySearchService
from riverhog_protocol import (
    COLLECTION_DESCRIPTION_RELATIVE_PATH,
    COLLECTION_DESCRIPTION_UTF8_BYTES_MAX,
    CatalogSyncUpsert,
    CollectionDescription,
    CollectionDescriptionDocument,
    collection_description_identity,
    collection_tag_set_identity,
    collection_tag_sha256,
)
from riverhog_protocol.errors import PreconditionFailed, ServiceUnavailable
from sqlalchemy import delete, select

from tests.unit.archive_object_fixtures import MemoryArchiveStore, archive_store_binding
from tests.unit.db_helpers import sqlite_url

NOW = "2026-09-07T00:00:00.000000Z"
DESCRIPTION = TypeAdapter(CollectionDescription)
PRINCIPAL = ApplicationPrincipal(
    app="catalog-editor",
    key_id="catalog-editor-key",
    access=frozenset(
        {
            ApplicationAccess(CATALOG_READ, ALL_RESOURCES),
            ApplicationAccess(COLLECTION_DESCRIPTIONS_MANAGE, ALL_RESOURCES),
        }
    ),
)


class FailingDescriptionStore(MemoryArchiveStore):
    fail = True

    def publish_collection_description(
        self,
        *,
        collection_id: int,
        archive_storage_prefix: str,
        document: bytes,
        passphrase_id: str,
        expected_current_stored_sha256: str | None = None,
    ) -> CollectionDescriptionReceipt:
        if self.fail:
            raise OSError("simulated unavailable description store")
        return super().publish_collection_description(
            collection_id=collection_id,
            archive_storage_prefix=archive_storage_prefix,
            document=document,
            passphrase_id=passphrase_id,
            expected_current_stored_sha256=expected_current_stored_sha256,
        )


class DelayedDescriptionStore(MemoryArchiveStore):
    def __init__(self) -> None:
        super().__init__()
        self.delay_next_description = False
        self.started = threading.Event()
        self.resume = threading.Event()

    def publish_collection_description(
        self,
        *,
        collection_id: int,
        archive_storage_prefix: str,
        document: bytes,
        passphrase_id: str,
        expected_current_stored_sha256: str | None = None,
    ) -> CollectionDescriptionReceipt:
        if self.delay_next_description:
            self.delay_next_description = False
            self.started.set()
            assert self.resume.wait(timeout=10)
        return super().publish_collection_description(
            collection_id=collection_id,
            archive_storage_prefix=archive_storage_prefix,
            document=document,
            passphrase_id=passphrase_id,
            expected_current_stored_sha256=expected_current_stored_sha256,
        )


class VersionedDescriptionStore(MemoryArchiveStore):
    def __init__(self) -> None:
        super().__init__()
        self.current_revisions: dict[str, str] = {}
        self.retained_revisions: dict[tuple[str, str], bytes] = {}
        self.deleted_revisions: list[tuple[str, str]] = []
        self.next_revision = 1
        self.lose_next_response = False
        self.lose_next_delete_response = False

    def publish_collection_description(
        self,
        *,
        collection_id: int,
        archive_storage_prefix: str,
        document: bytes,
        passphrase_id: str,
        expected_current_stored_sha256: str | None = None,
    ) -> CollectionDescriptionReceipt:
        path = f"{archive_storage_prefix}/{COLLECTION_DESCRIPTION_RELATIVE_PATH}"
        prior = self.objects.get(path)
        prior_revision = self.current_revisions.get(path)
        receipt = super().publish_collection_description(
            collection_id=collection_id,
            archive_storage_prefix=archive_storage_prefix,
            document=document,
            passphrase_id=passphrase_id,
            expected_current_stored_sha256=expected_current_stored_sha256,
        )
        if prior is self.objects.get(path) and prior_revision is not None:
            return replace(receipt, revision=prior_revision)
        if prior is not None and prior_revision is not None:
            self.retained_revisions[(path, prior_revision)] = prior
        revision = f"description-revision-{self.next_revision}"
        self.next_revision += 1
        self.current_revisions[path] = revision
        result = replace(receipt, revision=revision)
        if self.lose_next_response:
            self.lose_next_response = False
            raise OSError("ambiguous description replacement response")
        return result

    def delete_collection_document_revision(
        self,
        *,
        object_path: str,
        provider_revision: str,
        expected_stored_sha256: str,
    ) -> None:
        self.deleted_revisions.append((object_path, provider_revision))
        if self.current_revisions.get(object_path) == provider_revision:
            raise RuntimeError("refusing to reclaim current mutable collection document")
        prior = self.retained_revisions.get((object_path, provider_revision))
        if prior is None:
            return
        if hashlib.sha256(prior).hexdigest() != expected_stored_sha256:
            raise RuntimeError("mutable collection document revision differs")
        del self.retained_revisions[(object_path, provider_revision)]
        if self.lose_next_delete_response:
            self.lose_next_delete_response = False
            raise OSError("ambiguous description revision deletion response")


def _seed(
    path: Path | None,
    *,
    database_url: str | None = None,
) -> tuple[RuntimeConfig, object, MemoryArchiveStore, ArchiveStoreRegistry]:
    if database_url is None:
        if path is None:
            raise ValueError("path or database_url is required")
        database_url = sqlite_url(path)
    config = RuntimeConfig(
        database_url=database_url,
        browse_token_signing_key="description-test-key-000000000000",
    )
    initialize_db(config.database_url)
    factory = make_session_factory(config.database_url)
    with session_scope(factory) as session:
        collection = CollectionRecord(
            id=1,
            creation_idempotency_key="fixture-1",
            creation_identity_sha256="1" * 64,
            creation_custody_mode="producer-retained",
            archive_generation="2" * 64,
            content_identity="3" * 64,
            encryption_format="age-v1-scrypt",
            passphrase_id=DEV_ARCHIVE_PASSPHRASE_ID,
            provenance_mode="omitted",
            provenance_identity=None,
            inventory_identity="4" * 64,
            archive_root_sha256="5" * 64,
            description_revision=0,
            description_identity=collection_description_identity(
                archive_root_sha256="5" * 64,
                revision=0,
                description=None,
            ),
            created_by_app="fixture",
            created_at=NOW,
            is_published=True,
            file_count=1,
            file_bytes=7,
        )
        session.add(collection)
        session.add(
            CollectionArchiveCopyRecord(
                collection_id=1,
                store="archive",
                state="uploaded",
                archive_storage_prefix="archives/1",
                last_uploaded_at=NOW,
                last_verified_at=NOW,
            )
        )
        session.add(
            CollectionDescriptionPublicationRecord(
                collection_id=1,
                store="archive",
                desired_revision=0,
                desired_identity=collection.description_identity,
                published_revision=0,
                published_identity=collection.description_identity,
                state="published",
                next_attempt_at=None,
            )
        )
        session.add(
            CollectionArchiveObjectRecord(
                collection_id=1,
                store="archive",
                object_id="manifest",
                object_order=0,
                kind="manifest",
                object_path="archives/1/manifest.json.age",
                plaintext_bytes=1,
                stored_bytes=1,
                sha256="5" * 64,
                stored_sha256="6" * 64,
                revision="provider-revision",
                uploaded_at=NOW,
                verified_at=NOW,
            )
        )
        fixture_tag = "description-fixture"
        fixture_tag_sha256 = collection_tag_sha256(fixture_tag)
        session.add(
            CollectionTagRecord(
                tag_sha256=fixture_tag_sha256,
                tag=fixture_tag,
                search_text=fixture_tag,
                created_at=NOW,
                updated_at=NOW,
                collection_count=1,
            )
        )
        session.flush()
        session.add(
            CollectionTagMembershipRecord(
                collection_id=1,
                tag_sha256=fixture_tag_sha256,
                added_at=NOW,
            )
        )
        session.add(
            RetrievalCacheObjectRecord(
                source_store="archive",
                collection_id=1,
                object_id="manifest",
                cache_store="local",
                object_path="cache/1/manifest.json.age",
                revision="cache-revision",
                stored_bytes=1,
                stored_sha256="6" * 64,
                cached_at=NOW,
                verified_at=NOW,
                state="ready",
            )
        )
        session.add(
            CollectionFileRecord(
                collection_id=1,
                path="source/camera.bin",
                bytes=7,
                sha256="7" * 64,
            )
        )
        session.flush()
        record_catalog_event(
            session,
            change="created",
            collection_id=1,
            occurred_at=NOW,
            inventory_identity=collection.inventory_identity,
            before_tags=(),
            after_tags=(fixture_tag_sha256,),
        )
    store = MemoryArchiveStore()
    registry = ArchiveStoreRegistry({"archive": archive_store_binding(store)})
    return config, factory, store, registry


def _immutable_identity(collection: CollectionRecord) -> tuple[object, ...]:
    return (
        collection.creation_identity_sha256,
        collection.archive_generation,
        collection.content_identity,
        collection.encryption_format,
        collection.passphrase_id,
        collection.provenance_mode,
        collection.provenance_identity,
        collection.inventory_identity,
        collection.archive_root_sha256,
        collection.file_count,
        collection.file_bytes,
    )


def test_description_contract_accepts_exact_bounded_nfc_unicode() -> None:
    value = "Résumé of 東京 footage\nCaptured at dawn"
    assert DESCRIPTION.validate_python(value) == value
    assert DESCRIPTION.validate_python("a" * COLLECTION_DESCRIPTION_UTF8_BYTES_MAX) == (
        "a" * COLLECTION_DESCRIPTION_UTF8_BYTES_MAX
    )
    assert DESCRIPTION.validate_python("🦆" * (COLLECTION_DESCRIPTION_UTF8_BYTES_MAX // 4))


@pytest.mark.parametrize(
    "value",
    (
        "",
        " \t\n",
        "e\u0301",
        "contains\x00control",
        "a" * (COLLECTION_DESCRIPTION_UTF8_BYTES_MAX + 1),
        "🦆" * (COLLECTION_DESCRIPTION_UTF8_BYTES_MAX // 4 + 1),
    ),
)
def test_description_contract_rejects_noncanonical_or_oversized_text(value: str) -> None:
    with pytest.raises(ValidationError):
        DESCRIPTION.validate_python(value)


def test_description_is_outside_immutable_archive_authority() -> None:
    schema_root = (
        Path(__file__).resolve().parents[2] / "packages/riverhog-archive-contracts/schemas"
    )
    schemas = [json.loads(path.read_text(encoding="utf-8")) for path in schema_root.glob("*.json")]
    assert len(schemas) == 4

    def property_names(value: object) -> set[str]:
        if isinstance(value, list):
            return {name for item in value for name in property_names(item)}
        if not isinstance(value, dict):
            return set()
        names = set(value.get("properties", {}))
        return names | {name for item in value.values() for name in property_names(item)}

    assert all(
        {"description", "description_identity"}.isdisjoint(property_names(schema))
        for schema in schemas
    )


def test_description_replacement_is_durable_searchable_and_syncable(tmp_path: Path) -> None:
    config, factory, store, registry = _seed(tmp_path / "catalog.sqlite3")
    descriptions = SqlAlchemyCollectionDescriptionService(
        config,
        registry,
        session_factory=factory,
    )
    collections = SqlAlchemyCollectionService(config, session_factory=factory)
    files = SqlAlchemySearchService(config, session_factory=factory)
    sync = SqlAlchemyCatalogSyncService(config, session_factory=factory)

    checkpoint = sync.checkpoint(principal=PRINCIPAL)
    bootstrap = sync.collections(cursor=checkpoint.catalog_cursor, limit=10, principal=PRINCIPAL)
    assert bootstrap.changes_cursor is not None
    with session_scope(factory) as session:  # type: ignore[arg-type]
        collection = session.get(CollectionRecord, 1)
        assert collection is not None
        before = _immutable_identity(collection)
        object_rows = list(
            session.execute(
                select(
                    CollectionArchiveObjectRecord.object_path,
                    CollectionArchiveObjectRecord.sha256,
                    CollectionArchiveObjectRecord.stored_sha256,
                    CollectionArchiveObjectRecord.revision,
                )
            )
        )
        cache_rows = list(
            session.execute(
                select(
                    RetrievalCacheObjectRecord.source_store,
                    RetrievalCacheObjectRecord.object_id,
                    RetrievalCacheObjectRecord.object_path,
                    RetrievalCacheObjectRecord.revision,
                    RetrievalCacheObjectRecord.stored_sha256,
                    RetrievalCacheObjectRecord.state,
                )
            )
        )
        tag_rows = list(
            session.execute(
                select(
                    CollectionTagMembershipRecord.collection_id,
                    CollectionTagMembershipRecord.tag_sha256,
                    CollectionTagMembershipRecord.added_at,
                )
            )
        )

    description = "Camera seven — morning reference"
    initial_identity = collection_description_identity(
        archive_root_sha256="5" * 64,
        revision=0,
        description=None,
    )
    description_identity = collection_description_identity(
        archive_root_sha256="5" * 64,
        revision=1,
        description=description,
    )
    assert descriptions.replace(
        1,
        description=description,
        expected_identity=initial_identity,
        principal=PRINCIPAL,
    ) == {
        "collection_id": 1,
        "description": description,
        "description_revision": 1,
        "description_identity": description_identity,
        "description_publication": "current",
    }

    summary = collections.get(1, principal=PRINCIPAL)
    assert (summary.description, summary.description_identity) == (
        description,
        description_identity,
    )
    matched = collections.list(
        page_size=25,
        position=None,
        q="MORNING reference",
        principal=PRINCIPAL,
    )
    assert [item.id for item in matched.collections] == [1]
    assert (
        files.search(
            q="morning reference",
            page_size=25,
            position=None,
            sort="file_ref",
            order="asc",
            principal=PRINCIPAL,
        )["files"]
        == []
    )
    assert [
        item["path"]
        for item in files.search(
            q="camera.bin",
            page_size=25,
            position=None,
            sort="file_ref",
            order="asc",
            principal=PRINCIPAL,
        )["files"]
    ] == ["source/camera.bin"]

    caught_up = sync.changes(cursor=bootstrap.changes_cursor, limit=10, principal=PRINCIPAL)
    assert caught_up.changes == []
    changes = sync.changes(cursor=caught_up.next_cursor, limit=10, principal=PRINCIPAL)
    assert changes.changes == [
        CatalogSyncUpsert(
            collection_id=1,
            archive_root_sha256="5" * 64,
            content_identity="3" * 64,
            description=description,
            description_revision=1,
            description_identity=description_identity,
            tag_revision=1,
            tag_set_identity=collection_tag_set_identity(None),
            revision="2",
        )
    ]

    assert (
        descriptions.replace(
            1,
            description=description,
            expected_identity=initial_identity,
            principal=PRINCIPAL,
        )["description_identity"]
        == description_identity
    )

    with pytest.raises(PreconditionFailed):
        descriptions.replace(
            1,
            description="Stale writer",
            expected_identity=initial_identity,
            principal=PRINCIPAL,
        )
    assert (
        descriptions.replace(
            1,
            description=description,
            expected_identity=description_identity,
            principal=PRINCIPAL,
        )["description_identity"]
        == description_identity
    )
    cleared_identity = collection_description_identity(
        archive_root_sha256="5" * 64,
        revision=2,
        description=None,
    )
    cleared = descriptions.replace(
        1,
        description=None,
        expected_identity=description_identity,
        principal=PRINCIPAL,
    )
    assert cleared == {
        "collection_id": 1,
        "description": None,
        "description_revision": 2,
        "description_identity": cleared_identity,
        "description_publication": "current",
    }

    description_path = f"archives/1/{COLLECTION_DESCRIPTION_RELATIVE_PATH}"
    document = CollectionDescriptionDocument.from_json_bytes(
        decrypt_age_scrypt(store.objects[description_path], DEV_ARCHIVE_PASSPHRASE)
    )
    assert document.archive_root_sha256 == "5" * 64
    assert document.revision == 2
    assert document.description is None
    assert document.description_identity == cleared_identity

    with session_scope(factory) as session:  # type: ignore[arg-type]
        collection = session.get(CollectionRecord, 1)
        assert collection is not None
        assert _immutable_identity(collection) == before
        assert collection.description_revision == 2
        assert collection.description_identity == cleared_identity
        assert (
            list(
                session.execute(
                    select(
                        CollectionArchiveObjectRecord.object_path,
                        CollectionArchiveObjectRecord.sha256,
                        CollectionArchiveObjectRecord.stored_sha256,
                        CollectionArchiveObjectRecord.revision,
                    )
                )
            )
            == object_rows
        )
        assert (
            list(
                session.execute(
                    select(
                        RetrievalCacheObjectRecord.source_store,
                        RetrievalCacheObjectRecord.object_id,
                        RetrievalCacheObjectRecord.object_path,
                        RetrievalCacheObjectRecord.revision,
                        RetrievalCacheObjectRecord.stored_sha256,
                        RetrievalCacheObjectRecord.state,
                    )
                )
            )
            == cache_rows
        )
        assert (
            list(
                session.execute(
                    select(
                        CollectionTagMembershipRecord.collection_id,
                        CollectionTagMembershipRecord.tag_sha256,
                        CollectionTagMembershipRecord.added_at,
                    )
                )
            )
            == tag_rows
        )
        events = list(
            session.scalars(select(CatalogEventRecord).order_by(CatalogEventRecord.revision))
        )
        assert [(event.revision, event.description) for event in events] == [
            (1, None),
            (2, description),
            (3, None),
        ]


def test_description_projection_waits_for_durable_publication_and_restarts(
    tmp_path: Path,
) -> None:
    config, factory, _store, _registry = _seed(tmp_path / "catalog.sqlite3")
    store = FailingDescriptionStore()
    service = SqlAlchemyCollectionDescriptionService(
        config,
        ArchiveStoreRegistry({"archive": archive_store_binding(store)}),
        session_factory=factory,
    )
    initial_identity = collection_description_identity(
        archive_root_sha256="5" * 64,
        revision=0,
        description=None,
    )

    with pytest.raises(ServiceUnavailable, match="publication failed"):
        service.replace(
            1,
            description="Durable only after storage accepts it",
            expected_identity=initial_identity,
            principal=PRINCIPAL,
        )

    with session_scope(factory) as session:  # type: ignore[arg-type]
        collection = session.get(CollectionRecord, 1)
        assert collection is not None
        assert collection.description is None
        assert collection.description_revision == 0
        assert collection.description_identity == initial_identity
        assert collection.description_mutation_state == "retry_wait"
        collection.description_next_attempt_at = NOW

    store.fail = False
    assert service.process_due(limit=1) == 1
    with session_scope(factory) as session:  # type: ignore[arg-type]
        collection = session.get(CollectionRecord, 1)
        assert collection is not None
        assert collection.description == "Durable only after storage accepts it"
        assert collection.description_revision == 1
        assert collection.description_mutation_state == "idle"
        assert len(list(session.scalars(select(CatalogEventRecord)))) == 2


def test_delayed_primary_description_writer_cannot_overwrite_newer_authority(
    tmp_path: Path,
) -> None:
    config, factory, _initial, _registry = _seed(tmp_path / "catalog.sqlite3")
    store = DelayedDescriptionStore()
    service = SqlAlchemyCollectionDescriptionService(
        config,
        ArchiveStoreRegistry({"archive": archive_store_binding(store)}),
        session_factory=factory,
    )
    initial_identity = collection_description_identity(
        archive_root_sha256="5" * 64,
        revision=0,
        description=None,
    )
    store.delay_next_description = True

    with ThreadPoolExecutor(max_workers=1) as executor:
        delayed = executor.submit(
            service.replace,
            1,
            description="Delayed authority",
            expected_identity=initial_identity,
            principal=PRINCIPAL,
        )
        assert store.started.wait(timeout=10)
        restarted = SqlAlchemyCollectionDescriptionService(
            config,
            ArchiveStoreRegistry({"archive": archive_store_binding(store)}),
            session_factory=factory,
        )
        assert restarted.requeue_interrupted_for_startup(limit=1) == 1
        assert restarted.process_due(limit=1) == 1
        with session_scope(factory) as session:  # type: ignore[arg-type]
            collection = session.get(CollectionRecord, 1)
            assert collection is not None
            delayed_identity = collection.description_identity
        current = restarted.replace(
            1,
            description="Newer acknowledged authority",
            expected_identity=delayed_identity,
            principal=PRINCIPAL,
        )
        assert current["description_revision"] == 2
        store.resume.set()
        with pytest.raises(ServiceUnavailable):
            delayed.result(timeout=10)

    document = CollectionDescriptionDocument.from_json_bytes(
        decrypt_age_scrypt(
            store.objects[f"archives/1/{COLLECTION_DESCRIPTION_RELATIVE_PATH}"],
            DEV_ARCHIVE_PASSPHRASE,
        )
    )
    assert document.revision == 2
    assert document.description == "Newer acknowledged authority"


def test_delayed_description_replica_cannot_overwrite_newer_authority(
    tmp_path: Path,
) -> None:
    config, factory, primary, _registry = _seed(tmp_path / "catalog.sqlite3")
    initial_identity = collection_description_identity(
        archive_root_sha256="5" * 64,
        revision=0,
        description=None,
    )
    with session_scope(factory) as session:  # type: ignore[arg-type]
        session.add(
            CollectionArchiveCopyRecord(
                collection_id=1,
                store="mirror",
                state="uploaded",
                archive_storage_prefix="archives/mirror/1",
                last_uploaded_at=NOW,
                last_verified_at=NOW,
            )
        )
        session.add(
            CollectionDescriptionPublicationRecord(
                collection_id=1,
                store="mirror",
                desired_revision=0,
                desired_identity=initial_identity,
                published_revision=0,
                published_identity=initial_identity,
                state="published",
                next_attempt_at=None,
            )
        )
    mirror = DelayedDescriptionStore()
    registry = ArchiveStoreRegistry(
        {
            "archive": archive_store_binding(primary),
            "mirror": archive_store_binding(mirror),
        }
    )
    service = SqlAlchemyCollectionDescriptionService(
        config,
        registry,
        session_factory=factory,
    )
    first = service.replace(
        1,
        description="First authority",
        expected_identity=initial_identity,
        principal=PRINCIPAL,
    )
    assert service.process_due(limit=1) == 1
    second = service.replace(
        1,
        description="Delayed replica authority",
        expected_identity=str(first["description_identity"]),
        principal=PRINCIPAL,
    )
    mirror.delay_next_description = True

    with ThreadPoolExecutor(max_workers=1) as executor:
        delayed = executor.submit(service.process_due, limit=1)
        assert mirror.started.wait(timeout=10)
        restarted = SqlAlchemyCollectionDescriptionService(
            config,
            registry,
            session_factory=factory,
        )
        assert restarted.requeue_interrupted_for_startup(limit=1) == 1
        assert restarted.process_due(limit=1) == 1
        third = service.replace(
            1,
            description="Newer replica authority",
            expected_identity=str(second["description_identity"]),
            principal=PRINCIPAL,
        )
        assert restarted.process_due(limit=1) == 1
        mirror.resume.set()
        assert delayed.result(timeout=10) == 1

    document = CollectionDescriptionDocument.from_json_bytes(
        decrypt_age_scrypt(
            mirror.objects[f"archives/mirror/1/{COLLECTION_DESCRIPTION_RELATIVE_PATH}"],
            DEV_ARCHIVE_PASSPHRASE,
        )
    )
    assert document.revision == third["description_revision"] == 3
    assert document.description == "Newer replica authority"
    with session_scope(factory) as session:  # type: ignore[arg-type]
        publication = session.get(CollectionDescriptionPublicationRecord, (1, "mirror"))
        assert publication is not None
        assert publication.state == "published"
        assert publication.published_revision == 3


def test_description_acknowledges_one_copy_then_reconciles_every_retained_copy(
    tmp_path: Path,
) -> None:
    config, factory, primary, _registry = _seed(tmp_path / "catalog.sqlite3")
    initial_identity = collection_description_identity(
        archive_root_sha256="5" * 64,
        revision=0,
        description=None,
    )
    with session_scope(factory) as session:  # type: ignore[arg-type]
        session.add(
            CollectionArchiveCopyRecord(
                collection_id=1,
                store="mirror",
                state="uploaded",
                archive_storage_prefix="archives/mirror/1",
                last_uploaded_at=NOW,
                last_verified_at=NOW,
            )
        )
        session.add(
            CollectionDescriptionPublicationRecord(
                collection_id=1,
                store="mirror",
                desired_revision=0,
                desired_identity=initial_identity,
                published_revision=0,
                published_identity=initial_identity,
                state="published",
                next_attempt_at=None,
            )
        )
    mirror = MemoryArchiveStore()
    service = SqlAlchemyCollectionDescriptionService(
        config,
        ArchiveStoreRegistry(
            {
                "archive": archive_store_binding(primary),
                "mirror": archive_store_binding(mirror),
            }
        ),
        session_factory=factory,
    )

    updated = service.replace(
        1,
        description="Replicate me",
        expected_identity=initial_identity,
        principal=PRINCIPAL,
    )
    assert updated["description_publication"] == "reconciling"
    assert f"archives/1/{COLLECTION_DESCRIPTION_RELATIVE_PATH}" in primary.objects
    assert not mirror.objects

    assert service.process_due(limit=1) == 1
    assert f"archives/mirror/1/{COLLECTION_DESCRIPTION_RELATIVE_PATH}" in mirror.objects
    assert (
        SqlAlchemyCollectionService(config, session_factory=factory)
        .get(1, principal=PRINCIPAL)
        .description_publication
        == "current"
    )


@pytest.mark.parametrize("destination_initialized", (False, True))
def test_description_replica_reconciles_exact_ambiguous_attempt_before_newer_desired(
    tmp_path: Path,
    destination_initialized: bool,
) -> None:
    path = tmp_path / "catalog.sqlite3"
    config, factory, primary, _registry = _seed(path)
    initial_identity = collection_description_identity(
        archive_root_sha256="5" * 64,
        revision=0,
        description=None,
    )
    with session_scope(factory) as session:  # type: ignore[arg-type]
        session.add(
            CollectionArchiveCopyRecord(
                collection_id=1,
                store="mirror",
                state="uploaded",
                archive_storage_prefix="archives/mirror/1",
                last_uploaded_at=NOW,
                last_verified_at=NOW,
            )
        )
        session.add(
            CollectionDescriptionPublicationRecord(
                collection_id=1,
                store="mirror",
                desired_revision=0,
                desired_identity=initial_identity,
                published_revision=0,
                published_identity=initial_identity,
                state="published",
                next_attempt_at=None,
            )
        )
    mirror = VersionedDescriptionStore()
    registry = ArchiveStoreRegistry(
        {
            "archive": archive_store_binding(primary),
            "mirror": archive_store_binding(mirror),
        }
    )
    service = SqlAlchemyCollectionDescriptionService(
        config,
        registry,
        session_factory=factory,
    )

    first = service.replace(
        1,
        description="first",
        expected_identity=initial_identity,
        principal=PRINCIPAL,
    )
    if destination_initialized:
        assert service.process_due(limit=1) == 1
        second = service.replace(
            1,
            description="second",
            expected_identity=str(first["description_identity"]),
            principal=PRINCIPAL,
        )
        ambiguous_revision = 2
        expected_identity = str(second["description_identity"])
    else:
        ambiguous_revision = 1
        expected_identity = str(first["description_identity"])

    mirror.lose_next_response = True
    assert service.process_due(limit=1) == 1
    with session_scope(factory) as session:  # type: ignore[arg-type]
        publication = session.get(CollectionDescriptionPublicationRecord, (1, "mirror"))
        attempt = session.get(
            CollectionMutableDocumentPublicationAttemptRecord,
            (1, "mirror", "description"),
        )
        assert publication is not None and publication.state == "retry_wait"
        assert attempt is not None and attempt.document_revision == ambiguous_revision

    newest = service.replace(
        1,
        description="newest",
        expected_identity=expected_identity,
        principal=PRINCIPAL,
    )
    with session_scope(factory) as session:  # type: ignore[arg-type]
        publication = session.get(CollectionDescriptionPublicationRecord, (1, "mirror"))
        attempt = session.get(
            CollectionMutableDocumentPublicationAttemptRecord,
            (1, "mirror", "description"),
        )
        assert publication is not None
        assert publication.desired_revision == newest["description_revision"]
        assert publication.state == "retry_wait"
        assert attempt is not None and attempt.document_revision == ambiguous_revision
        publication.next_attempt_at = NOW

    restarted = SqlAlchemyCollectionDescriptionService(
        config,
        registry,
        session_factory=make_session_factory(config.database_url),
    )
    for _ in range(16):
        if restarted.process_due(limit=1) == 0:
            break

    document = CollectionDescriptionDocument.from_json_bytes(
        decrypt_age_scrypt(
            mirror.objects[f"archives/mirror/1/{COLLECTION_DESCRIPTION_RELATIVE_PATH}"],
            DEV_ARCHIVE_PASSPHRASE,
        )
    )
    assert document.revision == newest["description_revision"]
    assert document.description == "newest"
    with session_scope(factory) as session:  # type: ignore[arg-type]
        publication = session.get(CollectionDescriptionPublicationRecord, (1, "mirror"))
        assert publication is not None and publication.state == "published"
        assert publication.published_revision == newest["description_revision"]
        assert session.query(CollectionMutableDocumentPublicationAttemptRecord).count() == 0
        assert session.query(CollectionMutableDocumentReclamationRecord).count() == 0
    assert not mirror.retained_revisions


def test_description_status_excludes_incomplete_archive_copies(tmp_path: Path) -> None:
    config, factory, archive, registry = _seed(tmp_path / "catalog.sqlite3")
    with session_scope(factory) as session:  # type: ignore[arg-type]
        session.add(
            CollectionArchiveCopyRecord(
                collection_id=1,
                store="incomplete",
                state="failed",
                archive_storage_prefix="archives/incomplete/1",
            )
        )
    initial_identity = collection_description_identity(
        archive_root_sha256="5" * 64,
        revision=0,
        description=None,
    )

    updated = SqlAlchemyCollectionDescriptionService(
        config,
        registry,
        session_factory=factory,
    ).replace(
        1,
        description="Only retained copies participate",
        expected_identity=initial_identity,
        principal=PRINCIPAL,
    )

    assert updated["description_publication"] == "current"
    assert archive.objects
    summary = SqlAlchemyCollectionService(config, session_factory=factory).get(
        1,
        principal=PRINCIPAL,
    )
    assert summary.archive_copy_count == 2
    assert summary.description_publication == "current"


def test_superseded_description_revisions_are_reclaimed_one_at_a_time(
    tmp_path: Path,
) -> None:
    config, factory, _initial, _registry = _seed(tmp_path / "catalog.sqlite3")
    store = VersionedDescriptionStore()
    service = SqlAlchemyCollectionDescriptionService(
        config,
        ArchiveStoreRegistry({"archive": archive_store_binding(store)}),
        session_factory=factory,
    )
    identity = collection_description_identity(
        archive_root_sha256="5" * 64,
        revision=0,
        description=None,
    )
    for description in ("first", "second", None):
        current = service.replace(
            1,
            description=description,
            expected_identity=identity,
            principal=PRINCIPAL,
        )
        identity = str(current["description_identity"])

    with session_scope(factory) as session:  # type: ignore[arg-type]
        assert session.query(CollectionMutableDocumentReclamationRecord).count() == 2
    assert len(store.retained_revisions) == 2

    assert service.process_due(limit=1) == 1
    with session_scope(factory) as session:  # type: ignore[arg-type]
        assert session.query(CollectionMutableDocumentReclamationRecord).count() == 1
    assert len(store.retained_revisions) == 1
    assert service.process_due(limit=1) == 1
    assert service.process_due(limit=1) == 0

    with session_scope(factory) as session:  # type: ignore[arg-type]
        assert session.query(CollectionMutableDocumentReclamationRecord).count() == 0
    assert not store.retained_revisions
    path = f"archives/1/{COLLECTION_DESCRIPTION_RELATIVE_PATH}"
    current_document = CollectionDescriptionDocument.from_json_bytes(
        decrypt_age_scrypt(store.objects[path], DEV_ARCHIVE_PASSPHRASE)
    )
    assert current_document.revision == 3
    assert current_document.description is None


def test_description_replacement_reconciles_receipts_after_an_ambiguous_response(
    tmp_path: Path,
) -> None:
    config, factory, _initial, _registry = _seed(tmp_path / "catalog.sqlite3")
    store = VersionedDescriptionStore()
    service = SqlAlchemyCollectionDescriptionService(
        config,
        ArchiveStoreRegistry({"archive": archive_store_binding(store)}),
        session_factory=factory,
    )
    initial_identity = collection_description_identity(
        archive_root_sha256="5" * 64,
        revision=0,
        description=None,
    )
    first = service.replace(
        1,
        description="first",
        expected_identity=initial_identity,
        principal=PRINCIPAL,
    )
    store.lose_next_response = True
    with pytest.raises(ServiceUnavailable, match="publication failed"):
        service.replace(
            1,
            description="second",
            expected_identity=str(first["description_identity"]),
            principal=PRINCIPAL,
        )

    with session_scope(factory) as session:  # type: ignore[arg-type]
        collection = session.get(CollectionRecord, 1)
        assert collection is not None
        assert collection.description == "first"
        collection.description_next_attempt_at = NOW
    assert service.process_due(limit=1) == 1
    with session_scope(factory) as session:  # type: ignore[arg-type]
        collection = session.get(CollectionRecord, 1)
        assert collection is not None and collection.description == "second"
        assert session.query(CollectionMutableDocumentReclamationRecord).count() == 1
    assert len(store.retained_revisions) == 1


def test_description_revision_reclamation_reconciles_an_ambiguous_delete(
    tmp_path: Path,
) -> None:
    config, factory, _initial, _registry = _seed(tmp_path / "catalog.sqlite3")
    store = VersionedDescriptionStore()
    service = SqlAlchemyCollectionDescriptionService(
        config,
        ArchiveStoreRegistry({"archive": archive_store_binding(store)}),
        session_factory=factory,
    )
    initial_identity = collection_description_identity(
        archive_root_sha256="5" * 64,
        revision=0,
        description=None,
    )
    first = service.replace(
        1,
        description="first",
        expected_identity=initial_identity,
        principal=PRINCIPAL,
    )
    service.replace(
        1,
        description="second",
        expected_identity=str(first["description_identity"]),
        principal=PRINCIPAL,
    )

    store.lose_next_delete_response = True
    assert service.process_due(limit=1) == 1
    with session_scope(factory) as session:  # type: ignore[arg-type]
        row = session.scalar(select(CollectionMutableDocumentReclamationRecord))
        assert row is not None
        assert row.state == "retry_wait"
        row.next_attempt_at = NOW
    assert not store.retained_revisions

    assert service.process_due(limit=1) == 1
    with session_scope(factory) as session:  # type: ignore[arg-type]
        assert session.query(CollectionMutableDocumentReclamationRecord).count() == 0
    path = f"archives/1/{COLLECTION_DESCRIPTION_RELATIVE_PATH}"
    current_document = CollectionDescriptionDocument.from_json_bytes(
        decrypt_age_scrypt(store.objects[path], DEV_ARCHIVE_PASSPHRASE)
    )
    assert current_document.revision == 2


def test_description_revision_reclamation_resumes_after_interrupted_provider_effect(
    tmp_path: Path,
) -> None:
    config, factory, _initial, _registry = _seed(tmp_path / "catalog.sqlite3")
    store = VersionedDescriptionStore()
    service = SqlAlchemyCollectionDescriptionService(
        config,
        ArchiveStoreRegistry({"archive": archive_store_binding(store)}),
        session_factory=factory,
    )
    initial_identity = collection_description_identity(
        archive_root_sha256="5" * 64,
        revision=0,
        description=None,
    )
    first = service.replace(
        1,
        description="first",
        expected_identity=initial_identity,
        principal=PRINCIPAL,
    )
    service.replace(
        1,
        description="second",
        expected_identity=str(first["description_identity"]),
        principal=PRINCIPAL,
    )

    with session_scope(factory) as session:  # type: ignore[arg-type]
        row = session.scalar(select(CollectionMutableDocumentReclamationRecord))
        assert row is not None
        row.state = "deleting"
    restarted = SqlAlchemyCollectionDescriptionService(
        config,
        ArchiveStoreRegistry({"archive": archive_store_binding(store)}),
        session_factory=factory,
    )
    assert restarted.requeue_interrupted_for_startup(limit=1) == 1
    with session_scope(factory) as session:  # type: ignore[arg-type]
        row = session.scalar(select(CollectionMutableDocumentReclamationRecord))
        assert row is not None
        assert row.state == "pending"
        assert row.failure == "reclamation interrupted before completion"

    assert restarted.process_due(limit=1) == 1
    with session_scope(factory) as session:  # type: ignore[arg-type]
        assert session.query(CollectionMutableDocumentReclamationRecord).count() == 0
    assert not store.retained_revisions


def test_superseded_document_cleanup_receipt_survives_collection_retirement(
    tmp_path: Path,
) -> None:
    config, factory, _initial, _registry = _seed(tmp_path / "catalog.sqlite3")
    store = VersionedDescriptionStore()
    service = SqlAlchemyCollectionDescriptionService(
        config,
        ArchiveStoreRegistry({"archive": archive_store_binding(store)}),
        session_factory=factory,
    )
    initial_identity = collection_description_identity(
        archive_root_sha256="5" * 64,
        revision=0,
        description=None,
    )
    first = service.replace(
        1,
        description="first",
        expected_identity=initial_identity,
        principal=PRINCIPAL,
    )
    service.replace(
        1,
        description="second",
        expected_identity=str(first["description_identity"]),
        principal=PRINCIPAL,
    )

    with session_scope(factory) as session:  # type: ignore[arg-type]
        assert session.query(CollectionMutableDocumentReclamationRecord).count() == 1
        session.execute(delete(CollectionRecord).where(CollectionRecord.id == 1))
    with session_scope(factory) as session:  # type: ignore[arg-type]
        assert session.query(CollectionMutableDocumentReclamationRecord).count() == 1

    # Prefix retirement already reclaimed every provider revision.  The independent
    # receipt remains restartable and its exact deletion is safely idempotent.
    store.objects.clear()
    store.current_revisions.clear()
    store.retained_revisions.clear()
    assert service.process_due(limit=1) == 1
    with session_scope(factory) as session:  # type: ignore[arg-type]
        assert session.query(CollectionMutableDocumentReclamationRecord).count() == 0
