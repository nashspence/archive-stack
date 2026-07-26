from __future__ import annotations

import hashlib
import os
import threading
from collections.abc import Iterator, Sequence
from typing import cast

import pytest
from riverhog_core.app_permissions import ApplicationPrincipal
from riverhog_core.archive_store_registry import ArchiveStoreRegistry
from riverhog_core.catalog_db import (
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
    CollectionRecord,
    CollectionTagRecord,
    RetrievalJobRecord,
    TagRecord,
)
from riverhog_core.ports.archive_store import ArchiveObjectIdentity, ArchiveStore
from riverhog_core.ports.upload_store import UploadStore
from riverhog_core.runtime_config import RuntimeConfig
from riverhog_core.services.collection_deletions import SqlAlchemyCollectionDeletionService
from riverhog_core.services.retrieval import SqlAlchemyRetrievalService
from riverhog_protocol.errors import Conflict
from sqlalchemy import select

from tests.fixtures.crypto import FixtureProofVerifier

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
        self.deleted: list[tuple[str, ...]] = []

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

class UnusedUploadStore:
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
        for order, (object_id, kind, stored_bytes) in enumerate(
            (("data-000000", "file", 100), ("manifest", "manifest", 20), ("proof", "proof", 10))
        ):
            copy.objects.append(
                CollectionArchiveObjectRecord(
                    collection_id=COLLECTION_ID,
                    store="deep",
                    object_id=object_id,
                    object_order=order,
                    kind=kind,
                    object_path=f"archives/opaque-docs/{object_id}.age",
                    plaintext_bytes=stored_bytes - 1,
                    stored_bytes=stored_bytes,
                    sha256=chr(ord("a") + order) * 64,
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
                object_id="data-000000",
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
    store = BlockingArchiveStore()
    stores = ArchiveStoreRegistry({"deep": cast(ArchiveStore, store)})
    return (
        SqlAlchemyCollectionDeletionService(
            config,
            stores,
            cast(UploadStore, UnusedUploadStore()),
            None,
        ),
        SqlAlchemyRetrievalService(
            config,
            stores,
            None,
            proof_verifier=FixtureProofVerifier(),
        ),
        store,
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
    assert store.deleted == [("data-000000", "manifest", "proof")]
    factory = make_session_factory(database_url)
    with session_scope(factory) as session:
        assert session.get(CollectionRecord, COLLECTION_ID) is None
        assert session.get(CollectionDeletionRecord, COLLECTION_ID) is None
        assert session.scalar(select(RetrievalJobRecord)) is None
