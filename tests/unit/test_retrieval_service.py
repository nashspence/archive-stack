from __future__ import annotations

import hashlib
from collections.abc import Iterable, Iterator
from dataclasses import replace
from pathlib import Path

import pytest

from riverhog_age import encrypt_age_scrypt
from riverhog_core.archive_store_registry import ArchiveStoreRegistry
from riverhog_core.catalog_db import make_session_factory, session_scope
from riverhog_core.catalog_models import (
    CollectionArchiveCopyRecord,
    CollectionFileRecord,
    CollectionRecord,
    RetrievalCacheLeaseRecord,
    RetrievalCacheObjectRecord,
)
from riverhog_core.domain.errors import NotFound
from riverhog_core.portable_catalog import portable_collection_manifest
from riverhog_core.ports.archive_store import ArchiveObjectIdentity
from riverhog_core.ports.retrieval_cache import RetrievalCacheReceipt
from riverhog_core.runtime_config import RetrievalCacheConfig
from riverhog_core.services.archive_records import apply_archive_receipt
from riverhog_core.services.retrieval import SqlAlchemyRetrievalService
from tests.fixtures.crypto import FixtureProofVerifier
from tests.unit.archive_object_fixtures import (
    COLLECTION_ID,
    MemoryArchiveStore,
    archive_receipt,
    as_archive_store,
    make_archive,
    seed_archive_copy,
)

FILES = {
    "one.txt": b"first archived file\n",
    "two.txt": b"second archived file\n",
}
SECOND_COLLECTION_ID = "2026/20260102T030406Z__more-docs"


class MemoryRetrievalCache:
    def __init__(self) -> None:
        self.objects: dict[tuple[str, str | None], bytes] = {}
        self.deleted: list[tuple[str, str | None]] = []

    def put(
        self,
        *,
        source_store: str,
        collection_id: str,
        object_id: str,
        content: Iterable[bytes],
        content_length: int,
    ) -> RetrievalCacheReceipt:
        _ = content_length
        payload = b"".join(content)
        path = f"cache/{source_store}/{collection_id}/{object_id}"
        version = hashlib.sha256(payload).hexdigest()[:16]
        self.objects[(path, version)] = payload
        return RetrievalCacheReceipt(
            object_path=path,
            version_id=version,
            stored_bytes=len(payload),
            stored_sha256=hashlib.sha256(payload).hexdigest(),
            cached_at="2026-07-18T00:00:00.000000Z",
            verified_at="2026-07-18T00:00:00.000000Z",
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

    def delete(self, *, object_path: str, version_id: str | None) -> None:
        self.deleted.append((object_path, version_id))
        del self.objects[(object_path, version_id)]


class PreparedArchiveStore(MemoryArchiveStore):
    def __init__(self, archive, *, passphrase: str) -> None:
        super().__init__(archive, read_mode="restore_required")
        self._passphrase = passphrase

    def iter_stored_archive_object(
        self,
        *,
        collection_id: str,
        object: ArchiveObjectIdentity,
    ) -> Iterator[bytes]:
        plaintext = b"".join(
            super().iter_archive_object(collection_id=collection_id, object=object)
        )
        yield encrypt_age_scrypt(plaintext, self._passphrase, log_n=1)


class PartiallyReadyArchiveStore(PreparedArchiveStore):
    def get_archive_objects_read_status(
        self,
        *,
        collection_id: str,
        **_kwargs: object,
    ):
        from riverhog_core.ports.archive_store import ArchiveReadStatus

        return ArchiveReadStatus(
            state="ready" if collection_id == COLLECTION_ID else "requested"
        )


def test_immediate_retrieval_plan_serves_only_selected_logical_file(tmp_path: Path) -> None:
    config, archive = seed_archive_copy(tmp_path / "catalog.sqlite3", FILES)
    store = MemoryArchiveStore(archive)
    service = SqlAlchemyRetrievalService(
        config,
        ArchiveStoreRegistry({"deep": as_archive_store(store)}),
        None,
        proof_verifier=FixtureProofVerifier(),
    )
    selection = [(COLLECTION_ID, "two.txt")]

    plan = service.plan(selection)
    job = service.create(
        app="fishbox",
        files=selection,
        plan_etag=str(plan["etag"]),
    )
    chunks, byte_count, sha256 = service.content(
        app="fishbox",
        job_id=str(job["id"]),
        collection_id=COLLECTION_ID,
        path="two.txt",
    )

    assert plan["format"] == "riverhog-retrieval-plan/v1"
    assert [item["kind"] for item in plan["objects"]] == ["pack", "manifest", "proof"]
    assert job["state"] == "ready"
    assert byte_count == len(FILES["two.txt"])
    assert sha256 == hashlib.sha256(FILES["two.txt"]).hexdigest()
    assert b"".join(chunks) == FILES["two.txt"]
    with pytest.raises(NotFound):
        service.content_metadata(
            app="another-app",
            job_id=str(job["id"]),
            collection_id=COLLECTION_ID,
            path="two.txt",
        )
    assert service.acknowledge(app="fishbox", job_id=str(job["id"]))["state"] == "completed"


def test_app_can_cancel_its_ready_retrieval_job(tmp_path: Path) -> None:
    config, archive = seed_archive_copy(tmp_path / "catalog.sqlite3", FILES)
    service = SqlAlchemyRetrievalService(
        config,
        ArchiveStoreRegistry({"deep": as_archive_store(MemoryArchiveStore(archive))}),
        None,
        proof_verifier=FixtureProofVerifier(),
    )
    selection = [(COLLECTION_ID, "one.txt")]
    plan = service.plan(selection)
    job = service.create(app="fishbox", files=selection, plan_etag=str(plan["etag"]))

    canceled = service.cancel(app="fishbox", job_id=str(job["id"]))

    assert canceled["state"] == "canceled"
    assert canceled["canceled_at"] is not None
    assert service.process_due(limit=1) == 0


def test_provider_prepared_retrieval_uses_leased_encrypted_cache(tmp_path: Path) -> None:
    config, archive = seed_archive_copy(tmp_path / "catalog.sqlite3", FILES)
    deep = replace(config.archive_store("deep"), read_mode="restore_required")
    config = replace(
        config,
        archive_stores={"deep": deep},
        retrieval_cache=RetrievalCacheConfig(
            endpoint_url="https://cache.example.invalid",
            region="example",
            bucket="cache",
            access_key_id="key",
            secret_access_key="secret",
        ),
    )
    store = PreparedArchiveStore(archive, passphrase=config.archive_passphrase)
    cache = MemoryRetrievalCache()
    service = SqlAlchemyRetrievalService(
        config,
        ArchiveStoreRegistry({"deep": as_archive_store(store)}),
        cache,  # type: ignore[arg-type]
        proof_verifier=FixtureProofVerifier(),
    )
    selection = [(COLLECTION_ID, "one.txt")]
    plan = service.plan(selection)
    job = service.create(
        app="fishbox",
        files=selection,
        plan_etag=str(plan["etag"]),
    )

    assert job["state"] == "requested"
    assert store.prepared == [("data-000000",)]
    assert service.process_due(limit=1) == 1
    job = service.get(app="fishbox", job_id=str(job["id"]))
    chunks, _size, _sha256 = service.content(
        app="fishbox",
        job_id=str(job["id"]),
        collection_id=COLLECTION_ID,
        path="one.txt",
    )
    assert job["state"] == "ready"
    assert b"".join(chunks) == FILES["one.txt"]
    with session_scope(make_session_factory(config.database_url)) as session:
        assert session.query(RetrievalCacheObjectRecord).count() == 1
        assert session.query(RetrievalCacheLeaseRecord).count() == 1

    service.acknowledge(app="fishbox", job_id=str(job["id"]))
    assert service.sweep() == 1
    assert len(cache.deleted) == 1


def test_partially_prepared_job_keeps_completed_cache_objects_leased(tmp_path: Path) -> None:
    config, archive = seed_archive_copy(tmp_path / "catalog.sqlite3", FILES)
    second_archive = make_archive(
        {"three.txt": b"third archived file\n"},
        collection_id=SECOND_COLLECTION_ID,
    )
    with session_scope(make_session_factory(config.database_url)) as session:
        _manifest, manifest_etag = portable_collection_manifest(
            SECOND_COLLECTION_ID,
            ((file.path, file.bytes, file.sha256) for file in second_archive.files),
        )
        session.add(
            CollectionRecord(id=SECOND_COLLECTION_ID, manifest_etag=manifest_etag)
        )
        for file in second_archive.files:
            session.add(
                CollectionFileRecord(
                    collection_id=SECOND_COLLECTION_ID,
                    path=file.path,
                    bytes=file.bytes,
                    sha256=file.sha256,
                )
            )
        copy = CollectionArchiveCopyRecord(collection_id=SECOND_COLLECTION_ID, store="deep")
        session.add(copy)
        session.flush()
        apply_archive_receipt(copy, archive_receipt(second_archive), second_archive)

    deep = replace(config.archive_store("deep"), read_mode="restore_required")
    config = replace(
        config,
        archive_stores={"deep": deep},
        retrieval_cache=RetrievalCacheConfig(
            endpoint_url="https://cache.example.invalid",
            region="example",
            bucket="cache",
            access_key_id="key",
            secret_access_key="secret",
        ),
    )
    store = PartiallyReadyArchiveStore(archive, passphrase=config.archive_passphrase)
    cache = MemoryRetrievalCache()
    service = SqlAlchemyRetrievalService(
        config,
        ArchiveStoreRegistry({"deep": as_archive_store(store)}),
        cache,  # type: ignore[arg-type]
        proof_verifier=FixtureProofVerifier(),
    )
    selection = [
        (COLLECTION_ID, "one.txt"),
        (SECOND_COLLECTION_ID, "three.txt"),
    ]
    plan = service.plan(selection)
    job = service.create(app="fishbox", files=selection, plan_etag=str(plan["etag"]))

    assert service.process_due(limit=1) == 1
    assert service.get(app="fishbox", job_id=str(job["id"]))["state"] == "requested"
    with session_scope(make_session_factory(config.database_url)) as session:
        cached = session.query(RetrievalCacheObjectRecord).one()
        lease = session.query(RetrievalCacheLeaseRecord).one()
        assert (cached.collection_id, cached.object_id) == (COLLECTION_ID, "data-000000")
        assert lease.owner == f"job:{job['id']}"
    assert cache.deleted == []
