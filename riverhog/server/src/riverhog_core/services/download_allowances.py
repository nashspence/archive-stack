from __future__ import annotations

import secrets
import threading
import time
from collections.abc import Callable, Iterator, Mapping
from datetime import UTC, datetime, timedelta
from typing import Any, cast

from http_api_contracts import closed_literal_values
from riverhog_protocol import DownloadQuotaSort, SortOrder
from riverhog_protocol.errors import BadRequest, DownloadAllowanceExceeded, NotFound
from sqlalchemy import and_, asc, case, delete, desc, func, or_, select
from sqlalchemy.orm import Session
from state_schema import read_snapshot
from time_formats import format_utc_timestamp, utc_now

from riverhog_core.browse import bounded_page, keyset_statement, validate_page_size
from riverhog_core.catalog_db import SessionFactory, make_session_factory, session_scope
from riverhog_core.catalog_models import (
    AppKeyRecord,
    ArchiveDownloadReservationRecord,
    ArchiveDownloadUsageRecord,
    KeyDownloadReservationRecord,
    KeyDownloadUsageRecord,
)
from riverhog_core.domain.models import ArchiveDownloadAllowance
from riverhog_core.ports.download_allowance import DownloadAttribution
from riverhog_core.runtime_config import RuntimeConfig, StorageAdapterRegistration

_RESERVATION_LEASE = timedelta(hours=1)
_RESERVATION_HEARTBEAT_SECONDS = 5 * 60
_KEY_QUOTA_SORT_FIELDS = closed_literal_values(DownloadQuotaSort)
_SORT_ORDERS = closed_literal_values(SortOrder)


class SqlAlchemyDownloadAllowance:
    def __init__(
        self,
        config: RuntimeConfig,
        *,
        clock: Callable[[], datetime] = utc_now,
        session_factory: SessionFactory | None = None,
    ) -> None:
        self._session_factory = session_factory or make_session_factory(config.database_url)
        self._clock = clock
        self._policies = {
            name: store
            for name, store in config.archive_stores.items()
            if store.monthly_download_allowance_bytes is not None
        }
        self._locks = {name: threading.Lock() for name in self._policies}
        self._key_locks: dict[str, threading.Lock] = {}
        self._key_locks_guard = threading.Lock()
        self._ensure_usage_rows()

    def track(
        self,
        *,
        store: str,
        expected_bytes: int,
        content: Iterator[bytes],
        attribution: DownloadAttribution | None = None,
    ) -> Iterator[bytes]:
        policy = self._policies.get(store)
        if policy is None and attribution is None:
            return content
        if expected_bytes < 0:
            raise ValueError("expected download bytes must not be negative")
        reservation_id = (
            self._reserve(policy=policy, expected_bytes=expected_bytes)
            if policy is not None
            else None
        )
        try:
            key_reservation_id = (
                self._begin_key_stream(
                    attribution=attribution,
                    expected_bytes=expected_bytes,
                )
                if attribution is not None
                else None
            )
        except Exception:
            if reservation_id is not None:
                self._release_store_reservation(store=store, reservation_id=reservation_id)
            raise
        return self._tracked_content(
            store=store,
            reservation_id=reservation_id,
            key_reservation_id=key_reservation_id,
            attribution=attribution,
            content=content,
        )

    def reserve_retrieval(
        self,
        *,
        key_id: str,
        job_id: str,
        expected_bytes: int,
        expires_at: str,
    ) -> None:
        if expected_bytes < 0:
            raise ValueError("expected download bytes must not be negative")
        if expected_bytes == 0:
            return
        now = self._current_time()
        now_text = format_utc_timestamp(now)
        with self._key_lock(key_id):
            with session_scope(self._session_factory) as session:
                usage, key = self._locked_key_usage(session, key_id=key_id, now=now)
                self._reap_expired_key_reservations(session, usage=usage, now=now)
                reservation_id = _job_reservation_id(job_id)
                if session.get(KeyDownloadReservationRecord, reservation_id) is not None:
                    return
                self._require_key_capacity(
                    session,
                    key=key,
                    usage=usage,
                    expected_bytes=expected_bytes,
                    now=now,
                )
                session.add(
                    KeyDownloadReservationRecord(
                        id=reservation_id,
                        key_id=key_id,
                        job_id=job_id,
                        kind="job",
                        month_started_at=usage.month_started_at,
                        reserved_bytes=expected_bytes,
                        created_at=now_text,
                        expires_at=expires_at,
                    )
                )
                usage.updated_at = now_text

    def release_retrieval(self, *, job_id: str) -> None:
        with session_scope(self._session_factory) as session:
            session.execute(
                delete(KeyDownloadReservationRecord).where(
                    KeyDownloadReservationRecord.job_id == job_id,
                    KeyDownloadReservationRecord.kind == "job",
                )
            )

    def set_key_quota(
        self,
        *,
        app: str,
        key_id: str,
        monthly_bytes: int | None,
    ) -> dict[str, object]:
        if monthly_bytes is not None and monthly_bytes < 0:
            raise BadRequest("monthly download quota must not be negative")
        normalized_app = app.strip().casefold()
        normalized_key_id = key_id.strip().casefold()
        now = self._current_time()
        with self._key_lock(normalized_key_id):
            with session_scope(self._session_factory) as session:
                key = session.scalar(
                    select(AppKeyRecord)
                    .where(
                        AppKeyRecord.id == normalized_key_id,
                        AppKeyRecord.app == normalized_app,
                    )
                    .with_for_update()
                )
                if key is None:
                    raise NotFound(f"app key not found: {normalized_key_id}")
                key.monthly_download_quota_bytes = monthly_bytes
                usage, _key = self._locked_key_usage(
                    session,
                    key_id=normalized_key_id,
                    now=now,
                    require_active=False,
                )
                self._reap_expired_key_reservations(session, usage=usage, now=now)
                return self._key_quota_payload(session, key=key, usage=usage, now=now)

    def get_key_quota(self, *, key_id: str) -> dict[str, object]:
        normalized_key_id = key_id.strip().casefold()
        now = self._current_time()
        with self._key_lock(normalized_key_id):
            with session_scope(self._session_factory) as session:
                usage, key = self._locked_key_usage(
                    session,
                    key_id=normalized_key_id,
                    now=now,
                    require_active=False,
                )
                self._reap_expired_key_reservations(session, usage=usage, now=now)
                return self._key_quota_payload(session, key=key, usage=usage, now=now)

    def list_key_quotas(
        self,
        *,
        page_size: int,
        position: tuple[str | int | bool | bytes | None, ...] | None,
        q: str | None,
        sort: str,
        order: str,
        app: str | None = None,
        active: bool | None = None,
    ) -> dict[str, object]:
        validate_page_size(page_size)
        now = self._current_time()
        now_text, current_month, query, normalized_app, statement, key_columns = (
            _key_quota_statements(now=now, q=q, sort=sort, order=order, app=app, active=active)
        )
        with read_snapshot(self._session_factory) as session:
            rows, next_position = bounded_page(
                list(
                    session.execute(
                        keyset_statement(
                            statement,
                            columns=key_columns,
                            position=position,
                            order=order,
                            page_size=page_size,
                        )
                    ).mappings()
                ),
                page_size=page_size,
                position_of=lambda row: _key_quota_position(row, sort=sort),
            )
            row_payloads = [dict(row) for row in rows]
        quotas = [
            _key_quota_row(row, now=now, now_text=now_text, current_month=current_month)
            for row in row_payloads
        ]
        return {
            "page_size": page_size,
            "_next_position": next_position,
            "sort": sort,
            "order": order,
            "query": query,
            "app": normalized_app,
            "active": active,
            "quotas": quotas,
        }

    def iter_key_quotas(
        self,
        *,
        q: str | None,
        sort: str,
        order: str,
        app: str | None = None,
        active: bool | None = None,
    ) -> Iterator[dict[str, object]]:
        now = self._current_time()
        now_text, current_month, _, _, statement, key_columns = _key_quota_statements(
            now=now, q=q, sort=sort, order=order, app=app, active=active
        )
        direction = desc if order == "desc" else asc
        statement = statement.order_by(*(direction(column) for column in key_columns))
        with read_snapshot(self._session_factory) as session:
            rows = session.execute(statement.execution_options(yield_per=100)).mappings()
            for row in rows:
                yield _key_quota_row(
                    cast(Mapping[str, object], row),
                    now=now,
                    now_text=now_text,
                    current_month=current_month,
                )

    def get_statuses(self) -> tuple[ArchiveDownloadAllowance, ...]:
        return tuple(self._status(self._policies[name]) for name in sorted(self._policies))

    def _ensure_usage_rows(self) -> None:
        if not self._policies:
            return
        now = self._current_time()
        month_started_at = format_utc_timestamp(_month_start(now))
        now_text = format_utc_timestamp(now)
        with session_scope(self._session_factory) as session:
            for store in self._policies:
                if session.get(ArchiveDownloadUsageRecord, store) is None:
                    session.add(
                        ArchiveDownloadUsageRecord(
                            store=store,
                            month_started_at=month_started_at,
                            accounted_bytes=0,
                            updated_at=now_text,
                        )
                    )

    def _reserve(self, *, policy: StorageAdapterRegistration, expected_bytes: int) -> str:
        now = self._current_time()
        now_text = format_utc_timestamp(now)
        with self._locks[policy.name]:
            with session_scope(self._session_factory) as session:
                usage = self._locked_usage(session, store=policy.name, now=now)
                self._reap_expired(session, usage=usage, now=now)
                reserved_bytes = self._reserved_bytes(session, store=policy.name)
                effective_limit = _effective_limit(policy)
                projected = usage.accounted_bytes + reserved_bytes + expected_bytes
                if projected > effective_limit:
                    resets_at = format_utc_timestamp(_next_month_start(now))
                    remaining = max(
                        0,
                        effective_limit - usage.accounted_bytes - reserved_bytes,
                    )
                    raise DownloadAllowanceExceeded(
                        f"archive store {policy.name} monthly download allowance has "
                        f"{remaining} bytes remaining; {expected_bytes} bytes were requested; "
                        f"resets at {resets_at}"
                    )
                reservation_id = secrets.token_hex(16)
                session.add(
                    ArchiveDownloadReservationRecord(
                        id=reservation_id,
                        store=policy.name,
                        month_started_at=usage.month_started_at,
                        reserved_bytes=expected_bytes,
                        created_at=now_text,
                        expires_at=format_utc_timestamp(now + _RESERVATION_LEASE),
                    )
                )
                usage.updated_at = now_text
                return reservation_id

    def _tracked_content(
        self,
        *,
        store: str,
        reservation_id: str | None,
        key_reservation_id: str | None,
        attribution: DownloadAttribution | None,
        content: Iterator[bytes],
    ) -> Iterator[bytes]:
        transferred_bytes = 0
        next_heartbeat = time.monotonic() + _RESERVATION_HEARTBEAT_SECONDS
        try:
            for chunk in content:
                transferred_bytes += len(chunk)
                if time.monotonic() >= next_heartbeat:
                    if reservation_id is not None:
                        self._heartbeat(store=store, reservation_id=reservation_id)
                    if key_reservation_id is not None and attribution is not None:
                        self._heartbeat_key_stream(
                            key_id=attribution.key_id,
                            reservation_id=key_reservation_id,
                        )
                    next_heartbeat = time.monotonic() + _RESERVATION_HEARTBEAT_SECONDS
                yield chunk
        finally:
            try:
                if reservation_id is not None:
                    self._finish(
                        store=store,
                        reservation_id=reservation_id,
                        transferred_bytes=transferred_bytes,
                    )
            finally:
                if key_reservation_id is not None and attribution is not None:
                    self._finish_key_stream(
                        key_id=attribution.key_id,
                        reservation_id=key_reservation_id,
                        transferred_bytes=transferred_bytes,
                    )

    def _release_store_reservation(self, *, store: str, reservation_id: str) -> None:
        with self._locks[store]:
            with session_scope(self._session_factory) as session:
                reservation = session.get(ArchiveDownloadReservationRecord, reservation_id)
                if reservation is not None:
                    session.delete(reservation)

    def _begin_key_stream(
        self,
        *,
        attribution: DownloadAttribution,
        expected_bytes: int,
    ) -> str:
        now = self._current_time()
        now_text = format_utc_timestamp(now)
        with self._key_lock(attribution.key_id):
            with session_scope(self._session_factory) as session:
                usage, key = self._locked_key_usage(
                    session,
                    key_id=attribution.key_id,
                    now=now,
                )
                self._reap_expired_key_reservations(session, usage=usage, now=now)
                job_reservation = session.get(
                    KeyDownloadReservationRecord,
                    _job_reservation_id(attribution.job_id),
                )
                available = (
                    job_reservation.reserved_bytes
                    if job_reservation is not None and job_reservation.key_id == attribution.key_id
                    else 0
                )
                extra = max(0, expected_bytes - available)
                if extra:
                    self._require_key_capacity(
                        session,
                        key=key,
                        usage=usage,
                        expected_bytes=extra,
                        now=now,
                    )
                reservation_month = (
                    job_reservation.month_started_at
                    if job_reservation is not None
                    else usage.month_started_at
                )
                if job_reservation is not None:
                    job_reservation.reserved_bytes -= min(available, expected_bytes)
                    if job_reservation.reserved_bytes == 0:
                        session.delete(job_reservation)
                stream_id = secrets.token_hex(16)
                session.add(
                    KeyDownloadReservationRecord(
                        id=stream_id,
                        key_id=attribution.key_id,
                        job_id=attribution.job_id,
                        kind="stream",
                        month_started_at=reservation_month,
                        reserved_bytes=expected_bytes,
                        created_at=now_text,
                        expires_at=format_utc_timestamp(now + _RESERVATION_LEASE),
                    )
                )
                usage.updated_at = now_text
                return stream_id

    def _heartbeat_key_stream(self, *, key_id: str, reservation_id: str) -> None:
        now = self._current_time()
        with self._key_lock(key_id):
            with session_scope(self._session_factory) as session:
                reservation = session.get(KeyDownloadReservationRecord, reservation_id)
                if reservation is not None and reservation.kind == "stream":
                    reservation.expires_at = format_utc_timestamp(now + _RESERVATION_LEASE)

    def _finish_key_stream(
        self,
        *,
        key_id: str,
        reservation_id: str,
        transferred_bytes: int,
    ) -> None:
        now = self._current_time()
        now_text = format_utc_timestamp(now)
        with self._key_lock(key_id):
            with session_scope(self._session_factory) as session:
                usage, _key = self._locked_key_usage(
                    session,
                    key_id=key_id,
                    now=now,
                    require_active=False,
                )
                reservation = session.get(KeyDownloadReservationRecord, reservation_id)
                if reservation is None:
                    return
                accounted = (
                    transferred_bytes
                    if reservation.month_started_at == usage.month_started_at
                    else max(transferred_bytes, reservation.reserved_bytes)
                )
                usage.accounted_bytes += accounted
                usage.updated_at = now_text
                session.delete(reservation)

    def _locked_key_usage(
        self,
        session: Session,
        *,
        key_id: str,
        now: datetime,
        require_active: bool = True,
    ) -> tuple[KeyDownloadUsageRecord, AppKeyRecord]:
        key = session.scalar(
            select(AppKeyRecord).where(AppKeyRecord.id == key_id).with_for_update()
        )
        if key is None:
            raise NotFound(f"app key not found: {key_id}")
        if require_active and (
            key.revoked_at is not None
            or (key.expires_at is not None and key.expires_at <= format_utc_timestamp(now))
        ):
            raise DownloadAllowanceExceeded("application key is not active")
        usage = session.scalar(
            select(KeyDownloadUsageRecord)
            .where(KeyDownloadUsageRecord.key_id == key_id)
            .with_for_update()
        )
        current_month = format_utc_timestamp(_month_start(now))
        if usage is None:
            usage = KeyDownloadUsageRecord(
                key_id=key_id,
                month_started_at=current_month,
                accounted_bytes=0,
                updated_at=format_utc_timestamp(now),
            )
            session.add(usage)
            session.flush()
        elif usage.month_started_at != current_month:
            usage.month_started_at = current_month
            usage.accounted_bytes = 0
            usage.updated_at = format_utc_timestamp(now)
        return usage, key

    def _key_quota_payload(
        self,
        session: Session,
        *,
        key: AppKeyRecord,
        usage: KeyDownloadUsageRecord,
        now: datetime,
    ) -> dict[str, object]:
        reserved = self._reserved_key_bytes(session, key_id=key.id)
        monthly_bytes = key.monthly_download_quota_bytes
        remaining = (
            None
            if monthly_bytes is None
            else max(0, monthly_bytes - usage.accounted_bytes - reserved)
        )
        return {
            "id": key.id,
            "app": key.app,
            "key_id": key.id,
            "key_status": _key_status(
                {"revoked_at": key.revoked_at, "expires_at": key.expires_at},
                now_text=format_utc_timestamp(now),
            ),
            "monthly_bytes": monthly_bytes,
            "month_started_at": usage.month_started_at,
            "resets_at": format_utc_timestamp(_next_month_start(now)),
            "accounted_bytes": usage.accounted_bytes,
            "reserved_bytes": reserved,
            "remaining_bytes": remaining,
        }

    def _require_key_capacity(
        self,
        session: Session,
        *,
        key: AppKeyRecord,
        usage: KeyDownloadUsageRecord,
        expected_bytes: int,
        now: datetime,
    ) -> None:
        quota = key.monthly_download_quota_bytes
        if quota is None:
            return
        reserved = self._reserved_key_bytes(session, key_id=key.id)
        projected = usage.accounted_bytes + reserved + expected_bytes
        if projected > quota:
            remaining = max(0, quota - usage.accounted_bytes - reserved)
            raise DownloadAllowanceExceeded(
                f"application key {key.id} monthly download quota has {remaining} bytes "
                f"remaining; {expected_bytes} bytes were requested; resets at "
                f"{format_utc_timestamp(_next_month_start(now))}"
            )

    @staticmethod
    def _reserved_key_bytes(session: Session, *, key_id: str) -> int:
        return int(
            session.scalar(
                select(
                    func.coalesce(func.sum(KeyDownloadReservationRecord.reserved_bytes), 0)
                ).where(KeyDownloadReservationRecord.key_id == key_id)
            )
            or 0
        )

    @staticmethod
    def _reap_expired_key_reservations(
        session: Session,
        *,
        usage: KeyDownloadUsageRecord,
        now: datetime,
    ) -> None:
        now_text = format_utc_timestamp(now)
        expired_count, charged_bytes = session.execute(
            select(
                func.count(KeyDownloadReservationRecord.id),
                func.coalesce(
                    func.sum(
                        case(
                            (
                                KeyDownloadReservationRecord.kind == "stream",
                                KeyDownloadReservationRecord.reserved_bytes,
                            ),
                            else_=0,
                        )
                    ),
                    0,
                ),
            ).where(
                KeyDownloadReservationRecord.key_id == usage.key_id,
                KeyDownloadReservationRecord.expires_at <= now_text,
            )
        ).one()
        usage.accounted_bytes += int(charged_bytes)
        if expired_count:
            usage.updated_at = format_utc_timestamp(now)
            session.execute(
                delete(KeyDownloadReservationRecord).where(
                    KeyDownloadReservationRecord.key_id == usage.key_id,
                    KeyDownloadReservationRecord.expires_at <= now_text,
                )
            )

    def _key_lock(self, key_id: str) -> threading.Lock:
        with self._key_locks_guard:
            return self._key_locks.setdefault(key_id, threading.Lock())

    def _heartbeat(self, *, store: str, reservation_id: str) -> None:
        now = self._current_time()
        with self._locks[store]:
            with session_scope(self._session_factory) as session:
                reservation = session.get(ArchiveDownloadReservationRecord, reservation_id)
                if reservation is None:
                    return
                reservation.expires_at = format_utc_timestamp(now + _RESERVATION_LEASE)

    def _finish(
        self,
        *,
        store: str,
        reservation_id: str,
        transferred_bytes: int,
    ) -> None:
        now = self._current_time()
        now_text = format_utc_timestamp(now)
        with self._locks[store]:
            with session_scope(self._session_factory) as session:
                usage = self._locked_usage(session, store=store, now=now)
                reservation = session.get(ArchiveDownloadReservationRecord, reservation_id)
                if reservation is None:
                    return
                if reservation.month_started_at == usage.month_started_at:
                    accounted = transferred_bytes
                else:
                    accounted = max(transferred_bytes, reservation.reserved_bytes)
                usage.accounted_bytes += accounted
                usage.updated_at = now_text
                session.delete(reservation)

    def _status(self, policy: StorageAdapterRegistration) -> ArchiveDownloadAllowance:
        now = self._current_time()
        now_text = format_utc_timestamp(now)
        month_started_at = format_utc_timestamp(_month_start(now))
        with self._locks[policy.name]:
            with session_scope(self._session_factory) as session:
                usage = session.get(ArchiveDownloadUsageRecord, policy.name)
                accounted_bytes = (
                    usage.accounted_bytes
                    if usage is not None and usage.month_started_at == month_started_at
                    else 0
                )
                expired_bytes, reserved_bytes = session.execute(
                    select(
                        func.coalesce(
                            func.sum(
                                case(
                                    (
                                        ArchiveDownloadReservationRecord.expires_at <= now_text,
                                        ArchiveDownloadReservationRecord.reserved_bytes,
                                    ),
                                    else_=0,
                                )
                            ),
                            0,
                        ),
                        func.coalesce(
                            func.sum(
                                case(
                                    (
                                        ArchiveDownloadReservationRecord.expires_at > now_text,
                                        ArchiveDownloadReservationRecord.reserved_bytes,
                                    ),
                                    else_=0,
                                )
                            ),
                            0,
                        ),
                    ).where(
                        ArchiveDownloadReservationRecord.store == policy.name,
                        ArchiveDownloadReservationRecord.month_started_at == month_started_at,
                    )
                ).one()
                accounted_bytes += int(expired_bytes)
                reserved_bytes = int(reserved_bytes)
                effective_limit = _effective_limit(policy)
                remaining_bytes = max(
                    0,
                    effective_limit - accounted_bytes - reserved_bytes,
                )
                allowance = policy.monthly_download_allowance_bytes
                if allowance is None:
                    raise RuntimeError("download allowance policy is incomplete")
                return ArchiveDownloadAllowance(
                    store=policy.name,
                    state="open" if remaining_bytes > 0 else "closed",
                    month_started_at=month_started_at,
                    resets_at=format_utc_timestamp(_next_month_start(now)),
                    allowance_bytes=allowance,
                    safety_buffer_bytes=policy.download_safety_buffer_bytes,
                    effective_limit_bytes=effective_limit,
                    accounted_bytes=accounted_bytes,
                    reserved_bytes=reserved_bytes,
                    remaining_bytes=remaining_bytes,
                )

    def _locked_usage(
        self,
        session: Session,
        *,
        store: str,
        now: datetime,
    ) -> ArchiveDownloadUsageRecord:
        usage = session.scalar(
            select(ArchiveDownloadUsageRecord)
            .where(ArchiveDownloadUsageRecord.store == store)
            .with_for_update()
        )
        if usage is None:
            usage = ArchiveDownloadUsageRecord(
                store=store,
                month_started_at=format_utc_timestamp(_month_start(now)),
                accounted_bytes=0,
                updated_at=format_utc_timestamp(now),
            )
            session.add(usage)
            session.flush()
        current_month = format_utc_timestamp(_month_start(now))
        if usage.month_started_at != current_month:
            usage.month_started_at = current_month
            usage.accounted_bytes = 0
            usage.updated_at = format_utc_timestamp(now)
        return usage

    @staticmethod
    def _reap_expired(
        session: Session,
        *,
        usage: ArchiveDownloadUsageRecord,
        now: datetime,
    ) -> None:
        now_text = format_utc_timestamp(now)
        expired_count, charged_bytes = session.execute(
            select(
                func.count(ArchiveDownloadReservationRecord.id),
                func.coalesce(func.sum(ArchiveDownloadReservationRecord.reserved_bytes), 0),
            ).where(
                ArchiveDownloadReservationRecord.store == usage.store,
                ArchiveDownloadReservationRecord.expires_at <= now_text,
            )
        ).one()
        if not expired_count:
            return
        usage.accounted_bytes += int(charged_bytes)
        usage.updated_at = format_utc_timestamp(now)
        session.execute(
            delete(ArchiveDownloadReservationRecord).where(
                ArchiveDownloadReservationRecord.store == usage.store,
                ArchiveDownloadReservationRecord.expires_at <= now_text,
            )
        )
        session.flush()

    @staticmethod
    def _reserved_bytes(session: Session, *, store: str) -> int:
        return int(
            session.scalar(
                select(
                    func.coalesce(
                        func.sum(ArchiveDownloadReservationRecord.reserved_bytes),
                        0,
                    )
                ).where(ArchiveDownloadReservationRecord.store == store)
            )
            or 0
        )

    def _current_time(self) -> datetime:
        now = self._clock()
        if now.tzinfo is None:
            raise ValueError("download allowance clock must be timezone-aware")
        return now.astimezone(UTC)


def _key_quota_statements(
    *,
    now: datetime,
    q: str | None,
    sort: str,
    order: str,
    app: str | None,
    active: bool | None,
) -> tuple[str, str, str | None, str | None, Any, tuple[Any, ...]]:
    if sort not in _KEY_QUOTA_SORT_FIELDS:
        raise BadRequest(f"sort must be one of {', '.join(sorted(_KEY_QUOTA_SORT_FIELDS))}")
    if order not in _SORT_ORDERS:
        raise BadRequest("order must be asc or desc")
    now_text = format_utc_timestamp(now)
    current_month = format_utc_timestamp(_month_start(now))
    usage_accounted = (
        select(KeyDownloadUsageRecord.accounted_bytes)
        .where(
            KeyDownloadUsageRecord.key_id == AppKeyRecord.id,
            KeyDownloadUsageRecord.month_started_at == current_month,
        )
        .correlate(AppKeyRecord)
        .scalar_subquery()
    )
    expired_stream_accounted = (
        select(func.sum(KeyDownloadReservationRecord.reserved_bytes))
        .where(
            KeyDownloadReservationRecord.key_id == AppKeyRecord.id,
            KeyDownloadReservationRecord.kind == "stream",
            KeyDownloadReservationRecord.expires_at <= now_text,
        )
        .correlate(AppKeyRecord)
        .scalar_subquery()
    )
    active_reserved = (
        select(func.sum(KeyDownloadReservationRecord.reserved_bytes))
        .where(
            KeyDownloadReservationRecord.key_id == AppKeyRecord.id,
            KeyDownloadReservationRecord.expires_at > now_text,
        )
        .correlate(AppKeyRecord)
        .scalar_subquery()
    )
    accounted = func.coalesce(usage_accounted, 0) + func.coalesce(expired_stream_accounted, 0)
    reserved = func.coalesce(active_reserved, 0)
    remainder = AppKeyRecord.monthly_download_quota_bytes - accounted - reserved
    remaining = case(
        (AppKeyRecord.monthly_download_quota_bytes.is_(None), None),
        (remainder < 0, 0),
        else_=remainder,
    )
    columns = {
        "app": AppKeyRecord.app,
        "key_id": AppKeyRecord.id,
        "monthly_bytes": AppKeyRecord.monthly_download_quota_bytes,
        "accounted_bytes": accounted,
        "reserved_bytes": reserved,
        "remaining_bytes": remaining,
    }
    filters = []
    query = q.strip() if q is not None else None
    normalized_app = app.strip().casefold() if app is not None else None
    if normalized_app:
        filters.append(AppKeyRecord.app == normalized_app)
    if query:
        pattern = _like_pattern(query.casefold())
        filters.append(AppKeyRecord.search_text.like(pattern, escape="\\"))
    active_expression = and_(
        AppKeyRecord.revoked_at.is_(None),
        or_(AppKeyRecord.expires_at.is_(None), AppKeyRecord.expires_at > now_text),
    )
    if active is not None:
        filters.append(active_expression if active else ~active_expression)
    base = select(
        AppKeyRecord.id.label("id"),
        AppKeyRecord.app.label("app"),
        AppKeyRecord.id.label("key_id"),
        AppKeyRecord.monthly_download_quota_bytes.label("monthly_bytes"),
        accounted.label("accounted_bytes"),
        reserved.label("reserved_bytes"),
        remaining.label("remaining_bytes"),
        AppKeyRecord.expires_at.label("expires_at"),
        AppKeyRecord.revoked_at.label("revoked_at"),
    ).where(*filters)
    sort_column = columns[sort]
    if sort in {"monthly_bytes", "remaining_bytes"}:
        sort_column = func.coalesce(sort_column, -1)
    key_columns = (AppKeyRecord.id,) if sort == "key_id" else (sort_column, AppKeyRecord.id)
    return now_text, current_month, query, normalized_app, base, key_columns


def _key_quota_position(row: Any, *, sort: str) -> tuple[str | int, ...]:
    value = row[sort]
    if value is None:
        value = -1
    if not isinstance(value, (str, int)) or isinstance(value, bool):
        raise RuntimeError("download-quota browse position has an invalid value")
    key_id = row["key_id"]
    if not isinstance(key_id, str):
        raise RuntimeError("download-quota browse key has an invalid identifier")
    return (value,) if sort == "key_id" else (value, key_id)


def _key_quota_row(
    row: Mapping[str, object],
    *,
    now: datetime,
    now_text: str,
    current_month: str,
) -> dict[str, object]:
    payload = dict(row)
    payload["key_status"] = _key_status(payload, now_text=now_text)
    payload.pop("expires_at", None)
    payload.pop("revoked_at", None)
    payload["month_started_at"] = current_month
    payload["resets_at"] = format_utc_timestamp(_next_month_start(now))
    return payload


def _effective_limit(policy: StorageAdapterRegistration) -> int:
    allowance = policy.monthly_download_allowance_bytes
    if allowance is None:
        raise RuntimeError("download allowance policy is incomplete")
    return allowance - policy.download_safety_buffer_bytes


def _month_start(value: datetime) -> datetime:
    return datetime(value.year, value.month, 1, tzinfo=UTC)


def _next_month_start(value: datetime) -> datetime:
    if value.month == 12:
        return datetime(value.year + 1, 1, 1, tzinfo=UTC)
    return datetime(value.year, value.month + 1, 1, tzinfo=UTC)


def _job_reservation_id(job_id: str) -> str:
    return f"job:{job_id}"


def _like_pattern(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def _key_status(row: object, *, now_text: str) -> str:
    if isinstance(row, dict):
        revoked_at = row.get("revoked_at")
        expires_at = row.get("expires_at")
    else:
        revoked_at = getattr(row, "revoked_at", None)
        expires_at = getattr(row, "expires_at", None)
    if revoked_at is not None:
        return "revoked"
    if expires_at is not None and str(expires_at) <= now_text:
        return "expired"
    return "active"
