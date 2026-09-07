from __future__ import annotations

import hashlib
from collections.abc import Iterable, Iterator
from datetime import timedelta
from pathlib import Path
from typing import cast

import pytest
from riverhog_age import CHUNK_SIZE
from riverhog_api_client.source_hashing import hash_raw_source_chunks
from riverhog_core.app_permissions import (
    CATALOG_READ,
    RETRIEVAL_MANAGE,
    ApplicationAccess,
    ApplicationPrincipal,
)
from riverhog_core.archive_store_registry import ArchiveStoreBinding, ArchiveStoreRegistry
from riverhog_core.catalog_db import initialize_db, make_session_factory, session_scope
from riverhog_core.catalog_models import (
    CollectionAccessGroupMembershipRecord,
    CollectionAccessGroupRecord,
    CollectionArchiveFileObjectRecord,
    RetrievalJobRecord,
    RetrievalPlanObjectRecord,
    RetrievalPlanPlacementRecord,
    RetrievalPlanRecord,
)
from riverhog_core.collection_plan import CollectionVolumePolicy
from riverhog_core.ports.archive_store import ArchiveObjectIdentity, ArchiveStore
from riverhog_core.ports.download_allowance import DownloadAttribution
from riverhog_core.ports.retrieval_cache import RetrievalCacheAdmission, RetrievalCacheReceipt
from riverhog_core.runtime_config import RuntimeConfig
from riverhog_core.services.collection_uploads import SqlAlchemyCollectionUploadService
from riverhog_core.services.retrieval import SqlAlchemyRetrievalService
from riverhog_protocol import CollectionUploadRawDigestBatchDocument
from riverhog_protocol.errors import Conflict, NotFound, PreconditionFailed
from sqlalchemy import select

from tests.unit.archive_object_fixtures import MemoryArchiveStore
from tests.unit.artifact_scope_fixtures import persisted_artifact_scope
from tests.unit.db_helpers import sqlite_url
from tests.unit.test_archive_root import MemoryImmutableStore
from tests.unit.test_pack_upload import MemoryResumableStore

MIB = 1024 * 1024
GROUP_ID = "a" * 64


class MemoryArchiveRangeStore:
    def __init__(self, resumable: MemoryResumableStore) -> None:
        self._resumable = resumable
        self.requests: list[tuple[str, int, int]] = []

    def iter_object_range(
        self,
        *,
        object_path: str,
        revision: str | None,
        expected_bytes: int,
        offset: int,
        size: int,
    ) -> Iterator[bytes]:
        _ = revision
        assert expected_bytes == len(self._resumable.objects[object_path][0])
        self.requests.append((object_path, offset, size))
        yield self._resumable.objects[object_path][0][offset : offset + size]


class DirectArchiveStore(MemoryArchiveStore):
    def __init__(
        self,
        resumable: MemoryResumableStore,
        *,
        read_mode: str = "immediate",
    ) -> None:
        super().__init__(read_mode=read_mode)
        self._resumable = resumable
        self.prepare_calls = 0

    def prepare_archive_objects_read(
        self,
        **kwargs: object,
    ):  # type: ignore[no-untyped-def]
        self.prepare_calls += 1
        return super().prepare_archive_objects_read(**kwargs)

    def iter_stored_archive_object(
        self,
        *,
        collection_id: int,
        object: ArchiveObjectIdentity,
        attribution: DownloadAttribution | None = None,
    ) -> Iterator[bytes]:
        _ = collection_id, attribution
        yield self._resumable.objects[object.object_path][0]


class MemoryRetrievalCache:
    def __init__(self) -> None:
        self.objects: dict[tuple[str, str | None], bytes] = {}
        self.range_requests: list[tuple[str, int, int]] = []
        self.deleted: list[tuple[str, str | None]] = []

    def admit(
        self,
        *,
        owner: str,
        source_store: str,
        collection_id: int,
        object_id: str,
        expected_bytes: int,
    ) -> RetrievalCacheAdmission | None:
        return RetrievalCacheAdmission(
            owner=owner,
            cache_store="memory",
            source_store=source_store,
            collection_id=collection_id,
            object_id=object_id,
            object_path=f"cache/{source_store}/{collection_id}/{object_id}",
            expected_bytes=expected_bytes,
            write_token="memory-write",
            admitted_at="2026-08-08T00:00:00.000000Z",
        )

    def put(
        self,
        *,
        admission: RetrievalCacheAdmission,
        content: Iterable[bytes],
    ) -> RetrievalCacheReceipt:
        payload = b"".join(content)
        assert len(payload) == admission.expected_bytes
        path = admission.object_path
        version = hashlib.sha256(payload).hexdigest()[:16]
        self.objects[(path, version)] = payload
        return RetrievalCacheReceipt(
            cache_store=admission.cache_store,
            object_path=path,
            revision=version,
            stored_bytes=len(payload),
            stored_sha256=hashlib.sha256(payload).hexdigest(),
            cached_at="2026-08-08T00:00:00.000000Z",
            verified_at="2026-08-08T00:00:00.000000Z",
        )

    def iter_object(
        self,
        *,
        cache_store: str,
        object_path: str,
        revision: str | None,
        expected_bytes: int,
        expected_sha256: str,
    ) -> Iterator[bytes]:
        assert cache_store == "memory"
        payload = self.objects[(object_path, revision)]
        assert len(payload) == expected_bytes
        assert hashlib.sha256(payload).hexdigest() == expected_sha256
        yield payload

    def iter_object_range(
        self,
        *,
        cache_store: str,
        object_path: str,
        revision: str | None,
        expected_bytes: int,
        offset: int,
        size: int,
    ) -> Iterator[bytes]:
        assert cache_store == "memory"
        payload = self.objects[(object_path, revision)]
        assert expected_bytes == len(payload)
        self.range_requests.append((object_path, offset, size))
        yield payload[offset : offset + size]

    def delete(
        self,
        *,
        cache_store: str,
        object_path: str,
        revision: str | None,
    ) -> None:
        assert cache_store == "memory"
        self.deleted.append((object_path, revision))
        del self.objects[(object_path, revision)]

    def release(self, *, owner: str) -> int:
        _ = owner
        return 0

    def is_current(self, *, admission: RetrievalCacheAdmission) -> bool:
        _ = admission
        return True

    def reap_abandoned_populations(self, *, limit: int = 100) -> int:
        _ = limit
        return 0


class NoCapacityRetrievalCache(MemoryRetrievalCache):
    def admit(
        self,
        *,
        owner: str,
        source_store: str,
        collection_id: int,
        object_id: str,
        expected_bytes: int,
    ) -> None:
        _ = owner, source_store, collection_id, object_id, expected_bytes
        return None


class FirstAdmissionOnlyRetrievalCache(MemoryRetrievalCache):
    def __init__(self) -> None:
        super().__init__()
        self.admission_calls = 0

    def admit(
        self,
        *,
        owner: str,
        source_store: str,
        collection_id: int,
        object_id: str,
        expected_bytes: int,
    ) -> RetrievalCacheAdmission | None:
        self.admission_calls += 1
        if self.admission_calls > 1:
            return None
        return super().admit(
            owner=owner,
            source_store=source_store,
            collection_id=collection_id,
            object_id=object_id,
            expected_bytes=expected_bytes,
        )


class RecordingDownloadAllowance:
    def __init__(self) -> None:
        self.reservations: list[tuple[str, int]] = []
        self.tracked: list[tuple[str, int, DownloadAttribution | None]] = []
        self.released: list[str] = []

    def reserve_retrieval(
        self,
        *,
        key_id: str,
        job_id: str,
        expected_bytes: int,
        expires_at: str,
    ) -> None:
        _ = key_id, expires_at
        self.reservations.append((job_id, expected_bytes))

    def release_retrieval(self, *, job_id: str) -> None:
        self.released.append(job_id)

    def track(
        self,
        *,
        store: str,
        expected_bytes: int,
        content: Iterator[bytes],
        attribution: DownloadAttribution | None = None,
    ) -> Iterator[bytes]:
        self.tracked.append((store, expected_bytes, attribution))
        return content


def _policy(
    *,
    raw: bool = False,
    raw_volume_plaintext_bytes: int = 10 * MIB,
    raw_part_plaintext_bytes: int = 5 * MIB,
) -> CollectionVolumePolicy:
    return CollectionVolumePolicy(
        pack_source_bytes=16 * MIB,
        pack_files=100,
        pack_member_bytes=1 if raw else 8 * MIB,
        pack_part_plaintext_bytes=5 * MIB,
        raw_volume_plaintext_bytes=raw_volume_plaintext_bytes,
        raw_part_plaintext_bytes=raw_part_plaintext_bytes,
    )


def _seed_collection(
    tmp_path: Path,
    files: dict[str, bytes],
    *,
    database_url: str | None = None,
    raw: bool = False,
    read_mode: str = "immediate",
    cache: MemoryRetrievalCache | None = None,
    allowance: RecordingDownloadAllowance | None = None,
    pending_timeout: timedelta | None = None,
    raw_volume_plaintext_bytes: int = 10 * MIB,
    raw_part_plaintext_bytes: int = 5 * MIB,
) -> tuple[
    SqlAlchemyRetrievalService,
    int,
    MemoryArchiveRangeStore,
    DirectArchiveStore,
]:
    database_url = database_url or sqlite_url(tmp_path / "catalog.sqlite3")
    config = RuntimeConfig(
        database_url=database_url,
        archive_scrypt_work_factor=1,
        retrieval_pending_timeout=pending_timeout or timedelta(hours=72),
    )
    initialize_db(database_url)
    resumable = MemoryResumableStore()
    ranges = MemoryArchiveRangeStore(resumable)
    root_store = MemoryImmutableStore()
    archive_store = DirectArchiveStore(resumable, read_mode=read_mode)
    archive_registry = ArchiveStoreRegistry(
        {
            "archive": ArchiveStoreBinding(
                store=cast(ArchiveStore, archive_store),
                resumable_objects=resumable,
                immutable_objects=root_store,
                object_ranges=ranges,
            )
        }
    )
    policy = _policy(
        raw=raw,
        raw_volume_plaintext_bytes=raw_volume_plaintext_bytes,
        raw_part_plaintext_bytes=raw_part_plaintext_bytes,
    )
    uploads = SqlAlchemyCollectionUploadService(
        config,
        archive_registry,
        policy=policy,
    )
    opened = uploads.create_or_resume(
        idempotency_key="upload-1",
        ingest_source="fixture",
        archive_store=None,
        initiator=_creator(),
        event_context=None,
        provenance_mode="omitted",
        provenance_omission_reason="fixture does not exercise source observation",
    )
    collection_id = int(opened["collection_id"])
    manifest: list[dict[str, object]] = []
    for path, content in sorted(files.items()):
        entry: dict[str, object] = {
            "path": path,
            "bytes": len(content),
            "sha256": hashlib.sha256(content).hexdigest(),
        }
        if raw:
            digests = hash_raw_source_chunks(
                path=path,
                chunks=(content,),
                expected_bytes=len(content),
                part_plaintext_bytes=policy.raw_part_plaintext_bytes,
            )
            entry["raw_parts"] = {
                "part_plaintext_bytes": digests.summary.part_plaintext_bytes,
                "part_count": digests.summary.part_count,
                "ordered_sha256": digests.summary.ordered_part_sha256,
            }
            entry["raw_digest_spool"] = digests
        manifest.append(entry)
    uploads.register_files(
        collection_id,
        [
            {key: value for key, value in entry.items() if key != "raw_digest_spool"}
            for entry in manifest
        ],
    )
    for entry in manifest:
        digest_spool = entry.pop("raw_digest_spool", None)
        if digest_spool is None:
            continue
        for first_part, sha256s in digest_spool.iter_batches():
            uploads.register_raw_part_digests(
                collection_id,
                CollectionUploadRawDigestBatchDocument(
                    path=str(entry["path"]),
                    first_part=first_part,
                    sha256s=list(sha256s),
                ),
            )
        digest_spool.close()
    uploads.complete(collection_id)
    for volume in uploads.list_volumes(collection_id)["volumes"]:
        for unit in volume["units"]:
            payload = b"".join(
                files[str(source["path"])][
                    int(source["offset"]) : int(source["offset"]) + int(source["bytes"])
                ]
                for source in unit["sources"]
            )
            uploads.upload_unit(
                collection_id,
                str(volume["volume_id"]),
                int(unit["unit"]),
                plan_sha256=str(volume["plan_sha256"]),
                content=payload,
            )
    for _ in range(256):
        if uploads.get(collection_id)["state"] == "finalized":
            break
        assert uploads.process_due_finalizations(limit=1) == 1
    else:
        raise AssertionError("bounded collection finalization did not terminate")

    with session_scope(make_session_factory(database_url)) as session:
        session.add(
            CollectionAccessGroupRecord(
                id=GROUP_ID,
                creation_idempotency_key="docs",
                created_by_app="fixture",
                display_label="docs",
                status="active",
                authorization_revision=1,
                created_at="2026-08-08T00:00:00.000000Z",
                updated_at="2026-08-08T00:00:00.000000Z",
                collection_count=1,
            )
        )
        session.add(
            CollectionAccessGroupMembershipRecord(
                collection_id=collection_id,
                group_id=GROUP_ID,
                added_by_app="fixture",
                added_at="2026-08-08T00:00:00.000000Z",
            )
        )

    retrieval = SqlAlchemyRetrievalService(
        config,
        archive_registry,
        cache,
        download_allowance=allowance,
    )
    return retrieval, collection_id, ranges, archive_store


def _creator():
    from riverhog_core.app_permissions import (
        ALL_RESOURCES,
        COLLECTIONS_CREATE,
        ApplicationAccess,
        ApplicationPrincipal,
    )

    return ApplicationPrincipal(
        app="uploader",
        key_id="key-1",
        access=frozenset({ApplicationAccess(COLLECTIONS_CREATE, ALL_RESOURCES)}),
    )


def _ready_job(
    service: SqlAlchemyRetrievalService,
    collection_id: int,
    path: str,
    *,
    key_id: str | None = None,
) -> dict[str, object]:
    plan = service.plan(((collection_id, path),))
    return service.create(
        app="reader",
        key_id=key_id,
        plan_id=str(plan["id"]),
        plan_etag=str(plan["etag"]),
    )


def _drive_requested(
    service: SqlAlchemyRetrievalService,
    job: dict[str, object],
) -> dict[str, object]:
    current = job
    for _ in range(20):
        if current["state"] != "requested":
            return current
        assert service.process_due() == 1
        current = service.get(app="reader", job_id=str(job["id"]))
    raise AssertionError("retrieval did not converge in bounded test steps")


def test_immediate_retrieval_reads_only_the_selected_pack_member_range(
    tmp_path: Path,
) -> None:
    files = {
        "a.bin": b"a" * (2 * MIB),
        "target.bin": b"t" * (2 * MIB),
        "z.bin": b"z" * (2 * MIB),
    }
    service, collection_id, ranges, _store = _seed_collection(tmp_path, files)

    plan = service.plan(((collection_id, "target.bin"),))
    job = service.create(
        app="reader",
        plan_id=str(plan["id"]),
        plan_etag=str(plan["etag"]),
    )
    assert job["state"] == "ready"
    chunks, byte_count, sha256 = service.content(
        app="reader",
        job_id=str(job["id"]),
        collection_id=collection_id,
        path="target.bin",
    )

    assert b"".join(chunks) == files["target.bin"]
    assert byte_count == len(files["target.bin"])
    assert sha256 == hashlib.sha256(files["target.bin"]).hexdigest()
    assert len(ranges.requests) == 1
    assert ranges.requests[0][2] < sum(len(value) for value in files.values())
    assert service.acknowledge(app="reader", job_id=str(job["id"]))["state"] == "completed"


def test_retrieval_plan_creation_replays_after_a_lost_response(tmp_path: Path) -> None:
    service, collection_id, _ranges, _store = _seed_collection(
        tmp_path,
        {"target.bin": b"target"},
    )

    first = service.plan(
        ((collection_id, "target.bin"),),
        idempotency_key="lost-response",
    )
    replay = service.plan(
        ((collection_id, "target.bin"),),
        idempotency_key="lost-response",
    )

    assert replay == first
    with session_scope(make_session_factory(sqlite_url(tmp_path / "catalog.sqlite3"))) as session:
        assert len(session.scalars(select(RetrievalPlanRecord)).all()) == 1

    with pytest.raises(Conflict, match="idempotency identity changed"):
        service.plan(
            ((collection_id, "target.bin"),),
            idempotency_key="lost-response",
            restore_policy="never",
        )


def test_retrieval_plan_accepts_the_exact_capability_artifact(tmp_path: Path) -> None:
    files = {"selected.bin": b"selected", "sibling.bin": b"sibling"}
    service, collection_id, _ranges, _store = _seed_collection(tmp_path, files)
    principal = persisted_artifact_scope(
        sqlite_url(tmp_path / "catalog.sqlite3"),
        access=(
            ApplicationAccess(CATALOG_READ, f"collection:{collection_id}"),
            ApplicationAccess(RETRIEVAL_MANAGE, f"collection:{collection_id}"),
        ),
        artifacts=(
            (
                collection_id,
                "selected.bin",
                len(files["selected.bin"]),
                hashlib.sha256(files["selected.bin"]).hexdigest(),
            ),
        ),
    )

    plan = service.plan(((collection_id, "selected.bin"),), principal=principal)

    page = service.list_plan_files(
        app=principal.app,
        key_id=principal.key_id,
        plan_id=str(plan["id"]),
        etag=str(plan["etag"]),
        start_ordinal=0,
        page_size=100,
    )
    assert page["files"] == [
        {
            "collection_id": collection_id,
            "path": "selected.bin",
            "bytes": len(files["selected.bin"]),
            "sha256": hashlib.sha256(files["selected.bin"]).hexdigest(),
            "requires_restore": False,
        }
    ]


def test_raw_retrieval_reassembles_verified_parts_in_file_order(tmp_path: Path) -> None:
    content = bytes(range(256)) * (6 * MIB // 256)
    service, collection_id, ranges, _store = _seed_collection(
        tmp_path,
        {"large.bin": content},
        raw=True,
    )
    job = _ready_job(service, collection_id, "large.bin")
    chunks, byte_count, sha256 = service.content(
        app="reader",
        job_id=str(job["id"]),
        collection_id=collection_id,
        path="large.bin",
    )

    assert b"".join(chunks) == content
    assert byte_count == len(content)
    assert sha256 == hashlib.sha256(content).hexdigest()
    assert len(ranges.requests) == 2


def test_raw_head_middle_and_tail_ranges_read_only_overlapping_bounded_parts(
    tmp_path: Path,
) -> None:
    content = bytes(range(256)) * (6 * MIB // 256)
    service, collection_id, ranges, _store = _seed_collection(
        tmp_path,
        {"large.bin": content},
        raw=True,
    )
    job = _ready_job(service, collection_id, "large.bin")
    size = 4096

    for offset in (0, len(content) // 2, len(content) - size):
        ranges.requests.clear()
        chunks, byte_count, sha256 = service.content(
            app="reader",
            job_id=str(job["id"]),
            collection_id=collection_id,
            path="large.bin",
            offset=offset,
            size=size,
        )

        assert b"".join(chunks) == content[offset : offset + size]
        assert byte_count == len(content)
        assert sha256 == hashlib.sha256(content).hexdigest()
        assert len(ranges.requests) == 1
        assert ranges.requests[0][2] < len(content)


def test_retrieval_plan_resumes_across_more_than_two_internal_segment_pages(
    tmp_path: Path,
) -> None:
    segment_count = 65
    content = bytes(index % 251 for index in range(segment_count * CHUNK_SIZE))
    service, collection_id, _ranges, _store = _seed_collection(
        tmp_path,
        {"many-segments.bin": content},
        raw=True,
        raw_volume_plaintext_bytes=CHUNK_SIZE,
        raw_part_plaintext_bytes=CHUNK_SIZE,
    )

    plan = service.plan(((collection_id, "many-segments.bin"),))

    assert plan["state"] == "planning"
    with session_scope(service._session_factory) as session:
        assert len(session.scalars(select(RetrievalPlanObjectRecord)).all()) == 32
        assert len(session.scalars(select(RetrievalPlanPlacementRecord)).all()) == 32

    restarted = SqlAlchemyRetrievalService(
        service._config,
        service._archive_stores,
        service._cache,
        session_factory=service._session_factory,
    )
    plan = restarted.advance_plan(app="", plan_id=str(plan["id"]))
    assert plan["state"] == "planning"
    with session_scope(service._session_factory) as session:
        assert len(session.scalars(select(RetrievalPlanObjectRecord)).all()) == 64
        assert len(session.scalars(select(RetrievalPlanPlacementRecord)).all()) == 64

    plan = restarted.advance_plan(app="", plan_id=str(plan["id"]))
    assert plan["state"] == "ready"
    assert plan["file_count"] == 1
    assert plan["etag"]
    with session_scope(service._session_factory) as session:
        assert len(session.scalars(select(RetrievalPlanObjectRecord)).all()) == segment_count
        assert len(session.scalars(select(RetrievalPlanPlacementRecord)).all()) == segment_count

    page = restarted.list_plan_files(
        app="",
        plan_id=str(plan["id"]),
        etag=str(plan["etag"]),
        start_ordinal=0,
        page_size=1,
    )
    assert page["complete"] is True
    assert page["next_ordinal"] is None
    assert [item["path"] for item in page["files"]] == ["many-segments.bin"]
    with pytest.raises(PreconditionFailed):
        restarted.list_plan_files(
            app="",
            plan_id=str(plan["id"]),
            etag="0" * 64,
            start_ordinal=0,
            page_size=1,
        )

    job = restarted.create(
        app="reader",
        plan_id=str(plan["id"]),
        plan_etag=str(plan["etag"]),
    )
    assert job["state"] == "ready"
    retried = restarted.create(
        app="reader",
        plan_id=str(plan["id"]),
        plan_etag=str(plan["etag"]),
    )
    assert retried["id"] == job["id"]
    with pytest.raises(Conflict, match="event context"):
        restarted.create(
            app="reader",
            plan_id=str(plan["id"]),
            plan_etag=str(plan["etag"]),
            event_context={"changed": True},
        )


def test_retrieval_plan_failure_is_durable_and_does_not_skip_a_missing_segment(
    tmp_path: Path,
) -> None:
    content = b"x" * (65 * CHUNK_SIZE)
    service, collection_id, _ranges, _store = _seed_collection(
        tmp_path,
        {"many-segments.bin": content},
        raw=True,
        raw_volume_plaintext_bytes=CHUNK_SIZE,
        raw_part_plaintext_bytes=CHUNK_SIZE,
    )
    plan = service.plan(((collection_id, "many-segments.bin"),))
    assert plan["state"] == "planning"
    with session_scope(service._session_factory) as session:
        plan_record = session.get(RetrievalPlanRecord, str(plan["id"]))
        assert plan_record is not None
        missing = session.scalar(
            select(CollectionArchiveFileObjectRecord)
            .where(
                CollectionArchiveFileObjectRecord.collection_id == collection_id,
                CollectionArchiveFileObjectRecord.store == "archive",
                CollectionArchiveFileObjectRecord.path == "many-segments.bin",
                CollectionArchiveFileObjectRecord.sequence >= plan_record.next_placement_sequence,
            )
            .order_by(CollectionArchiveFileObjectRecord.sequence)
            .limit(1)
        )
        assert missing is not None
        session.delete(missing)

    failed = service.advance_plan(app="", plan_id=str(plan["id"]))

    assert failed["state"] == "failed"
    assert failed["failure"] == "retrieval plan placement order is not canonical"
    assert service.get_plan(app="", plan_id=str(plan["id"])) == failed


def test_restore_required_job_caches_ciphertext_then_serves_logical_range(
    tmp_path: Path,
) -> None:
    cache = MemoryRetrievalCache()
    files = {"a.bin": b"a" * MIB, "target.bin": b"t" * MIB}
    service, collection_id, ranges, store = _seed_collection(
        tmp_path,
        files,
        read_mode="restore_required",
        cache=cache,
    )
    job = _ready_job(service, collection_id, "target.bin")
    assert job["state"] == "requested"

    ready = _drive_requested(service, job)
    assert ready["state"] == "ready"
    assert store.prepared == [("pack-" + "0" * 64,)]

    cached_plan = service.plan(((collection_id, "target.bin"),))
    assert cached_plan["requires_restore"] is False
    cached_job = service.create(
        app="reader",
        plan_id=str(cached_plan["id"]),
        plan_etag=str(cached_plan["etag"]),
    )
    assert cached_job["state"] == "ready"

    chunks, _bytes, _sha256 = service.content(
        app="reader",
        job_id=str(job["id"]),
        collection_id=collection_id,
        path="target.bin",
    )
    assert b"".join(chunks) == files["target.bin"]
    assert cache.range_requests
    assert ranges.requests == []


def test_restore_is_not_requested_until_cache_placement_is_admitted(tmp_path: Path) -> None:
    cache = NoCapacityRetrievalCache()
    service, collection_id, _ranges, store = _seed_collection(
        tmp_path,
        {"document.txt": b"document"},
        read_mode="restore_required",
        cache=cache,
    )
    job = _ready_job(service, collection_id, "document.txt")

    assert service.process_due() == 1

    pending = service.get(app="reader", job_id=str(job["id"]))
    assert pending["state"] == "requested"
    assert store.prepare_calls == 0


def test_restore_waits_until_every_required_object_has_cache_admission(tmp_path: Path) -> None:
    cache = FirstAdmissionOnlyRetrievalCache()
    service, collection_id, _ranges, store = _seed_collection(
        tmp_path,
        {"large.bin": b"x" * (11 * MIB)},
        raw=True,
        read_mode="restore_required",
        cache=cache,
    )
    plan = service.plan(((collection_id, "large.bin"),))
    job = service.create(
        app="reader",
        plan_id=str(plan["id"]),
        plan_etag=str(plan["etag"]),
    )

    assert service.process_due() == 1

    pending = service.get(app="reader", job_id=str(job["id"]))
    assert pending["state"] == "requested"
    assert cache.admission_calls == 1
    assert store.prepare_calls == 1


def test_restore_work_resumes_after_restart_one_exact_object_per_step(tmp_path: Path) -> None:
    cache = MemoryRetrievalCache()
    service, collection_id, _ranges, store = _seed_collection(
        tmp_path,
        {"two-objects.bin": b"x" * (2 * CHUNK_SIZE)},
        raw=True,
        read_mode="restore_required",
        cache=cache,
        raw_volume_plaintext_bytes=CHUNK_SIZE,
        raw_part_plaintext_bytes=CHUNK_SIZE,
    )
    plan = service.plan(((collection_id, "two-objects.bin"),))
    job = service.create(
        app="reader",
        plan_id=str(plan["id"]),
        plan_etag=str(plan["etag"]),
    )

    assert service.process_due(limit=10) == 1
    assert store.prepare_calls == 1

    restarted = SqlAlchemyRetrievalService(
        service._config,
        service._archive_stores,
        cache,
        session_factory=service._session_factory,
    )
    assert restarted.process_due(limit=10) == 1
    assert store.prepare_calls == 2

    ready = _drive_requested(restarted, job)
    assert ready["state"] == "ready"
    assert store.prepare_calls == 2


def test_retrieval_reserves_and_attributes_the_planned_range_bytes(
    tmp_path: Path,
) -> None:
    allowance = RecordingDownloadAllowance()
    files = {"a.bin": b"a" * MIB, "target.bin": b"t" * MIB, "z.bin": b"z" * MIB}
    service, collection_id, _ranges, _store = _seed_collection(
        tmp_path,
        files,
        allowance=allowance,
    )
    plan = service.plan(((collection_id, "target.bin"),))
    job = service.create(
        app="reader",
        key_id="reader-key",
        plan_id=str(plan["id"]),
        plan_etag=str(plan["etag"]),
    )
    chunks, _bytes, _sha256 = service.content(
        app="reader",
        key_id="reader-key",
        job_id=str(job["id"]),
        collection_id=collection_id,
        path="target.bin",
    )
    assert b"".join(chunks) == files["target.bin"]

    assert allowance.reservations
    assert allowance.reservations[0][0] == str(job["id"])
    assert allowance.tracked
    assert allowance.tracked[0][0] == "archive"
    assert allowance.tracked[0][2] == DownloadAttribution(
        key_id="reader-key",
        job_id=str(job["id"]),
    )


def test_cancel_releases_a_ready_job_and_its_download_reservation(tmp_path: Path) -> None:
    allowance = RecordingDownloadAllowance()
    service, collection_id, _ranges, _store = _seed_collection(
        tmp_path,
        {"document.txt": b"document"},
        allowance=allowance,
    )
    job = _ready_job(service, collection_id, "document.txt", key_id="reader-key")

    canceled = service.cancel(
        app="reader",
        key_id="reader-key",
        job_id=str(job["id"]),
    )

    assert canceled["state"] == "canceled"
    assert allowance.released == [str(job["id"])]


def test_restore_policy_never_is_atomic_and_never_requests_archive_restore(
    tmp_path: Path,
) -> None:
    cache = MemoryRetrievalCache()
    service, collection_id, _ranges, store = _seed_collection(
        tmp_path,
        {"document.txt": b"document"},
        read_mode="restore_required",
        cache=cache,
    )
    plan = service.plan(((collection_id, "document.txt"),), restore_policy="never")

    assert plan["requires_restore"] is True
    with pytest.raises(Conflict, match="restore_policy is never"):
        service.create(
            app="reader",
            plan_id=str(plan["id"]),
            plan_etag=str(plan["etag"]),
        )
    assert store.prepared == []


def test_requested_retrieval_converges_after_its_pending_timeout(tmp_path: Path) -> None:
    cache = MemoryRetrievalCache()
    allowance = RecordingDownloadAllowance()
    service, collection_id, _ranges, store = _seed_collection(
        tmp_path,
        {"document.txt": b"document"},
        read_mode="restore_required",
        cache=cache,
        allowance=allowance,
        pending_timeout=timedelta(hours=1),
    )
    requested = _ready_job(
        service,
        collection_id,
        "document.txt",
        key_id="reader-key",
    )
    with session_scope(service._session_factory) as session:
        record = session.get(RetrievalJobRecord, str(requested["id"]))
        assert record is not None
        record.created_at = "2020-01-01T00:00:00.000000Z"

    assert service.process_due() == 1
    failed = service.get(
        app="reader",
        key_id="reader-key",
        job_id=str(requested["id"]),
    )
    assert failed["state"] == "failed"
    assert failed["failure"] == "retrieval exceeded the configured pending timeout"
    assert store.prepared == []
    assert allowance.released == [str(requested["id"])]


def test_ready_retrieval_renewal_extends_its_cache_lease(tmp_path: Path) -> None:
    cache = MemoryRetrievalCache()
    service, collection_id, _ranges, _store = _seed_collection(
        tmp_path,
        {"document.txt": b"document"},
        read_mode="restore_required",
        cache=cache,
    )
    requested = _ready_job(service, collection_id, "document.txt")
    ready = _drive_requested(service, requested)

    renewed = service.renew(
        app="reader",
        job_id=str(ready["id"]),
        lease=timedelta(hours=36),
    )

    assert renewed["state"] == "ready"
    assert renewed["lease_seconds"] == 36 * 60 * 60
    with session_scope(service._session_factory) as session:
        plan_object = session.scalar(
            select(RetrievalPlanObjectRecord).where(
                RetrievalPlanObjectRecord.plan_id == ready["plan_id"],
                RetrievalPlanObjectRecord.collection_id == collection_id,
            )
        )
        assert plan_object is not None
        assert service.cache_status()["protected_objects"] == 1


def test_cache_status_list_and_show_respect_catalog_group_access(tmp_path: Path) -> None:
    cache = MemoryRetrievalCache()
    service, collection_id, _ranges, _store = _seed_collection(
        tmp_path,
        {"document.txt": b"document"},
        read_mode="restore_required",
        cache=cache,
    )
    requested = _ready_job(service, collection_id, "document.txt")
    _drive_requested(service, requested)
    permitted = ApplicationPrincipal(
        app="indexer",
        key_id="indexer-key",
        access=frozenset({ApplicationAccess(CATALOG_READ, f"group:{GROUP_ID}")}),
    )
    denied = ApplicationPrincipal(
        app="outsider",
        key_id="outsider-key",
        access=frozenset({ApplicationAccess(CATALOG_READ, f"group:{'b' * 64}")}),
    )

    status = service.cache_status(principal=permitted)
    listed = service.list_cache_objects(
        page_size=25,
        position=None,
        q=None,
        sort="cached_at",
        order="desc",
        principal=permitted,
    )
    filtered = service.list_cache_objects(
        page_size=25,
        position=None,
        q=None,
        collection_id=collection_id,
        source_store="ARCHIVE",
        state="READY",
        protection="PROTECTED",
        expires_before="2099-01-01T00:00:00+00:00",
        expires_after="2020-01-01T00:00:00Z",
        sort="protected_until",
        order="asc",
        principal=permitted,
    )
    current = listed["objects"][0]
    shown = service.get_cache_object(
        collection_id=collection_id,
        source_store=str(current["source_store"]),
        object_id=str(current["object_id"]),
        principal=permitted,
    )

    assert status["objects"] == 1
    assert listed["_next_position"] is None
    assert filtered["objects"] == listed["objects"]
    assert filtered["filters"] == {
        "collection_id": collection_id,
        "source_store": "archive",
        "cache_store": None,
        "state": "ready",
        "protection": "protected",
        "expires_before": "2099-01-01T00:00:00.000000Z",
        "expires_after": "2020-01-01T00:00:00.000000Z",
    }
    assert shown["collection_id"] == collection_id
    assert shown["state"] == "ready"
    assert shown["lease_categories"] == ["retrieval_job"]
    assert service.cache_status(principal=denied)["objects"] == 0
    assert (
        service.list_cache_objects(
            page_size=25,
            position=None,
            q=None,
            sort="cached_at",
            order="desc",
            principal=denied,
        )["objects"]
        == []
    )
    with pytest.raises(NotFound):
        service.get_cache_object(
            collection_id=collection_id,
            source_store="archive",
            object_id=str(current["object_id"]),
            principal=denied,
        )
    assert requested["state"] == "requested"


def test_cache_status_reports_effective_new_archive_insertion(tmp_path: Path) -> None:
    service, _collection_id, _ranges, _store = _seed_collection(
        tmp_path,
        {"document.txt": b"document"},
    )

    status = service.cache_status()

    assert status["configured"] is False
    assert status["new_archive_enabled"] is False
    assert status["policy"] == {
        "new_archive_lease_seconds": 72 * 60 * 60,
        "retrieval_default_lease_seconds": 24 * 60 * 60,
        "retrieval_max_lease_seconds": 7 * 24 * 60 * 60,
        "pending_timeout_seconds": 72 * 60 * 60,
        "sweep_interval_seconds": 5 * 60,
        "restore_poll_interval_seconds": 5 * 60,
    }


def test_cache_sweep_removes_an_unleased_verified_object(tmp_path: Path) -> None:
    cache = MemoryRetrievalCache()
    service, collection_id, _ranges, _store = _seed_collection(
        tmp_path,
        {"document.txt": b"document"},
        read_mode="restore_required",
        cache=cache,
    )
    requested = _ready_job(service, collection_id, "document.txt")
    ready = _drive_requested(service, requested)
    completed = service.acknowledge(app="reader", job_id=str(ready["id"]))

    assert completed["state"] == "completed"
    assert service.sweep() == 1
    assert len(cache.deleted) == 1
    assert service.cache_status()["objects"] == 0
