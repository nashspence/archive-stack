from __future__ import annotations

from collections.abc import Iterable, Iterator, Mapping
from datetime import datetime

from riverhog_storage_adapter_protocol import StorageAdapterRejection
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from time_formats import format_utc_timestamp, utc_now

from riverhog_core.catalog_db import SessionFactory, session_scope
from riverhog_core.catalog_models import (
    RetrievalCacheLeaseRecord,
    RetrievalCacheObjectRecord,
    RetrievalCachePopulationClaimRecord,
    RetrievalCachePopulationRecord,
    RetrievalCacheStoreAccountingRecord,
)
from riverhog_core.domain.retrieval_cache import RetrievalCacheReceipt
from riverhog_core.ports.archive_objects import ArchiveResumableObjectStore, WriteSession
from riverhog_core.ports.retrieval_cache import RetrievalCacheAdmission
from riverhog_core.runtime_config import RetrievalCacheStoreRegistration
from riverhog_core.stores.storage_adapter_retrieval_cache import StorageAdapterRetrievalCache


class SqlAlchemyRetrievalCache:
    """Coordinate one logical cache placement across ordered named stores."""

    def __init__(
        self,
        stores: Mapping[str, StorageAdapterRetrievalCache],
        registrations: Mapping[str, RetrievalCacheStoreRegistration],
        *,
        session_factory: SessionFactory,
    ) -> None:
        if tuple(stores) != tuple(registrations):
            raise ValueError("retrieval cache stores and registrations differ")
        self._stores = dict(stores)
        self._registrations = dict(registrations)
        self._session_factory = session_factory
        self._ensure_accounting_rows()

    @property
    def store_names(self) -> tuple[str, ...]:
        return tuple(self._stores)

    def abort_incomplete_writes(self, *, initiated_before: datetime) -> int:
        return sum(
            store.abort_incomplete_writes(initiated_before=initiated_before)
            for store in self._stores.values()
        )

    def admit(
        self,
        *,
        owner: str,
        source_store: str,
        collection_id: int,
        object_id: str,
        expected_bytes: int,
    ) -> RetrievalCacheAdmission | None:
        return self._admit(
            owner=owner,
            source_store=source_store,
            collection_id=collection_id,
            object_id=object_id,
            expected_bytes=expected_bytes,
            eviction_attempted=frozenset(),
        )

    def _admit(
        self,
        *,
        owner: str,
        source_store: str,
        collection_id: int,
        object_id: str,
        expected_bytes: int,
        eviction_attempted: frozenset[str],
    ) -> RetrievalCacheAdmission | None:
        if not owner.strip():
            raise ValueError("retrieval cache admission owner must be non-empty")
        if expected_bytes < 1:
            raise ValueError("retrieval cache admission bytes must be positive")
        key = (source_store, collection_id, object_id)
        with session_scope(self._session_factory) as session:
            ready = session.get(RetrievalCacheObjectRecord, key)
            if ready is not None:
                if ready.stored_bytes != expected_bytes:
                    raise RuntimeError("retrieval cache object byte identity changed")
                return None
            population = session.scalar(
                select(RetrievalCachePopulationRecord)
                .where(
                    RetrievalCachePopulationRecord.source_store == source_store,
                    RetrievalCachePopulationRecord.collection_id == collection_id,
                    RetrievalCachePopulationRecord.object_id == object_id,
                )
                .with_for_update()
            )
            now = format_utc_timestamp(utc_now())
            if population is None:
                try:
                    with session.begin_nested():
                        session.add(
                            RetrievalCachePopulationRecord(
                                source_store=source_store,
                                collection_id=collection_id,
                                object_id=object_id,
                                cache_store=None,
                                object_path=None,
                                write_token=None,
                                expected_bytes=expected_bytes,
                                state="waiting",
                                initiated_at=now,
                                updated_at=now,
                                failure=None,
                            )
                        )
                        session.flush()
                except IntegrityError:
                    session.expire_all()
                population = session.scalar(
                    select(RetrievalCachePopulationRecord)
                    .where(
                        RetrievalCachePopulationRecord.source_store == source_store,
                        RetrievalCachePopulationRecord.collection_id == collection_id,
                        RetrievalCachePopulationRecord.object_id == object_id,
                    )
                    .with_for_update()
                )
                if population is None:
                    raise RuntimeError("retrieval cache population could not be established")
            elif population.expected_bytes != expected_bytes:
                raise RuntimeError("retrieval cache population byte identity changed")
            if population.state == "abandoning":
                return None
            self._claim(session, owner=owner, key=key)
            if population.state in {"admitted", "writing"}:
                assert population.cache_store is not None
                assert population.object_path is not None
                assert population.write_token is not None
                return self._admission(population, owner=owner)
            selected = population.cache_store

        all_candidates = tuple(self._stores)
        candidates = (
            all_candidates[all_candidates.index(selected) :]
            if selected is not None
            else all_candidates
        )
        for cache_store in candidates:
            registration = self._registrations[cache_store]
            if selected is None and not registration.admission_enabled:
                continue
            if selected is None:
                claimed = self._reserve_and_claim(cache_store, key, expected_bytes)
                if claimed is None:
                    return self._admit(
                        owner=owner,
                        source_store=source_store,
                        collection_id=collection_id,
                        object_id=object_id,
                        expected_bytes=expected_bytes,
                        eviction_attempted=eviction_attempted,
                    )
                if not claimed:
                    deficit = self._reservation_deficit(cache_store, expected_bytes)
                    if (
                        cache_store not in eviction_attempted
                        and deficit > 0
                        and self._evict_one(cache_store=cache_store, minimum_bytes=deficit)
                    ):
                        return self._admit(
                            owner=owner,
                            source_store=source_store,
                            collection_id=collection_id,
                            object_id=object_id,
                            expected_bytes=expected_bytes,
                            eviction_attempted=eviction_attempted | {cache_store},
                        )
                    continue
            candidate = self._stores[cache_store]
            completed = candidate.find_completed_population(
                source_store=source_store,
                collection_id=collection_id,
                object_id=object_id,
                expected_bytes=expected_bytes,
            )
            if completed is not None:
                receipt = candidate.receipt(completed=completed, stored_sha256=None)
                return RetrievalCacheAdmission(
                    owner=owner,
                    cache_store=cache_store,
                    source_store=source_store,
                    collection_id=collection_id,
                    object_id=object_id,
                    object_path=receipt.object_path,
                    expected_bytes=expected_bytes,
                    write_token=None,
                    admitted_at=format_utc_timestamp(utc_now()),
                    completed=receipt,
                )
            try:
                write = candidate.begin_population(
                    source_store=source_store,
                    collection_id=collection_id,
                    object_id=object_id,
                    expected_bytes=expected_bytes,
                )
            except StorageAdapterRejection as exc:
                if exc.code != "insufficient_storage":
                    raise
                self._release_reservation(cache_store, key, expected_bytes, waiting=True)
                if cache_store not in eviction_attempted and self._evict_one(
                    cache_store=cache_store, minimum_bytes=1
                ):
                    return self._admit(
                        owner=owner,
                        source_store=source_store,
                        collection_id=collection_id,
                        object_id=object_id,
                        expected_bytes=expected_bytes,
                        eviction_attempted=eviction_attempted | {cache_store},
                    )
                selected = None
                continue
            with session_scope(self._session_factory) as session:
                population = session.get(RetrievalCachePopulationRecord, key)
                if population is None or population.cache_store != cache_store:
                    candidate.resumable_object_store(
                        source_store=source_store,
                        collection_id=collection_id,
                        object_id=object_id,
                    ).abort_write(session=write)
                    raise RuntimeError("retrieval cache admission ownership changed")
                population.object_path = write.object_path
                population.write_token = write.write_token
                population.state = "admitted"
                population.updated_at = format_utc_timestamp(utc_now())
                claim = session.get(
                    RetrievalCachePopulationClaimRecord,
                    (owner, *key),
                )
                if claim is None:
                    candidate.resumable_object_store(
                        source_store=source_store,
                        collection_id=collection_id,
                        object_id=object_id,
                    ).abort_write(session=write)
                    raise RuntimeError("retrieval cache admission claim was released")
                return self._admission(population, owner=owner)
        return None

    def release(self, *, owner: str) -> int:
        if not owner.strip():
            raise ValueError("retrieval cache owner must be non-empty")
        with session_scope(self._session_factory) as session:
            session.execute(
                delete(RetrievalCachePopulationClaimRecord).where(
                    RetrievalCachePopulationClaimRecord.owner == owner
                )
            )
        return self.reap_abandoned_populations()

    def is_current(self, *, admission: RetrievalCacheAdmission) -> bool:
        if admission.write_token is None:
            return False
        key = (admission.source_store, admission.collection_id, admission.object_id)
        with session_scope(self._session_factory) as session:
            population = session.get(RetrievalCachePopulationRecord, key)
            claim = session.get(
                RetrievalCachePopulationClaimRecord,
                (admission.owner, *key),
            )
            return (
                population is not None
                and claim is not None
                and population.state in {"admitted", "writing"}
                and self._admission(population, owner=admission.owner) == admission
            )

    def reap_abandoned_populations(self, *, limit: int = 100) -> int:
        if limit < 1:
            return 0
        with session_scope(self._session_factory) as session:
            populations = list(
                session.scalars(
                    select(RetrievalCachePopulationRecord)
                    .where(
                        ~select(RetrievalCachePopulationClaimRecord.owner)
                        .where(
                            RetrievalCachePopulationClaimRecord.source_store
                            == RetrievalCachePopulationRecord.source_store,
                            RetrievalCachePopulationClaimRecord.collection_id
                            == RetrievalCachePopulationRecord.collection_id,
                            RetrievalCachePopulationClaimRecord.object_id
                            == RetrievalCachePopulationRecord.object_id,
                        )
                        .exists()
                    )
                    .order_by(
                        RetrievalCachePopulationRecord.updated_at,
                        RetrievalCachePopulationRecord.source_store,
                        RetrievalCachePopulationRecord.collection_id,
                        RetrievalCachePopulationRecord.object_id,
                    )
                    .limit(limit)
                    .with_for_update(skip_locked=True)
                )
            )
            cleanup = [
                (
                    current.source_store,
                    current.collection_id,
                    current.object_id,
                    current.cache_store,
                    current.object_path,
                    current.write_token,
                    current.expected_bytes,
                )
                for current in populations
            ]
            for current in populations:
                current.state = "abandoning"
                current.updated_at = format_utc_timestamp(utc_now())

        removed = 0
        for (
            source_store,
            collection_id,
            object_id,
            cache_store,
            object_path,
            token,
            size,
        ) in cleanup:
            if cache_store is not None:
                candidate = self._require_store(cache_store)
                completed = candidate.find_completed_population(
                    source_store=source_store,
                    collection_id=collection_id,
                    object_id=object_id,
                    expected_bytes=size,
                )
                if completed is not None:
                    candidate.delete(object_path=completed.object_path, revision=completed.revision)
                elif token is not None and object_path is not None:
                    candidate.resumable_object_store(
                        source_store=source_store,
                        collection_id=collection_id,
                        object_id=object_id,
                    ).abort_write(session=WriteSession(object_path, token, size))
            with session_scope(self._session_factory) as session:
                population = session.scalar(
                    select(RetrievalCachePopulationRecord)
                    .where(
                        RetrievalCachePopulationRecord.source_store == source_store,
                        RetrievalCachePopulationRecord.collection_id == collection_id,
                        RetrievalCachePopulationRecord.object_id == object_id,
                    )
                    .with_for_update()
                )
                if population is None or population.state != "abandoning":
                    continue
                if (
                    session.scalar(
                        select(RetrievalCachePopulationClaimRecord.owner)
                        .where(
                            RetrievalCachePopulationClaimRecord.source_store == source_store,
                            RetrievalCachePopulationClaimRecord.collection_id == collection_id,
                            RetrievalCachePopulationClaimRecord.object_id == object_id,
                        )
                        .limit(1)
                    )
                    is not None
                ):
                    population.state = "waiting" if population.cache_store is None else "admitting"
                    continue
                if cache_store is not None:
                    accounting = self._accounting(session, cache_store)
                    if accounting.reserved_bytes < size:
                        raise RuntimeError("retrieval cache reservation accounting is inconsistent")
                    accounting.reserved_bytes -= size
                    accounting.updated_at = format_utc_timestamp(utc_now())
                session.delete(population)
                removed += 1
        return removed

    def put(
        self,
        *,
        admission: RetrievalCacheAdmission,
        content: Iterable[bytes],
    ) -> RetrievalCacheReceipt:
        candidate = self._require_store(admission.cache_store)
        if admission.completed is not None:
            return admission.completed
        if admission.write_token is None:
            raise RuntimeError("retrieval cache admission has no continuation token")
        with session_scope(self._session_factory) as session:
            population = session.get(
                RetrievalCachePopulationRecord,
                (admission.source_store, admission.collection_id, admission.object_id),
            )
            claim = session.get(
                RetrievalCachePopulationClaimRecord,
                (
                    admission.owner,
                    admission.source_store,
                    admission.collection_id,
                    admission.object_id,
                ),
            )
            if (
                population is None
                or claim is None
                or self._admission(population, owner=admission.owner) != admission
            ):
                raise RuntimeError("retrieval cache admission is no longer current")
            population.state = "writing"
            population.updated_at = format_utc_timestamp(utc_now())
        return candidate.populate(
            session=WriteSession(
                admission.object_path,
                admission.write_token,
                admission.expected_bytes,
            ),
            source_store=admission.source_store,
            collection_id=admission.collection_id,
            object_id=admission.object_id,
            content=content,
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
        digest = __import__("hashlib").sha256()
        read = 0
        for chunk in self.iter_object_range(
            cache_store=cache_store,
            object_path=object_path,
            revision=revision,
            expected_bytes=expected_bytes,
            offset=0,
            size=expected_bytes,
        ):
            digest.update(chunk)
            read += len(chunk)
            yield chunk
        if read != expected_bytes or digest.hexdigest() != expected_sha256:
            raise RuntimeError("retrieval cache object integrity check failed")

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
        return self._require_store(cache_store).iter_object_range(
            object_path=object_path,
            revision=revision,
            expected_bytes=expected_bytes,
            offset=offset,
            size=size,
        )

    def delete(
        self,
        *,
        cache_store: str,
        object_path: str,
        revision: str | None,
    ) -> None:
        self._require_store(cache_store).delete(object_path=object_path, revision=revision)

    def resumable_object_store(
        self,
        *,
        admission: RetrievalCacheAdmission,
    ) -> ArchiveResumableObjectStore:
        candidate = self._require_store(admission.cache_store)
        return candidate.resumable_object_store(
            source_store=admission.source_store,
            collection_id=admission.collection_id,
            object_id=admission.object_id,
        )

    def _reserve_and_claim(
        self,
        cache_store: str,
        key: tuple[str, int, str],
        expected_bytes: int,
    ) -> bool | None:
        with session_scope(self._session_factory) as session:
            population = session.scalar(
                select(RetrievalCachePopulationRecord)
                .where(
                    RetrievalCachePopulationRecord.source_store == key[0],
                    RetrievalCachePopulationRecord.collection_id == key[1],
                    RetrievalCachePopulationRecord.object_id == key[2],
                )
                .with_for_update()
            )
            if population is None:
                return False
            if population.cache_store is not None or population.state != "waiting":
                return None
            accounting = self._accounting(session, cache_store)
            budget = self._registrations[cache_store].admission_budget_bytes
            if budget is not None and (
                accounting.reserved_bytes + accounting.committed_bytes + expected_bytes > budget
            ):
                return False
            accounting.reserved_bytes += expected_bytes
            accounting.updated_at = format_utc_timestamp(utc_now())
            population.cache_store = cache_store
            population.object_path = self._stores[cache_store].object_path(*key)
            population.write_token = None
            population.state = "admitting"
            population.updated_at = format_utc_timestamp(utc_now())
            population.failure = None
            return True

    def _release_reservation(
        self,
        cache_store: str,
        key: tuple[str, int, str],
        expected_bytes: int,
        *,
        waiting: bool,
    ) -> None:
        with session_scope(self._session_factory) as session:
            accounting = self._accounting(session, cache_store)
            accounting.reserved_bytes -= expected_bytes
            accounting.updated_at = format_utc_timestamp(utc_now())
            population = session.get(RetrievalCachePopulationRecord, key)
            if population is not None:
                population.cache_store = None
                population.object_path = None
                population.write_token = None
                population.state = "waiting" if waiting else population.state
                population.updated_at = format_utc_timestamp(utc_now())

    @staticmethod
    def _admission(
        population: RetrievalCachePopulationRecord,
        *,
        owner: str,
    ) -> RetrievalCacheAdmission:
        assert population.cache_store is not None
        assert population.object_path is not None
        assert population.write_token is not None
        return RetrievalCacheAdmission(
            owner=owner,
            cache_store=population.cache_store,
            source_store=population.source_store,
            collection_id=population.collection_id,
            object_id=population.object_id,
            object_path=population.object_path,
            expected_bytes=population.expected_bytes,
            write_token=population.write_token,
            admitted_at=population.initiated_at,
        )

    @staticmethod
    def _claim(
        session: Session,
        *,
        owner: str,
        key: tuple[str, int, str],
    ) -> None:
        if session.get(RetrievalCachePopulationClaimRecord, (owner, *key)) is None:
            session.add(
                RetrievalCachePopulationClaimRecord(
                    owner=owner,
                    source_store=key[0],
                    collection_id=key[1],
                    object_id=key[2],
                    created_at=format_utc_timestamp(utc_now()),
                )
            )

    def _reservation_deficit(self, cache_store: str, expected_bytes: int) -> int:
        budget = self._registrations[cache_store].admission_budget_bytes
        if budget is None:
            return 0
        with session_scope(self._session_factory) as session:
            accounting = self._accounting(session, cache_store)
            return max(
                0,
                accounting.reserved_bytes + accounting.committed_bytes + expected_bytes - budget,
            )

    def _evict_one(self, *, cache_store: str, minimum_bytes: int) -> bool:
        with session_scope(self._session_factory) as session:
            cached = session.scalar(
                select(RetrievalCacheObjectRecord)
                .where(
                    RetrievalCacheObjectRecord.cache_store == cache_store,
                    RetrievalCacheObjectRecord.state == "ready",
                    RetrievalCacheObjectRecord.stored_bytes >= minimum_bytes,
                    ~select(RetrievalCacheLeaseRecord.owner)
                    .where(
                        RetrievalCacheLeaseRecord.source_store
                        == RetrievalCacheObjectRecord.source_store,
                        RetrievalCacheLeaseRecord.collection_id
                        == RetrievalCacheObjectRecord.collection_id,
                        RetrievalCacheLeaseRecord.object_id == RetrievalCacheObjectRecord.object_id,
                    )
                    .exists(),
                )
                .order_by(
                    RetrievalCacheObjectRecord.cached_at,
                    RetrievalCacheObjectRecord.source_store,
                    RetrievalCacheObjectRecord.collection_id,
                    RetrievalCacheObjectRecord.object_id,
                )
                .limit(1)
                .with_for_update(skip_locked=True)
            )
            if cached is None:
                return False
            identity = (
                cached.source_store,
                cached.collection_id,
                cached.object_id,
                cached.object_path,
                cached.revision,
                cached.stored_bytes,
            )
            cached.state = "deleting"
        try:
            self._require_store(cache_store).delete(
                object_path=identity[3],
                revision=identity[4],
            )
        except Exception:
            with session_scope(self._session_factory) as session:
                current = session.get(RetrievalCacheObjectRecord, identity[:3])
                if current is not None and current.state == "deleting":
                    current.state = "delete_pending"
            return False
        with session_scope(self._session_factory) as session:
            current = session.scalar(
                select(RetrievalCacheObjectRecord)
                .where(
                    RetrievalCacheObjectRecord.source_store == identity[0],
                    RetrievalCacheObjectRecord.collection_id == identity[1],
                    RetrievalCacheObjectRecord.object_id == identity[2],
                )
                .with_for_update()
            )
            if current is None:
                return True
            if current.state != "deleting" or current.object_path != identity[3]:
                raise RuntimeError("retrieval cache eviction ownership changed")
            accounting = self._accounting(session, cache_store)
            if accounting.committed_bytes < identity[5]:
                raise RuntimeError("retrieval cache committed accounting is inconsistent")
            accounting.committed_bytes -= identity[5]
            accounting.updated_at = format_utc_timestamp(utc_now())
            session.delete(current)
        return True

    @staticmethod
    def _accounting(session: Session, cache_store: str) -> RetrievalCacheStoreAccountingRecord:
        row = session.scalar(
            select(RetrievalCacheStoreAccountingRecord)
            .where(RetrievalCacheStoreAccountingRecord.cache_store == cache_store)
            .with_for_update()
        )
        if row is None:
            raise RuntimeError(f"retrieval cache accounting is unavailable: {cache_store}")
        return row

    def _ensure_accounting_rows(self) -> None:
        with session_scope(self._session_factory) as session:
            for cache_store in self._stores:
                if session.get(RetrievalCacheStoreAccountingRecord, cache_store) is not None:
                    continue
                try:
                    with session.begin_nested():
                        session.add(
                            RetrievalCacheStoreAccountingRecord(
                                cache_store=cache_store,
                                reserved_bytes=0,
                                committed_bytes=0,
                                updated_at=format_utc_timestamp(utc_now()),
                            )
                        )
                        session.flush()
                except IntegrityError:
                    session.expire_all()

    def _require_store(self, cache_store: str) -> StorageAdapterRetrievalCache:
        try:
            return self._stores[cache_store]
        except KeyError as exc:
            raise RuntimeError(f"retrieval cache store is unavailable: {cache_store}") from exc


def _receipt_identity(record: RetrievalCacheObjectRecord) -> tuple[object, ...]:
    return (
        record.cache_store,
        record.object_path,
        record.revision,
        record.stored_bytes,
        record.stored_sha256,
    )


def _receipt_value(receipt: RetrievalCacheReceipt) -> tuple[object, ...]:
    return (
        receipt.cache_store,
        receipt.object_path,
        receipt.revision,
        receipt.stored_bytes,
        receipt.stored_sha256,
    )


def register_cache_ready(
    session: Session,
    *,
    source_store: str,
    collection_id: int,
    object_id: str,
    receipt: RetrievalCacheReceipt,
) -> None:
    key = (source_store, collection_id, object_id)
    population = session.get(RetrievalCachePopulationRecord, key)
    existing = session.get(RetrievalCacheObjectRecord, key)
    if existing is not None:
        if _receipt_identity(existing) != _receipt_value(receipt):
            raise RuntimeError("retrieval cache ready placement changed")
        if population is not None:
            if population.cache_store != receipt.cache_store:
                raise RuntimeError("retrieval cache store changed after admission")
            accounting = session.scalar(
                select(RetrievalCacheStoreAccountingRecord)
                .where(RetrievalCacheStoreAccountingRecord.cache_store == receipt.cache_store)
                .with_for_update()
            )
            if accounting is None:
                accounting = RetrievalCacheStoreAccountingRecord(
                    cache_store=receipt.cache_store,
                    reserved_bytes=0,
                    committed_bytes=existing.stored_bytes,
                    updated_at=format_utc_timestamp(utc_now()),
                )
                session.add(accounting)
                session.flush()
            accounting.reserved_bytes -= population.expected_bytes
            accounting.updated_at = format_utc_timestamp(utc_now())
            session.delete(population)
        return
    if population is not None and population.expected_bytes != receipt.stored_bytes:
        raise RuntimeError("retrieval cache ready byte identity changed")
    session.add(
        RetrievalCacheObjectRecord(
            source_store=source_store,
            collection_id=collection_id,
            object_id=object_id,
            cache_store=receipt.cache_store,
            object_path=receipt.object_path,
            revision=receipt.revision,
            stored_bytes=receipt.stored_bytes,
            stored_sha256=receipt.stored_sha256,
            cached_at=receipt.cached_at,
            verified_at=receipt.verified_at,
            state="ready",
        )
    )
    accounting = session.scalar(
        select(RetrievalCacheStoreAccountingRecord)
        .where(RetrievalCacheStoreAccountingRecord.cache_store == receipt.cache_store)
        .with_for_update()
    )
    if accounting is None:
        accounting = RetrievalCacheStoreAccountingRecord(
            cache_store=receipt.cache_store,
            reserved_bytes=0,
            committed_bytes=0,
            updated_at=format_utc_timestamp(utc_now()),
        )
        session.add(accounting)
        session.flush()
    accounting.committed_bytes += receipt.stored_bytes
    if population is not None:
        if population.cache_store != receipt.cache_store:
            raise RuntimeError("retrieval cache store changed after admission")
        accounting.reserved_bytes -= population.expected_bytes
        session.delete(population)
    accounting.updated_at = format_utc_timestamp(utc_now())


__all__ = ["SqlAlchemyRetrievalCache", "register_cache_ready"]
