from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
from riverhog_age import decrypt_age_scrypt
from riverhog_core.app_permissions import (
    ALL_RESOURCES,
    CATALOG_READ,
    COLLECTIONS_CREATE,
    PROVENANCE_EXPORT,
    PROVENANCE_READ,
    ApplicationAccess,
    ApplicationPrincipal,
)
from riverhog_core.archive_ingress_registry import ArchiveIngressStore, ArchiveIngressStoreRegistry
from riverhog_core.archive_manifest import parse_collection_archive_manifest
from riverhog_core.archive_store_registry import ArchiveStoreRegistry
from riverhog_core.catalog_db import initialize_db, make_session_factory, session_scope
from riverhog_core.catalog_models import (
    CollectionArchiveObjectRecord,
    CollectionArchiveObjectUploadRecord,
    CollectionFileProvenanceRecord,
    CollectionProvenanceEntityRecord,
    CollectionProvenanceJournalRecord,
    CollectionUploadRecord,
    TagRecord,
)
from riverhog_core.proofs import ProofStamper
from riverhog_core.runtime_config import RuntimeConfig
from riverhog_core.services.collection_uploads import SqlAlchemyCollectionUploadService
from riverhog_core.services.provenance import SqlAlchemyProvenanceService
from riverhog_protocol.errors import Conflict, NotFound
from riverhog_protocol.manifest import collection_content_etag
from riverhog_provenance import (
    FileProvenanceBinding,
    build_provenance_archive,
    create_observation_journal,
    validate_journal,
    validate_provenance_archive,
)

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
    service, config, _multipart, _root = _service_with_ingress(
        tmp_path,
        proof_stamper=proof_stamper,
    )
    return service, config


def _service_with_ingress(
    tmp_path: Path,
    *,
    proof_stamper: ProofStamper | None = None,
) -> tuple[
    SqlAlchemyCollectionUploadService,
    RuntimeConfig,
    MemoryMultipartStore,
    MemoryImmutableStore,
]:
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
    multipart = MemoryMultipartStore()
    root = MemoryImmutableStore()
    ingress = ArchiveIngressStore(
        multipart=multipart,
        root=root,
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
        multipart,
        root,
    )


def test_captured_and_omitted_file_provenance_is_one_immutable_mixed_archive(
    tmp_path: Path,
) -> None:
    service, config, _multipart, root = _service_with_ingress(tmp_path)
    contents = {
        "captured.bin": b"captured payload\n",
        "operator-note.txt": b"explicitly omitted provenance\n",
    }
    observed = tmp_path / "captured.bin"
    observed.write_bytes(contents["captured.bin"])
    journal = create_observation_journal(
        observed,
        relative_path="captured.bin",
        host_id="urn:uuid:00000000-0000-4000-8000-000000000001",
        agent_name="riverhog-test-client",
        agent_version="1.0.0",
    )
    summary = validate_journal(journal)
    bindings = (
        FileProvenanceBinding(
            path="captured.bin",
            bytes=len(contents["captured.bin"]),
            sha256=hashlib.sha256(contents["captured.bin"]).hexdigest(),
            status="captured",
            journal_id=summary.journal_id,
            current_state_id=summary.current_state_id,
        ),
        FileProvenanceBinding(
            path="operator-note.txt",
            bytes=len(contents["operator-note.txt"]),
            sha256=hashlib.sha256(contents["operator-note.txt"]).hexdigest(),
            status="omitted",
            omission_reason="operator explicitly omitted unavailable source provenance",
        ),
    )
    provenance = build_provenance_archive(
        bindings=bindings,
        journals={summary.journal_id: journal},
    )

    opened = service.create_or_resume(
        idempotency_key="mixed-provenance-upload",
        tags=("docs",),
        ingest_source="fixture",
        archive_store=None,
        initiator=_CREATOR,
        event_context=None,
        provenance_mode="captured",
        provenance_omission_reason=None,
    )
    collection_id = int(opened["collection_id"])
    service.put_provenance_journal(
        collection_id,
        summary.journal_id,
        content=journal,
        sha256=summary.journal_sha256,
    )
    service.register_files(
        collection_id,
        tuple(
            {
                "path": binding.path,
                "bytes": binding.bytes,
                "sha256": binding.sha256,
                "provenance": {
                    "status": binding.status,
                    **(
                        {
                            "journal_id": binding.journal_id,
                            "current_state_id": binding.current_state_id,
                        }
                        if binding.status == "captured"
                        else {"omission_reason": binding.omission_reason}
                    ),
                },
            }
            for binding in bindings
        ),
    )
    service.complete(
        collection_id,
        files_total=len(bindings),
        content_etag=collection_content_etag(
            (binding.path, binding.bytes, binding.sha256) for binding in bindings
        ),
        provenance_etag=provenance.identity,
    )
    for volume in service.list_volumes(collection_id)["volumes"]:
        for unit in volume["units"]:
            service.upload_unit(
                collection_id,
                str(volume["volume_id"]),
                int(unit["unit"]),
                plan_sha256=str(volume["plan_sha256"]),
                content=b"".join(contents[str(source["path"])] for source in unit["sources"]),
            )
    assert service.process_due_finalizations() == 1
    assert service.get(collection_id)["provenance_mode"] == "mixed"

    with session_scope(make_session_factory(config.database_url)) as session:
        objects = list(
            session.query(CollectionArchiveObjectRecord)
            .filter(CollectionArchiveObjectRecord.collection_id == collection_id)
            .order_by(CollectionArchiveObjectRecord.object_order)
        )
        bindings_by_path = {
            item.path: item
            for item in session.query(CollectionFileProvenanceRecord).filter_by(
                collection_id=collection_id
            )
        }
        exact = session.get(
            CollectionProvenanceJournalRecord,
            (collection_id, summary.journal_id),
        )
        projected = list(
            session.query(CollectionProvenanceEntityRecord).filter_by(
                collection_id=collection_id,
                journal_id=summary.journal_id,
            )
        )
    assert [item.kind for item in objects] == [
        "pack",
        "provenance-bundle",
        "provenance-index",
        "manifest",
        "proof",
    ]
    assert bindings_by_path["captured.bin"].status == "captured"
    assert bindings_by_path["operator-note.txt"].status == "omitted"
    assert exact is not None and exact.journal_bytes == journal
    assert projected

    stored_by_suffix = {path.rsplit("/", 1)[-1]: stored for path, stored in root.objects.items()}
    index_stored = stored_by_suffix["index.json.age"]
    index = decrypt_age_scrypt(index_stored.content, config.archive_passphrase)
    bundle_record = next(item for item in objects if item.kind == "provenance-bundle")
    bundle_stored = root.objects[bundle_record.object_path]
    bundle = decrypt_age_scrypt(bundle_stored.content, config.archive_passphrase)
    validated = validate_provenance_archive(index, {bundle_record.object_id: bundle})
    assert validated.identity == provenance.identity
    assert validated.bindings == bindings
    assert validated.journal_bytes == {summary.journal_id: journal}

    manifest = decrypt_age_scrypt(
        stored_by_suffix["manifest.json.age"].content,
        config.archive_passphrase,
    )
    parsed = parse_collection_archive_manifest(manifest)
    provenance_descriptor = parsed["provenance"]
    assert isinstance(provenance_descriptor, dict)
    assert provenance_descriptor["identity"] == provenance.identity
    assert provenance_descriptor["index"]["sha256"] == provenance.identity
    assert [item["sha256"] for item in provenance_descriptor["bundles"]] == [
        item.sha256 for item in provenance.bundles
    ]

    reader = ApplicationPrincipal(
        app="catalog-reader",
        key_id="key-reader",
        access=frozenset(
            {
                ApplicationAccess(CATALOG_READ, ALL_RESOURCES),
                ApplicationAccess(PROVENANCE_EXPORT, ALL_RESOURCES),
            }
        ),
    )
    provenance_service = SqlAlchemyProvenanceService(config)
    listed = provenance_service.list_files(
        collection_id,
        page=1,
        per_page=100,
        q=None,
        status=None,
        sort="path",
        order="asc",
        all_items=True,
        principal=reader,
    )
    assert listed["provenance_mode"] == "mixed"
    assert [item["provenance"]["status"] for item in listed["files"]] == [
        "captured",
        "omitted",
    ]
    shown = provenance_service.show_file(collection_id, "captured.bin", principal=reader)
    assert shown["journal"]["journal_id"] == summary.journal_id
    traced = provenance_service.trace_file(collection_id, "captured.bin", principal=reader)
    assert [item["journal_id"] for item in traced["journals"]] == [summary.journal_id]
    exported, exported_sha256 = provenance_service.export_journal(
        collection_id,
        summary.journal_id,
        principal=reader,
    )
    assert exported == journal
    assert exported_sha256 == summary.journal_sha256
    assert provenance_service.verify(collection_id, principal=reader)["valid"] is True

    catalog_only = ApplicationPrincipal(
        app="catalog-only",
        key_id="key-catalog",
        access=frozenset({ApplicationAccess(CATALOG_READ, ALL_RESOURCES)}),
    )
    with pytest.raises(NotFound):
        provenance_service.show_file(collection_id, "captured.bin", principal=catalog_only)
    read_only = ApplicationPrincipal(
        app="provenance-reader",
        key_id="key-provenance",
        access=frozenset(
            {
                ApplicationAccess(CATALOG_READ, ALL_RESOURCES),
                ApplicationAccess(PROVENANCE_READ, ALL_RESOURCES),
            }
        ),
    )
    with pytest.raises(NotFound):
        provenance_service.export_journal(
            collection_id,
            summary.journal_id,
            principal=read_only,
        )

    with session_scope(make_session_factory(config.database_url)) as session:
        session.query(CollectionProvenanceEntityRecord).filter_by(
            collection_id=collection_id
        ).delete()
        session.query(CollectionFileProvenanceRecord).filter_by(
            collection_id=collection_id
        ).delete()
        session.query(CollectionProvenanceJournalRecord).filter_by(
            collection_id=collection_id
        ).delete()
    rebuilt = provenance_service.rebuild_catalog_projection(
        collection_id,
        index_content=index,
        bundles={bundle_record.object_id: bundle},
    )
    assert rebuilt == {
        "collection_id": collection_id,
        "provenance_mode": "mixed",
        "provenance_etag": provenance.identity,
        "files": 2,
        "journals": 1,
        "entities": len(projected),
    }
    assert provenance_service.verify(collection_id, principal=reader)["valid"] is True


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
        provenance_mode="omitted",
        provenance_omission_reason="fixture does not exercise source observation",
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
        provenance_mode="omitted",
        provenance_omission_reason="fixture does not exercise source observation",
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
            provenance_mode="omitted",
            provenance_omission_reason="fixture does not exercise source observation",
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
        provenance_mode="omitted",
        provenance_omission_reason="fixture does not exercise source observation",
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
        provenance_mode="omitted",
        provenance_omission_reason="fixture does not exercise source observation",
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
