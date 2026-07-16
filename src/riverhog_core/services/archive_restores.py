from __future__ import annotations

import json
import logging
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from datetime import datetime, timedelta
from math import ceil
from typing import cast

from sqlalchemy import asc, desc, func, or_, select
from sqlalchemy.orm import Session
from sqlalchemy.sql.elements import ColumnElement

from riverhog_core.archive_objects import (
    CollectionArchiveFile,
    iter_verified_file_chunks,
    load_collection_archive,
)
from riverhog_core.archive_store_registry import ArchiveStoreRegistry
from riverhog_core.catalog_db import make_session_factory, session_scope
from riverhog_core.catalog_models import (
    ArchiveRestoreFileRecord,
    ArchiveRestoreObjectRecord,
    ArchiveRestoreRecord,
    CollectionArchiveCopyRecord,
    CollectionArchiveFileObjectRecord,
    CollectionArchiveObjectRecord,
    CollectionFileRecord,
    CollectionRecord,
    FetchFileRecord,
    FetchRecord,
)
from riverhog_core.domain.enums import ArchiveRestoreState, ArchiveState, FetchState
from riverhog_core.domain.errors import BadRequest, InvalidState, NotFound
from riverhog_core.domain.models import (
    ArchiveCopyStatus,
    ArchiveRestoreCollection,
    ArchiveRestoreListPage,
    ArchiveRestoreNotificationStatus,
    ArchiveRestoreProgress,
    ArchiveRestoreSummary,
    CollectionManifestStatus,
)
from riverhog_core.domain.types import CollectionId
from riverhog_core.operator_reminders import operator_reminder_due
from riverhog_core.ports.archive_store import (
    ArchiveObjectIdentity,
    ArchiveReadStatus,
    ArchiveStore,
    CollectionArchiveIdentity,
)
from riverhog_core.ports.hot_store import HotStore
from riverhog_core.proofs import CommandProofVerifier, ProofVerifier
from riverhog_core.runtime_config import RuntimeConfig
from riverhog_core.services.archive_records import (
    ArchiveCopyAggregate,
    archive_copy_aggregates,
    archive_copy_is_complete,
)
from riverhog_core.services.collection_custody import require_collection_custody_idle
from riverhog_core.timestamps import format_utc_timestamp, parse_utc_timestamp, utc_now
from riverhog_core.webhooks import (
    WebhookConfig,
    build_archive_restore_canceled_payload,
    build_archive_restore_completed_payload,
    build_archive_restore_failed_payload,
    build_archive_restore_ready_payload,
    build_archive_restore_retrying_payload,
    build_archive_restore_started_payload,
    post_webhook,
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
class _RestoreCollection:
    collection: CollectionRecord
    archive_copy: CollectionArchiveCopyRecord
    files: tuple[CollectionFileRecord, ...]
    objects: CollectionArchiveIdentity


class SqlAlchemyArchiveRestoreService:
    def __init__(
        self,
        config: RuntimeConfig,
        archive_stores: ArchiveStoreRegistry,
        hot_store: HotStore | None = None,
        *,
        proof_verifier: ProofVerifier | None = None,
    ) -> None:
        self._config = config
        self._archive_stores = archive_stores
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
            stmt = stmt.join(ArchiveRestoreFileRecord).where(
                ArchiveRestoreFileRecord.collection_id == collection
            )
        with session_scope(self._session_factory) as session:
            total = int(session.scalar(select(func.count()).select_from(stmt.subquery())) or 0)
            records = session.scalars(
                stmt.order_by(*_restore_order_by(sort=sort, order=order))
                .offset((page - 1) * per_page)
                .limit(per_page)
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
            require_collection_custody_idle(session, collection_id)
            collection = _require_collection(session, collection_id)
            archive_copy = _require_collection_archive_uploaded(collection, self._config)
            files = tuple(
                session.scalars(
                    select(CollectionFileRecord)
                    .where(CollectionFileRecord.collection_id == collection_id)
                    .order_by(CollectionFileRecord.path)
                ).all()
            )
            active = _active_restore_for_collection(
                session,
                collection_id,
                paths=[file.path for file in files],
            )
            if active is None:
                record = _create_restore(
                    session,
                    config=self._config,
                    collection=collection,
                    archive_copy=archive_copy,
                    files=files,
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
            stmt = (
                select(ArchiveRestoreRecord)
                .join(ArchiveRestoreFileRecord)
                .join(
                    FetchFileRecord,
                    (FetchFileRecord.collection_id == ArchiveRestoreFileRecord.collection_id)
                    & (FetchFileRecord.path == ArchiveRestoreFileRecord.path),
                )
                .where(FetchFileRecord.fetch_id == fetch_id)
                .distinct()
            )
            if state is not None:
                stmt = stmt.where(ArchiveRestoreRecord.state == state)
            total = int(session.scalar(select(func.count()).select_from(stmt.subquery())) or 0)
            records = session.scalars(
                stmt.order_by(*_restore_order_by(sort=sort, order=order))
                .offset((page - 1) * per_page)
                .limit(per_page)
            ).all()
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
                restores=[_restore_summary(session, record, self._config) for record in records],
            )

    def create_or_resume_for_fetch(self, fetch_id: str) -> ArchiveRestoreListPage:
        with session_scope(self._session_factory) as session:
            fetch = _require_fetch(session, fetch_id)
            missing_files = session.execute(
                select(CollectionFileRecord.collection_id, CollectionFileRecord.path)
                .join(
                    FetchFileRecord,
                    (FetchFileRecord.collection_id == CollectionFileRecord.collection_id)
                    & (FetchFileRecord.path == CollectionFileRecord.path),
                )
                .where(
                    FetchFileRecord.fetch_id == fetch_id,
                    CollectionFileRecord.hot.is_(False),
                )
                .order_by(FetchFileRecord.file_order)
            ).all()
            missing_by_collection: dict[str, list[str]] = {}
            for collection_id, path in missing_files:
                missing_by_collection.setdefault(str(collection_id), []).append(str(path))
            for collection_id in missing_by_collection:
                require_collection_custody_idle(session, collection_id)
            if fetch.fetch_state == FetchState.QUEUED_ARCHIVE.value:
                fetch.fetch_state = FetchState.RESTORING_ARCHIVE.value

        for collection_id, paths in missing_by_collection.items():
            with session_scope(self._session_factory) as session:
                collection = _require_collection(session, collection_id)
                archive_copy = _require_collection_archive_uploaded(collection, self._config)
                files = tuple(
                    session.scalars(
                        select(CollectionFileRecord).where(
                            CollectionFileRecord.collection_id == collection_id,
                            CollectionFileRecord.path.in_(paths),
                        )
                    ).all()
                )
                active = _active_restore_for_collection(
                    session,
                    collection_id,
                    paths=[file.path for file in files],
                )
                record = active or _create_restore(
                    session,
                    config=self._config,
                    collection=collection,
                    archive_copy=archive_copy,
                    files=files,
                )
                restore_id = record.restore_id
            try:
                self._process_one(restore_id)
            except Exception as exc:
                self._record_processing_failure(restore_id, exc)

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
            restore_ids = list(
                session.scalars(
                    select(ArchiveRestoreRecord.restore_id)
                    .join(ArchiveRestoreFileRecord)
                    .join(
                        FetchFileRecord,
                        (FetchFileRecord.collection_id == ArchiveRestoreFileRecord.collection_id)
                        & (FetchFileRecord.path == ArchiveRestoreFileRecord.path),
                    )
                    .where(
                        FetchFileRecord.fetch_id == fetch_id,
                        ArchiveRestoreRecord.state.in_(_ACTIVE_STATES),
                    )
                    .distinct()
                    .order_by(ArchiveRestoreRecord.restore_id)
                ).all()
            )
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
        current = utc_now()
        with session_scope(self._session_factory) as session:
            record = _require_restore(session, restore_id)
            if record.state == ArchiveRestoreState.CANCELED.value:
                _notify_canceled(session, record, self._config, current)
                return _restore_summary(session, record, self._config)
            if record.state not in _ACTIVE_STATES:
                raise InvalidState("archive restore is not active and cannot be canceled")
            _cleanup_restore(session, record, self._archive_stores)
            record.state = ArchiveRestoreState.CANCELED.value
            record.canceled_at = format_utc_timestamp(current)
            record.next_poll_at = None
            record.started_notification_next_attempt_at = None
            record.completed_notification_next_attempt_at = None
            record.latest_message = "Archive restore was canceled."
            _notify_canceled(session, record, self._config, current)
            return _restore_summary(session, record, self._config)

    def process_due_restores(self, *, limit: int = 100) -> int:
        if limit < 1:
            return 0
        current_text = format_utc_timestamp(utc_now())
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
        fetches_to_restore: set[str] = set()
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
                selected_count = _fetch_file_count(session, fetch.fetch_id)
                listed: dict[str, dict[str, int]] = {}
                fetch_missing = False
                for file in selected:
                    if missing_count >= limit:
                        break
                    if _hot_file_available_for_audit(
                        self._hot_store,
                        file,
                        selected_count=selected_count,
                        listed_hot_files=listed,
                    ):
                        file.hot = True
                        continue
                    file.hot = False
                    fetch_missing = True
                    missing_count += 1
                    fetches_to_restore.add(fetch.fetch_id)
                if fetch_missing and fetch.fetch_state == FetchState.DONE.value:
                    fetch.fetch_state = FetchState.QUEUED_ARCHIVE.value
                elif not fetch_missing and selected_count > 0:
                    fetch.fetch_state = FetchState.DONE.value

        for fetch_id in sorted(fetches_to_restore):
            try:
                self.create_or_resume_for_fetch(fetch_id)
            except Exception:
                _LOG.exception("automatic archive restore failed: fetch=%s", fetch_id)
        if missing_count:
            with session_scope(self._session_factory) as session:
                _sync_fetch_states(session, hot_store=self._hot_store)
        return missing_count

    def _process_one(self, restore_id: str) -> None:
        current = utc_now()
        current_text = format_utc_timestamp(current)
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
                    archive_stores=self._archive_stores,
                    config=self._config,
                    current=current,
                )
                _notify_started(session, record, self._config, current)
                status = _poll_restore(
                    session,
                    record,
                    archive_stores=self._archive_stores,
                    current=current,
                )
                if status.state == "ready":
                    record.state = ArchiveRestoreState.READY.value
                    record.ready_at = status.ready_at or current_text
                    record.expires_at = status.expires_at or format_utc_timestamp(
                        current + self._config.archive_restore_availability_ttl
                    )
                    record.next_poll_at = None
                    record.latest_message = (
                        "Archive is ready; Riverhog is materializing the collection."
                    )
                    _notify_ready(session, record, self._config, current)
                elif status.state == "expired":
                    _expire_restore(session, record, self._archive_stores)
                    return
                else:
                    record.next_poll_at = format_utc_timestamp(
                        current + self._config.archive_restore_sweep_interval
                    )
                    record.latest_message = status.message or (
                        "Archive retrieval is in progress; Riverhog will poll again."
                    )
                    return
            if record.state != ArchiveRestoreState.READY.value:
                return
            if record.expires_at is not None and record.expires_at <= current_text:
                _expire_restore(session, record, self._archive_stores)
                return
            _materialize_restore(
                session,
                record,
                archive_stores=self._archive_stores,
                hot_store=self._hot_store,
                proof_verifier=self._proof_verifier,
                config=self._config,
                current=current,
            )

    def _record_processing_failure(self, restore_id: str, exc: Exception) -> None:
        current = utc_now()
        current_text = format_utc_timestamp(current)
        retryable = _failure_is_retryable(exc)
        error = _error_text(exc)
        next_retry_at = (
            format_utc_timestamp(current + self._config.archive_restore_sweep_interval)
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


def _require_collection_archive_uploaded(
    collection: CollectionRecord,
    config: RuntimeConfig,
) -> CollectionArchiveCopyRecord:
    uploaded = [copy for copy in collection.archive_copies if archive_copy_is_complete(copy)]
    if not uploaded:
        raise InvalidState(
            f"collection archive is not uploaded and cannot be restored: {collection.id}"
        )
    read_rank = {store: index for index, store in enumerate(config.archive_read_order)}
    return min(uploaded, key=lambda copy: (read_rank.get(copy.store, len(read_rank)), copy.store))


def _restore_collections(
    session: Session,
    record: ArchiveRestoreRecord,
) -> list[_RestoreCollection]:
    rows = session.execute(
        select(
            ArchiveRestoreFileRecord.collection_id,
            ArchiveRestoreFileRecord.archive_store,
            func.min(ArchiveRestoreFileRecord.file_order).label("first_order"),
        )
        .where(ArchiveRestoreFileRecord.restore_id == record.restore_id)
        .group_by(
            ArchiveRestoreFileRecord.collection_id,
            ArchiveRestoreFileRecord.archive_store,
        )
        .order_by("first_order", ArchiveRestoreFileRecord.collection_id)
    ).all()
    result: list[_RestoreCollection] = []
    for collection_id, archive_store, _first_order in rows:
        collection = _require_collection(session, str(collection_id))
        copy = session.get(
            CollectionArchiveCopyRecord,
            (str(collection_id), str(archive_store)),
        )
        if copy is None or not archive_copy_is_complete(copy):
            raise InvalidState(f"collection archive copy is incomplete: {collection_id}")
        files = tuple(
            session.scalars(
                select(CollectionFileRecord)
                .join(
                    ArchiveRestoreFileRecord,
                    (ArchiveRestoreFileRecord.collection_id == CollectionFileRecord.collection_id)
                    & (ArchiveRestoreFileRecord.path == CollectionFileRecord.path),
                )
                .where(
                    ArchiveRestoreFileRecord.restore_id == record.restore_id,
                    ArchiveRestoreFileRecord.collection_id == collection_id,
                )
                .order_by(ArchiveRestoreFileRecord.file_order)
            ).all()
        )
        objects = session.scalars(
            select(CollectionArchiveObjectRecord)
            .join(
                ArchiveRestoreObjectRecord,
                (
                    ArchiveRestoreObjectRecord.collection_id
                    == CollectionArchiveObjectRecord.collection_id
                )
                & (ArchiveRestoreObjectRecord.archive_store == CollectionArchiveObjectRecord.store)
                & (ArchiveRestoreObjectRecord.object_id == CollectionArchiveObjectRecord.object_id),
            )
            .where(
                ArchiveRestoreObjectRecord.restore_id == record.restore_id,
                ArchiveRestoreObjectRecord.collection_id == collection_id,
            )
            .order_by(ArchiveRestoreObjectRecord.object_order)
        ).all()
        result.append(
            _RestoreCollection(
                collection=collection,
                archive_copy=copy,
                files=files,
                objects=CollectionArchiveIdentity(
                    objects=tuple(_object_identity(current) for current in objects)
                ),
            )
        )
    return result


def _fetch_collection_ids(session: Session, fetch_id: str) -> list[str]:
    return list(
        session.scalars(
            select(FetchFileRecord.collection_id)
            .where(FetchFileRecord.fetch_id == fetch_id)
            .group_by(FetchFileRecord.collection_id)
            .order_by(
                func.min(FetchFileRecord.file_order),
                FetchFileRecord.collection_id,
            )
        ).all()
    )


def _fetch_files(session: Session, fetch_id: str) -> list[CollectionFileRecord]:
    return list(
        session.scalars(
            select(CollectionFileRecord)
            .join(
                FetchFileRecord,
                (FetchFileRecord.collection_id == CollectionFileRecord.collection_id)
                & (FetchFileRecord.path == CollectionFileRecord.path),
            )
            .where(FetchFileRecord.fetch_id == fetch_id)
            .order_by(
                FetchFileRecord.file_order,
            )
        ).all()
    )


def _fetch_file_count(session: Session, fetch_id: str) -> int:
    return int(
        session.scalar(
            select(func.count(CollectionFileRecord.path))
            .select_from(FetchFileRecord)
            .join(
                CollectionFileRecord,
                (CollectionFileRecord.collection_id == FetchFileRecord.collection_id)
                & (CollectionFileRecord.path == FetchFileRecord.path),
            )
            .where(FetchFileRecord.fetch_id == fetch_id)
        )
        or 0
    )


def _restore_order_by(*, sort: str, order: str) -> tuple[ColumnElement[object], ...]:
    sort_expr = {
        "created_at": ArchiveRestoreRecord.created_at,
        "id": ArchiveRestoreRecord.restore_id,
        "state": ArchiveRestoreRecord.state,
        "ready_at": ArchiveRestoreRecord.ready_at,
        "expires_at": ArchiveRestoreRecord.expires_at,
    }[sort]
    direction = desc if order == "desc" else asc
    if sort == "id":
        return (direction(sort_expr),)
    return (direction(sort_expr), asc(ArchiveRestoreRecord.restore_id))


def _active_restore_for_collection(
    session: Session,
    collection_id: str,
    *,
    paths: Iterable[str],
) -> ArchiveRestoreRecord | None:
    selected_paths = tuple(dict.fromkeys(paths))
    if not selected_paths:
        return None
    covering_restore_ids = (
        select(ArchiveRestoreFileRecord.restore_id)
        .where(
            ArchiveRestoreFileRecord.collection_id == collection_id,
            ArchiveRestoreFileRecord.path.in_(selected_paths),
        )
        .group_by(ArchiveRestoreFileRecord.restore_id)
        .having(func.count(ArchiveRestoreFileRecord.path) == len(selected_paths))
    )
    return session.scalar(
        select(ArchiveRestoreRecord)
        .join(ArchiveRestoreFileRecord)
        .where(
            ArchiveRestoreFileRecord.collection_id == collection_id,
            ArchiveRestoreRecord.state.in_(_ACTIVE_STATES),
            ArchiveRestoreRecord.restore_id.in_(covering_restore_ids),
        )
        .order_by(ArchiveRestoreRecord.created_at.desc())
        .limit(1)
    )


def _create_restore(
    session: Session,
    *,
    config: RuntimeConfig,
    collection: CollectionRecord,
    archive_copy: CollectionArchiveCopyRecord,
    files: tuple[CollectionFileRecord, ...],
) -> ArchiveRestoreRecord:
    if not files:
        raise InvalidState("archive restore has no files")
    restore_id = _generated_restore_id(session, collection.id)
    record = ArchiveRestoreRecord(
        restore_id=restore_id,
        state=ArchiveRestoreState.REQUESTED.value,
        created_at=format_utc_timestamp(utc_now()),
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
    for file_order, file in enumerate(files):
        session.add(
            ArchiveRestoreFileRecord(
                restore_id=restore_id,
                collection_id=collection.id,
                path=file.path,
                archive_store=archive_copy.store,
                file_order=file_order,
            )
        )
    required_object_ids = session.scalars(
        select(CollectionArchiveFileObjectRecord.object_id)
        .where(
            CollectionArchiveFileObjectRecord.collection_id == collection.id,
            CollectionArchiveFileObjectRecord.store == archive_copy.store,
            CollectionArchiveFileObjectRecord.path.in_([file.path for file in files]),
        )
        .distinct()
    ).all()
    objects = session.scalars(
        select(CollectionArchiveObjectRecord)
        .where(
            CollectionArchiveObjectRecord.collection_id == collection.id,
            CollectionArchiveObjectRecord.store == archive_copy.store,
            CollectionArchiveObjectRecord.object_id.in_(required_object_ids),
        )
        .order_by(CollectionArchiveObjectRecord.object_order)
    ).all()
    if not objects:
        raise InvalidState("archive restore files have no mapped archive objects")
    for object_order, current in enumerate(objects):
        session.add(
            ArchiveRestoreObjectRecord(
                restore_id=restore_id,
                collection_id=collection.id,
                archive_store=archive_copy.store,
                object_id=current.object_id,
                object_order=object_order,
            )
        )
    session.flush()
    return record


def _request_restore(
    session: Session,
    record: ArchiveRestoreRecord,
    *,
    archive_stores: ArchiveStoreRegistry,
    config: RuntimeConfig,
    current: datetime,
) -> None:
    if record.requested_at is not None:
        return
    bindings = _restore_collections(session, record)
    if not bindings:
        raise InvalidState("archive restore has no files")
    requested_at = format_utc_timestamp(current)
    estimated_ready_at = format_utc_timestamp(current + config.archive_restore_estimated_latency)
    statuses = [
        archive_stores.require(binding.archive_copy.store).prepare_archive_objects_read(
            collection_id=binding.collection.id,
            objects=binding.objects.objects,
            retrieval_tier=record.retrieval_tier,
            hold_days=record.hold_days,
            requested_at=requested_at,
            estimated_ready_at=estimated_ready_at,
        )
        for binding in bindings
    ]
    record.requested_at = requested_at
    record.ready_at = (
        _max_timestamp(status.ready_at for status in statuses if status.ready_at is not None)
        or estimated_ready_at
    )
    record.expires_at = _min_timestamp(
        status.expires_at for status in statuses if status.expires_at is not None
    )
    record.next_poll_at = format_utc_timestamp(current + config.archive_restore_sweep_interval)
    record.latest_message = (
        "Archive retrieval requested; Riverhog will materialize the selected files when ready."
    )


def _poll_restore(
    session: Session,
    record: ArchiveRestoreRecord,
    *,
    archive_stores: ArchiveStoreRegistry,
    current: datetime,
) -> ArchiveReadStatus:
    bindings = _restore_collections(session, record)
    if not bindings:
        raise InvalidState("archive restore has no files")
    statuses = [
        archive_stores.require(binding.archive_copy.store).get_archive_objects_read_status(
            collection_id=binding.collection.id,
            objects=binding.objects.objects,
            requested_at=record.requested_at or format_utc_timestamp(current),
            estimated_ready_at=record.ready_at,
            estimated_expires_at=record.expires_at,
        )
        for binding in bindings
    ]
    if any(status.state == "expired" for status in statuses):
        return ArchiveReadStatus(state="expired")
    if statuses and all(status.state == "ready" for status in statuses):
        return ArchiveReadStatus(
            state="ready",
            ready_at=_max_timestamp(
                status.ready_at for status in statuses if status.ready_at is not None
            ),
            expires_at=_min_timestamp(
                status.expires_at for status in statuses if status.expires_at is not None
            ),
        )
    return ArchiveReadStatus(
        state="requested",
        message="Archive retrieval is in progress; Riverhog will poll again.",
    )


def _materialize_restore(
    session: Session,
    record: ArchiveRestoreRecord,
    *,
    archive_stores: ArchiveStoreRegistry,
    hot_store: HotStore | None,
    proof_verifier: ProofVerifier,
    config: RuntimeConfig,
    current: datetime,
) -> None:
    if hot_store is None:
        raise InvalidState("archive restore service has no hot store")
    for binding in _restore_collections(session, record):
        collection = binding.collection
        selected_files = tuple(
            CollectionArchiveFile(path=file.path, bytes=file.bytes, sha256=file.sha256)
            for file in binding.files
        )
        if not selected_files:
            continue
        archive_store = archive_stores.require(binding.archive_copy.store)
        record.archive_verification_state = "in_progress"
        record.extraction_state = "in_progress"
        record.materialization_state = "in_progress"
        session.flush()
        manifest_identity = _copy_object_identity(binding.archive_copy, "manifest")
        proof_identity = _copy_object_identity(binding.archive_copy, "proof")
        manifest_bytes = b"".join(
            archive_store.iter_archive_object(
                collection_id=collection.id,
                object=manifest_identity,
            )
        )
        proof_bytes = b"".join(
            archive_store.iter_archive_object(
                collection_id=collection.id,
                object=proof_identity,
            )
        )
        all_files = _expected_files(session, collection.id)
        data_by_id = {current.object_id: current for current in binding.objects.data_objects}
        object_cache: dict[str, bytes] = {}

        def read_object(
            object_id: str,
            data_by_id: dict[str, ArchiveObjectIdentity] = data_by_id,
            object_cache: dict[str, bytes] = object_cache,
            archive_store: ArchiveStore = archive_store,
            collection_id: str = collection.id,
        ) -> tuple[bytes, ...] | Iterator[bytes]:
            identity = data_by_id[object_id]
            if identity.kind == "pack":
                if object_id not in object_cache:
                    object_cache[object_id] = b"".join(
                        archive_store.iter_archive_object(
                            collection_id=collection_id,
                            object=identity,
                        )
                    )
                return (object_cache[object_id],)
            return archive_store.iter_archive_object(
                collection_id=collection_id,
                object=identity,
            )

        archive = load_collection_archive(
            collection_id=collection.id,
            files=all_files,
            proof_bytes=proof_bytes,
            manifest_bytes=manifest_bytes,
            read_object_chunks=read_object,
            verifier=proof_verifier,
        )
        record.archive_verification_state = "completed"
        for file in selected_files:
            content, content_length = iter_verified_file_chunks(
                archive,
                path=file.path,
                read_object=read_object,
            )
            hot_store.put_collection_file_stream(
                collection.id,
                file.path,
                content,
                content_length=content_length,
            )
            row = session.get(
                CollectionFileRecord,
                {"collection_id": collection.id, "path": file.path},
            )
            if row is not None:
                row.hot = True
    record.extraction_state = "completed"
    record.materialization_state = "completed"
    _cleanup_restore(session, record, archive_stores)
    record.state = ArchiveRestoreState.COMPLETED.value
    record.completed_at = format_utc_timestamp(current)
    record.expires_at = record.completed_at
    record.next_poll_at = None
    record.latest_message = "The selected files were verified and materialized to hot storage."
    _sync_fetch_states(session, hot_store=hot_store)
    _notify_completed(session, record, config, current)


def _expire_restore(
    session: Session,
    record: ArchiveRestoreRecord,
    archive_stores: ArchiveStoreRegistry,
) -> None:
    _cleanup_restore(session, record, archive_stores)
    record.state = ArchiveRestoreState.EXPIRED.value
    record.next_poll_at = None
    record.latest_message = "Temporary archive retrieval expired; start a new restore."


def _cleanup_restore(
    session: Session,
    record: ArchiveRestoreRecord,
    archive_stores: ArchiveStoreRegistry,
) -> None:
    for binding in _restore_collections(session, record):
        archive_stores.require(binding.archive_copy.store).cleanup_archive_objects_read(
            collection_id=binding.collection.id,
            objects=binding.objects.objects,
        )


def _expected_files(session: Session, collection_id: str) -> tuple[CollectionArchiveFile, ...]:
    rows = session.scalars(
        select(CollectionFileRecord)
        .where(CollectionFileRecord.collection_id == collection_id)
        .order_by(CollectionFileRecord.path)
    ).all()
    return tuple(
        CollectionArchiveFile(path=row.path, bytes=row.bytes, sha256=row.sha256) for row in rows
    )


def _object_identity(record: CollectionArchiveObjectRecord) -> ArchiveObjectIdentity:
    return ArchiveObjectIdentity(
        object_id=record.object_id,
        kind=record.kind,
        object_path=record.object_path,
        plaintext_bytes=record.plaintext_bytes,
        stored_bytes=record.stored_bytes,
        sha256=record.sha256,
    )


def _copy_object_identity(
    copy: CollectionArchiveCopyRecord,
    object_id: str,
) -> ArchiveObjectIdentity:
    for current in copy.objects:
        if current.object_id == object_id:
            return _object_identity(current)
    raise InvalidState(f"collection archive object is missing: {object_id}")


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
            hot_listing = hot_store.list_collection_files(file.collection_id)
            listing = {listed_file.path: listed_file.bytes for listed_file in hot_listing.files}
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
    bindings = _restore_collections(session, record)
    collection_ids = [binding.collection.id for binding in bindings]
    copy_aggregates = archive_copy_aggregates(session, collection_ids=collection_ids)
    selected_storage = {
        (str(collection_id), str(store)): int(stored_bytes)
        for collection_id, store, stored_bytes in session.execute(
            select(
                ArchiveRestoreObjectRecord.collection_id,
                ArchiveRestoreObjectRecord.archive_store,
                func.coalesce(func.sum(CollectionArchiveObjectRecord.stored_bytes), 0),
            )
            .join(
                CollectionArchiveObjectRecord,
                (
                    CollectionArchiveObjectRecord.collection_id
                    == ArchiveRestoreObjectRecord.collection_id
                )
                & (CollectionArchiveObjectRecord.store == ArchiveRestoreObjectRecord.archive_store)
                & (CollectionArchiveObjectRecord.object_id == ArchiveRestoreObjectRecord.object_id),
            )
            .where(ArchiveRestoreObjectRecord.restore_id == record.restore_id)
            .group_by(
                ArchiveRestoreObjectRecord.collection_id,
                ArchiveRestoreObjectRecord.archive_store,
            )
        )
    }
    collections = tuple(
        ArchiveRestoreCollection(
            id=CollectionId(binding.collection.id),
            archive_copy=_archive_status(
                binding.archive_copy,
                aggregates=copy_aggregates,
            ),
            stored_bytes=selected_storage.get(
                (binding.collection.id, binding.archive_copy.store),
                0,
            ),
        )
        for binding in bindings
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


def _archive_status(
    archive: CollectionArchiveCopyRecord,
    *,
    aggregates: dict[tuple[str, str], ArchiveCopyAggregate],
) -> ArchiveCopyStatus:
    object_count, stored_bytes = aggregates.get((archive.collection_id, archive.store), (0, 0))
    return ArchiveCopyStatus(
        store=archive.store,
        state=ArchiveState(archive.state),
        storage_prefix=archive.archive_storage_prefix,
        object_count=object_count,
        stored_bytes=stored_bytes,
        backend=archive.backend,
        storage_class=archive.storage_class,
        last_uploaded_at=archive.last_uploaded_at,
        last_verified_at=archive.last_verified_at,
        failure=archive.failure,
        collection_manifest=_manifest_status(archive),
    )


def _manifest_status(
    archive: CollectionArchiveCopyRecord,
) -> CollectionManifestStatus:
    manifest = next(
        (current for current in archive.objects if current.object_id == "manifest"),
        None,
    )
    proof = next(
        (current for current in archive.objects if current.object_id == "proof"),
        None,
    )
    proof_state = "uploaded" if proof else "pending"
    if ArchiveState(archive.state) == ArchiveState.FAILED:
        proof_state = "failed"
    return CollectionManifestStatus(
        object_path=manifest.object_path if manifest else None,
        sha256=manifest.sha256 if manifest else None,
        proof_object_path=proof.object_path if proof else None,
        proof_state=proof_state,
        proof_sha256=proof.sha256 if proof else None,
    )


def _build_warnings(config: RuntimeConfig) -> tuple[str, ...]:
    return (
        "Archive retrieval may take "
        f"{_format_timedelta(config.archive_restore_estimated_latency)}.",
        "Riverhog verifies the manifest, proof, archive objects, and selected files.",
        "Temporary retrieval availability expires after "
        f"{_format_timedelta(config.archive_restore_availability_ttl)}.",
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
    current_text = format_utc_timestamp(current)
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
        record.started_notification_next_attempt_at = format_utc_timestamp(
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
    current_text = format_utc_timestamp(current)
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
        record.completed_notification_next_attempt_at = format_utc_timestamp(
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
    current_text = format_utc_timestamp(current)
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
        record.canceled_notification_next_attempt_at = format_utc_timestamp(
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
    failed_at = record.last_failure_at or format_utc_timestamp(current)
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
        {"collection_id": str(collection_id)}
        for collection_id in session.scalars(
            select(ArchiveRestoreFileRecord.collection_id)
            .where(ArchiveRestoreFileRecord.restore_id == record.restore_id)
            .distinct()
        ).all()
    ]


def _webhook_config(config: RuntimeConfig) -> WebhookConfig:
    return WebhookConfig(
        url=config.operator_webhook_url or "",
        base_url=config.public_base_url or "",
        timeout_seconds=config.webhook_timeout.total_seconds(),
        retry_seconds=config.operator_webhook_retry_delay.total_seconds(),
        reminder_interval_seconds=config.operator_webhook_reminder_interval.total_seconds(),
        reminder_time=config.operator_webhook_reminder_time,
        reminder_timezone=config.operator_webhook_reminder_timezone,
    )


def _generated_restore_id(session: Session, collection_id: str) -> str:
    safe_collection_id = collection_id.replace("/", "-")
    ordinal = 1
    while True:
        candidate = f"ar-{safe_collection_id}-{ordinal}"
        if session.get(ArchiveRestoreRecord, candidate) is None:
            return candidate
        ordinal += 1


def _restore_hold_days(config: RuntimeConfig) -> int:
    return max(ceil(config.archive_restore_availability_ttl.total_seconds() / 86400), 1)


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
        previous = parse_utc_timestamp(last_notified_at)
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


def _max_timestamp(values: Iterable[str]) -> str | None:
    items = list(values)
    return max(items) if items else None


def _min_timestamp(values: Iterable[str]) -> str | None:
    items = list(values)
    return min(items) if items else None
