from __future__ import annotations

import hashlib
from dataclasses import replace
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
from riverhog_core.archive_manifest import parse_collection_archive_manifest
from riverhog_core.archive_store_registry import ArchiveStoreRegistry
from riverhog_core.catalog_db import initialize_db, make_session_factory, session_scope
from riverhog_core.catalog_models import (
    CollectionArchiveObjectRecord,
    CollectionArchiveObjectUploadRecord,
    CollectionFileProvenanceRecord,
    CollectionProvenanceEntityRecord,
    CollectionProvenanceExternalStateReferenceRecord,
    CollectionProvenanceJournalRecord,
    CollectionUploadRecord,
    RetrievalCacheLeaseRecord,
    RetrievalCacheObjectRecord,
    TagRecord,
)
from riverhog_core.collection_plan import CollectionVolumePolicy
from riverhog_core.domain.archive import ArchiveFile
from riverhog_core.incremental_plan import (
    incremental_volume_planner_checkpoint_bytes,
    parse_incremental_volume_planner_checkpoint,
)
from riverhog_core.ports.archive_objects import CompletedObjectReceipt
from riverhog_core.ports.retrieval_cache import RetrievalCacheReceipt
from riverhog_core.proofs import ProofStamper
from riverhog_core.runtime_config import RuntimeConfig
from riverhog_core.services.collection_uploads import SqlAlchemyCollectionUploadService
from riverhog_core.services.provenance import SqlAlchemyProvenanceService
from riverhog_core.throughput import ArchiveThroughputTuning, log_transfer_timing
from riverhog_protocol.errors import Conflict, NotFound
from riverhog_protocol.manifest import collection_content_identity
from riverhog_provenance import (
    FileProvenanceBinding,
    build_provenance_archive,
    create_observation_journal,
    validate_journal,
    validate_provenance_archive,
)

from tests.fixtures.crypto import FixtureProofStamper
from tests.unit.archive_object_fixtures import MemoryArchiveStore, archive_store_binding
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
        raise AssertionError("collection upload does not read archive ranges")


class _FailOnceProofStamper:
    def __init__(self) -> None:
        self.attempts = 0

    def stamp(self, manifest_path: Path) -> Path:
        self.attempts += 1
        if self.attempts == 1:
            raise RuntimeError("temporary proof service failure")
        return FixtureProofStamper().stamp(manifest_path)


class _MemoryMultipartCache:
    def __init__(self) -> None:
        self.multipart = MemoryMultipartStore()

    def multipart_object_store(self, **_: object) -> MemoryMultipartStore:
        return self.multipart

    def verify_multipart_object(
        self,
        *,
        completed: CompletedObjectReceipt,
        parts: tuple[object, ...] = (),
    ) -> RetrievalCacheReceipt:
        assert parts
        content = self.multipart.objects[completed.object_path][0]
        assert len(content) == completed.bytes
        return RetrievalCacheReceipt(
            object_path=completed.object_path,
            version_id=completed.version_id,
            stored_bytes=len(content),
            stored_sha256=hashlib.sha256(content).hexdigest(),
            cached_at=completed.completed_at,
            verified_at=completed.completed_at,
        )


def _service(
    tmp_path: Path,
    *,
    proof_stamper: ProofStamper | None = None,
) -> tuple[SqlAlchemyCollectionUploadService, RuntimeConfig]:
    service, config, _multipart, _root = _service_with_archive_objects(
        tmp_path,
        proof_stamper=proof_stamper,
    )
    return service, config


def _service_with_archive_objects(
    tmp_path: Path,
    *,
    proof_stamper: ProofStamper | None = None,
    policy: CollectionVolumePolicy | None = None,
    throughput_tuning: ArchiveThroughputTuning | None = None,
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
    binding = replace(
        archive_store_binding(archive_store),
        multipart_objects=multipart,
        immutable_objects=root,
        object_ranges=_UnusedRangeStore(),
    )
    return (
        SqlAlchemyCollectionUploadService(
            config,
            ArchiveStoreRegistry({"archive": binding}),
            proof_stamper=proof_stamper or FixtureProofStamper(),
            policy=policy,
            throughput_tuning=throughput_tuning,
        ),
        config,
        multipart,
        root,
    )


def test_collection_ingress_uses_the_configured_source_read_chunk(
    tmp_path: Path,
) -> None:
    tuning = ArchiveThroughputTuning(source_read_chunk_bytes=256 * 1024)
    service, _config, multipart, _root = _service_with_archive_objects(
        tmp_path,
        throughput_tuning=tuning,
    )

    pack_uploader = service._pack_uploader(multipart)
    raw_uploader = service._raw_uploader(multipart)
    assert pack_uploader._source_read_chunk_bytes == 256 * 1024
    assert raw_uploader._source_read_chunk_bytes == 256 * 1024
    assert pack_uploader._timing_observer is log_transfer_timing
    assert raw_uploader._timing_observer is log_transfer_timing


def test_restore_required_ingress_commits_verified_encrypted_cache_with_initial_lease(
    tmp_path: Path,
) -> None:
    database_url = sqlite_url(tmp_path / "catalog.sqlite3")
    baseline = RuntimeConfig(database_url=database_url)
    archive = replace(
        baseline.archive_store("archive"),
    )
    config = RuntimeConfig(
        database_url=database_url,
        archive_passphrase="test archive secret",
        archive_scrypt_work_factor=1,
        archive_stores={"archive": archive},
    )
    initialize_db(database_url)
    with session_scope(make_session_factory(database_url)) as session:
        session.add(
            TagRecord(
                id="docs",
                created_by_app="fixture",
                created_at="2026-08-08T00:00:00.000000Z",
            )
        )
    archive_multipart = MemoryMultipartStore()
    cache = _MemoryMultipartCache()
    binding = replace(
        archive_store_binding(MemoryArchiveStore(read_mode="restore_required")),
        multipart_objects=archive_multipart,
        immutable_objects=MemoryImmutableStore(),
        object_ranges=_UnusedRangeStore(),
    )
    service = SqlAlchemyCollectionUploadService(
        config,
        ArchiveStoreRegistry({"archive": binding}),
        proof_stamper=FixtureProofStamper(),
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
        tags=("docs",),
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
    service.complete(
        collection_id,
        files_total=1,
        content_identity=collection_content_identity((("document.txt", len(content), sha256),)),
    )
    volume = service.list_volumes(collection_id)["volumes"][0]
    unit = volume["units"][0]
    service.upload_unit(
        collection_id,
        str(volume["volume_id"]),
        int(unit["unit"]),
        plan_sha256=str(volume["plan_sha256"]),
        content=content,
    )
    assert service.process_due_finalizations() == 1

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
        archive_ciphertext = archive_multipart.objects[archive_object.object_path][0]
        cache_ciphertext = cache.multipart.objects[cached.object_path][0]
        assert cache_ciphertext == archive_ciphertext
        assert cache_ciphertext != content
        assert cached.stored_sha256 == hashlib.sha256(cache_ciphertext).hexdigest()


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
    archive_multipart = MemoryMultipartStore()
    binding = replace(
        archive_store_binding(MemoryArchiveStore(read_mode="restore_required")),
        multipart_objects=archive_multipart,
    )
    service = SqlAlchemyCollectionUploadService(
        config,
        ArchiveStoreRegistry({"archive": binding}),
        proof_stamper=FixtureProofStamper(),
        retrieval_cache=_MemoryMultipartCache(),  # type: ignore[arg-type]
    )

    selected = service._volume_object_store(
        store_name="archive",
        collection_id=42,
        object_id="pack-000000000000",
    )

    assert selected is archive_multipart


def test_captured_and_omitted_file_provenance_is_one_immutable_mixed_archive(
    tmp_path: Path,
) -> None:
    service, config, _multipart, root = _service_with_archive_objects(tmp_path)
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
    staged_content, staged_sha256 = service.export_provenance_journal(
        collection_id,
        summary.journal_id,
    )
    assert staged_content == journal
    assert staged_sha256 == summary.journal_sha256
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
        content_identity=collection_content_identity(
            (binding.path, binding.bytes, binding.sha256) for binding in bindings
        ),
        provenance_identity=provenance.identity,
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
        external_state_references = list(
            session.query(CollectionProvenanceExternalStateReferenceRecord).filter_by(
                collection_id=collection_id,
                from_journal_id=summary.journal_id,
            )
        )
        exact_bytes = exact.journal_bytes if exact is not None else None
    assert [item.kind for item in objects] == [
        "pack",
        "provenance-bundle",
        "provenance-index",
        "manifest",
        "proof",
    ]
    assert bindings_by_path["captured.bin"].status == "captured"
    assert bindings_by_path["operator-note.txt"].status == "omitted"
    assert exact is not None and exact_bytes == journal
    assert exact.entries == len(summary.frames)
    assert exact.agent_ids_json
    assert exact.entity_counts_json
    assert projected
    assert external_state_references == []

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
        "provenance_identity": provenance.identity,
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
        content_identity=collection_content_identity((("document.txt", len(content), sha256),)),
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


def test_completion_requires_volume_plans_to_match_registered_file_identities(
    tmp_path: Path,
) -> None:
    service, config = _service(tmp_path)
    content = b"current registered payload\n"
    sha256 = hashlib.sha256(content).hexdigest()
    opened = service.create_or_resume(
        idempotency_key="stale-plan-upload",
        tags=(),
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
        service.complete(
            collection_id,
            files_total=1,
            content_identity=collection_content_identity((("document.txt", len(content), sha256),)),
        )


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
    service, _config, _multipart, _root = _service_with_archive_objects(tmp_path, policy=policy)
    content = b"raw payload"
    sha256 = hashlib.sha256(content).hexdigest()
    opened = service.create_or_resume(
        idempotency_key="raw-source-identity",
        tags=(),
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
                    "sha256s": [sha256],
                },
            },
        ),
    )
    service.complete(
        collection_id,
        files_total=1,
        content_identity=collection_content_identity((("media.bin", len(content), sha256),)),
    )

    volume = service.list_volumes(collection_id)["volumes"][0]
    assert volume["kind"] == "segment"
    assert volume["units"][0]["sources"] == [
        {
            "path": "media.bin",
            "offset": 0,
            "bytes": len(content),
            "sha256": sha256,
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
        content_identity=collection_content_identity((("document.txt", len(content), sha256),)),
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
        content_identity=collection_content_identity((("document.txt", len(content), sha256),)),
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
