from __future__ import annotations

import hashlib
from dataclasses import replace
from pathlib import Path

import pytest
from riverhog_age import decrypt_age_scrypt
from riverhog_archive_contracts import CollectionArchiveManifest, RecoveryDescriptor
from riverhog_core.app_permissions import (
    ALL_RESOURCES,
    CATALOG_READ,
    COLLECTIONS_CREATE,
    COLLECTIONS_DELETE,
    COLLECTION_TAGS_MANAGE,
    PROVENANCE_EXPORT,
    PROVENANCE_READ,
    ApplicationAccess,
    ApplicationPrincipal,
)
from riverhog_core.archive_store_registry import ArchiveStoreRegistry
from riverhog_core.catalog_db import initialize_db, make_session_factory, session_scope
from riverhog_core.catalog_models import (
    CollectionArchiveObjectRecord,
    CollectionArchiveObjectUploadRecord,
    CollectionDescriptionPublicationRecord,
    CollectionFileProvenanceRecord,
    CollectionFileRecord,
    CollectionProvenanceEntityRecord,
    CollectionProvenanceExternalStateReferenceRecord,
    CollectionProvenanceJournalChunkRecord,
    CollectionProvenanceJournalRecord,
    CollectionRecord,
    CollectionTagNodeRecord,
    CollectionUploadProvenanceJournalChunkRecord,
    CollectionUploadProvenanceJournalRecord,
    CollectionUploadRecord,
    CollectionUploadTagNodeReferenceRecord,
    RetrievalCacheLeaseRecord,
    RetrievalCacheObjectRecord,
)
from riverhog_core.catalog_workflow_models import CollectionProcessingClaimRecord
from riverhog_core.collection_plan import CollectionVolumePolicy
from riverhog_core.domain.archive import ArchiveFile
from riverhog_core.incremental_plan import (
    incremental_volume_planner_checkpoint_bytes,
    parse_incremental_volume_planner_checkpoint,
)
from riverhog_core.ports.retrieval_cache import RetrievalCacheAdmission
from riverhog_core.runtime_config import RuntimeConfig
from riverhog_core.services.collection_uploads import SqlAlchemyCollectionUploadService
from riverhog_core.services.catalog_sync import SqlAlchemyCatalogSyncService
from riverhog_core.services.lifecycle_events import SqlAlchemyLifecycleEventService
from riverhog_core.services.provenance import SqlAlchemyProvenanceService
from riverhog_core.throughput import ArchiveThroughputTuning, log_transfer_timing
from riverhog_protocol import (
    COLLECTION_DESCRIPTION_RELATIVE_PATH,
    COLLECTION_UPLOAD_PROVENANCE_APPEND_BYTES_MAX,
    CollectionDescriptionDocument,
    CollectionUploadProvenanceJournalCreateDocument,
    CollectionUploadRawDigestBatchDocument,
)
from riverhog_protocol.errors import Conflict, NotFound
from riverhog_protocol.manifest import collection_content_identity
from riverhog_protocol.raw_ingress import ordered_raw_part_commitment
from riverhog_provenance import (
    FileProvenanceBinding,
    ProvenanceRootDocument,
    ProvenanceTerminalDocument,
    ProvenanceVolumeDocument,
    create_observation_journal,
    parse_binding_segment,
    update_ordered_volume_commitment,
    validate_journal,
)
from sqlalchemy import select

from tests.provenance_observer import native_provenance_observer
from tests.unit.archive_object_fixtures import MemoryArchiveStore, archive_store_binding
from tests.unit.db_helpers import sqlite_url
from tests.unit.test_archive_root import MemoryImmutableStore
from tests.unit.test_pack_upload import MemoryResumableStore

_CREATOR = ApplicationPrincipal(
    app="uploader",
    key_id="key-1",
    access=frozenset({ApplicationAccess(COLLECTIONS_CREATE, ALL_RESOURCES)}),
)

_TAGGED_CREATOR = ApplicationPrincipal(
    app="tagged-uploader",
    key_id="tagged-key-1",
    access=frozenset(
        {
            ApplicationAccess(COLLECTIONS_CREATE, ALL_RESOURCES),
            ApplicationAccess(COLLECTION_TAGS_MANAGE, ALL_RESOURCES),
        }
    ),
)

_DELETER = ApplicationPrincipal(
    app="operator",
    key_id="key-operator",
    access=frozenset({ApplicationAccess(COLLECTIONS_DELETE, ALL_RESOURCES)}),
)

_OTHER_DELETER = ApplicationPrincipal(
    app="other-operator",
    key_id="key-other",
    access=frozenset({ApplicationAccess(COLLECTIONS_DELETE, "collection:999")}),
)


class _UnusedRangeStore:
    def iter_object_range(self, **_: object):
        raise AssertionError("collection upload does not read archive ranges")


class _MemoryResumableCache:
    def __init__(self) -> None:
        self.resumable = MemoryResumableStore()

    def admit(
        self,
        *,
        owner: str,
        source_store: str,
        collection_id: int,
        object_id: str,
        expected_bytes: int,
    ) -> RetrievalCacheAdmission:
        path = f"cache/{source_store}/{collection_id}/{object_id}"
        session = self.resumable.begin_write(
            object_path=path,
            expected_bytes=expected_bytes,
            content_type="application/octet-stream",
            metadata={},
        )
        return RetrievalCacheAdmission(
            owner=owner,
            cache_store="memory",
            source_store=source_store,
            collection_id=collection_id,
            object_id=object_id,
            object_path=path,
            expected_bytes=expected_bytes,
            write_token=session.write_token,
            admitted_at="2026-08-08T00:00:00.000000Z",
        )

    def resumable_object_store(self, **_: object) -> MemoryResumableStore:
        return self.resumable

    def release(self, *, owner: str) -> int:
        _ = owner
        return 0

    def is_current(self, *, admission: RetrievalCacheAdmission) -> bool:
        _ = admission
        return True

    def reap_abandoned_populations(self, *, limit: int = 100) -> int:
        _ = limit
        return 0


def _service(tmp_path: Path) -> tuple[SqlAlchemyCollectionUploadService, RuntimeConfig]:
    service, config, _resumable, _root = _service_with_archive_objects(tmp_path)
    return service, config


def test_upload_description_is_exact_and_bound_to_create_idempotency(tmp_path: Path) -> None:
    service, config = _service(tmp_path)
    description = "Camera seven — morning reference"
    opened = service.create_or_resume(
        idempotency_key="described-upload",
        ingest_source="fixture",
        description=description,
        archive_store=None,
        initiator=_CREATOR,
        event_context=None,
        provenance_mode="omitted",
        provenance_omission_reason="fixture has no source provenance",
    )
    resumed = service.create_or_resume(
        idempotency_key="described-upload",
        ingest_source="fixture",
        description=description,
        archive_store=None,
        initiator=_CREATOR,
        event_context=None,
        provenance_mode="omitted",
        provenance_omission_reason="fixture has no source provenance",
    )

    assert opened["description"] == resumed["description"] == description
    assert opened["description_revision"] is resumed["description_revision"] is None
    assert opened["description_identity"] is resumed["description_identity"] is None
    assert opened["description_publication"] == resumed["description_publication"] == "pending"
    with session_scope(make_session_factory(config.database_url)) as session:
        upload = session.get(CollectionUploadRecord, int(opened["collection_id"]))
        assert upload is not None
        assert (upload.description, upload.description_identity) == (
            description,
            None,
        )
    with pytest.raises(Conflict, match="idempotency identity changed"):
        service.create_or_resume(
            idempotency_key="described-upload",
            ingest_source="fixture",
            description="Different description",
            archive_store=None,
            initiator=_CREATOR,
            event_context=None,
            provenance_mode="omitted",
            provenance_omission_reason="fixture has no source provenance",
        )


def test_open_upload_retains_tag_nodes_until_publication_can_finish(tmp_path: Path) -> None:
    service, config = _service(tmp_path)
    opened = service.create_or_resume(
        idempotency_key="tag-node-retention",
        ingest_source="fixture",
        tags=("stove0/conformance", "source/camera"),
        archive_store=None,
        initiator=_TAGGED_CREATOR,
        event_context=None,
        provenance_mode="omitted",
        provenance_omission_reason="fixture has no source provenance",
    )
    collection_id = int(opened["collection_id"])
    factory = make_session_factory(config.database_url)

    with session_scope(factory) as session:
        retained = set(
            session.scalars(
                select(CollectionUploadTagNodeReferenceRecord.node_digest).where(
                    CollectionUploadTagNodeReferenceRecord.collection_id == collection_id
                )
            )
        )
        assert retained
        assert set(session.scalars(select(CollectionTagNodeRecord.digest))) == retained

    sync = SqlAlchemyCatalogSyncService(config, session_factory=factory)
    assert sync.reap_expired_history(limit=10_000) == 0
    with session_scope(factory) as session:
        assert set(session.scalars(select(CollectionTagNodeRecord.digest))) == retained
        upload = session.get(CollectionUploadRecord, collection_id)
        assert upload is not None
        session.delete(upload)

    assert sync.reap_expired_history(limit=10_000) == 0
    with session_scope(factory) as session:
        assert list(session.scalars(select(CollectionTagNodeRecord.digest))) == []


def _process_until(
    service: SqlAlchemyCollectionUploadService,
    collection_id: int,
    *,
    archive_phase: str = "finalized",
) -> dict[str, object]:
    """Drive bounded finalization claims until the requested externally visible state."""

    for _ in range(256):
        current = service.get(collection_id)
        if current["state"] == "finalized" or current["archive_phase"] == archive_phase:
            return current
        assert service.process_due_finalizations() == 1
    raise AssertionError("bounded finalization did not reach the expected state")


def _upload_provenance_journal(
    service: SqlAlchemyCollectionUploadService,
    collection_id: int,
    journal_id: str,
    content: bytes,
    sha256: str,
) -> dict[str, object]:
    service.create_provenance_journal(
        collection_id,
        journal_id,
        CollectionUploadProvenanceJournalCreateDocument(
            bytes=len(content),
            sha256=sha256,
        ),
    )
    offset = 0
    for start in range(0, len(content), COLLECTION_UPLOAD_PROVENANCE_APPEND_BYTES_MAX):
        chunk = content[start : start + COLLECTION_UPLOAD_PROVENANCE_APPEND_BYTES_MAX]
        service.append_provenance_journal(
            collection_id,
            journal_id,
            offset=offset,
            content=chunk,
        )
        offset += len(chunk)
    service.seal_provenance_journal(collection_id, journal_id)
    for _ in range(64):
        status = service.get_provenance_journal(collection_id, journal_id)
        if status["state"] in {"sealed", "failed"}:
            assert status["state"] == "sealed", status["failure"]
            return status
        assert service.process_due_provenance_journal_validations() == 1
    raise AssertionError("bounded provenance validation did not terminate")


def test_provenance_append_persists_next_ordinal_across_retry_and_restart(
    tmp_path: Path,
) -> None:
    journal_id = "urn:uuid:00000000-0000-4000-8000-000000000077"
    service, config = _service(tmp_path)
    opened = service.create_or_resume(
        idempotency_key="bounded-provenance-append",
        ingest_source="fixture",
        archive_store=None,
        initiator=_CREATOR,
        event_context=None,
        provenance_mode="captured",
        provenance_omission_reason=None,
    )
    collection_id = int(opened["collection_id"])
    chunks = (b"a" * COLLECTION_UPLOAD_PROVENANCE_APPEND_BYTES_MAX, b"terminal")
    content = b"".join(chunks)
    service.create_provenance_journal(
        collection_id,
        journal_id,
        CollectionUploadProvenanceJournalCreateDocument(
            bytes=len(content),
            sha256=hashlib.sha256(content).hexdigest(),
        ),
    )
    service.append_provenance_journal(
        collection_id,
        journal_id,
        offset=0,
        content=chunks[0],
    )
    service.append_provenance_journal(
        collection_id,
        journal_id,
        offset=len(chunks[0]),
        content=chunks[1],
    )

    restarted = SqlAlchemyCollectionUploadService(
        config,
        ArchiveStoreRegistry({"archive": archive_store_binding(MemoryArchiveStore())}),
    )
    replay = restarted.append_provenance_journal(
        collection_id,
        journal_id,
        offset=len(chunks[0]),
        content=chunks[1],
    )
    assert replay["accepted_bytes"] == len(content)
    with session_scope(make_session_factory(config.database_url)) as session:
        journal = session.get(
            CollectionUploadProvenanceJournalRecord,
            (collection_id, journal_id),
        )
        assert journal is not None
        assert journal.next_chunk_ordinal == 2
        assert list(
            session.scalars(
                select(CollectionUploadProvenanceJournalChunkRecord.ordinal)
                .where(
                    CollectionUploadProvenanceJournalChunkRecord.collection_id == collection_id,
                    CollectionUploadProvenanceJournalChunkRecord.journal_id == journal_id,
                )
                .order_by(CollectionUploadProvenanceJournalChunkRecord.ordinal)
            )
        ) == [0, 1]


def _verify_provenance(
    service: SqlAlchemyProvenanceService,
    collection_id: int,
    *,
    principal: ApplicationPrincipal,
) -> dict[str, object]:
    service.request_verification(collection_id, principal=principal)
    for _ in range(256):
        status = service.get_verification(collection_id, principal=principal)
        if status["state"] in {"succeeded", "failed"}:
            assert status["state"] == "succeeded", status.get("failure")
            result = status["result"]
            assert isinstance(result, dict)
            return result
        assert service.process_due_verifications() == 1
    raise AssertionError("bounded provenance verification did not terminate")


def _service_with_archive_objects(
    tmp_path: Path,
    *,
    policy: CollectionVolumePolicy | None = None,
    throughput_tuning: ArchiveThroughputTuning | None = None,
) -> tuple[
    SqlAlchemyCollectionUploadService,
    RuntimeConfig,
    MemoryResumableStore,
    MemoryImmutableStore,
]:
    database_url = sqlite_url(tmp_path / "catalog.sqlite3")
    config = RuntimeConfig(database_url=database_url, archive_scrypt_work_factor=1)
    initialize_db(database_url)
    archive_store = MemoryArchiveStore()
    resumable = MemoryResumableStore()
    root = MemoryImmutableStore()
    binding = replace(
        archive_store_binding(archive_store),
        resumable_objects=resumable,
        immutable_objects=root,
        object_ranges=_UnusedRangeStore(),
    )
    return (
        SqlAlchemyCollectionUploadService(
            config,
            ArchiveStoreRegistry({"archive": binding}),
            policy=policy,
            throughput_tuning=throughput_tuning,
        ),
        config,
        resumable,
        root,
    )


def test_collection_ingress_uses_the_configured_source_read_chunk(
    tmp_path: Path,
) -> None:
    tuning = ArchiveThroughputTuning(source_read_chunk_bytes=256 * 1024)
    service, _config, resumable, _root = _service_with_archive_objects(
        tmp_path,
        throughput_tuning=tuning,
    )

    pack_uploader = service._pack_uploader(
        resumable,
        passphrase_id=_config.archive_active_passphrase_id,
    )
    raw_uploader = service._raw_uploader(
        resumable,
        passphrase_id=_config.archive_active_passphrase_id,
    )
    assert pack_uploader._source_read_chunk_bytes == 256 * 1024
    assert raw_uploader._source_read_chunk_bytes == 256 * 1024
    assert pack_uploader._timing_observer is log_transfer_timing
    assert raw_uploader._timing_observer is log_transfer_timing


def test_upload_resume_keeps_its_frozen_key_generation_after_rotation(tmp_path: Path) -> None:
    database_url = sqlite_url(tmp_path / "catalog.sqlite3")
    initialize_db(database_url)
    passphrases = {
        "collection-test-key-v1": "first archive secret",
        "collection-test-key-v2": "second archive secret",
    }
    archive_store = MemoryArchiveStore()
    resumable = MemoryResumableStore()
    root = MemoryImmutableStore()
    binding = replace(
        archive_store_binding(archive_store),
        resumable_objects=resumable,
        immutable_objects=root,
        object_ranges=_UnusedRangeStore(),
    )

    def service(active: str) -> SqlAlchemyCollectionUploadService:
        return SqlAlchemyCollectionUploadService(
            RuntimeConfig(
                database_url=database_url,
                archive_passphrases=passphrases,
                archive_active_passphrase_id=active,
                archive_scrypt_work_factor=1,
            ),
            ArchiveStoreRegistry({"archive": binding}),
        )

    first = service("collection-test-key-v1").create_or_resume(
        idempotency_key="before-rotation",
        ingest_source="fixture",
        archive_store=None,
        initiator=_CREATOR,
        event_context=None,
        provenance_mode="omitted",
        provenance_omission_reason="fixture has no source provenance",
    )
    rotated = service("collection-test-key-v2")
    resumed = rotated.create_or_resume(
        idempotency_key="before-rotation",
        ingest_source="fixture",
        archive_store=None,
        initiator=_CREATOR,
        event_context=None,
        provenance_mode="omitted",
        provenance_omission_reason="fixture has no source provenance",
    )
    assert first["passphrase_id"] == resumed["passphrase_id"] == "collection-test-key-v1"

    content = b"frozen generation\n"
    sha256 = hashlib.sha256(content).hexdigest()
    collection_id = int(first["collection_id"])
    rotated.register_files(
        collection_id,
        ({"path": "file.txt", "bytes": len(content), "sha256": sha256},),
    )
    rotated.complete(collection_id)
    volume = rotated.list_volumes(collection_id)["volumes"][0]
    rotated.upload_unit(
        collection_id,
        str(volume["volume_id"]),
        0,
        plan_sha256=str(volume["plan_sha256"]),
        content=content,
    )
    finalized = _process_until(rotated, collection_id)
    assert finalized["passphrase_id"] == "collection-test-key-v1"
    descriptor_object = next(
        stored for path, stored in root.objects.items() if path.endswith("/recovery.json")
    )
    descriptor = RecoveryDescriptor.from_json_bytes(descriptor_object.content)
    assert descriptor.encryption.passphrase_id == "collection-test-key-v1"
    encrypted_root = next(
        stored for path, stored in root.objects.items() if path.endswith("/manifest.json.age")
    )
    assert decrypt_age_scrypt(encrypted_root.content, passphrases["collection-test-key-v1"])

    archive_store.new_archive_prefix = "archives/memory/reencrypted-copy"
    after_rotation = rotated.create_or_resume(
        idempotency_key="after-rotation",
        ingest_source="fixture",
        archive_store=None,
        initiator=_CREATOR,
        event_context=None,
        provenance_mode="omitted",
        provenance_omission_reason="fixture has no source provenance",
    )
    assert after_rotation["passphrase_id"] == "collection-test-key-v2"
    reencrypted_collection_id = int(after_rotation["collection_id"])
    rotated.register_files(
        reencrypted_collection_id,
        ({"path": "file.txt", "bytes": len(content), "sha256": sha256},),
    )
    rotated.complete(reencrypted_collection_id)
    reencrypted_volume = rotated.list_volumes(reencrypted_collection_id)["volumes"][0]
    rotated.upload_unit(
        reencrypted_collection_id,
        str(reencrypted_volume["volume_id"]),
        0,
        plan_sha256=str(reencrypted_volume["plan_sha256"]),
        content=content,
    )
    reencrypted = _process_until(rotated, reencrypted_collection_id)
    assert reencrypted_collection_id != collection_id
    assert reencrypted["content_identity"] == finalized["content_identity"]
    assert reencrypted["passphrase_id"] == "collection-test-key-v2"


def test_restore_required_ingress_commits_encrypted_cache_with_initial_lease(
    tmp_path: Path,
) -> None:
    database_url = sqlite_url(tmp_path / "catalog.sqlite3")
    baseline = RuntimeConfig(database_url=database_url)
    archive = replace(
        baseline.archive_store("archive"),
    )
    config = RuntimeConfig(
        database_url=database_url,
        archive_passphrases={"collection-test-key-v1": "test archive secret"},
        archive_active_passphrase_id="collection-test-key-v1",
        archive_scrypt_work_factor=1,
        archive_stores={"archive": archive},
    )
    initialize_db(database_url)
    archive_resumable = MemoryResumableStore()
    cache = _MemoryResumableCache()
    binding = replace(
        archive_store_binding(MemoryArchiveStore(read_mode="restore_required")),
        resumable_objects=archive_resumable,
        immutable_objects=MemoryImmutableStore(),
        object_ranges=_UnusedRangeStore(),
    )
    service = SqlAlchemyCollectionUploadService(
        config,
        ArchiveStoreRegistry({"archive": binding}),
        policy=CollectionVolumePolicy(
            pack_source_bytes=16 * 1024 * 1024,
            pack_files=100,
            pack_member_bytes=8 * 1024 * 1024,
            pack_part_plaintext_bytes=5 * 1024 * 1024,
            raw_volume_plaintext_bytes=10 * 1024 * 1024,
            raw_part_plaintext_bytes=5 * 1024 * 1024,
        ),
        retrieval_cache=cache,  # type: ignore[arg-type]
    )
    content = b"plaintext relinquished by the client"
    sha256 = hashlib.sha256(content).hexdigest()
    opened = service.create_or_resume(
        idempotency_key="deep-cache-upload",
        ingest_source="fixture",
        archive_store=None,
        initiator=_CREATOR,
        event_context=None,
        provenance_mode="omitted",
        provenance_omission_reason="fixture",
    )
    collection_id = int(opened["collection_id"])
    service.register_files(
        collection_id,
        ({"path": "document.txt", "bytes": len(content), "sha256": sha256},),
    )
    service.complete(collection_id)
    volume = service.list_volumes(collection_id)["volumes"][0]
    unit = volume["units"][0]
    service.upload_unit(
        collection_id,
        str(volume["volume_id"]),
        int(unit["unit"]),
        plan_sha256=str(volume["plan_sha256"]),
        content=content,
    )
    _process_until(service, collection_id)

    with session_scope(make_session_factory(database_url)) as session:
        cached = session.get(
            RetrievalCacheObjectRecord,
            ("archive", collection_id, str(volume["volume_id"])),
        )
        lease = session.get(
            RetrievalCacheLeaseRecord,
            ("new-archive", "archive", collection_id, str(volume["volume_id"])),
        )
        assert cached is not None
        assert lease is not None
        archive_object = session.get(
            CollectionArchiveObjectRecord,
            (collection_id, "archive", str(volume["volume_id"])),
        )
        assert archive_object is not None
        archive_ciphertext = archive_resumable.objects[archive_object.object_path][0]
        cache_ciphertext = cache.resumable.objects[cached.object_path][0]
        assert cache_ciphertext == archive_ciphertext
        assert cache_ciphertext != content
        assert cached.cache_store == "memory"


def test_restore_required_ingress_uses_archive_only_when_new_archive_cache_is_disabled(
    tmp_path: Path,
) -> None:
    database_url = sqlite_url(tmp_path / "catalog.sqlite3")
    baseline = RuntimeConfig(database_url=database_url)
    archive = replace(
        baseline.archive_store("archive"),
    )
    config = RuntimeConfig(
        database_url=database_url,
        archive_stores={"archive": archive},
        retrieval_cache_new_archive_enabled=False,
    )
    archive_resumable = MemoryResumableStore()
    binding = replace(
        archive_store_binding(MemoryArchiveStore(read_mode="restore_required")),
        resumable_objects=archive_resumable,
    )
    service = SqlAlchemyCollectionUploadService(
        config,
        ArchiveStoreRegistry({"archive": binding}),
        retrieval_cache=_MemoryResumableCache(),  # type: ignore[arg-type]
    )

    selected = service._volume_object_store(
        store_name="archive",
        collection_id=42,
        object_id=f"pack-{0:064x}",
    )

    assert selected is archive_resumable


def test_captured_and_omitted_file_provenance_is_one_immutable_mixed_archive(
    tmp_path: Path,
) -> None:
    service, config, _resumable, root = _service_with_archive_objects(tmp_path)
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
        observer=native_provenance_observer(),
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
    opened = service.create_or_resume(
        idempotency_key="mixed-provenance-upload",
        ingest_source="fixture",
        archive_store=None,
        initiator=_CREATOR,
        event_context=None,
        provenance_mode="captured",
        provenance_omission_reason=None,
    )
    collection_id = int(opened["collection_id"])
    staged = _upload_provenance_journal(
        service,
        collection_id,
        summary.journal_id,
        journal,
        summary.journal_sha256,
    )
    assert staged["accepted_bytes"] == len(journal)
    assert staged["sha256"] == summary.journal_sha256
    assert staged["current_state_id"] == summary.current_state_id
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
    closing = service.complete(collection_id)
    assert closing["provenance_identity"] is None
    for volume in service.list_volumes(collection_id)["volumes"]:
        for unit in volume["units"]:
            service.upload_unit(
                collection_id,
                str(volume["volume_id"]),
                int(unit["unit"]),
                plan_sha256=str(volume["plan_sha256"]),
                content=b"".join(contents[str(source["path"])] for source in unit["sources"]),
            )
    assert _process_until(service, collection_id)["provenance_mode"] == "mixed"

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
        external_state_references = list(
            session.query(CollectionProvenanceExternalStateReferenceRecord).filter_by(
                collection_id=collection_id,
                from_journal_id=summary.journal_id,
            )
        )
        exact_bytes = b"".join(
            session.scalars(
                select(CollectionProvenanceJournalChunkRecord.content)
                .where(
                    CollectionProvenanceJournalChunkRecord.collection_id == collection_id,
                    CollectionProvenanceJournalChunkRecord.journal_id == summary.journal_id,
                )
                .order_by(CollectionProvenanceJournalChunkRecord.ordinal)
            )
        )
    assert [item.kind for item in objects] == [
        "pack",
        "volume-metadata",
        "volume-terminal",
        "provenance-bindings",
        "provenance-volume-metadata",
        "provenance-journal-segment",
        "provenance-volume-metadata",
        "provenance-terminal",
        "provenance-root",
        "manifest",
        "recovery-descriptor",
    ]
    assert bindings_by_path["captured.bin"].status == "captured"
    assert bindings_by_path["operator-note.txt"].status == "omitted"
    assert exact is not None and exact_bytes == journal
    assert exact.entries == len(summary.frames)
    assert exact.agent_count == len(summary.agent_ids)
    assert exact.entity_counts_json
    assert projected
    assert external_state_references == []

    stored_by_suffix = {path.rsplit("/", 1)[-1]: stored for path, stored in root.objects.items()}
    passphrase = config.archive_passphrase_for(config.archive_active_passphrase_id)
    root_bytes = decrypt_age_scrypt(
        stored_by_suffix["root.json.age"].content,
        passphrase,
    )
    provenance_root = ProvenanceRootDocument.from_json_bytes(root_bytes)
    volume_digest = hashlib.sha256()
    recovered_bindings: list[dict[str, object]] = []
    recovered_journal = bytearray()
    sequence = 0
    while True:
        metadata_record = next(
            item
            for item in objects
            if item.object_id
            in {
                f"provenance-volume-{sequence:064x}",
                f"provenance-terminal-{sequence:064x}",
            }
        )
        metadata_bytes = decrypt_age_scrypt(
            root.objects[metadata_record.object_path].content,
            passphrase,
        )
        if metadata_record.object_id.startswith("provenance-terminal-"):
            terminal = ProvenanceTerminalDocument.from_json_bytes(metadata_bytes)
            assert terminal.sequence == sequence
            update_ordered_volume_commitment(volume_digest, terminal)
            break
        volume = ProvenanceVolumeDocument.from_json_bytes(metadata_bytes)
        update_ordered_volume_commitment(volume_digest, volume)
        payload_record = next(
            item for item in objects if item.object_id == f"provenance-payload-{sequence:064x}"
        )
        payload = decrypt_age_scrypt(
            root.objects[payload_record.object_path].content,
            passphrase,
        )
        assert hashlib.sha256(payload).hexdigest() == volume.payload.sha256
        if volume.payload.kind == "bindings":
            _first, current = parse_binding_segment(payload)
            recovered_bindings.extend(current)
        else:
            recovered_journal.extend(payload)
        sequence += 1
    assert volume_digest.hexdigest() == provenance_root.ordered_volume_sha256
    assert recovered_bindings == [
        {
            "path": binding.path,
            "bytes": binding.bytes,
            "sha256": binding.sha256,
            "status": binding.status,
            **(
                {
                    "journal_id": binding.journal_id,
                    "current_state_id": binding.current_state_id,
                }
                if binding.status == "captured"
                else {"omission_reason": binding.omission_reason}
            ),
        }
        for binding in bindings
    ]
    assert bytes(recovered_journal) == journal

    manifest = decrypt_age_scrypt(
        stored_by_suffix["manifest.json.age"].content,
        passphrase,
    )
    parsed = CollectionArchiveManifest.from_json_bytes(manifest).to_mapping()
    provenance_descriptor = parsed["provenance"]
    assert isinstance(provenance_descriptor, dict)
    assert provenance_descriptor["identity"] == provenance_root.identity
    assert provenance_descriptor["root"]["sha256"] == provenance_root.identity

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
        page_size=100,
        position=None,
        q=None,
        status=None,
        sort="path",
        order="asc",
        principal=reader,
    )
    assert listed["provenance_mode"] == "mixed"
    assert [item["provenance"]["status"] for item in listed["files"]] == [
        "captured",
        "omitted",
    ]
    shown = provenance_service.show_file(collection_id, "captured.bin", principal=reader)
    assert shown["journal"]["journal_id"] == summary.journal_id
    traced = provenance_service.trace_file(
        collection_id,
        "captured.bin",
        page_size=100,
        position=None,
        principal=reader,
    )
    assert [
        item["journal"]["journal_id"] for item in traced["items"] if item["kind"] == "journal"
    ] == [summary.journal_id]
    _exported_bytes, exported_sha256 = provenance_service.journal_metadata(
        collection_id,
        summary.journal_id,
        principal=reader,
    )
    exported = b"".join(
        provenance_service.iter_journal(
            collection_id,
            summary.journal_id,
            principal=reader,
        )
    )
    assert exported == journal
    assert exported_sha256 == summary.journal_sha256
    assert (
        _verify_provenance(
            provenance_service,
            collection_id,
            principal=reader,
        )["valid"]
        is True
    )

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
        list(
            provenance_service.iter_journal(
                collection_id,
                summary.journal_id,
                principal=read_only,
            )
        )


@pytest.mark.parametrize(
    ("custody_mode", "description"),
    (
        ("producer-retained", None),
        ("custody-transfer", "Camera seven — morning reference"),
    ),
)
def test_small_collection_moves_directly_from_source_unit_to_final_custody(
    tmp_path: Path,
    custody_mode: str,
    description: str | None,
) -> None:
    service, config, _resumable, immutable = _service_with_archive_objects(tmp_path)
    content = b"direct final archive\n"
    sha256 = hashlib.sha256(content).hexdigest()

    opened = service.create_or_resume(
        idempotency_key="upload-1",
        ingest_source="fixture",
        description=description,
        archive_store=None,
        initiator=_CREATOR,
        event_context=None,
        provenance_mode="omitted",
        provenance_omission_reason="fixture does not exercise source observation",
        custody_mode=custody_mode,
    )
    assert opened["custody_mode"] == custody_mode
    assert (opened["upload_state_expires_at"] is not None) == (custody_mode == "custody-transfer")
    collection_id = int(opened["collection_id"])
    with pytest.raises(Conflict, match="idempotency identity changed"):
        service.create_or_resume(
            idempotency_key="upload-1",
            ingest_source="other-fixture",
            archive_store=None,
            initiator=_CREATOR,
            event_context=None,
            provenance_mode="omitted",
            provenance_omission_reason="fixture does not exercise source observation",
            custody_mode=custody_mode,
        )
    registered = service.register_files(
        collection_id,
        ({"path": "document.txt", "bytes": len(content), "sha256": sha256},),
    )
    assert registered["volumes"] == []
    with session_scope(make_session_factory(config.database_url)) as session:
        upload = session.get(CollectionUploadRecord, collection_id)
        assert upload is not None
        assert (upload.file_count, upload.file_bytes) == (1, len(content))

    closed = service.complete(collection_id)
    assert closed["state"] == ("closing" if custody_mode == "custody-transfer" else "uploading")
    assert (closed["upload_state_expires_at"] is not None) == (custody_mode == "custody-transfer")
    assert closed["orphaned_at"] is None
    assert service.complete(collection_id)["state"] == closed["state"]
    volume = service.list_volumes(collection_id)["volumes"][0]
    assert volume["kind"] == "pack"
    unit = volume["units"][0]
    assert unit["sources"] == [
        {
            "path": "document.txt",
            "offset": 0,
            "bytes": len(content),
            "artifact_sha256": sha256,
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
    assert queued["upload_state_expires_at"] is None
    assert queued["orphaned_at"] is None
    assert queued["archive_phase"] == "finalization_queued"
    assert queued["latest_failure"] is None
    assert queued["archive_next_attempt_at"] is not None
    finalized = _process_until(service, collection_id)
    assert finalized["state"] == "finalized"
    assert finalized["custody"] == {"state": "complete"}
    assert finalized["custody_mode"] == custody_mode
    assert finalized["description"] == description
    assert finalized["description_revision"] == (1 if description is not None else 0)
    assert finalized["description_publication"] == (
        "current" if description is not None else "not_required"
    )
    assert service.complete(collection_id)["state"] == "finalized"

    with session_scope(make_session_factory(config.database_url)) as session:
        collection = session.get(CollectionRecord, collection_id)
        assert collection is not None
        assert (collection.file_count, collection.file_bytes) == (1, len(content))
        file = session.get(CollectionFileRecord, (collection_id, "document.txt"))
        assert file is not None and file.provenance_status == "omitted"
        objects = list(
            session.query(CollectionArchiveObjectRecord)
            .filter(CollectionArchiveObjectRecord.collection_id == collection_id)
            .order_by(CollectionArchiveObjectRecord.object_order)
        )
        assert [current.object_id for current in objects] == [
            f"pack-{0:064x}",
            f"volume-metadata-{0:064x}",
            f"volume-terminal-{1:064x}",
            "manifest",
            "recovery-descriptor",
        ]
        assert objects[0].object_path.endswith(f"/volumes/pack-{0:064x}.tar.age")
        assert objects[1].object_path.endswith(f"/metadata/volume-{0:064x}.json.age")
        assert objects[2].object_path.endswith(f"/metadata/volume-{1:064x}.json.age")
        assert objects[3].object_path.endswith("/manifest.json.age")
        assert objects[4].object_path.endswith("/recovery.json")
        root_object = immutable.objects[objects[3].object_path]
        plaintext_root_sha256 = root_object.identity["riverhog-plaintext-sha256"]
        assert objects[3].sha256 == plaintext_root_sha256
        assert objects[3].stored_sha256 == root_object.receipt.stored_sha256
        assert objects[3].sha256 != objects[3].stored_sha256
        publication = session.get(
            CollectionDescriptionPublicationRecord,
            (collection_id, "archive"),
        )
        assert publication is not None
        assert publication.published_revision == (1 if description is not None else 0)

    archive_store = service._archive_stores.require("archive").store
    assert isinstance(archive_store, MemoryArchiveStore)
    description_path = f"{archive_store.new_archive_prefix}/{COLLECTION_DESCRIPTION_RELATIVE_PATH}"
    if description is None:
        assert description_path not in archive_store.objects
    else:
        document = CollectionDescriptionDocument.from_json_bytes(
            decrypt_age_scrypt(
                archive_store.objects[description_path],
                config.archive_passphrase_for(config.archive_active_passphrase_id),
            )
        )
        assert document.archive_root_sha256 == plaintext_root_sha256
        assert document.revision == 1
        assert document.description == description
        assert document.description_identity == finalized["description_identity"]

    assert finalized["archive_root_sha256"] == plaintext_root_sha256
    finalized_events = [
        event
        for event in SqlAlchemyLifecycleEventService(config)
        .page(
            owner_app=_CREATOR.app,
            after=None,
            limit=100,
        )
        .events
        if event.type.endswith("collection.finalized") and event.subject == str(collection_id)
    ]
    assert len(finalized_events) == 1
    assert finalized_events[0].data["archive_root_sha256"] == plaintext_root_sha256

    resumed = service.create_or_resume(
        idempotency_key="upload-1",
        ingest_source="fixture",
        description=description,
        archive_store=None,
        initiator=_CREATOR,
        event_context=None,
        provenance_mode="omitted",
        provenance_omission_reason="fixture does not exercise source observation",
        custody_mode=custody_mode,
    )
    assert resumed["collection_id"] == collection_id
    assert resumed["state"] == "finalized"
    changed_custody_mode = (
        "custody-transfer" if custody_mode == "producer-retained" else "producer-retained"
    )
    with pytest.raises(Conflict, match="idempotency identity changed"):
        service.create_or_resume(
            idempotency_key="upload-1",
            ingest_source="fixture",
            archive_store=None,
            initiator=_CREATOR,
            event_context=None,
            provenance_mode="omitted",
            provenance_omission_reason="fixture does not exercise source observation",
            custody_mode=changed_custody_mode,
        )
    with pytest.raises(Conflict, match="idempotency identity changed"):
        service.create_or_resume(
            idempotency_key="upload-1",
            ingest_source="other-fixture",
            archive_store=None,
            initiator=_CREATOR,
            event_context=None,
            provenance_mode="omitted",
            provenance_omission_reason="fixture does not exercise source observation",
            custody_mode=custody_mode,
        )
    with pytest.raises(Conflict, match="idempotency identity changed"):
        service.create_or_resume(
            idempotency_key="upload-1",
            ingest_source="fixture",
            archive_store=None,
            initiator=_CREATOR,
            event_context={"source": "other-fixture"},
            provenance_mode="omitted",
            provenance_omission_reason="fixture does not exercise source observation",
            custody_mode=custody_mode,
        )
    with pytest.raises(Conflict, match="idempotency identity changed"):
        service.create_or_resume(
            idempotency_key="upload-1",
            ingest_source="fixture",
            archive_store=None,
            initiator=_CREATOR,
            event_context=None,
            provenance_mode="omitted",
            provenance_omission_reason="a different explicit omission",
            custody_mode=custody_mode,
        )


def test_closed_custody_transfer_keeps_lease_until_final_tail_is_custodied(
    tmp_path: Path,
) -> None:
    service, config = _service(tmp_path)
    content = b"tail remains producer dependent\n"
    sha256 = hashlib.sha256(content).hexdigest()
    opened = service.create_or_resume(
        idempotency_key="leased-final-tail",
        ingest_source="fixture",
        archive_store=None,
        initiator=_CREATOR,
        event_context=None,
        provenance_mode="omitted",
        provenance_omission_reason="fixture",
        custody_mode="custody-transfer",
    )
    collection_id = int(opened["collection_id"])
    service.register_files(
        collection_id,
        ({"path": "tail.txt", "bytes": len(content), "sha256": sha256},),
    )
    closing = service.complete(collection_id)
    assert closing["state"] == "closing"
    assert closing["custody"] == {"state": "pending", "files": 0, "bytes": 0}
    assert closing["upload_state_expires_at"] is not None

    with session_scope(make_session_factory(config.database_url)) as session:
        upload = session.get(CollectionUploadRecord, collection_id)
        assert upload is not None
        upload.lease_expires_at = "2020-01-01T00:00:00.000000Z"
    assert service.reap_expired_custody_transfers() == 1
    assert service.get(collection_id)["state"] == "orphaned"

    resumed = service.create_or_resume(
        idempotency_key="leased-final-tail",
        ingest_source="fixture",
        archive_store=None,
        initiator=_CREATOR,
        event_context=None,
        provenance_mode="omitted",
        provenance_omission_reason="fixture",
        custody_mode="custody-transfer",
    )
    assert resumed["state"] == "closing"
    assert resumed["archive_phase"] == "uploading"
    assert service.heartbeat(collection_id)["state"] == "closing"

    volume = service.list_volumes(collection_id)["volumes"][0]
    unit = volume["units"][0]
    service.upload_unit(
        collection_id,
        str(volume["volume_id"]),
        int(unit["unit"]),
        plan_sha256=str(volume["plan_sha256"]),
        content=content,
    )
    queued = service.get(collection_id)
    assert queued["state"] == "finalizing"
    assert queued["custody"] == {"state": "complete"}
    assert queued["upload_state_expires_at"] is None


def test_custody_transfer_receipt_orphan_resume_and_guarded_discard(
    tmp_path: Path,
) -> None:
    policy = CollectionVolumePolicy(
        pack_source_bytes=1024,
        pack_files=1,
        pack_member_bytes=1024,
        pack_part_plaintext_bytes=5 * 1024 * 1024,
        raw_volume_plaintext_bytes=5 * 1024 * 1024,
        raw_part_plaintext_bytes=5 * 1024 * 1024,
    )
    service, config, _resumable, _root = _service_with_archive_objects(
        tmp_path,
        policy=policy,
    )
    contents = {"a.txt": b"first", "b.txt": b"second"}
    opened = service.create_or_resume(
        idempotency_key="custody-transfer",
        ingest_source="fixture",
        archive_store=None,
        initiator=_CREATOR,
        event_context=None,
        provenance_mode="omitted",
        provenance_omission_reason="fixture intentionally omits host provenance",
        custody_mode="custody-transfer",
    )
    collection_id = int(opened["collection_id"])
    assert opened["custody_mode"] == "custody-transfer"
    assert opened["upload_state_expires_at"] is not None

    for path in contents:
        content = contents[path]
        service.register_files(
            collection_id,
            (
                {
                    "path": path,
                    "bytes": len(content),
                    "sha256": hashlib.sha256(content).hexdigest(),
                },
            ),
        )
    volume = service.list_volumes(collection_id)["volumes"][0]
    unit = volume["units"][0]
    service.upload_unit(
        collection_id,
        str(volume["volume_id"]),
        int(unit["unit"]),
        plan_sha256=str(volume["plan_sha256"]),
        content=b"".join(contents[str(source["path"])] for source in unit["sources"]),
    )
    files = service.list_files(collection_id, page_size=100, position=None)["files"]
    by_path = {str(item["path"]): item for item in files}
    assert by_path["a.txt"]["custody_receipt"] is not None
    assert by_path["b.txt"]["custody_receipt"] is None
    assert service.get(collection_id)["custody"] == {
        "state": "pending",
        "files": 1,
        "bytes": 5,
    }
    with session_scope(make_session_factory(config.database_url)) as session:
        upload = session.get(CollectionUploadRecord, collection_id)
        assert upload is not None
        assert (upload.file_count, upload.file_bytes) == (2, 11)
        assert (upload.custodied_file_count, upload.custodied_file_bytes) == (1, 5)

    service.require_read_access(collection_id, _DELETER)
    service.require_discard_access(collection_id, _DELETER)
    with pytest.raises(NotFound):
        service.require_read_access(collection_id, _OTHER_DELETER)
    with pytest.raises(NotFound):
        service.require_discard_access(collection_id, _OTHER_DELETER)
    assert (
        len(
            service.list(
                page_size=100,
                position=None,
                q=None,
                state=None,
                sort="id",
                order="asc",
                principal=_DELETER,
            )["uploads"]
        )
        == 1
    )
    assert (
        len(
            service.list(
                page_size=100,
                position=None,
                q=None,
                state=None,
                sort="id",
                order="asc",
                principal=_OTHER_DELETER,
            )["uploads"]
        )
        == 0
    )

    with session_scope(make_session_factory(config.database_url)) as session:
        upload = session.get(CollectionUploadRecord, collection_id)
        assert upload is not None
        upload.lease_expires_at = "2020-01-01T00:00:00.000000Z"
    assert service.reap_expired_custody_transfers() == 1
    assert service.get(collection_id)["state"] == "orphaned"
    assert service.reap_expired_custody_transfers() == 0

    resumed = service.create_or_resume(
        idempotency_key="custody-transfer",
        ingest_source="fixture",
        archive_store=None,
        initiator=_CREATOR,
        event_context=None,
        provenance_mode="omitted",
        provenance_omission_reason="fixture intentionally omits host provenance",
        custody_mode="custody-transfer",
    )
    assert resumed["state"] == "open"
    assert resumed["custody"] == {"state": "pending", "files": 1, "bytes": 5}
    assert service.plan_orphan_discard(collection_id)["status"] == "blocked"

    with session_scope(make_session_factory(config.database_url)) as session:
        upload = session.get(CollectionUploadRecord, collection_id)
        assert upload is not None
        upload.lease_expires_at = "2020-01-01T00:00:00.000000Z"
    assert service.reap_expired_custody_transfers() == 1
    execution_id = "a" * 64
    with session_scope(make_session_factory(config.database_url)) as session:
        upload = session.get(CollectionUploadRecord, collection_id)
        assert upload is not None
        upload.initiated_by_app = f"transform:{execution_id}"
        session.add(
            CollectionProcessingClaimRecord(
                id="b" * 64,
                work_id="c" * 64,
                consumer_app="fixture-controller",
                consumer_key_id="fixture-key",
                purpose="fixture-operation",
                work_document_json="{}",
                work_document_sha256="d" * 64,
                execution_id=execution_id,
                state="active",
                fence=1,
                expires_at="2099-01-01T00:00:00.000000Z",
                created_at="2026-08-25T00:00:00.000000Z",
                updated_at="2026-08-25T00:00:00.000000Z",
            )
        )
    active_plan = service.plan_orphan_discard(collection_id)
    assert active_plan["status"] == "blocked"
    assert active_plan["challenge"] is None
    assert "owning processing claim remains active" in str(active_plan["blockers"])
    with session_scope(make_session_factory(config.database_url)) as session:
        claim = session.get(CollectionProcessingClaimRecord, "b" * 64)
        assert claim is not None
        claim.expires_at = "2020-01-01T00:00:00.000000Z"
    plan = service.plan_orphan_discard(collection_id)
    assert plan["status"] == "ready"
    assert "permanently destroys" in str(plan["warning"])
    result = service.discard_orphan(collection_id, challenge=str(plan["challenge"]))
    assert result == {
        "status": "discarded",
        "collection_id": collection_id,
        "files": 2,
        "bytes": 11,
        "custody": {"state": "pending", "files": 1, "bytes": 5},
        "archive_objects": 1,
    }
    with pytest.raises(NotFound):
        service.get(collection_id)


def test_failed_orphan_cleanup_remains_visible_and_exactly_retryable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, config = _service(tmp_path)
    opened = service.create_or_resume(
        idempotency_key="failed-discard",
        ingest_source="fixture",
        archive_store=None,
        initiator=_CREATOR,
        event_context=None,
        provenance_mode="omitted",
        provenance_omission_reason="fixture",
        custody_mode="custody-transfer",
    )
    collection_id = int(opened["collection_id"])
    with session_scope(make_session_factory(config.database_url)) as session:
        upload = session.get(CollectionUploadRecord, collection_id)
        assert upload is not None
        upload.lease_expires_at = "2020-01-01T00:00:00.000000Z"
    assert service.reap_expired_custody_transfers() == 1
    store = service._archive_stores.require("archive").store  # noqa: SLF001
    original = store.discard_collection_archive_upload
    monkeypatch.setattr(
        store,
        "discard_collection_archive_upload",
        lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("provider cleanup failed")),
    )
    plan = service.plan_orphan_discard(collection_id)

    with pytest.raises(RuntimeError, match="provider cleanup failed"):
        service.discard_orphan(collection_id, challenge=str(plan["challenge"]))

    retained = service.get(collection_id)
    assert retained["state"] == "orphaned"
    assert retained["latest_failure"] == "provider cleanup failed"
    with session_scope(make_session_factory(config.database_url)) as session:
        upload = session.get(CollectionUploadRecord, collection_id)
        assert upload is not None
        upload.state = "discarding"
    assert service.requeue_interrupted_orphan_discards_for_startup() == 1
    assert service.get(collection_id)["state"] == "orphaned"
    monkeypatch.setattr(store, "discard_collection_archive_upload", original)
    retry = service.plan_orphan_discard(collection_id)
    assert (
        service.discard_orphan(collection_id, challenge=str(retry["challenge"]))["status"]
        == "discarded"
    )


def test_completion_defers_canonical_identity_to_bounded_server_finalization(
    tmp_path: Path,
) -> None:
    service, config = _service(tmp_path)
    files = tuple(
        {
            "path": f"many/file-{index:04d}.txt",
            "bytes": index,
            "sha256": hashlib.sha256(f"payload-{index}".encode()).hexdigest(),
        }
        for index in range(513)
    )
    opened = service.create_or_resume(
        idempotency_key="bounded-completion",
        ingest_source="fixture",
        archive_store=None,
        initiator=_CREATOR,
        event_context=None,
        provenance_mode="omitted",
        provenance_omission_reason="fixture",
    )
    collection_id = int(opened["collection_id"])
    for start in range(0, len(files), 64):
        service.register_files(collection_id, files[start : start + 64])
    result = service.complete(collection_id)

    assert result["state"] == "uploading"
    with session_scope(make_session_factory(config.database_url)) as session:
        upload = session.get(CollectionUploadRecord, collection_id)
        assert upload is not None
        checkpoint = parse_incremental_volume_planner_checkpoint(upload.planner_checkpoint_json)
        assert checkpoint.closed is True
        assert checkpoint.files_seen == len(files)
        assert checkpoint.content_identity is not None
        assert upload.catalog_content_identity is None
        assert upload.catalog_phase == "content-identity"


def test_server_owned_membership_is_independent_of_registration_order(
    tmp_path: Path,
) -> None:
    contents = {
        "a/first.txt": b"first\n",
        "m/middle.txt": b"middle\n",
        "z/last.txt": b"last\n",
    }

    def publish(order: tuple[str, ...], key: str) -> dict[str, object]:
        service, _config = _service(tmp_path / key)
        opened = service.create_or_resume(
            idempotency_key=key,
            ingest_source="fixture",
            archive_store=None,
            initiator=_CREATOR,
            event_context=None,
            provenance_mode="omitted",
            provenance_omission_reason="fixture",
        )
        collection_id = int(opened["collection_id"])
        for path in order:
            content = contents[path]
            service.register_files(
                collection_id,
                (
                    {
                        "path": path,
                        "bytes": len(content),
                        "sha256": hashlib.sha256(content).hexdigest(),
                    },
                ),
            )
        service.complete(collection_id)
        for volume in service.list_volumes(collection_id)["volumes"]:
            for unit in volume["units"]:
                payload = b"".join(
                    contents[str(source["path"])][
                        int(source["offset"]) : int(source["offset"]) + int(source["bytes"])
                    ]
                    for source in unit["sources"]
                )
                service.upload_unit(
                    collection_id,
                    str(volume["volume_id"]),
                    int(unit["unit"]),
                    plan_sha256=str(volume["plan_sha256"]),
                    content=payload,
                )
        return _process_until(service, collection_id)

    forward = publish(tuple(contents), "server-membership-forward")
    shuffled = publish(tuple(reversed(contents)), "server-membership-shuffled")
    expected = collection_content_identity(
        (
            (path, len(content), hashlib.sha256(content).hexdigest())
            for path, content in contents.items()
        )
    )

    assert forward["content_identity"] == shuffled["content_identity"] == expected


def test_completion_requires_volume_plans_to_match_registered_file_identities(
    tmp_path: Path,
) -> None:
    service, config = _service(tmp_path)
    content = b"current registered payload\n"
    sha256 = hashlib.sha256(content).hexdigest()
    opened = service.create_or_resume(
        idempotency_key="stale-plan-upload",
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
    with session_scope(make_session_factory(config.database_url)) as session:
        upload = session.get(CollectionUploadRecord, collection_id)
        assert upload is not None
        checkpoint = parse_incremental_volume_planner_checkpoint(upload.planner_checkpoint_json)
        upload.planner_checkpoint_json = incremental_volume_planner_checkpoint_bytes(
            replace(
                checkpoint,
                pending_pack_files=(
                    ArchiveFile(
                        path="document.txt",
                        bytes=len(content),
                        sha256="b" * 64,
                    ),
                ),
            )
        ).decode("utf-8")

    with pytest.raises(Conflict, match="volume plans differ from registered files"):
        service.complete(collection_id)


def test_raw_upload_units_expose_the_registered_source_identity(tmp_path: Path) -> None:
    part_bytes = 5 * 1024 * 1024
    policy = CollectionVolumePolicy(
        pack_source_bytes=1,
        pack_files=1,
        pack_member_bytes=1,
        pack_part_plaintext_bytes=part_bytes,
        raw_volume_plaintext_bytes=part_bytes,
        raw_part_plaintext_bytes=part_bytes,
    )
    service, _config, _resumable, _root = _service_with_archive_objects(tmp_path, policy=policy)
    content = b"raw payload"
    sha256 = hashlib.sha256(content).hexdigest()
    part_count, part_commitment = ordered_raw_part_commitment((sha256,))
    opened = service.create_or_resume(
        idempotency_key="raw-source-identity",
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
        (
            {
                "path": "media.bin",
                "bytes": len(content),
                "sha256": sha256,
                "raw_parts": {
                    "part_plaintext_bytes": part_bytes,
                    "part_count": part_count,
                    "ordered_sha256": part_commitment,
                },
            },
        ),
    )
    service.register_raw_part_digests(
        collection_id,
        CollectionUploadRawDigestBatchDocument(
            path="media.bin",
            first_part=0,
            sha256s=[sha256],
        ),
    )
    service.complete(collection_id)

    volume = service.list_volumes(collection_id)["volumes"][0]
    assert volume["kind"] == "segment"
    assert volume["units"][0]["sources"] == [
        {
            "path": "media.bin",
            "offset": 0,
            "bytes": len(content),
            "artifact_sha256": sha256,
        }
    ]


def test_startup_reconciles_interrupted_finalization_from_its_durable_checkpoint(
    tmp_path: Path,
) -> None:
    service, config = _service(tmp_path)
    content = b"restart-safe direct final archive\n"
    sha256 = hashlib.sha256(content).hexdigest()
    opened = service.create_or_resume(
        idempotency_key="restart-upload",
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
    service.complete(collection_id)
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
    assert recovered["archive_next_attempt_at"] is not None
    assert _process_until(service, collection_id)["state"] == "finalized"
