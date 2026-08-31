from __future__ import annotations

import hashlib
from collections.abc import Iterable, Iterator
from datetime import timedelta
from pathlib import Path
from typing import cast

import pytest
from riverhog_api_client.source_hashing import hash_raw_source_chunks
from riverhog_core.app_permissions import (
    CATALOG_READ,
    RETRIEVAL_MANAGE,
    TAG_PREFIX,
    ApplicationAccess,
    ApplicationPrincipal,
)
from riverhog_core.archive_store_registry import ArchiveStoreBinding, ArchiveStoreRegistry
from riverhog_core.catalog_db import initialize_db, make_session_factory, session_scope
from riverhog_core.catalog_models import RetrievalCacheLeaseRecord, RetrievalJobRecord, TagRecord
from riverhog_core.collection_plan import CollectionVolumePolicy
from riverhog_core.ports.archive_store import ArchiveObjectIdentity, ArchiveStore
from riverhog_core.ports.download_allowance import DownloadAttribution
from riverhog_core.ports.retrieval_cache import RetrievalCacheAdmission, RetrievalCacheReceipt
from riverhog_core.runtime_config import RuntimeConfig
from riverhog_core.services.collection_uploads import SqlAlchemyCollectionUploadService
from riverhog_core.services.retrieval import SqlAlchemyRetrievalService
from riverhog_protocol import CollectionUploadRawDigestBatchDocument
from riverhog_protocol.errors import Conflict, NotFound
from riverhog_protocol.manifest import collection_content_identity
from riverhog_protocol.paths import tag_set_identity

from tests.unit.archive_object_fixtures import MemoryArchiveStore
from tests.unit.artifact_scope_fixtures import persisted_artifact_scope
from tests.unit.db_helpers import sqlite_url
from tests.unit.test_archive_root import MemoryImmutableStore
from tests.unit.test_pack_upload import MemoryResumableStore

MIB = 1024 * 1024


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


def _policy(*, raw: bool = False) -> CollectionVolumePolicy:
    return CollectionVolumePolicy(
        pack_source_bytes=16 * MIB,
        pack_files=100,
        pack_member_bytes=1 if raw else 8 * MIB,
        pack_part_plaintext_bytes=5 * MIB,
        raw_volume_plaintext_bytes=10 * MIB,
        raw_part_plaintext_bytes=5 * MIB,
    )


def _seed_collection(
    tmp_path: Path,
    files: dict[str, bytes],
    *,
    raw: bool = False,
    read_mode: str = "immediate",
    cache: MemoryRetrievalCache | None = None,
    allowance: RecordingDownloadAllowance | None = None,
    pending_timeout: timedelta | None = None,
) -> tuple[
    SqlAlchemyRetrievalService,
    int,
    MemoryArchiveRangeStore,
    DirectArchiveStore,
]:
    database_url = sqlite_url(tmp_path / "catalog.sqlite3")
    config = RuntimeConfig(
        database_url=database_url,
        archive_scrypt_work_factor=1,
        retrieval_pending_timeout=pending_timeout or timedelta(hours=72),
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
    uploads = SqlAlchemyCollectionUploadService(
        config,
        archive_registry,
        policy=_policy(raw=raw),
    )
    opened = uploads.create_or_resume(
        idempotency_key="upload-1",
        initial_tag="docs",
        tag_set_identity_sha256=tag_set_identity(("docs",)),
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
                part_plaintext_bytes=_policy(raw=True).raw_part_plaintext_bytes,
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
    uploads.complete(
        collection_id,
        files_total=len(manifest),
        content_identity=collection_content_identity(
            (str(item["path"]), int(item["bytes"]), str(item["sha256"])) for item in manifest
        ),
    )
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
        files=((collection_id, path),),
        plan_etag=str(plan["etag"]),
    )


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
    assert len(plan["objects"]) == 1
    planned = plan["objects"][0]
    assert planned["kind"] == "pack"
    assert planned["retrieval_bytes"] < planned["stored_bytes"]

    job = service.create(
        app="reader",
        files=((collection_id, "target.bin"),),
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
    assert ranges.requests[0][2] < planned["stored_bytes"]
    assert service.acknowledge(app="reader", job_id=str(job["id"]))["state"] == "completed"


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

    assert plan["files"] == [
        {
            "collection_id": collection_id,
            "path": "selected.bin",
            "bytes": len(files["selected.bin"]),
            "sha256": hashlib.sha256(files["selected.bin"]).hexdigest(),
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

    assert service.process_due() == 1
    ready = service.get(app="reader", job_id=str(job["id"]))
    assert ready["state"] == "ready"
    assert store.prepared == [("pack-" + "0" * 64,)]

    cached_plan = service.plan(((collection_id, "target.bin"),))
    assert [current["read_mode"] for current in cached_plan["objects"]] == ["cache"]
    cached_job = service.create(
        app="reader",
        files=((collection_id, "target.bin"),),
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
    assert len(plan["objects"]) == 2
    job = service.create(
        app="reader",
        files=((collection_id, "large.bin"),),
        plan_etag=str(plan["etag"]),
    )

    assert service.process_due() == 1

    pending = service.get(app="reader", job_id=str(job["id"]))
    assert pending["state"] == "requested"
    assert cache.admission_calls == 2
    assert store.prepare_calls == 0


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
        files=((collection_id, "target.bin"),),
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

    assert allowance.reservations == [(str(job["id"]), int(plan["objects"][0]["retrieval_bytes"]))]
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
            files=((collection_id, "document.txt"),),
            plan_etag=str(plan["etag"]),
            restore_policy="never",
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
    assert service.process_due() == 1
    ready = service.get(app="reader", job_id=str(requested["id"]))

    renewed = service.renew(
        app="reader",
        job_id=str(ready["id"]),
        lease=timedelta(hours=36),
    )

    assert renewed["state"] == "ready"
    assert renewed["lease_seconds"] == 36 * 60 * 60
    with session_scope(service._session_factory) as session:
        lease = session.get(
            RetrievalCacheLeaseRecord,
            (
                f"job:{ready['id']}",
                "archive",
                collection_id,
                "pack-" + "0" * 64,
            ),
        )
        assert lease is not None
        assert lease.expires_at == renewed["expires_at"]


def test_cache_status_list_and_show_respect_catalog_tag_access(tmp_path: Path) -> None:
    cache = MemoryRetrievalCache()
    service, collection_id, _ranges, _store = _seed_collection(
        tmp_path,
        {"document.txt": b"document"},
        read_mode="restore_required",
        cache=cache,
    )
    requested = _ready_job(service, collection_id, "document.txt")
    assert service.process_due() == 1
    permitted = ApplicationPrincipal(
        app="indexer",
        key_id="indexer-key",
        access=frozenset({ApplicationAccess(CATALOG_READ, f"{TAG_PREFIX}docs")}),
    )
    denied = ApplicationPrincipal(
        app="outsider",
        key_id="outsider-key",
        access=frozenset({ApplicationAccess(CATALOG_READ, f"{TAG_PREFIX}other")}),
    )

    status = service.cache_status(principal=permitted)
    listed = service.list_cache_objects(
        page_size=25,
        position=None,
        q=None,
        tag="docs",
        sort="cached_at",
        order="desc",
        principal=permitted,
    )
    filtered = service.list_cache_objects(
        page_size=25,
        position=None,
        q=None,
        tag="docs",
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
        "tag": "docs",
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
            tag=None,
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
    assert service.process_due() == 1
    ready = service.get(app="reader", job_id=str(requested["id"]))
    completed = service.acknowledge(app="reader", job_id=str(ready["id"]))

    assert completed["state"] == "completed"
    assert service.sweep() == 1
    assert len(cache.deleted) == 1
    assert service.cache_status()["objects"] == 0
