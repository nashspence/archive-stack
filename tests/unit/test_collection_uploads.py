from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
from riverhog_core.app_permissions import (
    ALL_RESOURCES,
    COLLECTIONS_CREATE,
    ApplicationAccess,
    ApplicationPrincipal,
)
from riverhog_core.archive_ingress_registry import ArchiveIngressStore, ArchiveIngressStoreRegistry
from riverhog_core.archive_store_registry import ArchiveStoreRegistry
from riverhog_core.catalog_db import initialize_db, make_session_factory, session_scope
from riverhog_core.catalog_models import (
    CollectionArchiveObjectRecord,
    CollectionArchiveObjectUploadRecord,
    CollectionUploadRecord,
    TagRecord,
)
from riverhog_core.proofs import ProofStamper
from riverhog_core.runtime_config import RuntimeConfig
from riverhog_core.services.collection_uploads import SqlAlchemyCollectionUploadService
from riverhog_protocol.errors import Conflict
from riverhog_protocol.manifest import collection_content_etag

from tests.fixtures.crypto import FixtureProofStamper
from tests.unit.archive_object_fixtures import MemoryArchiveStore, as_archive_store
from tests.unit.db_helpers import sqlite_url
from tests.unit.test_archive_root import MemoryImmutableStore
from tests.unit.test_pack_upload import MemoryMultipartStore

_CREATOR = ApplicationPrincipal(
    app="uploader",
    key_id="key-1",
    access=frozenset({ApplicationAccess(COLLECTIONS_CREATE, ALL_RESOURCES)}),
)


class _UnusedRangeStore:
    def iter_object_range(self, **_: object):
        raise AssertionError("ingress does not read archive ranges")


class _FailOnceProofStamper:
    def __init__(self) -> None:
        self.attempts = 0

    def stamp(self, manifest_path: Path) -> Path:
        self.attempts += 1
        if self.attempts == 1:
            raise RuntimeError("temporary proof service failure")
        return FixtureProofStamper().stamp(manifest_path)


def _service(
    tmp_path: Path,
    *,
    proof_stamper: ProofStamper | None = None,
) -> tuple[SqlAlchemyCollectionUploadService, RuntimeConfig]:
    database_url = sqlite_url(tmp_path / "catalog.sqlite3")
    config = RuntimeConfig(database_url=database_url, archive_scrypt_work_factor=1)
    initialize_db(database_url)
    with session_scope(make_session_factory(database_url)) as session:
        session.add(
            TagRecord(
                id="docs",
                created_by_app="fixture",
                created_at="2026-08-08T00:00:00.000000Z",
            )
        )
    archive_store = MemoryArchiveStore()
    ingress = ArchiveIngressStore(
        multipart=MemoryMultipartStore(),
        root=MemoryImmutableStore(),
        ranges=_UnusedRangeStore(),
    )
    return (
        SqlAlchemyCollectionUploadService(
            config,
            ArchiveStoreRegistry({"archive": as_archive_store(archive_store)}),
            ArchiveIngressStoreRegistry({"archive": ingress}),
            proof_stamper=proof_stamper or FixtureProofStamper(),
        ),
        config,
    )


@pytest.mark.parametrize(
    "tags",
    [pytest.param(("docs",), id="tagged"), pytest.param((), id="untagged")],
)
def test_small_collection_moves_directly_from_source_unit_to_final_custody(
    tmp_path: Path, tags: tuple[str, ...]
) -> None:
    service, config = _service(tmp_path)
    content = b"direct final archive\n"
    sha256 = hashlib.sha256(content).hexdigest()

    opened = service.create_or_resume(
        idempotency_key="upload-1",
        tags=tags,
        ingest_source="fixture",
        archive_store=None,
        initiator=_CREATOR,
        event_context=None,
    )
    assert opened["tags"] == list(tags)
    collection_id = int(opened["collection_id"])
    registered = service.register_files(
        collection_id,
        ({"path": "document.txt", "bytes": len(content), "sha256": sha256},),
    )
    assert registered["volumes"] == []

    closed = service.complete(
        collection_id,
        files_total=1,
        content_etag=collection_content_etag((("document.txt", len(content), sha256),)),
    )
    assert closed["state"] == "uploading"
    volume = service.list_volumes(collection_id)["volumes"][0]
    assert volume["kind"] == "pack"
    unit = volume["units"][0]
    assert unit["sources"] == [
        {
            "path": "document.txt",
            "offset": 0,
            "bytes": len(content),
            "sha256": sha256,
        }
    ]

    committed = service.upload_unit(
        collection_id,
        str(volume["volume_id"]),
        0,
        plan_sha256=str(volume["plan_sha256"]),
        content=content,
    )
    assert committed["state"] == "committed"
    queued = service.get(collection_id)
    assert queued["state"] == "finalizing"
    assert queued["archive_phase"] == "retry_wait"
    assert service.process_due_finalizations() == 1
    finalized = service.get(collection_id)
    assert finalized["state"] == "finalized"
    assert finalized["tags"] == list(tags)
    assert finalized["uploaded_bytes"] == len(content)

    with session_scope(make_session_factory(config.database_url)) as session:
        objects = list(
            session.query(CollectionArchiveObjectRecord)
            .filter(CollectionArchiveObjectRecord.collection_id == collection_id)
            .order_by(CollectionArchiveObjectRecord.object_order)
        )
    assert [current.object_id for current in objects] == [
        "pack-000000000000",
        "manifest",
        "proof",
    ]
    assert objects[0].object_path.endswith("/volumes/pack-000000000000.tar.age")
    assert objects[1].object_path.endswith("/manifest.json.age")
    assert objects[2].object_path.endswith("/manifest.json.ots.age")

    resumed = service.create_or_resume(
        idempotency_key="upload-1",
        tags=tags,
        ingest_source="fixture",
        archive_store=None,
        initiator=_CREATOR,
        event_context=None,
    )
    assert resumed["collection_id"] == collection_id
    assert resumed["state"] == "finalized"
    changed_tags = () if tags else ("docs",)
    with pytest.raises(Conflict, match="idempotency identity changed"):
        service.create_or_resume(
            idempotency_key="upload-1",
            tags=changed_tags,
            ingest_source="fixture",
            archive_store=None,
            initiator=_CREATOR,
            event_context=None,
        )


def test_startup_reconciles_interrupted_finalization_from_its_durable_checkpoint(
    tmp_path: Path,
) -> None:
    service, config = _service(tmp_path)
    content = b"restart-safe direct final archive\n"
    sha256 = hashlib.sha256(content).hexdigest()
    opened = service.create_or_resume(
        idempotency_key="restart-upload",
        tags=("docs",),
        ingest_source="fixture",
        archive_store=None,
        initiator=_CREATOR,
        event_context=None,
    )
    collection_id = int(opened["collection_id"])
    service.register_files(
        collection_id,
        ({"path": "document.txt", "bytes": len(content), "sha256": sha256},),
    )
    service.complete(
        collection_id,
        files_total=1,
        content_etag=collection_content_etag((("document.txt", len(content), sha256),)),
    )
    volume = service.list_volumes(collection_id)["volumes"][0]
    service.upload_unit(
        collection_id,
        str(volume["volume_id"]),
        0,
        plan_sha256=str(volume["plan_sha256"]),
        content=content,
    )

    with session_scope(make_session_factory(config.database_url)) as session:
        upload = session.get(CollectionUploadRecord, collection_id)
        stored_volume = session.get(
            CollectionArchiveObjectUploadRecord,
            (collection_id, str(volume["volume_id"])),
        )
        assert upload is not None
        assert stored_volume is not None
        assert stored_volume.checkpoint_json is not None
        stored_volume.sealed_receipt_json = None
        upload.state = "finalizing"
        upload.archive_phase = "finalizing"
        upload.archive_next_attempt_at = None

    assert service.requeue_interrupted_finalizations_for_startup() == 1
    recovered = service.get(collection_id)
    assert recovered["state"] == "finalizing"
    assert recovered["archive_phase"] == "retry_wait"
    assert recovered["latest_failure"] == "archive finalization interrupted before completion"
    assert service.process_due_finalizations() == 1
    assert service.get(collection_id)["state"] == "finalized"


def test_due_finalization_retries_a_temporary_publication_failure(tmp_path: Path) -> None:
    proof_stamper = _FailOnceProofStamper()
    service, config = _service(tmp_path, proof_stamper=proof_stamper)
    content = b"retryable direct final archive\n"
    sha256 = hashlib.sha256(content).hexdigest()
    opened = service.create_or_resume(
        idempotency_key="retry-upload",
        tags=("docs",),
        ingest_source="fixture",
        archive_store=None,
        initiator=_CREATOR,
        event_context=None,
    )
    collection_id = int(opened["collection_id"])
    service.register_files(
        collection_id,
        ({"path": "document.txt", "bytes": len(content), "sha256": sha256},),
    )
    service.complete(
        collection_id,
        files_total=1,
        content_etag=collection_content_etag((("document.txt", len(content), sha256),)),
    )
    volume = service.list_volumes(collection_id)["volumes"][0]
    service.upload_unit(
        collection_id,
        str(volume["volume_id"]),
        0,
        plan_sha256=str(volume["plan_sha256"]),
        content=content,
    )

    assert service.process_due_finalizations() == 1
    retry_wait = service.get(collection_id)
    assert retry_wait["state"] == "finalizing"
    assert retry_wait["archive_phase"] == "retry_wait"
    assert retry_wait["latest_failure"] == ("RuntimeError: temporary proof service failure")
    with session_scope(make_session_factory(config.database_url)) as session:
        upload = session.get(CollectionUploadRecord, collection_id)
        assert upload is not None
        assert upload.archive_attempt_count == 1
        assert upload.archive_next_attempt_at is not None
        upload.archive_next_attempt_at = upload.archive_phase_updated_at

    assert service.process_due_finalizations() == 1
    assert service.get(collection_id)["state"] == "finalized"
    assert proof_stamper.attempts == 2
