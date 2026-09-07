from __future__ import annotations

import json
import secrets
from datetime import datetime
from typing import cast

from riverhog_protocol.errors import (
    BadRequest,
    Conflict,
    InvalidState,
    NotFound,
    ServiceUnavailable,
)
from sqlalchemy import case, func, select
from sqlalchemy.orm import Session
from time_formats import format_utc_timestamp, utc_now

from riverhog_core.archive_safety import ARCHIVE_DATA_LOSS_WARNING
from riverhog_core.archive_store_registry import ArchiveStoreRegistry
from riverhog_core.catalog_db import SessionFactory, make_session_factory, session_scope
from riverhog_core.catalog_models import (
    ArchiveCopyJobRecord,
    ArchiveCopyRetirementRecord,
    CollectionArchiveCopyRecord,
    CollectionDeletionRecord,
    CollectionRecord,
    RetrievalJobObjectProgressRecord,
    RetrievalJobRecord,
    RetrievalPlanFileRecord,
    RetrievalPlanObjectRecord,
    RetrievalPlanPlacementRecord,
    RetrievalPlanRecord,
)
from riverhog_core.ports.archive_store import ArchiveVerificationError
from riverhog_core.runtime_config import RuntimeConfig
from riverhog_core.services.archive_copy_states import ARCHIVE_COPY_BLOCKING_STATES
from riverhog_core.services.archive_records import (
    archive_copy_aggregates,
    archive_copy_identity,
    archive_copy_is_complete,
    archive_copy_owned_identity,
)
from riverhog_core.services.collections import _normalize_collection_id_or_raise
from riverhog_core.services.operation_plans import (
    PLAN_TTL,
    challenge_expiry,
    challenge_has_shape,
    plan_challenge,
)

_CHALLENGE_PREFIX = "retire-copy"
_ACTIVE_RETRIEVAL_STATES = {"requested", "ready"}
_BLOCKER_SAMPLE_LIMIT = 10
_RETRIEVAL_CLEANUP_BATCH = 100
_RETIREMENT_WARNING = (
    f"{ARCHIVE_DATA_LOSS_WARNING}\n\n"
    "This operation permanently removes one collection archive copy. Riverhog will "
    "proceed only after a different complete copy passes remote verification. Confirm "
    "the exact collection and archive store."
)


class SqlAlchemyArchiveCopyRetirementService:
    def __init__(
        self,
        config: RuntimeConfig,
        archive_stores: ArchiveStoreRegistry,
        *,
        session_factory: SessionFactory | None = None,
    ) -> None:
        self._config = config
        self._archive_stores = archive_stores
        self._session_factory = session_factory or make_session_factory(config.database_url)

    def plan(self, collection_id: int, *, store: str) -> dict[str, object]:
        normalized_id = _normalize_collection_id_or_raise(collection_id)
        normalized_store = self._configured_store(store)
        with session_scope(self._session_factory) as session:
            active = session.get(
                ArchiveCopyRetirementRecord,
                (normalized_id, normalized_store),
            )
            if active is not None:
                return cast(dict[str, object], json.loads(active.plan_json))
            expires = (utc_now() + PLAN_TTL).replace(microsecond=0)
            plan = _build_plan(
                session,
                config=self._config,
                collection_id=normalized_id,
                store=normalized_store,
                expires_at=expires,
            )
            plan["challenge"] = (
                None if plan["blockers"] else plan_challenge(_CHALLENGE_PREFIX, plan, expires)
            )
            return plan

    def retire(
        self,
        collection_id: int,
        *,
        store: str,
        challenge: str,
    ) -> dict[str, object]:
        normalized_id = _normalize_collection_id_or_raise(collection_id)
        normalized_store = self._configured_store(store)
        supplied_challenge = challenge.strip()
        if not supplied_challenge:
            raise BadRequest("archive copy retirement challenge is required")

        already_absent = False
        plan: dict[str, object]
        with session_scope(self._session_factory) as session:
            session.scalar(
                select(CollectionRecord.id)
                .where(CollectionRecord.id == normalized_id)
                .with_for_update()
            )
            active = session.get(
                ArchiveCopyRetirementRecord,
                (normalized_id, normalized_store),
            )
            target = session.get(
                CollectionArchiveCopyRecord,
                (normalized_id, normalized_store),
            )
            if active is not None:
                if not secrets.compare_digest(active.challenge, supplied_challenge):
                    raise Conflict(
                        "archive copy retirement challenge does not match active retirement"
                    )
                plan = cast(dict[str, object], json.loads(active.plan_json))
            elif target is None:
                if not challenge_has_shape(supplied_challenge, prefix=_CHALLENGE_PREFIX):
                    raise NotFound(f"archive copy not found: {normalized_id} in {normalized_store}")
                plan = _absent_plan(normalized_id, normalized_store)
                already_absent = True
            else:
                expires = challenge_expiry(
                    supplied_challenge,
                    prefix=_CHALLENGE_PREFIX,
                    operation="archive copy retirement",
                )
                if utc_now() > expires:
                    raise Conflict("archive copy retirement plan has expired; request a new plan")
                plan = _build_plan(
                    session,
                    config=self._config,
                    collection_id=normalized_id,
                    store=normalized_store,
                    expires_at=expires,
                )
                expected_challenge = plan_challenge(_CHALLENGE_PREFIX, plan, expires)
                if not secrets.compare_digest(expected_challenge, supplied_challenge):
                    raise Conflict("archive copy retirement plan changed; request a new plan")
                blockers = cast(list[str], plan["blockers"])
                if blockers:
                    raise Conflict("archive copy retirement is blocked: " + "; ".join(blockers))
                plan["challenge"] = supplied_challenge
                plan["status"] = "retiring"
                session.add(
                    ArchiveCopyRetirementRecord(
                        collection_id=normalized_id,
                        store=normalized_store,
                        challenge=supplied_challenge,
                        plan_json=json.dumps(plan, sort_keys=True, separators=(",", ":")),
                        started_at=format_utc_timestamp(utc_now()),
                    )
                )

        target_store = self._archive_stores.require(normalized_store).store
        if already_absent:
            return _result(plan, status="already_absent", verified_store=None)

        try:
            verified_store = self._verify_retained_copy(
                normalized_id,
                supplied_challenge,
                plan,
            )
        except Exception:
            self._clear_active(normalized_id, normalized_store, supplied_challenge)
            raise

        with session_scope(self._session_factory) as session:
            target = session.get(
                CollectionArchiveCopyRecord,
                (normalized_id, normalized_store),
            )
            if target is None or not archive_copy_is_complete(target):
                raise Conflict("archive copy changed during retirement")
            target_objects = archive_copy_owned_identity(target).objects
        target_store.delete_collection_archive(
            collection_id=normalized_id,
            objects=target_objects,
        )
        self._purge_terminal_retrieval_plans(
            normalized_id,
            normalized_store,
        )
        return self._finish(
            normalized_id,
            normalized_store,
            supplied_challenge,
            plan,
            verified_store=verified_store,
        )

    def _verify_retained_copy(
        self,
        collection_id: int,
        challenge: str,
        plan: dict[str, object],
    ) -> str:
        failures: list[Exception] = []
        candidates = cast(list[dict[str, object]], plan["retained_copies"])
        for candidate in candidates:
            store = str(candidate["store"])
            with session_scope(self._session_factory) as session:
                copy = session.get(CollectionArchiveCopyRecord, (collection_id, store))
                if copy is None or not archive_copy_is_complete(copy):
                    failures.append(
                        ArchiveVerificationError(
                            f"retained archive copy is incomplete: {collection_id} in {store}"
                        )
                    )
                    continue
                identity = archive_copy_identity(copy)
            try:
                self._archive_stores.require(store).store.verify_collection_archive(
                    collection_id=collection_id,
                    archive=identity,
                )
            except Exception as exc:
                failures.append(exc)
                continue

            verified_at = format_utc_timestamp(utc_now())
            with session_scope(self._session_factory) as session:
                active = session.get(
                    ArchiveCopyRetirementRecord,
                    (collection_id, str(plan["store"])),
                )
                if active is None or not secrets.compare_digest(active.challenge, challenge):
                    raise Conflict("archive copy retirement is no longer active")
                copy = session.get(CollectionArchiveCopyRecord, (collection_id, store))
                if copy is None or not archive_copy_is_complete(copy):
                    raise Conflict(
                        "retained archive copy changed during retirement: "
                        f"{collection_id} in {store}"
                    )
                copy.last_verified_at = verified_at
            return store

        if failures and all(isinstance(failure, ArchiveVerificationError) for failure in failures):
            raise Conflict(
                f"no retained archive copy matches its upload record: {collection_id}"
            ) from failures[-1]
        if failures:
            raise ServiceUnavailable(
                f"cannot verify a retained archive copy before retirement: {collection_id}"
            ) from failures[-1]
        raise Conflict(f"collection has no retained archive copy: {collection_id}")

    def _finish(
        self,
        collection_id: int,
        store: str,
        challenge: str,
        plan: dict[str, object],
        *,
        verified_store: str,
    ) -> dict[str, object]:
        now_text = format_utc_timestamp(utc_now())
        with session_scope(self._session_factory) as session:
            active = session.scalar(
                select(ArchiveCopyRetirementRecord)
                .where(
                    ArchiveCopyRetirementRecord.collection_id == collection_id,
                    ArchiveCopyRetirementRecord.store == store,
                )
                .with_for_update()
            )
            if active is None:
                return _result(plan, status="already_absent", verified_store=verified_store)
            if not secrets.compare_digest(active.challenge, challenge):
                raise Conflict("archive copy retirement challenge does not match active retirement")
            active_plans = session.scalars(
                select(RetrievalPlanRecord.id)
                .join(RetrievalPlanObjectRecord)
                .where(
                    RetrievalPlanObjectRecord.collection_id == collection_id,
                    RetrievalPlanObjectRecord.source_store == store,
                    RetrievalPlanRecord.state.in_({"planning", "ready"}),
                    RetrievalPlanRecord.expires_at > now_text,
                )
                .order_by(RetrievalPlanRecord.id)
                .limit(_BLOCKER_SAMPLE_LIMIT + 1)
            ).all()
            if active_plans:
                raise Conflict(
                    "retrieval plan became active during archive copy retirement: "
                    + ", ".join(active_plans)
                )
            active_retrievals = session.scalars(
                select(RetrievalJobRecord.id)
                .join(
                    RetrievalPlanObjectRecord,
                    RetrievalPlanObjectRecord.plan_id == RetrievalJobRecord.plan_id,
                )
                .where(
                    RetrievalPlanObjectRecord.collection_id == collection_id,
                    RetrievalPlanObjectRecord.source_store == store,
                    RetrievalJobRecord.state.in_(_ACTIVE_RETRIEVAL_STATES),
                )
                .order_by(RetrievalJobRecord.id)
                .limit(_BLOCKER_SAMPLE_LIMIT + 1)
            ).all()
            if active_retrievals:
                raise Conflict(
                    "retrieval became active during archive copy retirement: "
                    + ", ".join(active_retrievals)
                )
            copy_jobs = session.execute(
                select(
                    ArchiveCopyJobRecord.source_store,
                    ArchiveCopyJobRecord.destination_store,
                ).where(
                    ArchiveCopyJobRecord.collection_id == collection_id,
                    ArchiveCopyJobRecord.state.in_(ARCHIVE_COPY_BLOCKING_STATES),
                )
            ).all()
            if copy_jobs:
                raise Conflict(
                    "archive copy became active during archive copy retirement: "
                    + ", ".join(
                        f"{source_store} -> {destination_store}"
                        for source_store, destination_store in copy_jobs
                    )
                )
            terminal_plan = session.scalar(
                select(RetrievalPlanRecord.id)
                .join(RetrievalPlanObjectRecord)
                .where(
                    RetrievalPlanObjectRecord.collection_id == collection_id,
                    RetrievalPlanObjectRecord.source_store == store,
                    (
                        ~RetrievalPlanRecord.state.in_({"planning", "ready"})
                        | (RetrievalPlanRecord.expires_at <= now_text)
                    ),
                )
                .order_by(RetrievalPlanRecord.id)
                .limit(1)
            )
            if terminal_plan is not None:
                raise Conflict("terminal retrieval plan cleanup is incomplete; retry retirement")
            session.delete(active)
            session.flush()
            target = session.get(CollectionArchiveCopyRecord, (collection_id, store))
            if target is not None:
                session.delete(target)
                session.flush()
        return _result(plan, status="retired", verified_store=verified_store)

    def _purge_terminal_retrieval_plans(self, collection_id: int, store: str) -> None:
        """Reclaim exact plan authorities in bounded, restartable catalog steps."""

        while True:
            now_text = format_utc_timestamp(utc_now())
            with session_scope(self._session_factory) as session:
                plan_id = session.scalar(
                    select(RetrievalPlanRecord.id)
                    .join(RetrievalPlanObjectRecord)
                    .where(
                        RetrievalPlanObjectRecord.collection_id == collection_id,
                        RetrievalPlanObjectRecord.source_store == store,
                        (
                            ~RetrievalPlanRecord.state.in_({"planning", "ready"})
                            | (RetrievalPlanRecord.expires_at <= now_text)
                        ),
                    )
                    .order_by(RetrievalPlanRecord.id)
                    .limit(1)
                )
                if plan_id is None:
                    return
                active_job = session.scalar(
                    select(RetrievalJobRecord.id)
                    .where(
                        RetrievalJobRecord.plan_id == plan_id,
                        RetrievalJobRecord.state.in_(_ACTIVE_RETRIEVAL_STATES),
                    )
                    .limit(1)
                )
                if active_job is not None:
                    raise Conflict(
                        f"retrieval became active during archive copy retirement: {active_job}"
                    )
                progress = list(
                    session.scalars(
                        select(RetrievalJobObjectProgressRecord)
                        .where(RetrievalJobObjectProgressRecord.plan_id == plan_id)
                        .order_by(
                            RetrievalJobObjectProgressRecord.job_id,
                            RetrievalJobObjectProgressRecord.object_order,
                        )
                        .limit(_RETRIEVAL_CLEANUP_BATCH)
                    )
                )
                if progress:
                    for progress_row in progress:
                        session.delete(progress_row)
                    continue
                placements = list(
                    session.scalars(
                        select(RetrievalPlanPlacementRecord)
                        .where(RetrievalPlanPlacementRecord.plan_id == plan_id)
                        .order_by(
                            RetrievalPlanPlacementRecord.file_order,
                            RetrievalPlanPlacementRecord.sequence,
                        )
                        .limit(_RETRIEVAL_CLEANUP_BATCH)
                    )
                )
                if placements:
                    for placement_row in placements:
                        session.delete(placement_row)
                    continue
                files = list(
                    session.scalars(
                        select(RetrievalPlanFileRecord)
                        .where(RetrievalPlanFileRecord.plan_id == plan_id)
                        .order_by(RetrievalPlanFileRecord.file_order)
                        .limit(_RETRIEVAL_CLEANUP_BATCH)
                    )
                )
                if files:
                    for file_row in files:
                        session.delete(file_row)
                    continue
                objects = list(
                    session.scalars(
                        select(RetrievalPlanObjectRecord)
                        .where(RetrievalPlanObjectRecord.plan_id == plan_id)
                        .order_by(
                            case(
                                (
                                    (RetrievalPlanObjectRecord.collection_id == collection_id)
                                    & (RetrievalPlanObjectRecord.source_store == store),
                                    1,
                                ),
                                else_=0,
                            ),
                            RetrievalPlanObjectRecord.object_order,
                        )
                        .limit(_RETRIEVAL_CLEANUP_BATCH)
                    )
                )
                if objects:
                    for object_row in objects:
                        session.delete(object_row)
                    session.flush()
                    remaining = session.scalar(
                        select(RetrievalPlanObjectRecord.plan_id)
                        .where(RetrievalPlanObjectRecord.plan_id == plan_id)
                        .limit(1)
                    )
                    if remaining is not None:
                        continue
                job = session.scalar(
                    select(RetrievalJobRecord).where(RetrievalJobRecord.plan_id == plan_id)
                )
                if job is not None:
                    session.delete(job)
                    session.flush()
                plan = session.get(RetrievalPlanRecord, plan_id)
                if plan is not None:
                    session.delete(plan)

    def _clear_active(self, collection_id: int, store: str, challenge: str) -> None:
        with session_scope(self._session_factory) as session:
            active = session.get(ArchiveCopyRetirementRecord, (collection_id, store))
            if active is not None and secrets.compare_digest(active.challenge, challenge):
                session.delete(active)

    def _configured_store(self, value: str) -> str:
        try:
            return self._config.archive_store(value).name
        except ValueError as exc:
            raise BadRequest(str(exc)) from exc


def _build_plan(
    session: Session,
    *,
    config: RuntimeConfig,
    collection_id: int,
    store: str,
    expires_at: datetime,
) -> dict[str, object]:
    db = session
    now_text = format_utc_timestamp(utc_now())
    if db.get(CollectionRecord, collection_id) is None:
        raise NotFound(f"collection not found: {collection_id}")
    target = db.get(CollectionArchiveCopyRecord, (collection_id, store))
    if target is None:
        raise NotFound(f"archive copy not found: {collection_id} in {store}")
    if not archive_copy_is_complete(target):
        raise InvalidState(f"archive copy is incomplete: {collection_id} in {store}")

    copies = db.scalars(
        select(CollectionArchiveCopyRecord)
        .where(
            CollectionArchiveCopyRecord.collection_id == collection_id,
            CollectionArchiveCopyRecord.store != store,
        )
        .order_by(CollectionArchiveCopyRecord.store)
    ).all()
    read_rank = {name: index for index, name in enumerate(config.archive_read_order)}
    retained = sorted(
        (copy for copy in copies if archive_copy_is_complete(copy)),
        key=lambda copy: (read_rank.get(copy.store, len(read_rank)), copy.store),
    )
    active_retrievals = db.scalars(
        select(RetrievalJobRecord.id)
        .join(
            RetrievalPlanObjectRecord,
            RetrievalPlanObjectRecord.plan_id == RetrievalJobRecord.plan_id,
        )
        .where(
            RetrievalPlanObjectRecord.collection_id == collection_id,
            RetrievalPlanObjectRecord.source_store == store,
            RetrievalJobRecord.state.in_(_ACTIVE_RETRIEVAL_STATES),
        )
        .order_by(RetrievalJobRecord.id)
        .limit(_BLOCKER_SAMPLE_LIMIT + 1)
    ).all()
    active_plans = db.scalars(
        select(RetrievalPlanRecord.id)
        .join(RetrievalPlanObjectRecord)
        .where(
            RetrievalPlanObjectRecord.collection_id == collection_id,
            RetrievalPlanObjectRecord.source_store == store,
            RetrievalPlanRecord.state.in_({"planning", "ready"}),
            RetrievalPlanRecord.expires_at > now_text,
        )
        .order_by(RetrievalPlanRecord.id)
        .limit(_BLOCKER_SAMPLE_LIMIT + 1)
    ).all()
    copy_jobs = db.execute(
        select(
            ArchiveCopyJobRecord.source_store,
            ArchiveCopyJobRecord.destination_store,
        )
        .where(
            ArchiveCopyJobRecord.collection_id == collection_id,
            ArchiveCopyJobRecord.state.in_(ARCHIVE_COPY_BLOCKING_STATES),
        )
        .order_by(ArchiveCopyJobRecord.destination_store)
    ).all()
    other_retirements = db.scalars(
        select(ArchiveCopyRetirementRecord.store)
        .where(
            ArchiveCopyRetirementRecord.collection_id == collection_id,
            ArchiveCopyRetirementRecord.store != store,
        )
        .order_by(ArchiveCopyRetirementRecord.store)
    ).all()
    terminal_retrieval_count = int(
        db.scalar(
            select(func.count(func.distinct(RetrievalJobRecord.id)))
            .join(
                RetrievalPlanObjectRecord,
                RetrievalPlanObjectRecord.plan_id == RetrievalJobRecord.plan_id,
            )
            .where(
                RetrievalPlanObjectRecord.collection_id == collection_id,
                RetrievalPlanObjectRecord.source_store == store,
                ~RetrievalJobRecord.state.in_(_ACTIVE_RETRIEVAL_STATES),
            )
        )
        or 0
    )

    blockers: list[str] = []
    if db.get(CollectionDeletionRecord, collection_id) is not None:
        blockers.append(f"collection deletion is active: {collection_id}")
    blockers.extend(
        f"retrieval is active: {job_id}" for job_id in active_retrievals[:_BLOCKER_SAMPLE_LIMIT]
    )
    if len(active_retrievals) > _BLOCKER_SAMPLE_LIMIT:
        blockers.append("additional active retrievals exist; list retrievals for details")
    blockers.extend(
        f"retrieval plan is active: {plan_id}" for plan_id in active_plans[:_BLOCKER_SAMPLE_LIMIT]
    )
    if len(active_plans) > _BLOCKER_SAMPLE_LIMIT:
        blockers.append("additional active retrieval plans exist; request a fresh retirement plan")
    blockers.extend(
        f"archive copy is active: {source_store} -> {destination_store}"
        for source_store, destination_store in copy_jobs
    )
    blockers.extend(
        f"archive copy retirement is active: {retirement_store}"
        for retirement_store in other_retirements
    )
    if not retained:
        blockers.append("retirement would remove the collection's last complete archive copy")

    aggregates = archive_copy_aggregates(session, collection_ids=[collection_id])
    target_object_count, target_stored_bytes = aggregates.get(
        (collection_id, target.store),
        (0, 0),
    )
    return {
        "status": "blocked" if blockers else "ready",
        "collection_id": collection_id,
        "store": store,
        "warning": _RETIREMENT_WARNING,
        "expires_at": format_utc_timestamp(expires_at),
        "target_copy": {
            "store": target.store,
            "last_verified_at": target.last_verified_at,
            "remote_storage_bytes": target_stored_bytes,
            "object_count": target_object_count,
        },
        "retained_copies": [
            {
                "store": copy.store,
                "last_verified_at": copy.last_verified_at,
                "remote_storage_bytes": aggregates.get((collection_id, copy.store), (0, 0))[1],
            }
            for copy in retained
        ],
        "retired_retrieval_job_count": terminal_retrieval_count,
        "blockers": blockers,
        "verification_note": (
            "Execution requires a different retained copy to pass current remote "
            "object verification before any selected-store object is deleted."
        ),
        "billing_note": (
            "Provider retention, object versions, minimum-storage duration, and billing "
            "timing can affect realized savings."
        ),
    }


def _absent_plan(collection_id: int, store: str) -> dict[str, object]:
    return {
        "collection_id": collection_id,
        "store": store,
        "target_copy": {"remote_storage_bytes": 0},
    }


def _result(
    plan: dict[str, object],
    *,
    status: str,
    verified_store: str | None,
) -> dict[str, object]:
    target = cast(dict[str, object], plan["target_copy"])
    return {
        "status": status,
        "collection_id": plan["collection_id"],
        "store": plan["store"],
        "remote_storage_bytes": target["remote_storage_bytes"],
        "verified_store": verified_store,
    }
