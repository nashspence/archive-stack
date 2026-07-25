from __future__ import annotations

import hashlib
from collections.abc import Iterable, Iterator
from dataclasses import replace
from pathlib import Path

import pytest
from riverhog_age import encrypt_age_scrypt
from riverhog_core.app_permissions import (
    CATALOG_READ,
    KEYS_MANAGE,
    RETRIEVAL_MANAGE,
    ApplicationAccess,
    ApplicationPrincipal,
)
from riverhog_core.archive_store_registry import ArchiveStoreRegistry
from riverhog_core.catalog_db import make_session_factory, session_scope
from riverhog_core.catalog_models import (
    CatalogEventRecord,
    CollectionArchiveCopyRecord,
    CollectionFileRecord,
    CollectionRecord,
    CollectionSlugRecord,
    RetrievalCacheLeaseRecord,
    RetrievalCacheObjectRecord,
)
from riverhog_core.portable_catalog import portable_collection_manifest
from riverhog_core.ports.archive_store import ArchiveObjectIdentity
from riverhog_core.ports.download_allowance import DownloadAttribution
from riverhog_core.ports.retrieval_cache import RetrievalCacheReceipt
from riverhog_core.runtime_config import RetrievalCacheConfig
from riverhog_core.services.app_keys import SqlAlchemyAppKeyService
from riverhog_core.services.archive_records import apply_archive_receipt
from riverhog_core.services.download_allowances import SqlAlchemyDownloadAllowance
from riverhog_core.services.retrieval import SqlAlchemyRetrievalService
from riverhog_protocol.errors import DownloadAllowanceExceeded, NotFound

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
SECOND_COLLECTION_ID = "more-docs/20260102T030406Z"
BOOTSTRAP = ApplicationPrincipal(
    app="bootstrap",
    key_id=None,
    access=frozenset({ApplicationAccess(KEYS_MANAGE)}),
    unrestricted_delegation=True,
)


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
        _ = (key_id, expires_at)
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


class PreparedArchiveStore(MemoryArchiveStore):
    def __init__(self, archive, *, passphrase: str) -> None:
        super().__init__(archive, read_mode="restore_required")
        self._passphrase = passphrase

    def iter_stored_archive_object(
        self,
        *,
        collection_id: str,
        object: ArchiveObjectIdentity,
        attribution: DownloadAttribution | None = None,
    ) -> Iterator[bytes]:
        _ = attribution
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

        return ArchiveReadStatus(state="ready" if collection_id == COLLECTION_ID else "requested")


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
        app="local",
        files=selection,
        plan_etag=str(plan["etag"]),
    )
    object_chunks, object_bytes, object_sha256 = service.object_content(
        app="local",
        job_id=str(job["id"]),
        collection_id=COLLECTION_ID,
        object_id="data-000000",
    )
    object_content = b"".join(object_chunks)
    chunks, byte_count, sha256 = service.content(
        app="local",
        job_id=str(job["id"]),
        collection_id=COLLECTION_ID,
        path="two.txt",
    )

    assert plan["format"] == "riverhog-retrieval-plan/v1"
    assert [item["kind"] for item in plan["objects"]] == ["pack", "manifest", "proof"]
    assert plan["objects"][0]["placements"] == [
        {
            "path": "two.txt",
            "sequence": 0,
            "file_offset": 0,
            "bytes": len(FILES["two.txt"]),
            "member": "two.txt",
        }
    ]
    assert job["state"] == "ready"
    assert job["objects"] == plan["objects"]
    assert object_bytes == len(object_content)
    assert object_sha256 == hashlib.sha256(object_content).hexdigest()
    assert store.read[0] == "data-000000"
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
    assert service.acknowledge(app="local", job_id=str(job["id"]))["state"] == "completed"


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
    job = service.create(app="local", files=selection, plan_etag=str(plan["etag"]))

    canceled = service.cancel(app="local", job_id=str(job["id"]))

    assert canceled["state"] == "canceled"
    assert canceled["canceled_at"] is not None
    assert service.process_due(limit=1) == 0


def test_retrieval_grants_and_job_ownership_follow_the_logical_key(tmp_path: Path) -> None:
    config, archive = seed_archive_copy(tmp_path / "catalog.sqlite3", FILES)
    service = SqlAlchemyRetrievalService(
        config,
        ArchiveStoreRegistry({"deep": as_archive_store(MemoryArchiveStore(archive))}),
        None,
        proof_verifier=FixtureProofVerifier(),
    )
    principal = ApplicationPrincipal(
        app="review",
        key_id="key-one",
        access=frozenset(
            {ApplicationAccess(RETRIEVAL_MANAGE, f"collection:{COLLECTION_ID}")}
        ),
    )
    selection = [(COLLECTION_ID, "one.txt")]
    plan = service.plan(selection, principal=principal)
    job = service.create(
        app=principal.app,
        key_id=principal.key_id,
        files=selection,
        plan_etag=str(plan["etag"]),
        principal=principal,
    )

    assert (
        service.get(
            app="review",
            key_id="key-one",
            job_id=str(job["id"]),
        )["state"]
        == "ready"
    )
    with pytest.raises(NotFound):
        service.get(app="review", key_id="key-two", job_id=str(job["id"]))
    with pytest.raises(NotFound):
        service.plan(
            selection,
            principal=ApplicationPrincipal(
                app="other",
                key_id="other-key",
                access=frozenset({ApplicationAccess(RETRIEVAL_MANAGE, "slug:other")}),
            ),
        )


def test_retrieval_reserves_key_quota_before_creating_or_preparing_a_job(
    tmp_path: Path,
) -> None:
    config, archive = seed_archive_copy(tmp_path / "catalog.sqlite3", FILES)
    keys = SqlAlchemyAppKeyService(config)
    created = keys.create(
        app="review",
        access=(
            ApplicationAccess(RETRIEVAL_MANAGE, f"collection:{COLLECTION_ID}"),
        ),
        grantor=BOOTSTRAP,
    )
    principal = keys.authenticate(str(created["token"]))
    assert principal is not None
    allowance = SqlAlchemyDownloadAllowance(config)
    store = MemoryArchiveStore(archive)
    service = SqlAlchemyRetrievalService(
        config,
        ArchiveStoreRegistry({"deep": as_archive_store(store)}),
        None,
        download_allowance=allowance,
        proof_verifier=FixtureProofVerifier(),
    )
    selection = [(COLLECTION_ID, "one.txt")]
    plan = service.plan(selection, principal=principal)

    with pytest.raises(DownloadAllowanceExceeded, match="0 bytes remaining"):
        service.create(
            app=principal.app,
            key_id=principal.key_id,
            files=selection,
            plan_etag=str(plan["etag"]),
            principal=principal,
        )

    assert store.prepared == []
    expected_archive_bytes = sum(
        int(current["stored_bytes"])
        for current in plan["objects"]
        if current["read_mode"] != "cache"
    )
    allowance.set_key_quota(
        app=principal.app,
        key_id=str(principal.key_id),
        monthly_bytes=expected_archive_bytes,
    )
    job = service.create(
        app=principal.app,
        key_id=principal.key_id,
        files=selection,
        plan_etag=str(plan["etag"]),
        principal=principal,
    )
    quota = allowance.get_key_quota(key_id=str(principal.key_id))
    assert job["state"] == "ready"
    assert quota["reserved_bytes"] == expected_archive_bytes
    assert quota["remaining_bytes"] == 0


def test_filtered_catalog_changes_advance_past_invisible_rows(tmp_path: Path) -> None:
    config, archive = seed_archive_copy(tmp_path / "catalog.sqlite3", FILES)
    with session_scope(make_session_factory(config.database_url)) as session:
        session.add_all(
            [
                CatalogEventRecord(
                    change="created",
                    collection_id="hidden/20260102T030404Z",
                    occurred_at="2026-07-18T00:00:00.000000Z",
                    manifest_etag="a" * 64,
                ),
                CatalogEventRecord(
                    change="created",
                    collection_id=COLLECTION_ID,
                    occurred_at="2026-07-18T00:00:01.000000Z",
                    manifest_etag="b" * 64,
                ),
            ]
        )
    service = SqlAlchemyRetrievalService(
        config,
        ArchiveStoreRegistry({"deep": as_archive_store(MemoryArchiveStore(archive))}),
        None,
        proof_verifier=FixtureProofVerifier(),
    )
    principal = ApplicationPrincipal(
        app="reader",
        key_id="reader-key",
        access=frozenset(
            {ApplicationAccess(CATALOG_READ, f"collection:{COLLECTION_ID}")}
        ),
    )

    first = service.change_list(after=0, limit=1, principal=principal)
    second = service.change_list(after=int(first["cursor"]), limit=1, principal=principal)

    assert first == {"cursor": 1, "changes": []}
    assert second["cursor"] == 2
    assert [change["collection_id"] for change in second["changes"]] == [COLLECTION_ID]


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
    principal = ApplicationPrincipal(
        app="local",
        key_id="local-key",
        access=frozenset(
            {ApplicationAccess(RETRIEVAL_MANAGE, f"collection:{COLLECTION_ID}")}
        ),
    )
    allowance = RecordingDownloadAllowance()
    service = SqlAlchemyRetrievalService(
        config,
        ArchiveStoreRegistry({"deep": as_archive_store(store)}),
        cache,  # type: ignore[arg-type]
        download_allowance=allowance,  # type: ignore[arg-type]
        proof_verifier=FixtureProofVerifier(),
    )
    selection = [(COLLECTION_ID, "one.txt")]
    plan = service.plan(selection, principal=principal)
    expected_remote_bytes = sum(
        int(current["stored_bytes"])
        * (2 if current["read_mode"] == "restore_required" else 1)
        for current in plan["objects"]
    )
    job = service.create(
        app=principal.app,
        key_id=principal.key_id,
        files=selection,
        plan_etag=str(plan["etag"]),
        principal=principal,
    )

    assert job["state"] == "requested"
    assert allowance.reservations == [(job["id"], expected_remote_bytes)]
    assert store.prepared == [("data-000000",)]
    assert service.process_due(limit=1) == 1
    job = service.get(
        app=principal.app,
        key_id=principal.key_id,
        job_id=str(job["id"]),
    )
    chunks, _size, _sha256 = service.content(
        app=principal.app,
        key_id=principal.key_id,
        job_id=str(job["id"]),
        collection_id=COLLECTION_ID,
        path="one.txt",
    )
    assert job["state"] == "ready"
    assert b"".join(chunks) == FILES["one.txt"]
    assert {store for store, _bytes, _attribution in allowance.tracked} == {
        "retrieval-cache"
    }
    with session_scope(make_session_factory(config.database_url)) as session:
        assert session.query(RetrievalCacheObjectRecord).count() == 1
        assert session.query(RetrievalCacheLeaseRecord).count() == 1

    cached_principal = ApplicationPrincipal(
        app="cached-reader",
        key_id="cached-key",
        access=principal.access,
    )
    cached_plan = service.plan(selection, principal=cached_principal)
    assert "restore_required" not in {
        str(current["read_mode"]) for current in cached_plan["objects"]
    }
    cached_expected_bytes = sum(
        int(current["stored_bytes"]) for current in cached_plan["objects"]
    )
    cached_job = service.create(
        app=cached_principal.app,
        key_id=cached_principal.key_id,
        files=selection,
        plan_etag=str(cached_plan["etag"]),
        principal=cached_principal,
    )
    assert allowance.reservations[-1] == (cached_job["id"], cached_expected_bytes)
    cached_chunks, _cached_size, _cached_sha256 = service.content(
        app=cached_principal.app,
        key_id=cached_principal.key_id,
        job_id=str(cached_job["id"]),
        collection_id=COLLECTION_ID,
        path="one.txt",
    )
    assert b"".join(cached_chunks) == FILES["one.txt"]
    service.acknowledge(
        app=cached_principal.app,
        key_id=cached_principal.key_id,
        job_id=str(cached_job["id"]),
    )

    service.acknowledge(
        app=principal.app,
        key_id=principal.key_id,
        job_id=str(job["id"]),
    )
    assert set(allowance.released) == {str(job["id"]), str(cached_job["id"])}
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
            CollectionSlugRecord(
                id="more-docs",
                created_by_app="fixture",
                created_at="2026-01-01T00:00:00.000000Z",
            )
        )
        session.add(
            CollectionRecord(
                id=SECOND_COLLECTION_ID,
                slug="more-docs",
                manifest_etag=manifest_etag,
            )
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
    job = service.create(app="local", files=selection, plan_etag=str(plan["etag"]))

    assert service.process_due(limit=1) == 1
    assert service.get(app="local", job_id=str(job["id"]))["state"] == "requested"
    with session_scope(make_session_factory(config.database_url)) as session:
        cached = session.query(RetrievalCacheObjectRecord).one()
        lease = session.query(RetrievalCacheLeaseRecord).one()
        assert (cached.collection_id, cached.object_id) == (COLLECTION_ID, "data-000000")
        assert lease.owner == f"job:{job['id']}"
    assert cache.deleted == []
