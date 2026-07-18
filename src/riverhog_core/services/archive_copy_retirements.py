from __future__ import annotations

import hashlib
import json
import re
import secrets
from datetime import UTC, datetime, timedelta
from typing import cast

from sqlalchemy import select
from sqlalchemy.orm import Session

from riverhog_core.archive_custody import ARCHIVE_CUSTODY_WARNING
from riverhog_core.archive_store_registry import ArchiveStoreRegistry
from riverhog_core.catalog_db import make_session_factory, session_scope
from riverhog_core.catalog_models import (
    ArchiveCopyJobRecord,
    ArchiveCopyRetirementRecord,
    CollectionArchiveCopyRecord,
    CollectionDeletionRecord,
    CollectionRecord,
    RetrievalJobObjectRecord,
    RetrievalJobRecord,
)
from riverhog_core.domain.errors import (
    BadRequest,
    Conflict,
    InvalidState,
    NotFound,
    ServiceUnavailable,
)
from riverhog_core.ports.archive_store import ArchiveVerificationError
from riverhog_core.runtime_config import RuntimeConfig
from riverhog_core.services.archive_catalog import publish_archive_catalog
from riverhog_core.services.archive_records import (
    archive_copy_aggregates,
    archive_copy_identity,
    archive_copy_is_complete,
)
from riverhog_core.services.archive_reporting import record_archive_usage_snapshot
from riverhog_core.services.collections import _normalize_collection_id_or_raise
from riverhog_core.timestamps import format_utc_timestamp, utc_now

_PLAN_TTL = timedelta(minutes=15)
_CHALLENGE_RE = re.compile(r"^retire-copy-(\d+)-([0-9a-f]{64})$")
_ACTIVE_RETRIEVAL_STATES = {"requested", "ready"}
_ACTIVE_COPY_STATES = {"requested", "waiting", "copying"}
_RETIREMENT_WARNING = (
    f"{ARCHIVE_CUSTODY_WARNING}\n\n"
    "This operation permanently removes one collection archive copy. Riverhog will "
    "proceed only after a different complete copy passes remote verification. Confirm "
    "the exact collection and archive store."
)


class SqlAlchemyArchiveCopyRetirementService:
    def __init__(
        self,
        config: RuntimeConfig,
        archive_stores: ArchiveStoreRegistry,
    ) -> None:
        self._config = config
        self._archive_stores = archive_stores
        self._session_factory = make_session_factory(config.database_url)

    def plan(self, collection_id: str, *, store: str) -> dict[str, object]:
        normalized_id = _normalize_collection_id_or_raise(collection_id)
        normalized_store = self._configured_store(store)
        with session_scope(self._session_factory) as session:
            active = session.get(
                ArchiveCopyRetirementRecord,
                (normalized_id, normalized_store),
            )
            if active is not None:
                return cast(dict[str, object], json.loads(active.plan_json))
            expires = (utc_now() + _PLAN_TTL).replace(microsecond=0)
            plan = _build_plan(
                session,
                config=self._config,
                collection_id=normalized_id,
                store=normalized_store,
                expires_at=expires,
            )
            plan["challenge"] = None if plan["blockers"] else _plan_challenge(plan, expires)
            return plan

    def retire(
        self,
        collection_id: str,
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
                if _CHALLENGE_RE.fullmatch(supplied_challenge) is None:
                    raise NotFound(f"archive copy not found: {normalized_id} in {normalized_store}")
                plan = _absent_plan(normalized_id, normalized_store)
                already_absent = True
            else:
                expires = _challenge_expiry(supplied_challenge)
                if utc_now() > expires:
                    raise Conflict("archive copy retirement plan has expired; request a new plan")
                plan = _build_plan(
                    session,
                    config=self._config,
                    collection_id=normalized_id,
                    store=normalized_store,
                    expires_at=expires,
                )
                expected_challenge = _plan_challenge(plan, expires)
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

        target_store = self._archive_stores.require(normalized_store)
        if already_absent:
            publish_archive_catalog(
                store_name=normalized_store,
                archive_store=target_store,
                session_factory=self._session_factory,
            )
            return _result(plan, status="already_absent", verified_store=None)

        try:
            verified_store = self._verify_retained_copy(
                normalized_id,
                supplied_challenge,
                plan,
            )
            publish_archive_catalog(
                store_name=verified_store,
                archive_store=self._archive_stores.require(verified_store),
                session_factory=self._session_factory,
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
            target_objects = archive_copy_identity(target).objects
        target_store.delete_collection_archive(
            collection_id=normalized_id,
            objects=target_objects,
        )
        publish_archive_catalog(
            store_name=normalized_store,
            archive_store=target_store,
            session_factory=self._session_factory,
            excluded_collection_ids={normalized_id},
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
        collection_id: str,
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
                self._archive_stores.require(store).verify_collection_archive(
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
        collection_id: str,
        store: str,
        challenge: str,
        plan: dict[str, object],
        *,
        verified_store: str,
    ) -> dict[str, object]:
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
            active_retrievals = session.scalars(
                select(RetrievalJobRecord.id)
                .join(RetrievalJobObjectRecord)
                .where(
                    RetrievalJobObjectRecord.collection_id == collection_id,
                    RetrievalJobObjectRecord.source_store == store,
                    RetrievalJobRecord.state.in_(_ACTIVE_RETRIEVAL_STATES),
                )
                .order_by(RetrievalJobRecord.id)
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
                    ArchiveCopyJobRecord.state.in_(_ACTIVE_COPY_STATES),
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
            terminal_retrievals = session.scalars(
                select(RetrievalJobRecord)
                .join(RetrievalJobObjectRecord)
                .where(
                    RetrievalJobObjectRecord.collection_id == collection_id,
                    RetrievalJobObjectRecord.source_store == store,
                    ~RetrievalJobRecord.state.in_(_ACTIVE_RETRIEVAL_STATES),
                )
            ).unique()
            for retrieval in terminal_retrievals:
                session.delete(retrieval)
            session.delete(active)
            session.flush()
            target = session.get(CollectionArchiveCopyRecord, (collection_id, store))
            if target is not None:
                session.delete(target)
                session.flush()
            record_archive_usage_snapshot(session, config=self._config)
        return _result(plan, status="retired", verified_store=verified_store)

    def _clear_active(self, collection_id: str, store: str, challenge: str) -> None:
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
    collection_id: str,
    store: str,
    expires_at: datetime,
) -> dict[str, object]:
    db = session
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
        .join(RetrievalJobObjectRecord)
        .where(
            RetrievalJobObjectRecord.collection_id == collection_id,
            RetrievalJobObjectRecord.source_store == store,
            RetrievalJobRecord.state.in_(_ACTIVE_RETRIEVAL_STATES),
        )
        .order_by(RetrievalJobRecord.id)
    ).all()
    copy_jobs = db.execute(
        select(
            ArchiveCopyJobRecord.source_store,
            ArchiveCopyJobRecord.destination_store,
        )
        .where(
            ArchiveCopyJobRecord.collection_id == collection_id,
            ArchiveCopyJobRecord.state.in_(_ACTIVE_COPY_STATES),
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
    terminal_retrieval_ids = db.scalars(
        select(RetrievalJobRecord.id)
        .join(RetrievalJobObjectRecord)
        .where(
            RetrievalJobObjectRecord.collection_id == collection_id,
            RetrievalJobObjectRecord.source_store == store,
            ~RetrievalJobRecord.state.in_(_ACTIVE_RETRIEVAL_STATES),
        )
        .order_by(RetrievalJobRecord.id)
    ).all()

    blockers: list[str] = []
    if db.get(CollectionDeletionRecord, collection_id) is not None:
        blockers.append(f"collection deletion is active: {collection_id}")
    blockers.extend(f"retrieval is active: {job_id}" for job_id in active_retrievals)
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

    target_objects = [
        {
            "kind": current.kind,
            "object_path": current.object_path,
            "stored_bytes": current.stored_bytes,
        }
        for current in sorted(target.objects, key=lambda item: item.object_order)
    ]
    aggregates = archive_copy_aggregates(session, collection_ids=[collection_id])
    return {
        "status": "blocked" if blockers else "ready",
        "collection_id": collection_id,
        "store": store,
        "warning": _RETIREMENT_WARNING,
        "expires_at": format_utc_timestamp(expires_at),
        "target_copy": {
            "store": target.store,
            "last_verified_at": target.last_verified_at,
            "remote_storage_bytes": aggregates.get((collection_id, target.store), (0, 0))[1],
            "objects": target_objects,
        },
        "retained_copies": [
            {
                "store": copy.store,
                "last_verified_at": copy.last_verified_at,
                "remote_storage_bytes": aggregates.get((collection_id, copy.store), (0, 0))[1],
            }
            for copy in retained
        ],
        "retired_retrieval_jobs": list(terminal_retrieval_ids),
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


def _plan_challenge(plan: dict[str, object], expires_at: datetime) -> str:
    payload = json.dumps(plan, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return f"retire-copy-{int(expires_at.timestamp())}-{hashlib.sha256(payload).hexdigest()}"


def _challenge_expiry(challenge: str) -> datetime:
    match = _CHALLENGE_RE.fullmatch(challenge)
    if match is None:
        raise BadRequest("invalid archive copy retirement challenge")
    return datetime.fromtimestamp(int(match.group(1)), tz=UTC)


def _absent_plan(collection_id: str, store: str) -> dict[str, object]:
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
