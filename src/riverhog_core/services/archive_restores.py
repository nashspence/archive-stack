from __future__ import annotations

import json
import logging
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from math import ceil
from typing import cast

from sqlalchemy import asc, desc, func, or_, select
from sqlalchemy.orm import Session

from riverhog_core.catalog_db import make_session_factory, session_scope
from riverhog_core.catalog_models import (
    ArchiveRestoreCollectionRecord,
    ArchiveRestoreRecord,
    CollectionArchiveRecord,
    CollectionFileRecord,
    CollectionRecord,
    FetchCollectionRecord,
    FetchRecord,
)
from riverhog_core.collection_archives import (
    CollectionArchiveExpectedFile,
    iter_verified_collection_archive_file_chunks,
    verify_collection_manifest,
    verify_collection_manifest_proof,
)
from riverhog_core.domain.enums import ArchiveRestoreState, ArchiveState, FetchState
from riverhog_core.domain.errors import BadRequest, InvalidState, NotFound
from riverhog_core.domain.models import (
    ArchiveRestoreCollection,
    ArchiveRestoreListPage,
    ArchiveRestoreNotificationStatus,
    ArchiveRestoreProgress,
    ArchiveRestoreSummary,
    ArchiveStatus,
    CollectionManifestStatus,
)
from riverhog_core.domain.types import CollectionId
from riverhog_core.operator_reminders import operator_reminder_due
from riverhog_core.ports.archive_store import ArchiveRestoreStatus, ArchiveStore
from riverhog_core.ports.hot_store import HotStore
from riverhog_core.proofs import CommandProofVerifier, ProofVerifier
from riverhog_core.runtime_config import RuntimeConfig
from riverhog_core.services.collection_deletions import require_collection_not_deleting
from riverhog_core.webhooks import (
    WebhookConfig,
    build_archive_restore_canceled_payload,
    build_archive_restore_completed_payload,
    build_archive_restore_failed_payload,
    build_archive_restore_ready_payload,
    build_archive_restore_retrying_payload,
    build_archive_restore_started_payload,
    post_webhook,
    utcnow,
)

_LOG = logging.getLogger(__name__)

_ACTIVE_STATES = {
    ArchiveRestoreState.REQUESTED.value,
    ArchiveRestoreState.READY.value,
}
_PUBLIC_STATES = {
    ArchiveRestoreState.REQUESTED.value,
    ArchiveRestoreState.READY.value,
    ArchiveRestoreState.EXPIRED.value,
    ArchiveRestoreState.COMPLETED.value,
    ArchiveRestoreState.FAILED.value,
    ArchiveRestoreState.CANCELED.value,
}
_TERMINAL_STATES = _PUBLIC_STATES - _ACTIVE_STATES
_TERMINAL_FILTERS = {"active", "terminal", "all"}
_SORT_FIELDS = {"created_at", "id", "state", "ready_at", "expires_at"}


@dataclass(frozen=True, slots=True)
class _CollectionArchiveObjects:
    collection_id: str
    archive_object_path: str
    manifest_object_path: str
    proof_object_path: str
    manifest_sha256: str
    proof_sha256: str


class SqlAlchemyArchiveRestoreService:
    def __init__(
        self,
        config: RuntimeConfig,
        archive_store: ArchiveStore,
        hot_store: HotStore | None = None,
        *,
        proof_verifier: ProofVerifier | None = None,
    ) -> None:
        self._config = config
        self._archive_store = archive_store
        self._hot_store = hot_store
        self._proof_verifier = proof_verifier or CommandProofVerifier(config.ots_verify_command)
        self._session_factory = make_session_factory(config.database_url)

    def list(
        self,
        *,
        page: int,
        per_page: int,
        sort: str,
        order: str,
        terminal: str = "all",
        state: str | None = None,
        collection: str | None = None,
    ) -> ArchiveRestoreListPage:
        _validate_list_options(
            page=page,
            per_page=per_page,
            sort=sort,
            order=order,
            terminal=terminal,
            state=state,
        )
        stmt = select(ArchiveRestoreRecord)
        if terminal == "active":
            stmt = stmt.where(ArchiveRestoreRecord.state.in_(_ACTIVE_STATES))
        elif terminal == "terminal":
            stmt = stmt.where(ArchiveRestoreRecord.state.in_(_TERMINAL_STATES))
        if state is not None:
            stmt = stmt.where(ArchiveRestoreRecord.state == state)
        if collection is not None:
            stmt = stmt.join(ArchiveRestoreCollectionRecord).where(
                ArchiveRestoreCollectionRecord.collection_id == collection
            )
        sort_expr = {
            "created_at": ArchiveRestoreRecord.created_at,
            "id": ArchiveRestoreRecord.restore_id,
            "state": ArchiveRestoreRecord.state,
            "ready_at": ArchiveRestoreRecord.ready_at,
            "expires_at": ArchiveRestoreRecord.expires_at,
        }[sort]
        direction = desc if order == "desc" else asc
        order_by = [direction(sort_expr)]
        if sort != "id":
            order_by.append(asc(ArchiveRestoreRecord.restore_id))

        with session_scope(self._session_factory) as session:
            total = int(session.scalar(select(func.count()).select_from(stmt.subquery())) or 0)
            records = session.scalars(
                stmt.order_by(*order_by).offset((page - 1) * per_page).limit(per_page)
            ).all()
            return ArchiveRestoreListPage(
                page=page,
                per_page=per_page,
                total=total,
                pages=ceil(total / per_page) if total else 0,
                sort=sort,
                order=order,
                terminal=terminal,
                state=state,
                collection=collection,
                restores=[_restore_summary(session, record, self._config) for record in records],
            )

    def get(self, restore_id: str) -> ArchiveRestoreSummary:
        with session_scope(self._session_factory) as session:
            return _restore_summary(session, _require_restore(session, restore_id), self._config)

    def create_or_resume_for_collection(self, collection_id: str) -> ArchiveRestoreSummary:
        with session_scope(self._session_factory) as session:
            require_collection_not_deleting(session, collection_id)
            collection = _require_collection(session, collection_id)
            _require_collection_archive_uploaded(collection)
            active = _active_restore_for_collection(session, collection_id)
            if active is None:
                record = _create_restore(
                    session,
                    config=self._config,
                    collection=collection,
                )
            else:
                record = active
            restore_id = record.restore_id
        try:
            self._process_one(restore_id)
        except Exception as exc:
            self._record_processing_failure(restore_id, exc)
        return self.get(restore_id)

    def list_for_fetch(
        self,
        fetch_id: str,
        *,
        page: int,
        per_page: int,
        sort: str,
        order: str,
        state: str | None = None,
    ) -> ArchiveRestoreListPage:
        _validate_list_options(
            page=page,
            per_page=per_page,
            sort=sort,
            order=order,
            terminal="all",
            state=state,
        )
        with session_scope(self._session_factory) as session:
            _require_fetch(session, fetch_id)
            records = _records_for_fetch(session, fetch_id=fetch_id, state=state)
            records = _sort_records(records, sort=sort, order=order)
            total = len(records)
            start = (page - 1) * per_page
            return ArchiveRestoreListPage(
                page=page,
                per_page=per_page,
                total=total,
                pages=ceil(total / per_page) if total else 0,
                sort=sort,
                order=order,
                terminal="all",
                state=state,
                collection=None,
                restores=[
                    _restore_summary(session, record, self._config)
                    for record in records[start : start + per_page]
                ],
            )

    def create_or_resume_for_fetch(self, fetch_id: str) -> ArchiveRestoreListPage:
        with session_scope(self._session_factory) as session:
            fetch = _require_fetch(session, fetch_id)
            files = _fetch_files(session, fetch_id)
            missing_collection_ids = {file.collection_id for file in files if not file.hot}
            for collection_id in sorted(missing_collection_ids):
                require_collection_not_deleting(session, collection_id)
            if fetch.fetch_state == FetchState.QUEUED_ARCHIVE.value:
                fetch.fetch_state = FetchState.RESTORING_ARCHIVE.value

        for collection_id in sorted(missing_collection_ids):
            self.create_or_resume_for_collection(collection_id)

        with session_scope(self._session_factory) as session:
            _sync_fetch_states(session, hot_store=self._hot_store)
        return self.list_for_fetch(
            fetch_id,
            page=1,
            per_page=100,
            sort="created_at",
            order="desc",
        )

    def cancel_for_fetch(self, fetch_id: str) -> ArchiveRestoreListPage:
        with session_scope(self._session_factory) as session:
            fetch = _require_fetch(session, fetch_id)
            restore_ids = [
                record.restore_id
                for record in _records_for_fetch(session, fetch_id=fetch_id, state=None)
                if record.state in _ACTIVE_STATES
            ]
            if fetch.fetch_state in {
                FetchState.QUEUED_ARCHIVE.value,
                FetchState.RESTORING_ARCHIVE.value,
            }:
                fetch.fetch_state = FetchState.DRAFT.value
        for restore_id in restore_ids:
            self.cancel(restore_id)
        return self.list_for_fetch(
            fetch_id,
            page=1,
            per_page=100,
            sort="created_at",
            order="desc",
        )

    def cancel(self, restore_id: str) -> ArchiveRestoreSummary:
        current = utcnow()
        with session_scope(self._session_factory) as session:
            record = _require_restore(session, restore_id)
            if record.state == ArchiveRestoreState.CANCELED.value:
                _notify_canceled(session, record, self._config, current)
                return _restore_summary(session, record, self._config)
            if record.state not in _ACTIVE_STATES:
                raise InvalidState("archive restore is not active and cannot be canceled")
            _cleanup_restore(session, record, self._archive_store)
            record.state = ArchiveRestoreState.CANCELED.value
            record.canceled_at = _isoformat_z(current)
            record.next_poll_at = None
            record.started_notification_next_attempt_at = None
            record.completed_notification_next_attempt_at = None
            record.latest_message = "Archive restore was canceled."
            _notify_canceled(session, record, self._config, current)
            return _restore_summary(session, record, self._config)

    def process_due_restores(self, *, limit: int = 100) -> int:
        if limit < 1:
            return 0
        current_text = _isoformat_z(utcnow())
        with session_scope(self._session_factory) as session:
            due_ids = session.scalars(
                select(ArchiveRestoreRecord.restore_id)
                .where(
                    or_(
                        (
                            (ArchiveRestoreRecord.state == ArchiveRestoreState.REQUESTED.value)
                            & (
                                ArchiveRestoreRecord.next_poll_at.is_(None)
                                | (ArchiveRestoreRecord.next_poll_at <= current_text)
                                | (ArchiveRestoreRecord.ready_at <= current_text)
                            )
                        ),
                        (
                            (ArchiveRestoreRecord.state == ArchiveRestoreState.REQUESTED.value)
                            & ArchiveRestoreRecord.started_notification_next_attempt_at.is_not(None)
                            & (
                                ArchiveRestoreRecord.started_notification_next_attempt_at
                                <= current_text
                            )
                        ),
                        (
                            (ArchiveRestoreRecord.state == ArchiveRestoreState.READY.value)
                            & (ArchiveRestoreRecord.materialization_state != "completed")
                            & (
                                ArchiveRestoreRecord.next_poll_at.is_(None)
                                | (ArchiveRestoreRecord.next_poll_at <= current_text)
                            )
                        ),
                        (
                            (ArchiveRestoreRecord.state == ArchiveRestoreState.READY.value)
                            & ArchiveRestoreRecord.expires_at.is_not(None)
                            & (ArchiveRestoreRecord.expires_at <= current_text)
                        ),
                        (
                            (ArchiveRestoreRecord.state == ArchiveRestoreState.COMPLETED.value)
                            & ArchiveRestoreRecord.completed_notification_next_attempt_at.is_not(
                                None
                            )
                            & (
                                ArchiveRestoreRecord.completed_notification_next_attempt_at
                                <= current_text
                            )
                        ),
                        (
                            (ArchiveRestoreRecord.state == ArchiveRestoreState.CANCELED.value)
                            & ArchiveRestoreRecord.canceled_notification_next_attempt_at.is_not(
                                None
                            )
                            & (
                                ArchiveRestoreRecord.canceled_notification_next_attempt_at
                                <= current_text
                            )
                        ),
                    )
                )
                .order_by(ArchiveRestoreRecord.created_at, ArchiveRestoreRecord.restore_id)
                .limit(limit)
            ).all()
        for restore_id in due_ids:
            try:
                self._process_one(restore_id)
            except Exception as exc:
                self._record_processing_failure(restore_id, exc)
        return len(due_ids)

    def repair_missing_fetch_hot_files(self, *, limit: int = 100) -> int:
        if limit < 1 or self._hot_store is None:
            return 0
        collections_to_restore: set[str] = set()
        missing_count = 0
        with session_scope(self._session_factory) as session:
            fetches = session.scalars(
                select(FetchRecord)
                .where(FetchRecord.fetch_state != FetchState.DRAFT.value)
                .order_by(FetchRecord.fetch_order)
            ).all()
            for fetch in fetches:
                if missing_count >= limit:
                    break
                selected = _fetch_files(session, fetch.fetch_id)
                listed: dict[str, dict[str, int]] = {}
                fetch_missing = False
                for file in selected:
                    if missing_count >= limit:
                        break
                    if _hot_file_available_for_audit(
                        self._hot_store,
                        file,
                        selected_count=len(selected),
                        listed_hot_files=listed,
                    ):
                        file.hot = True
                        continue
                    file.hot = False
                    fetch_missing = True
                    missing_count += 1
                    collections_to_restore.add(file.collection_id)
                if fetch_missing and fetch.fetch_state == FetchState.DONE.value:
                    fetch.fetch_state = FetchState.QUEUED_ARCHIVE.value
                elif not fetch_missing and selected:
                    fetch.fetch_state = FetchState.DONE.value

        for collection_id in sorted(collections_to_restore):
            try:
                self.create_or_resume_for_collection(collection_id)
            except Exception:
                _LOG.exception("automatic archive restore failed: collection=%s", collection_id)
        if missing_count:
            with session_scope(self._session_factory) as session:
                _sync_fetch_states(session, hot_store=self._hot_store)
        return missing_count

    def _process_one(self, restore_id: str) -> None:
        current = utcnow()
        current_text = _isoformat_z(current)
        with session_scope(self._session_factory) as session:
            record = session.get(ArchiveRestoreRecord, restore_id)
            if record is None:
                return
            if record.state == ArchiveRestoreState.COMPLETED.value:
                _notify_completed(session, record, self._config, current)
                return
            if record.state == ArchiveRestoreState.CANCELED.value:
                _notify_canceled(session, record, self._config, current)
                return
            if record.state == ArchiveRestoreState.REQUESTED.value:
                _request_restore(
                    session,
                    record,
                    archive_store=self._archive_store,
                    config=self._config,
                    current=current,
                )
                _notify_started(session, record, self._config, current)
                status = _poll_restore(
                    session,
                    record,
                    archive_store=self._archive_store,
                    current=current,
                )
                if status.state == "ready":
                    record.state = ArchiveRestoreState.READY.value
                    record.ready_at = status.ready_at or current_text
                    record.expires_at = status.expires_at or _isoformat_z(
                        current + self._config.archive_restore_ready_ttl
                    )
                    record.next_poll_at = None
                    record.latest_message = (
                        "Archive is ready; Riverhog is materializing the collection."
                    )
                    _notify_ready(session, record, self._config, current)
                elif status.state == "expired":
                    _expire_restore(session, record, self._archive_store)
                    return
                else:
                    record.next_poll_at = _isoformat_z(
                        current + self._config.archive_restore_sweep_interval
                    )
                    record.latest_message = status.message or (
                        "Archive retrieval is in progress; Riverhog will poll again."
                    )
                    return
            if record.state != ArchiveRestoreState.READY.value:
                return
            if record.expires_at is not None and record.expires_at <= current_text:
                _expire_restore(session, record, self._archive_store)
                return
            _materialize_restore(
                session,
                record,
                archive_store=self._archive_store,
                hot_store=self._hot_store,
                proof_verifier=self._proof_verifier,
                config=self._config,
                current=current,
            )

    def _record_processing_failure(self, restore_id: str, exc: Exception) -> None:
        current = utcnow()
        current_text = _isoformat_z(current)
        retryable = _failure_is_retryable(exc)
        error = _error_text(exc)
        next_retry_at = (
            _isoformat_z(current + self._config.archive_restore_sweep_interval)
            if retryable
            else None
        )
        with session_scope(self._session_factory) as session:
            record = session.get(ArchiveRestoreRecord, restore_id)
            if record is None or record.state in {
                ArchiveRestoreState.COMPLETED.value,
                ArchiveRestoreState.CANCELED.value,
            }:
                return
            record.failure_count = int(record.failure_count or 0) + 1
            record.last_failure_at = current_text
            record.last_failure = error
            notify = False
            if retryable:
                record.next_poll_at = next_retry_at
                record.latest_message = f"Archive restore will retry: {error}"
                if _failure_notification_due(
                    record.last_failure_notification_at,
                    current=current,
                    config=self._config,
                ):
                    record.last_failure_notification_at = current_text
                    notify = True
            else:
                record.state = ArchiveRestoreState.FAILED.value
                record.next_poll_at = None
                record.archive_verification_state = _failed_progress_state(
                    record.archive_verification_state
                )
                record.extraction_state = _failed_progress_state(record.extraction_state)
                record.materialization_state = _failed_progress_state(record.materialization_state)
                record.latest_message = f"Archive restore failed: {error}"
                record.last_failure_notification_at = current_text
                notify = True
            if notify:
                _notify_failure(
                    session,
                    record,
                    self._config,
                    current,
                    retryable=retryable,
                    error=error,
                    next_retry_at=next_retry_at,
                )


def _validate_list_options(
    *,
    page: int,
    per_page: int,
    sort: str,
    order: str,
    terminal: str,
    state: str | None,
) -> None:
    if page < 1:
        raise BadRequest("page must be greater than or equal to 1")
    if per_page < 1 or per_page > 100:
        raise BadRequest("per_page must be between 1 and 100")
    if sort not in _SORT_FIELDS:
        raise BadRequest(f"sort must be one of {', '.join(sorted(_SORT_FIELDS))}")
    if order not in {"asc", "desc"}:
        raise BadRequest("order must be asc or desc")
    if terminal not in _TERMINAL_FILTERS:
        raise BadRequest("terminal must be active, terminal, or all")
    if state is not None and state not in _PUBLIC_STATES:
        raise BadRequest(f"state must be one of {', '.join(sorted(_PUBLIC_STATES))}")


def _require_restore(session: Session, restore_id: str) -> ArchiveRestoreRecord:
    record = session.get(ArchiveRestoreRecord, restore_id)
    if record is None:
        raise NotFound(f"archive restore not found: {restore_id}")
    return record


def _require_fetch(session: Session, fetch_id: str) -> FetchRecord:
    record = session.get(FetchRecord, fetch_id)
    if record is None:
        raise NotFound(f"fetch not found: {fetch_id}")
    return record


def _require_collection(session: Session, collection_id: str) -> CollectionRecord:
    collection = cast(CollectionRecord | None, session.get(CollectionRecord, collection_id))
    if collection is None:
        raise NotFound(f"collection not found: {collection_id}")
    return collection


def _require_collection_archive_uploaded(collection: CollectionRecord) -> None:
    archive = collection.archive
    if archive is None or ArchiveState(archive.state) != ArchiveState.UPLOADED:
        raise InvalidState(
            f"collection archive is not uploaded and cannot be restored: {collection.id}"
        )


def _require_archive_objects(collection: CollectionRecord) -> _CollectionArchiveObjects:
    archive = collection.archive
    if archive is None or not archive.object_path:
        raise InvalidState(f"collection archive object is missing: {collection.id}")
    if not archive.manifest_object_path or not archive.manifest_sha256:
        raise InvalidState(f"collection archive manifest is missing: {collection.id}")
    if not archive.ots_object_path or not archive.ots_sha256:
        raise InvalidState(f"collection archive proof is missing: {collection.id}")
    return _CollectionArchiveObjects(
        collection_id=collection.id,
        archive_object_path=archive.object_path,
        manifest_object_path=archive.manifest_object_path,
        proof_object_path=archive.ots_object_path,
        manifest_sha256=archive.manifest_sha256,
        proof_sha256=archive.ots_sha256,
    )


def _restore_collections(session: Session, record: ArchiveRestoreRecord) -> list[CollectionRecord]:
    rows = session.scalars(
        select(ArchiveRestoreCollectionRecord)
        .where(ArchiveRestoreCollectionRecord.restore_id == record.restore_id)
        .order_by(ArchiveRestoreCollectionRecord.collection_order)
    ).all()
    return [_require_collection(session, row.collection_id) for row in rows]


def _fetch_collection_ids(session: Session, fetch_id: str) -> list[str]:
    return list(
        session.scalars(
            select(FetchCollectionRecord.collection_id)
            .where(FetchCollectionRecord.fetch_id == fetch_id)
            .order_by(
                FetchCollectionRecord.collection_order,
                FetchCollectionRecord.collection_id,
            )
        ).all()
    )


def _fetch_files(session: Session, fetch_id: str) -> list[CollectionFileRecord]:
    collection_ids = _fetch_collection_ids(session, fetch_id)
    if not collection_ids:
        return []
    return list(
        session.scalars(
            select(CollectionFileRecord)
            .where(CollectionFileRecord.collection_id.in_(collection_ids))
            .order_by(CollectionFileRecord.collection_id, CollectionFileRecord.path)
        ).all()
    )


def _records_for_fetch(
    session: Session,
    *,
    fetch_id: str,
    state: str | None,
) -> list[ArchiveRestoreRecord]:
    collection_ids = _fetch_collection_ids(session, fetch_id)
    if not collection_ids:
        return []
    stmt = (
        select(ArchiveRestoreRecord)
        .join(ArchiveRestoreCollectionRecord)
        .where(ArchiveRestoreCollectionRecord.collection_id.in_(collection_ids))
    )
    if state is not None:
        stmt = stmt.where(ArchiveRestoreRecord.state == state)
    return list(session.scalars(stmt).unique().all())


def _sort_records(
    records: list[ArchiveRestoreRecord], *, sort: str, order: str
) -> list[ArchiveRestoreRecord]:
    def value(record: ArchiveRestoreRecord) -> str:
        return str(
            {
                "created_at": record.created_at,
                "id": record.restore_id,
                "state": record.state,
                "ready_at": record.ready_at or "",
                "expires_at": record.expires_at or "",
            }[sort]
        )

    return sorted(
        records,
        key=lambda record: (value(record), record.restore_id),
        reverse=order == "desc",
    )


def _active_restore_for_collection(
    session: Session, collection_id: str
) -> ArchiveRestoreRecord | None:
    return session.scalar(
        select(ArchiveRestoreRecord)
        .join(ArchiveRestoreCollectionRecord)
        .where(
            ArchiveRestoreCollectionRecord.collection_id == collection_id,
            ArchiveRestoreRecord.state.in_(_ACTIVE_STATES),
        )
        .order_by(ArchiveRestoreRecord.created_at.desc())
        .limit(1)
    )


def _create_restore(
    session: Session,
    *,
    config: RuntimeConfig,
    collection: CollectionRecord,
) -> ArchiveRestoreRecord:
    existing_ids = session.scalars(select(ArchiveRestoreRecord.restore_id)).all()
    restore_id = _generated_restore_id(collection.id, existing_ids=existing_ids)
    record = ArchiveRestoreRecord(
        restore_id=restore_id,
        state=ArchiveRestoreState.REQUESTED.value,
        created_at=_isoformat_z(utcnow()),
        requested_at=None,
        ready_at=None,
        next_poll_at=None,
        expires_at=None,
        completed_at=None,
        canceled_at=None,
        latest_message="Archive restore queued for materialization.",
        retrieval_tier=config.archive_restore_retrieval_tier,
        hold_days=_restore_hold_days(config),
        warnings_json=json.dumps(list(_build_warnings(config))),
        failure_count=0,
        last_failure_at=None,
        last_failure=None,
        last_failure_notification_at=None,
    )
    session.add(record)
    session.flush()
    session.add(
        ArchiveRestoreCollectionRecord(
            restore_id=restore_id,
            collection_id=collection.id,
            collection_order=0,
        )
    )
    session.flush()
    return record


def _request_restore(
    session: Session,
    record: ArchiveRestoreRecord,
    *,
    archive_store: ArchiveStore,
    config: RuntimeConfig,
    current: datetime,
) -> None:
    if record.requested_at is not None:
        return
    collections = _restore_collections(session, record)
    if not collections:
        raise InvalidState("archive restore has no collections")
    requested_at = _isoformat_z(current)
    estimated_ready_at = _isoformat_z(current + config.archive_restore_latency)
    statuses = [
        archive_store.request_collection_archive_restore(
            collection_id=archive.collection_id,
            object_path=archive.archive_object_path,
            retrieval_tier=record.retrieval_tier,
            hold_days=record.hold_days,
            requested_at=requested_at,
            estimated_ready_at=estimated_ready_at,
            manifest_object_path=archive.manifest_object_path,
            proof_object_path=archive.proof_object_path,
        )
        for archive in (_require_archive_objects(collection) for collection in collections)
    ]
    record.requested_at = requested_at
    record.ready_at = (
        _max_timestamp(status.ready_at for status in statuses if status.ready_at is not None)
        or estimated_ready_at
    )
    record.expires_at = _min_timestamp(
        status.expires_at for status in statuses if status.expires_at is not None
    )
    record.next_poll_at = _isoformat_z(current + config.archive_restore_sweep_interval)
    record.latest_message = (
        "Archive retrieval requested; Riverhog will materialize the collection when ready."
    )


def _poll_restore(
    session: Session,
    record: ArchiveRestoreRecord,
    *,
    archive_store: ArchiveStore,
    current: datetime,
) -> ArchiveRestoreStatus:
    collections = _restore_collections(session, record)
    if not collections:
        raise InvalidState("archive restore has no collections")
    statuses = [
        archive_store.get_collection_archive_restore_status(
            collection_id=archive.collection_id,
            object_path=archive.archive_object_path,
            requested_at=record.requested_at or _isoformat_z(current),
            estimated_ready_at=record.ready_at,
            estimated_expires_at=record.expires_at,
            manifest_object_path=archive.manifest_object_path,
            proof_object_path=archive.proof_object_path,
        )
        for archive in (_require_archive_objects(collection) for collection in collections)
    ]
    if any(status.state == "expired" for status in statuses):
        return ArchiveRestoreStatus(state="expired")
    if statuses and all(status.state == "ready" for status in statuses):
        return ArchiveRestoreStatus(
            state="ready",
            ready_at=_max_timestamp(
                status.ready_at for status in statuses if status.ready_at is not None
            ),
            expires_at=_min_timestamp(
                status.expires_at for status in statuses if status.expires_at is not None
            ),
        )
    return ArchiveRestoreStatus(
        state="requested",
        message="Archive retrieval is in progress; Riverhog will poll again.",
    )


def _materialize_restore(
    session: Session,
    record: ArchiveRestoreRecord,
    *,
    archive_store: ArchiveStore,
    hot_store: HotStore | None,
    proof_verifier: ProofVerifier,
    config: RuntimeConfig,
    current: datetime,
) -> None:
    if hot_store is None:
        raise InvalidState("archive restore service has no hot store")
    for collection in _restore_collections(session, record):
        expected_files = _expected_files(session, collection.id)
        if not expected_files:
            continue
        archive = _require_archive_objects(collection)
        record.archive_verification_state = "in_progress"
        record.extraction_state = "in_progress"
        record.materialization_state = "in_progress"
        session.flush()
        manifest_bytes = archive_store.read_restored_collection_manifest(
            collection_id=archive.collection_id,
            object_path=archive.manifest_object_path,
        )
        verify_collection_manifest(
            manifest_bytes=manifest_bytes,
            expected_sha256=archive.manifest_sha256,
            collection_id=archive.collection_id,
            files=expected_files,
        )
        proof_bytes = archive_store.read_restored_collection_manifest_proof(
            collection_id=archive.collection_id,
            object_path=archive.proof_object_path,
        )
        verify_collection_manifest_proof(
            proof_bytes=proof_bytes,
            expected_sha256=archive.proof_sha256,
            manifest_bytes=manifest_bytes,
            verifier=proof_verifier,
        )
        record.archive_verification_state = "completed"
        materialized: set[str] = set()
        chunks = archive_store.iter_restored_collection_archive(
            collection_id=archive.collection_id,
            object_path=archive.archive_object_path,
        )
        for path, content, content_length in iter_verified_collection_archive_file_chunks(
            chunks,
            files=expected_files,
        ):
            hot_store.put_collection_file_stream(
                collection.id,
                path,
                content,
                content_length=content_length,
            )
            row = session.get(
                CollectionFileRecord,
                {"collection_id": collection.id, "path": path},
            )
            if row is not None:
                row.hot = True
            materialized.add(path)
        expected_paths = {file.path for file in expected_files}
        if materialized != expected_paths:
            raise ValueError(
                f"collection archive missing member: {sorted(expected_paths - materialized)[0]}"
            )
    record.extraction_state = "completed"
    record.materialization_state = "completed"
    _cleanup_restore(session, record, archive_store)
    record.state = ArchiveRestoreState.COMPLETED.value
    record.completed_at = _isoformat_z(current)
    record.expires_at = record.completed_at
    record.next_poll_at = None
    record.latest_message = "The collection was verified and materialized to hot storage."
    _sync_fetch_states(session, hot_store=hot_store)
    _notify_completed(session, record, config, current)


def _expire_restore(
    session: Session,
    record: ArchiveRestoreRecord,
    archive_store: ArchiveStore,
) -> None:
    _cleanup_restore(session, record, archive_store)
    record.state = ArchiveRestoreState.EXPIRED.value
    record.next_poll_at = None
    record.latest_message = "Temporary archive retrieval expired; start a new restore."


def _cleanup_restore(
    session: Session,
    record: ArchiveRestoreRecord,
    archive_store: ArchiveStore,
) -> None:
    for collection in _restore_collections(session, record):
        archive = _require_archive_objects(collection)
        archive_store.cleanup_collection_archive_restore(
            collection_id=collection.id,
            object_path=archive.archive_object_path,
            manifest_object_path=archive.manifest_object_path,
            proof_object_path=archive.proof_object_path,
        )


def _expected_files(
    session: Session, collection_id: str
) -> tuple[CollectionArchiveExpectedFile, ...]:
    rows = session.scalars(
        select(CollectionFileRecord)
        .where(CollectionFileRecord.collection_id == collection_id)
        .order_by(CollectionFileRecord.path)
    ).all()
    return tuple(
        CollectionArchiveExpectedFile(path=row.path, bytes=row.bytes, sha256=row.sha256)
        for row in rows
    )


def _hot_file_available_for_audit(
    hot_store: HotStore,
    file: CollectionFileRecord,
    *,
    selected_count: int,
    listed_hot_files: dict[str, dict[str, int]],
) -> bool:
    if selected_count <= 1:
        return _hot_file_available(hot_store, file)
    listing = listed_hot_files.get(file.collection_id)
    if listing is None:
        try:
            listing = {
                path: int(byte_count)
                for path, byte_count in hot_store.list_collection_files(file.collection_id)
            }
        except Exception:
            return _hot_file_available(hot_store, file)
        listed_hot_files[file.collection_id] = listing
    return listing.get(file.path) == int(file.bytes)


def _hot_file_available(hot_store: HotStore, file: CollectionFileRecord) -> bool:
    try:
        stat = hot_store.stat_collection_file(file.collection_id, file.path)
    except FileNotFoundError:
        return False
    if stat is None or int(stat.bytes) != int(file.bytes):
        return False
    return stat.sha256 is None or stat.sha256 == file.sha256


def _sync_fetch_states(session: Session, *, hot_store: HotStore | None) -> None:
    if hot_store is None:
        return
    fetches = session.scalars(
        select(FetchRecord)
        .where(FetchRecord.fetch_state != FetchState.DRAFT.value)
        .order_by(FetchRecord.fetch_order)
    ).all()
    for fetch in fetches:
        files = _fetch_files(session, fetch.fetch_id)
        fetch.fetch_state = (
            FetchState.DONE.value
            if files and all(_hot_file_available(hot_store, file) for file in files)
            else FetchState.RESTORING_ARCHIVE.value
        )


def _restore_summary(
    session: Session,
    record: ArchiveRestoreRecord,
    config: RuntimeConfig,
) -> ArchiveRestoreSummary:
    collections = tuple(
        ArchiveRestoreCollection(
            id=CollectionId(collection.id),
            archive=_archive_status(collection.archive),
            collection_manifest=_manifest_status(collection.archive),
            stored_bytes=int(collection.archive.stored_bytes or 0)
            if collection.archive is not None
            else 0,
        )
        for collection in _restore_collections(session, record)
    )
    return ArchiveRestoreSummary(
        id=record.restore_id,
        state=ArchiveRestoreState(record.state),
        created_at=record.created_at,
        requested_at=record.requested_at,
        ready_at=record.ready_at,
        expires_at=record.expires_at,
        completed_at=record.completed_at,
        canceled_at=record.canceled_at,
        latest_message=record.latest_message,
        warnings=tuple(str(item) for item in json.loads(record.warnings_json)),
        notification=ArchiveRestoreNotificationStatus(
            webhook_configured=bool(config.operator_webhook_url),
            failure_count=int(record.failure_count or 0),
            last_failure_at=record.last_failure_at,
            last_failure=record.last_failure,
        ),
        progress=ArchiveRestoreProgress(
            archive_verification=record.archive_verification_state or "pending",
            extraction=record.extraction_state or "pending",
            materialization=record.materialization_state or "pending",
        ),
        collections=collections,
    )


def _archive_status(archive: CollectionArchiveRecord | None) -> ArchiveStatus:
    if archive is None:
        return ArchiveStatus()
    return ArchiveStatus(
        state=ArchiveState(archive.state),
        object_path=archive.object_path,
        stored_bytes=archive.stored_bytes,
        backend=archive.backend,
        storage_class=archive.storage_class,
        last_uploaded_at=archive.last_uploaded_at,
        last_verified_at=archive.last_verified_at,
        failure=archive.failure,
    )


def _manifest_status(
    archive: CollectionArchiveRecord | None,
) -> CollectionManifestStatus | None:
    if archive is None:
        return None
    ots_state = "uploaded" if archive.ots_object_path else "pending"
    if ArchiveState(archive.state) == ArchiveState.FAILED:
        ots_state = "failed"
    return CollectionManifestStatus(
        object_path=archive.manifest_object_path,
        sha256=archive.manifest_sha256,
        ots_object_path=archive.ots_object_path,
        ots_state=ots_state,
        ots_sha256=archive.ots_sha256,
    )


def _build_warnings(config: RuntimeConfig) -> tuple[str, ...]:
    return (
        f"Archive retrieval may take {_format_timedelta(config.archive_restore_latency)}.",
        "Riverhog verifies the collection manifest, proof, and content before materialization.",
        "Temporary retrieval availability expires after "
        f"{_format_timedelta(config.archive_restore_ready_ttl)}.",
    )


def _notify_ready(
    session: Session,
    record: ArchiveRestoreRecord,
    config: RuntimeConfig,
    current: datetime,
) -> None:
    if not config.operator_webhook_url:
        return
    webhook = _webhook_config(config)
    try:
        post_webhook(
            config=webhook,
            payload=build_archive_restore_ready_payload(
                config=webhook,
                restore_id=record.restore_id,
                expires_at=record.expires_at,
                collections=_collection_payload(session, record),
                delivered_at=current,
            ),
        )
    except Exception:
        _LOG.warning(
            "failed to deliver archive restore ready webhook: restore=%s",
            record.restore_id,
            exc_info=True,
        )


def _notify_started(
    session: Session,
    record: ArchiveRestoreRecord,
    config: RuntimeConfig,
    current: datetime,
) -> None:
    current_text = _isoformat_z(current)
    if not config.operator_webhook_url:
        record.started_notification_next_attempt_at = None
        record.started_notification_failure = None
        return
    if record.started_notification_sent_at is not None:
        return
    if (
        record.started_notification_next_attempt_at is not None
        and record.started_notification_next_attempt_at > current_text
    ):
        return
    webhook = _webhook_config(config)
    try:
        post_webhook(
            config=webhook,
            payload=build_archive_restore_started_payload(
                config=webhook,
                restore_id=record.restore_id,
                retrieval_tier=record.retrieval_tier,
                estimated_ready_at=record.ready_at,
                collections=_collection_payload(session, record),
                delivered_at=current,
            ),
        )
    except Exception as exc:
        record.started_notification_failure = _error_text(exc)
        record.started_notification_next_attempt_at = _isoformat_z(
            current + config.operator_webhook_retry_delay
        )
        return
    record.started_notification_sent_at = current_text
    record.started_notification_next_attempt_at = None
    record.started_notification_failure = None


def _notify_completed(
    session: Session,
    record: ArchiveRestoreRecord,
    config: RuntimeConfig,
    current: datetime,
) -> None:
    current_text = _isoformat_z(current)
    if not config.operator_webhook_url:
        record.completed_notification_next_attempt_at = None
        record.completed_notification_failure = None
        return
    if record.completed_notification_sent_at is not None:
        return
    if (
        record.completed_notification_next_attempt_at is not None
        and record.completed_notification_next_attempt_at > current_text
    ):
        return
    webhook = _webhook_config(config)
    try:
        post_webhook(
            config=webhook,
            payload=build_archive_restore_completed_payload(
                config=webhook,
                restore_id=record.restore_id,
                collections=_collection_payload(session, record),
                delivered_at=current,
            ),
        )
    except Exception as exc:
        record.completed_notification_failure = _error_text(exc)
        record.completed_notification_next_attempt_at = _isoformat_z(
            current + config.operator_webhook_retry_delay
        )
        return
    record.completed_notification_sent_at = current_text
    record.completed_notification_next_attempt_at = None
    record.completed_notification_failure = None


def _notify_canceled(
    session: Session,
    record: ArchiveRestoreRecord,
    config: RuntimeConfig,
    current: datetime,
) -> None:
    current_text = _isoformat_z(current)
    if not config.operator_webhook_url:
        record.canceled_notification_next_attempt_at = None
        record.canceled_notification_failure = None
        return
    if record.canceled_notification_sent_at is not None:
        return
    if (
        record.canceled_notification_next_attempt_at is not None
        and record.canceled_notification_next_attempt_at > current_text
    ):
        return
    webhook = _webhook_config(config)
    try:
        post_webhook(
            config=webhook,
            payload=build_archive_restore_canceled_payload(
                config=webhook,
                restore_id=record.restore_id,
                collections=_collection_payload(session, record),
                delivered_at=current,
            ),
        )
    except Exception as exc:
        record.canceled_notification_failure = _error_text(exc)
        record.canceled_notification_next_attempt_at = _isoformat_z(
            current + config.operator_webhook_retry_delay
        )
        return
    record.canceled_notification_sent_at = current_text
    record.canceled_notification_next_attempt_at = None
    record.canceled_notification_failure = None


def _notify_failure(
    session: Session,
    record: ArchiveRestoreRecord,
    config: RuntimeConfig,
    current: datetime,
    *,
    retryable: bool,
    error: str,
    next_retry_at: str | None,
) -> None:
    if not config.operator_webhook_url:
        return
    webhook = _webhook_config(config)
    collections = _collection_payload(session, record)
    attempts = int(record.failure_count or 0)
    failed_at = record.last_failure_at or _isoformat_z(current)
    try:
        payload = (
            build_archive_restore_retrying_payload(
                config=webhook,
                restore_id=record.restore_id,
                collections=collections,
                delivered_at=current,
                attempts=attempts,
                failed_at=failed_at,
                next_retry_at=next_retry_at,
                retry_delay_seconds=config.archive_restore_sweep_interval.total_seconds(),
                error=error,
            )
            if retryable
            else build_archive_restore_failed_payload(
                config=webhook,
                restore_id=record.restore_id,
                collections=collections,
                delivered_at=current,
                attempts=attempts,
                failed_at=failed_at,
                error=error,
            )
        )
        post_webhook(config=webhook, payload=payload)
    except Exception:
        _LOG.warning(
            "failed to deliver archive restore failure webhook: restore=%s retryable=%s",
            record.restore_id,
            retryable,
            exc_info=True,
        )


def _collection_payload(session: Session, record: ArchiveRestoreRecord) -> list[dict[str, str]]:
    return [
        {"collection_id": row.collection_id}
        for row in session.scalars(
            select(ArchiveRestoreCollectionRecord).where(
                ArchiveRestoreCollectionRecord.restore_id == record.restore_id
            )
        ).all()
    ]


def _webhook_config(config: RuntimeConfig) -> WebhookConfig:
    return WebhookConfig(
        url=config.operator_webhook_url or "",
        base_url=config.public_base_url or "",
        timeout_seconds=config.operator_webhook_timeout.total_seconds(),
        retry_seconds=config.operator_webhook_retry_delay.total_seconds(),
        reminder_interval_seconds=config.operator_webhook_reminder_interval.total_seconds(),
        reminder_time=config.operator_webhook_reminder_time,
        reminder_timezone=config.operator_webhook_reminder_timezone,
    )


def _generated_restore_id(collection_id: str, *, existing_ids: Sequence[str]) -> str:
    existing = set(existing_ids)
    safe_collection_id = collection_id.replace("/", "-")
    ordinal = 1
    while True:
        candidate = f"ar-{safe_collection_id}-{ordinal}"
        if candidate not in existing:
            return candidate
        ordinal += 1


def _restore_hold_days(config: RuntimeConfig) -> int:
    return max(ceil(config.archive_restore_ready_ttl.total_seconds() / 86400), 1)


def _format_timedelta(value: timedelta) -> str:
    seconds = int(value.total_seconds())
    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    parts: list[str] = []
    if hours:
        parts.append(f"{hours}h")
    if minutes:
        parts.append(f"{minutes}m")
    if seconds or not parts:
        parts.append(f"{seconds}s")
    return "".join(parts)


def _failure_is_retryable(exc: Exception) -> bool:
    return not isinstance(exc, (InvalidState, NotFound, ValueError))


def _failed_progress_state(value: str | None) -> str:
    return "completed" if value == "completed" else "failed"


def _failure_notification_due(
    last_notified_at: str | None,
    *,
    current: datetime,
    config: RuntimeConfig,
) -> bool:
    if last_notified_at is None:
        return True
    try:
        previous = datetime.fromisoformat(last_notified_at.replace("Z", "+00:00"))
    except ValueError:
        return True
    return operator_reminder_due(
        last_sent_at=previous,
        current=current,
        interval=config.operator_webhook_reminder_interval,
        reminder_time=config.operator_webhook_reminder_time,
        reminder_timezone=config.operator_webhook_reminder_timezone,
    )


def _error_text(exc: Exception) -> str:
    return str(exc).strip() or exc.__class__.__name__


def _isoformat_z(value: datetime) -> str:
    return value.strftime("%Y-%m-%dT%H:%M:%SZ")


def _max_timestamp(values: Iterable[str]) -> str | None:
    items = list(values)
    return max(items) if items else None


def _min_timestamp(values: Iterable[str]) -> str | None:
    items = list(values)
    return min(items) if items else None
