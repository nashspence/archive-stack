from __future__ import annotations

import hashlib
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import cast

from riverhog_core.archive_store_registry import ArchiveStoreBinding, ArchiveStoreRegistry
from riverhog_core.catalog_db import initialize_db, make_session_factory, session_scope
from riverhog_core.catalog_models import TagRecord
from riverhog_core.collection_plan import CollectionVolumePolicy
from riverhog_core.ports.archive_store import ArchiveObjectIdentity, ArchiveStore
from riverhog_core.ports.download_allowance import DownloadAttribution
from riverhog_core.ports.retrieval_cache import RetrievalCacheReceipt
from riverhog_core.runtime_config import RuntimeConfig
from riverhog_core.services.collection_uploads import SqlAlchemyCollectionUploadService
from riverhog_core.services.retrieval import SqlAlchemyRetrievalService
from riverhog_protocol.manifest import collection_content_etag
from riverhog_protocol.raw_ingress import hash_raw_source

from tests.fixtures.crypto import FixtureProofStamper
from tests.unit.archive_object_fixtures import MemoryArchiveStore
from tests.unit.db_helpers import sqlite_url
from tests.unit.test_archive_root import MemoryImmutableStore
from tests.unit.test_pack_upload import MemoryMultipartStore

MIB = 1024 * 1024


class MemoryArchiveRangeStore:
    def __init__(self, multipart: MemoryMultipartStore) -> None:
        self._multipart = multipart
        self.requests: list[tuple[str, int, int]] = []

    def iter_object_range(
        self,
        *,
        object_path: str,
        version_id: str | None,
        offset: int,
        size: int,
    ) -> Iterator[bytes]:
        _ = version_id
        self.requests.append((object_path, offset, size))
        yield self._multipart.objects[object_path][0][offset : offset + size]


class DirectArchiveStore(MemoryArchiveStore):
    def __init__(
        self,
        multipart: MemoryMultipartStore,
        *,
        read_mode: str = "immediate",
    ) -> None:
        super().__init__(read_mode=read_mode)
        self._multipart = multipart

    def iter_stored_archive_object(
        self,
        *,
        collection_id: int,
        object: ArchiveObjectIdentity,
        attribution: DownloadAttribution | None = None,
    ) -> Iterator[bytes]:
        _ = collection_id, attribution
        yield self._multipart.objects[object.object_path][0]


class MemoryRetrievalCache:
    def __init__(self) -> None:
        self.objects: dict[tuple[str, str | None], bytes] = {}
        self.range_requests: list[tuple[str, int, int]] = []
        self.deleted: list[tuple[str, str | None]] = []

    def put(
        self,
        *,
        source_store: str,
        collection_id: int,
        object_id: str,
        content: Iterable[bytes],
        content_length: int,
    ) -> RetrievalCacheReceipt:
        payload = b"".join(content)
        assert len(payload) == content_length
        path = f"cache/{source_store}/{collection_id}/{object_id}"
        version = hashlib.sha256(payload).hexdigest()[:16]
        self.objects[(path, version)] = payload
        return RetrievalCacheReceipt(
            object_path=path,
            version_id=version,
            stored_bytes=len(payload),
            stored_sha256=hashlib.sha256(payload).hexdigest(),
            cached_at="2026-08-08T00:00:00.000000Z",
            verified_at="2026-08-08T00:00:00.000000Z",
        )

    def iter_object(
        self,
        *,
        object_path: str,
        version_id: str | None,
        expected_bytes: int,
        expected_sha256: str,
    ) -> Iterator[bytes]:
        payload = self.objects[(object_path, version_id)]
        assert len(payload) == expected_bytes
        assert hashlib.sha256(payload).hexdigest() == expected_sha256
        yield payload

    def iter_object_range(
        self,
        *,
        object_path: str,
        version_id: str | None,
        offset: int,
        size: int,
    ) -> Iterator[bytes]:
        self.range_requests.append((object_path, offset, size))
        yield self.objects[(object_path, version_id)][offset : offset + size]

    def delete(self, *, object_path: str, version_id: str | None) -> None:
        self.deleted.append((object_path, version_id))
        del self.objects[(object_path, version_id)]


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
) -> tuple[
    SqlAlchemyRetrievalService,
    int,
    MemoryArchiveRangeStore,
    DirectArchiveStore,
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

    multipart = MemoryMultipartStore()
    ranges = MemoryArchiveRangeStore(multipart)
    root_store = MemoryImmutableStore()
    archive_store = DirectArchiveStore(multipart, read_mode=read_mode)
    archive_registry = ArchiveStoreRegistry(
        {
            "archive": ArchiveStoreBinding(
                store=cast(ArchiveStore, archive_store),
                multipart_objects=multipart,
                immutable_objects=root_store,
                object_ranges=ranges,
            )
        }
    )
    uploads = SqlAlchemyCollectionUploadService(
        config,
        archive_registry,
        proof_stamper=FixtureProofStamper(),
        policy=_policy(raw=raw),
    )
    opened = uploads.create_or_resume(
        idempotency_key="upload-1",
        tags=("docs",),
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
            digests = hash_raw_source(
                path=path,
                chunks=(content,),
                expected_bytes=len(content),
                part_plaintext_bytes=_policy(raw=True).raw_part_plaintext_bytes,
            )
            entry["raw_parts"] = {
                "part_plaintext_bytes": digests.part_plaintext_bytes,
                "sha256s": list(digests.part_sha256s),
            }
        manifest.append(entry)
    uploads.register_files(collection_id, manifest)
    uploads.complete(
        collection_id,
        files_total=len(manifest),
        content_etag=collection_content_etag(
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
    assert uploads.process_due_finalizations(limit=1) == 1

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
    assert store.prepared == [("pack-000000000000",)]

    chunks, _bytes, _sha256 = service.content(
        app="reader",
        job_id=str(job["id"]),
        collection_id=collection_id,
        path="target.bin",
    )
    assert b"".join(chunks) == files["target.bin"]
    assert cache.range_requests
    assert ranges.requests == []


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
