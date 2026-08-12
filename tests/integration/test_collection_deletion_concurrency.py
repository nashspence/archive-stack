from __future__ import annotations

import hashlib
import os
import threading
from collections.abc import Iterator, Sequence
from dataclasses import replace
from typing import cast

import pytest
from riverhog_core.app_permissions import ApplicationPrincipal
from riverhog_core.archive_store_registry import ArchiveStoreBinding, ArchiveStoreRegistry
from riverhog_core.catalog_db import (
    STATE_VERSION_TABLE,
    Base,
    create_catalog_engine,
    initialize_db,
    make_session_factory,
    session_scope,
)
from riverhog_core.catalog_models import (
    CollectionArchiveCopyRecord,
    CollectionArchiveFileObjectRecord,
    CollectionArchiveObjectRecord,
    CollectionDeletionRecord,
    CollectionFileRecord,
    CollectionMetadataPublicationRecord,
    CollectionRecord,
    CollectionTagRecord,
    RetrievalJobRecord,
    TagRecord,
)
from riverhog_core.ports.archive_store import (
    ArchiveObjectIdentity,
    ArchiveStore,
    CollectionArchiveIdentity,
    MutableManifestReceipt,
)
from riverhog_core.runtime_config import RuntimeConfig
from riverhog_core.services.archive_copy_retirements import (
    SqlAlchemyArchiveCopyRetirementService,
)
from riverhog_core.services.archive_maintenance import SqlAlchemyArchiveMaintenanceService
from riverhog_core.services.collection_deletions import SqlAlchemyCollectionDeletionService
from riverhog_core.services.retrieval import SqlAlchemyRetrievalService
from riverhog_protocol.errors import Conflict
from sqlalchemy import select, text

from tests.unit.archive_object_fixtures import MemoryArchiveStore, archive_store_binding

pytestmark = pytest.mark.integration

COLLECTION_ID = 1
FILE_PATH = "document.txt"
CONTENT = b"archived document"
DELETER = ApplicationPrincipal(
    app="riverhog-client",
    key_id="client-key",
    access=frozenset(),
)


class BlockingArchiveStore:
    def __init__(self) -> None:
        self.delete_started = threading.Event()
        self.allow_delete = threading.Event()
        self.metadata_started = threading.Event()
        self.allow_metadata = threading.Event()
        self.deleted: list[tuple[str, ...]] = []
        self.published_metadata: list[bytes] = []

    def read_mode(self) -> str:
        return "immediate"

    def delete_collection_archive(
        self,
        *,
        collection_id: int,
        objects: Sequence[ArchiveObjectIdentity],
    ) -> None:
        assert collection_id == COLLECTION_ID
        self.delete_started.set()
        if not self.allow_delete.wait(10):
            raise RuntimeError("timed out waiting to finish archive deletion")
        self.deleted.append(tuple(current.object_id for current in objects))

    def publish_collection_metadata(
        self,
        *,
        collection_id: int,
        archive_storage_prefix: str,
        manifest: bytes,
    ) -> MutableManifestReceipt:
        assert collection_id == COLLECTION_ID
        self.metadata_started.set()
        if not self.allow_metadata.wait(10):
            raise RuntimeError("timed out waiting to finish metadata publication")
        self.published_metadata.append(manifest)
        return MutableManifestReceipt(
            object_path=f"{archive_storage_prefix}/metadata.json.age",
            version_id="metadata-version",
            stored_bytes=len(manifest),
            stored_sha256=hashlib.sha256(manifest).hexdigest(),
            published_at="2026-07-18T00:00:00.000000Z",
        )

    def verify_collection_archive(
        self,
        *,
        collection_id: int,
        archive: CollectionArchiveIdentity,
    ) -> None:
        assert collection_id == COLLECTION_ID
        assert archive.objects


def _archive_store_binding(store: BlockingArchiveStore) -> ArchiveStoreBinding:
    return replace(
        archive_store_binding(MemoryArchiveStore()),
        store=cast(ArchiveStore, store),
    )


@pytest.fixture
def database_url() -> Iterator[str]:
    value = os.getenv("RIVERHOG_TEST_POSTGRES_URL", "").strip()
    if not value:
        pytest.skip("RIVERHOG_TEST_POSTGRES_URL is required")
    engine = create_catalog_engine(value)
    Base.metadata.drop_all(engine)
    with engine.begin() as connection:
        connection.execute(text(f'DROP TABLE IF EXISTS "{STATE_VERSION_TABLE}"'))
    engine.dispose()
    initialize_db(value)
    try:
        yield value
    finally:
        engine = create_catalog_engine(value)
        Base.metadata.drop_all(engine)
        with engine.begin() as connection:
            connection.execute(text(f'DROP TABLE IF EXISTS "{STATE_VERSION_TABLE}"'))
        engine.dispose()


def _seed(database_url: str) -> None:
    factory = make_session_factory(database_url)
    with session_scope(factory) as session:
        session.add(
            TagRecord(
                id="docs",
                created_by_app="fixture",
                created_at="2026-01-01T00:00:00.000000Z",
            )
        )
        session.add(
            CollectionRecord(
                id=COLLECTION_ID,
                creation_idempotency_key="fixture-docs",
                content_etag="0" * 64,
                record_etag="0" * 64,
                metadata_revision=1,
                metadata_updated_at="2026-01-01T00:00:00.000000Z",
                created_at="2026-01-01T00:00:00.000000Z",
            )
        )
        session.add(
            CollectionTagRecord(
                collection_id=COLLECTION_ID,
                tag_id="docs",
                assigned_by_app="fixture",
                assigned_at="2026-01-01T00:00:00.000000Z",
            )
        )
        session.add(
            CollectionFileRecord(
                collection_id=COLLECTION_ID,
                path=FILE_PATH,
                bytes=len(CONTENT),
                sha256=hashlib.sha256(CONTENT).hexdigest(),
            )
        )
        copy = CollectionArchiveCopyRecord(
            collection_id=COLLECTION_ID,
            store="deep",
            state="uploaded",
            archive_storage_prefix="archives/opaque-docs",
            backend="s3",
            storage_class="STANDARD",
            last_uploaded_at="2026-07-18T00:00:00.000000Z",
            last_verified_at="2026-07-18T00:00:00.000000Z",
        )
        session.add(copy)
        for order, (object_id, kind, relative_path, stored_bytes) in enumerate(
            (
                ("segment-000000000000", "segment", "volumes/segment-000000000000.bin.age", 100),
                ("manifest", "manifest", "manifest.json.age", 20),
                ("proof", "proof", "manifest.json.ots.age", 10),
            )
        ):
            copy.objects.append(
                CollectionArchiveObjectRecord(
                    collection_id=COLLECTION_ID,
                    store="deep",
                    object_id=object_id,
                    object_order=order,
                    kind=kind,
                    object_path=f"archives/opaque-docs/{relative_path}",
                    plaintext_bytes=stored_bytes - 1,
                    stored_bytes=stored_bytes,
                    sha256=chr(ord("a") + order) * 64,
                    stored_sha256=chr(ord("d") + order) * 64,
                    backend="s3",
                    storage_class="STANDARD",
                    uploaded_at="2026-07-18T00:00:00.000000Z",
                    verified_at="2026-07-18T00:00:00.000000Z",
                )
            )
        session.add(
            CollectionArchiveFileObjectRecord(
                collection_id=COLLECTION_ID,
                store="deep",
                path=FILE_PATH,
                sequence=0,
                object_id="segment-000000000000",
                file_offset=0,
                bytes=len(CONTENT),
            )
        )


def _services(
    database_url: str,
) -> tuple[
    SqlAlchemyCollectionDeletionService,
    SqlAlchemyRetrievalService,
    BlockingArchiveStore,
]:
    config = RuntimeConfig(database_url=database_url)
    deep = replace(config.archive_store("archive"), name="deep")
    config = replace(
        config,
        archive_stores={"deep": deep},
        archive_write_store="deep",
        archive_read_order=("deep",),
    )
    store = BlockingArchiveStore()
    stores = ArchiveStoreRegistry({"deep": _archive_store_binding(store)})
    return (
        SqlAlchemyCollectionDeletionService(
            config,
            stores,
            None,
        ),
        SqlAlchemyRetrievalService(
            config,
            stores,
            None,
        ),
        store,
    )


def _seed_b2_copy(database_url: str) -> None:
    with session_scope(make_session_factory(database_url)) as session:
        deep = session.get(CollectionArchiveCopyRecord, (COLLECTION_ID, "deep"))
        assert deep is not None
        b2 = CollectionArchiveCopyRecord(
            collection_id=COLLECTION_ID,
            store="b2",
            state="uploaded",
            archive_storage_prefix="archives/b2-opaque-docs",
            backend="b2",
            storage_class="STANDARD",
            last_uploaded_at=deep.last_uploaded_at,
            last_verified_at=deep.last_verified_at,
        )
        session.add(b2)
        for current in sorted(deep.objects, key=lambda item: item.object_order):
            copied = CollectionArchiveObjectRecord(
                collection_id=COLLECTION_ID,
                store="b2",
                object_id=current.object_id,
                object_order=current.object_order,
                kind=current.kind,
                object_path=current.object_path.replace(
                    "archives/opaque-docs",
                    "archives/b2-opaque-docs",
                ),
                plaintext_bytes=current.plaintext_bytes,
                stored_bytes=current.stored_bytes,
                sha256=current.sha256,
                stored_sha256=current.stored_sha256,
                backend="b2",
                storage_class="STANDARD",
                uploaded_at=current.uploaded_at,
                verified_at=current.verified_at,
            )
            b2.objects.append(copied)
            for placement in current.placements:
                copied.placements.append(
                    CollectionArchiveFileObjectRecord(
                        collection_id=COLLECTION_ID,
                        store="b2",
                        path=placement.path,
                        sequence=placement.sequence,
                        object_id=current.object_id,
                        file_offset=placement.file_offset,
                        bytes=placement.bytes,
                        member=placement.member,
                    )
                )


def _create_retrieval(service: SqlAlchemyRetrievalService) -> dict[str, object]:
    files = [(COLLECTION_ID, FILE_PATH)]
    plan = service.plan(files)
    return service.create(app="local", files=files, plan_etag=str(plan["etag"]))


def test_active_retrieval_blocks_collection_deletion(database_url: str) -> None:
    _seed(database_url)
    deletion, retrieval, _store = _services(database_url)
    job = _create_retrieval(retrieval)

    blocked = deletion.plan(COLLECTION_ID)

    assert blocked["status"] == "blocked"
    assert blocked["challenge"] is None
    assert blocked["blockers"] == [f"retrieval job is active: {job['id']}"]
    retrieval.acknowledge(app="local", job_id=str(job["id"]))
    assert deletion.plan(COLLECTION_ID)["status"] == "ready"


def test_deletion_marker_rejects_retrieval_started_during_remote_delete(
    database_url: str,
) -> None:
    _seed(database_url)
    deletion, retrieval, store = _services(database_url)
    plan = deletion.plan(COLLECTION_ID)
    challenge = str(plan["challenge"])
    failures: list[BaseException] = []

    def delete_collection() -> None:
        try:
            deletion.delete(COLLECTION_ID, challenge=challenge, initiator=DELETER)
        except BaseException as exc:  # pragma: no cover - asserted by the parent thread
            failures.append(exc)

    thread = threading.Thread(target=delete_collection)
    thread.start()
    assert store.delete_started.wait(10)
    try:
        with pytest.raises(Conflict, match="collection deletion is active"):
            _create_retrieval(retrieval)
    finally:
        store.allow_delete.set()
        thread.join(10)

    assert not thread.is_alive()
    assert failures == []
    assert store.deleted == [("segment-000000000000", "manifest", "proof")]
    factory = make_session_factory(database_url)
    with session_scope(factory) as session:
        assert session.get(CollectionRecord, COLLECTION_ID) is None
        assert session.get(CollectionDeletionRecord, COLLECTION_ID) is None
        assert session.scalar(select(RetrievalJobRecord)) is None


def test_metadata_publication_and_deletion_cannot_cross_collection_archive_operations(
    database_url: str,
) -> None:
    _seed(database_url)
    deletion, _retrieval, store = _services(database_url)
    factory = make_session_factory(database_url)
    with session_scope(factory) as session:
        session.add(
            CollectionMetadataPublicationRecord(
                collection_id=COLLECTION_ID,
                store="deep",
                desired_revision=1,
                state="pending",
                attempt_count=0,
                next_attempt_at="2026-01-01T00:00:00.000000Z",
            )
        )
    publisher = SqlAlchemyArchiveMaintenanceService(
        RuntimeConfig(database_url=database_url),
        ArchiveStoreRegistry({"deep": _archive_store_binding(store)}),
    )
    deletion_plan = deletion.plan(COLLECTION_ID)
    failures: list[BaseException] = []

    def publish_metadata() -> None:
        try:
            assert publisher.process_due_metadata_publications(limit=1) == 1
        except BaseException as exc:  # pragma: no cover - asserted by the parent thread
            failures.append(exc)

    thread = threading.Thread(target=publish_metadata)
    thread.start()
    assert store.metadata_started.wait(10)
    try:
        with pytest.raises(Conflict, match="plan changed"):
            deletion.delete(
                COLLECTION_ID,
                challenge=str(deletion_plan["challenge"]),
                initiator=DELETER,
            )
    finally:
        store.allow_metadata.set()
        thread.join(10)

    assert not thread.is_alive()
    assert failures == []
    assert len(store.published_metadata) == 1
    assert deletion.plan(COLLECTION_ID)["status"] == "ready"


def test_deletion_marker_prevents_a_due_metadata_publication_claim(
    database_url: str,
) -> None:
    _seed(database_url)
    deletion, _retrieval, store = _services(database_url)
    with session_scope(make_session_factory(database_url)) as session:
        session.add(
            CollectionMetadataPublicationRecord(
                collection_id=COLLECTION_ID,
                store="deep",
                desired_revision=1,
                state="pending",
                attempt_count=0,
                next_attempt_at="2026-01-01T00:00:00.000000Z",
            )
        )
    publisher = SqlAlchemyArchiveMaintenanceService(
        RuntimeConfig(database_url=database_url),
        ArchiveStoreRegistry({"deep": _archive_store_binding(store)}),
    )
    challenge = str(deletion.plan(COLLECTION_ID)["challenge"])
    failures: list[BaseException] = []

    def delete_collection() -> None:
        try:
            deletion.delete(COLLECTION_ID, challenge=challenge, initiator=DELETER)
        except BaseException as exc:  # pragma: no cover - asserted by the parent thread
            failures.append(exc)

    thread = threading.Thread(target=delete_collection)
    thread.start()
    assert store.delete_started.wait(10)
    try:
        assert publisher.process_due_metadata_publications(limit=1) == 0
        assert store.published_metadata == []
    finally:
        store.allow_delete.set()
        thread.join(10)

    assert not thread.is_alive()
    assert failures == []


def test_retirement_marker_forces_retrieval_to_replan_onto_a_retained_copy(
    database_url: str,
) -> None:
    _seed(database_url)
    _seed_b2_copy(database_url)
    base = RuntimeConfig(database_url=database_url)
    archive = base.archive_store("archive")
    b2_config = replace(
        archive,
        name="b2",
        backend="b2",
        storage_class="STANDARD",
    )
    config = replace(
        base,
        archive_stores={"deep": replace(archive, name="deep"), "b2": b2_config},
        archive_write_store="deep",
        archive_read_order=("deep", "b2"),
    )
    deep = BlockingArchiveStore()
    b2 = BlockingArchiveStore()
    b2.allow_delete.set()
    stores = ArchiveStoreRegistry(
        {
            "deep": _archive_store_binding(deep),
            "b2": _archive_store_binding(b2),
        }
    )
    retirement = SqlAlchemyArchiveCopyRetirementService(config, stores)
    retrieval = SqlAlchemyRetrievalService(
        config,
        stores,
        None,
    )
    files = [(COLLECTION_ID, FILE_PATH)]
    stale_plan = retrieval.plan(files)
    assert {str(item["source_store"]) for item in stale_plan["objects"]} == {"deep"}
    challenge = str(retirement.plan(COLLECTION_ID, store="deep")["challenge"])
    failures: list[BaseException] = []

    def retire_copy() -> None:
        try:
            retirement.retire(COLLECTION_ID, store="deep", challenge=challenge)
        except BaseException as exc:  # pragma: no cover - asserted by the parent thread
            failures.append(exc)

    thread = threading.Thread(target=retire_copy)
    thread.start()
    assert deep.delete_started.wait(10)
    try:
        current_plan = retrieval.plan(files)
        assert {str(item["source_store"]) for item in current_plan["objects"]} == {"b2"}
        with pytest.raises(Conflict, match="retrieval plan changed"):
            retrieval.create(
                app="local",
                files=files,
                plan_etag=str(stale_plan["etag"]),
            )
    finally:
        deep.allow_delete.set()
        thread.join(10)

    assert not thread.is_alive()
    assert failures == []
