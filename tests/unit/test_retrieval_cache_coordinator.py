from __future__ import annotations

from pathlib import Path

import pytest
from riverhog_core.catalog_db import initialize_db, make_session_factory, session_scope
from riverhog_core.catalog_models import (
    CollectionArchiveCopyRecord,
    CollectionArchiveObjectRecord,
    CollectionRecord,
    RetrievalCacheLeaseRecord,
    RetrievalCacheObjectRecord,
    RetrievalCachePopulationRecord,
    RetrievalCacheStoreAccountingRecord,
)
from riverhog_core.ports.archive_objects import WriteSession
from riverhog_core.runtime_config import (
    RetrievalCacheStoreRegistration,
    StorageAdapterRegistration,
)
from riverhog_core.services.retrieval_cache import SqlAlchemyRetrievalCache
from riverhog_storage_adapter_protocol import StorageAdapterRejection

from tests.unit.db_helpers import sqlite_url


class _Candidate:
    def __init__(self, name: str, failures: list[str] | None = None) -> None:
        self.name = name
        self.failures = failures or []
        self.begin_calls: list[tuple[str, int, str, int]] = []
        self.abort_calls: list[WriteSession] = []
        self.delete_calls: list[tuple[str, str | None]] = []

    @staticmethod
    def object_path(source_store: str, collection_id: int, object_id: str) -> str:
        return f"objects/{source_store}/{collection_id}/{object_id}"

    def find_completed_population(self, **_: object) -> None:
        return None

    def begin_population(
        self,
        *,
        source_store: str,
        collection_id: int,
        object_id: str,
        expected_bytes: int,
    ) -> WriteSession:
        self.begin_calls.append((source_store, collection_id, object_id, expected_bytes))
        if self.failures:
            code = self.failures.pop(0)
            raise StorageAdapterRejection(code, f"{self.name} refused admission")  # type: ignore[arg-type]
        return WriteSession(
            self.object_path(source_store, collection_id, object_id),
            f"{self.name}-write-{len(self.begin_calls)}",
            expected_bytes,
        )

    def resumable_object_store(self, **_: object) -> _Candidate:
        return self

    def abort_write(self, *, session: WriteSession) -> None:
        self.abort_calls.append(session)

    def delete(self, *, object_path: str, revision: str | None) -> None:
        self.delete_calls.append((object_path, revision))


def _registration(
    name: str,
    *,
    budget: int | None = None,
    enabled: bool = True,
) -> RetrievalCacheStoreRegistration:
    return RetrievalCacheStoreRegistration(
        name=name,
        adapter=StorageAdapterRegistration(
            name=name,
            base_url=f"https://{name}.example.test",
            token_file=Path(f"/run/secrets/{name}.token"),
        ),
        admission_enabled=enabled,
        admission_budget_bytes=budget,
    )


def _coordinator(
    tmp_path: Path,
    candidates: tuple[_Candidate, ...],
    registrations: tuple[RetrievalCacheStoreRegistration, ...],
) -> tuple[SqlAlchemyRetrievalCache, object]:
    database_url = sqlite_url(tmp_path / "catalog.sqlite3")
    initialize_db(database_url)
    factory = make_session_factory(database_url)
    return (
        SqlAlchemyRetrievalCache(
            {candidate.name: candidate for candidate in candidates},  # type: ignore[arg-type]
            {registration.name: registration for registration in registrations},
            session_factory=factory,
        ),
        factory,
    )


def _seed_ready_object(
    factory: object,
    *,
    cache_store: str,
    object_id: str = "old-volume",
    stored_bytes: int = 60,
    leased: bool = False,
) -> None:
    now = "2026-08-08T00:00:00.000000Z"
    with session_scope(factory) as session:  # type: ignore[arg-type]
        if session.get(CollectionRecord, 1) is None:
            session.add(
                CollectionRecord(
                    id=1,
                    creation_idempotency_key="fixture",
                    creation_identity_sha256="1" * 64,
                    creation_custody_mode="producer-retained",
                    archive_generation="2" * 64,
                    content_identity="3" * 64,
                    tag_set_identity="4" * 64,
                    encryption_format="age-scrypt/v1",
                    passphrase_id="fixture",
                    provenance_mode="omitted",
                    provenance_identity=None,
                    inventory_identity="5" * 64,
                    metadata_revision=1,
                    metadata_updated_at=now,
                    ingest_source=None,
                    created_by_app="fixture",
                    created_by_key_id=None,
                    created_at=now,
                    is_published=True,
                    file_count=0,
                    file_bytes=0,
                )
            )
            session.add(
                CollectionArchiveCopyRecord(
                    collection_id=1,
                    store="deep",
                    state="uploaded",
                    archive_storage_prefix="archives/1",
                    last_uploaded_at=now,
                    last_verified_at=now,
                    failure=None,
                )
            )
            session.flush()
        session.add(
            CollectionArchiveObjectRecord(
                collection_id=1,
                store="deep",
                object_id=object_id,
                object_order=0,
                kind="raw",
                object_path=f"archives/1/{object_id}",
                plaintext_bytes=stored_bytes,
                stored_bytes=stored_bytes,
                sha256=None,
                stored_sha256=None,
                revision="archive-revision",
                age_state_json=None,
                archive_parts_json=None,
                plan_sha256=None,
                index_sha256=None,
                uploaded_at=now,
                verified_at=now,
            )
        )
        session.flush()
        session.add(
            RetrievalCacheObjectRecord(
                source_store="deep",
                collection_id=1,
                object_id=object_id,
                cache_store=cache_store,
                object_path=f"cache/{object_id}",
                revision="cache-revision",
                stored_bytes=stored_bytes,
                stored_sha256=None,
                cached_at=now,
                verified_at=now,
                state="ready",
            )
        )
        session.flush()
        accounting = session.get(RetrievalCacheStoreAccountingRecord, cache_store)
        assert accounting is not None
        accounting.committed_bytes += stored_bytes
        if leased:
            session.add(
                RetrievalCacheLeaseRecord(
                    owner="active-job",
                    source_store="deep",
                    collection_id=1,
                    object_id=object_id,
                    expires_at="2026-09-08T00:00:00.000000Z",
                )
            )


def test_explicit_capacity_refusal_falls_through_in_declared_order(tmp_path: Path) -> None:
    local = _Candidate("local", ["insufficient_storage"])
    elastic = _Candidate("elastic")
    cache, factory = _coordinator(
        tmp_path,
        (local, elastic),
        (_registration("local", budget=100), _registration("elastic")),
    )

    admission = cache.admit(
        owner="job:1",
        source_store="deep",
        collection_id=1,
        object_id="volume-0",
        expected_bytes=60,
    )

    assert admission is not None and admission.cache_store == "elastic"
    assert len(local.begin_calls) == len(elastic.begin_calls) == 1
    with session_scope(factory) as session:  # type: ignore[arg-type]
        assert session.get(RetrievalCacheStoreAccountingRecord, "local").reserved_bytes == 0
        assert session.get(RetrievalCacheStoreAccountingRecord, "elastic").reserved_bytes == 60


def test_ambiguous_admission_retries_the_same_store_without_double_reserving(
    tmp_path: Path,
) -> None:
    local = _Candidate("local", ["provider_unavailable"])
    elastic = _Candidate("elastic")
    cache, factory = _coordinator(
        tmp_path,
        (local, elastic),
        (_registration("local", budget=100), _registration("elastic")),
    )

    with pytest.raises(StorageAdapterRejection, match="local refused"):
        cache.admit(
            owner="job:1",
            source_store="deep",
            collection_id=1,
            object_id="volume-0",
            expected_bytes=60,
        )
    restarted = SqlAlchemyRetrievalCache(
        {"local": local, "elastic": elastic},  # type: ignore[arg-type]
        {
            "local": _registration("local", budget=100),
            "elastic": _registration("elastic"),
        },
        session_factory=factory,  # type: ignore[arg-type]
    )
    admission = restarted.admit(
        owner="job:1",
        source_store="deep",
        collection_id=1,
        object_id="volume-0",
        expected_bytes=60,
    )

    assert admission is not None and admission.cache_store == "local"
    assert len(local.begin_calls) == 2
    assert elastic.begin_calls == []
    with session_scope(factory) as session:  # type: ignore[arg-type]
        assert session.get(RetrievalCacheStoreAccountingRecord, "local").reserved_bytes == 60


def test_one_collection_may_span_candidates_without_splitting_an_object(tmp_path: Path) -> None:
    local = _Candidate("local")
    elastic = _Candidate("elastic")
    cache, factory = _coordinator(
        tmp_path,
        (local, elastic),
        (_registration("local", budget=100), _registration("elastic")),
    )

    first = cache.admit(
        owner="job:1",
        source_store="deep",
        collection_id=1,
        object_id="volume-0",
        expected_bytes=60,
    )
    second = cache.admit(
        owner="job:1",
        source_store="deep",
        collection_id=1,
        object_id="volume-1",
        expected_bytes=60,
    )

    assert first is not None and first.cache_store == "local"
    assert second is not None and second.cache_store == "elastic"
    with session_scope(factory) as session:  # type: ignore[arg-type]
        placements = {
            row.object_id: row.cache_store
            for row in session.query(RetrievalCachePopulationRecord).all()
        }
    assert placements == {"volume-0": "local", "volume-1": "elastic"}


def test_disabled_candidate_drains_existing_state_but_accepts_no_new_work(
    tmp_path: Path,
) -> None:
    local = _Candidate("local")
    elastic = _Candidate("elastic")
    cache, _factory = _coordinator(
        tmp_path,
        (local, elastic),
        (_registration("local", enabled=False), _registration("elastic")),
    )

    admission = cache.admit(
        owner="job:1",
        source_store="deep",
        collection_id=1,
        object_id="volume-0",
        expected_bytes=60,
    )

    assert admission is not None and admission.cache_store == "elastic"
    assert local.begin_calls == []


def test_full_finite_store_evicts_one_unleased_object_before_fallback(tmp_path: Path) -> None:
    local = _Candidate("local")
    elastic = _Candidate("elastic")
    cache, factory = _coordinator(
        tmp_path,
        (local, elastic),
        (_registration("local", budget=100), _registration("elastic")),
    )
    _seed_ready_object(factory, cache_store="local")

    admission = cache.admit(
        owner="job:1",
        source_store="deep",
        collection_id=1,
        object_id="new-volume",
        expected_bytes=60,
    )

    assert admission is not None and admission.cache_store == "local"
    assert local.delete_calls == [("cache/old-volume", "cache-revision")]
    assert elastic.begin_calls == []
    with session_scope(factory) as session:  # type: ignore[arg-type]
        accounting = session.get(RetrievalCacheStoreAccountingRecord, "local")
        assert accounting is not None
        assert (accounting.committed_bytes, accounting.reserved_bytes) == (0, 60)


def test_finite_store_never_evicts_a_leased_object_to_avoid_fallback(tmp_path: Path) -> None:
    local = _Candidate("local")
    elastic = _Candidate("elastic")
    cache, factory = _coordinator(
        tmp_path,
        (local, elastic),
        (_registration("local", budget=100), _registration("elastic")),
    )
    _seed_ready_object(factory, cache_store="local", leased=True)

    admission = cache.admit(
        owner="job:1",
        source_store="deep",
        collection_id=1,
        object_id="new-volume",
        expected_bytes=60,
    )

    assert admission is not None and admission.cache_store == "elastic"
    assert local.delete_calls == []


def test_population_survives_one_shared_owner_and_is_reclaimed_after_the_last(
    tmp_path: Path,
) -> None:
    local = _Candidate("local")
    cache, factory = _coordinator(
        tmp_path,
        (local,),
        (_registration("local", budget=100),),
    )
    first = cache.admit(
        owner="job:1",
        source_store="deep",
        collection_id=1,
        object_id="volume-0",
        expected_bytes=60,
    )
    second = cache.admit(
        owner="job:2",
        source_store="deep",
        collection_id=1,
        object_id="volume-0",
        expected_bytes=60,
    )
    assert first is not None and second is not None
    assert first.write_token == second.write_token

    assert cache.release(owner="job:1") == 0
    with session_scope(factory) as session:  # type: ignore[arg-type]
        assert session.get(RetrievalCachePopulationRecord, ("deep", 1, "volume-0")) is not None
        accounting = session.get(RetrievalCacheStoreAccountingRecord, "local")
        assert accounting is not None and accounting.reserved_bytes == 60

    assert cache.release(owner="job:2") == 1
    assert len(local.abort_calls) == 1
    with session_scope(factory) as session:  # type: ignore[arg-type]
        assert session.get(RetrievalCachePopulationRecord, ("deep", 1, "volume-0")) is None
        accounting = session.get(RetrievalCacheStoreAccountingRecord, "local")
        assert accounting is not None and accounting.reserved_bytes == 0
