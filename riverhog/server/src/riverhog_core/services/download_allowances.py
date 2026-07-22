from __future__ import annotations

import secrets
import threading
import time
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta

from riverhog_protocol.errors import DownloadAllowanceExceeded
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from time_formats import format_utc_timestamp, utc_now

from riverhog_core.catalog_db import make_session_factory, session_scope
from riverhog_core.catalog_models import (
    ArchiveDownloadReservationRecord,
    ArchiveDownloadUsageRecord,
)
from riverhog_core.domain.models import ArchiveDownloadAllowance
from riverhog_core.runtime_config import ArchiveStoreConfig, RuntimeConfig

_RESERVATION_LEASE = timedelta(hours=1)
_RESERVATION_HEARTBEAT_SECONDS = 5 * 60


class SqlAlchemyDownloadAllowance:
    def __init__(
        self,
        config: RuntimeConfig,
        *,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self._session_factory = make_session_factory(config.database_url)
        self._clock = clock
        self._policies = {
            name: store
            for name, store in config.archive_stores.items()
            if store.monthly_download_allowance_bytes is not None
        }
        self._locks = {name: threading.Lock() for name in self._policies}
        self._ensure_usage_rows()

    def track(
        self,
        *,
        store: str,
        expected_bytes: int,
        content: Iterator[bytes],
    ) -> Iterator[bytes]:
        policy = self._policies.get(store)
        if policy is None:
            return content
        if expected_bytes < 0:
            raise ValueError("expected download bytes must not be negative")
        reservation_id = self._reserve(policy=policy, expected_bytes=expected_bytes)
        return self._tracked_content(
            store=store,
            reservation_id=reservation_id,
            content=content,
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

    def _reserve(self, *, policy: ArchiveStoreConfig, expected_bytes: int) -> str:
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
        reservation_id: str,
        content: Iterator[bytes],
    ) -> Iterator[bytes]:
        transferred_bytes = 0
        next_heartbeat = time.monotonic() + _RESERVATION_HEARTBEAT_SECONDS
        try:
            for chunk in content:
                transferred_bytes += len(chunk)
                if time.monotonic() >= next_heartbeat:
                    self._heartbeat(store=store, reservation_id=reservation_id)
                    next_heartbeat = time.monotonic() + _RESERVATION_HEARTBEAT_SECONDS
                yield chunk
        finally:
            self._finish(
                store=store,
                reservation_id=reservation_id,
                transferred_bytes=transferred_bytes,
            )

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

    def _status(self, policy: ArchiveStoreConfig) -> ArchiveDownloadAllowance:
        now = self._current_time()
        with self._locks[policy.name]:
            with session_scope(self._session_factory) as session:
                usage = self._locked_usage(session, store=policy.name, now=now)
                self._reap_expired(session, usage=usage, now=now)
                reserved_bytes = self._reserved_bytes(session, store=policy.name)
                effective_limit = _effective_limit(policy)
                remaining_bytes = max(
                    0,
                    effective_limit - usage.accounted_bytes - reserved_bytes,
                )
                allowance = policy.monthly_download_allowance_bytes
                if allowance is None:
                    raise RuntimeError("download allowance policy is incomplete")
                return ArchiveDownloadAllowance(
                    store=policy.name,
                    state="open" if remaining_bytes > 0 else "closed",
                    month_started_at=usage.month_started_at,
                    resets_at=format_utc_timestamp(_next_month_start(now)),
                    allowance_bytes=allowance,
                    safety_buffer_bytes=policy.download_safety_buffer_bytes,
                    effective_limit_bytes=effective_limit,
                    accounted_bytes=usage.accounted_bytes,
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
        expired = list(
            session.scalars(
                select(ArchiveDownloadReservationRecord).where(
                    ArchiveDownloadReservationRecord.store == usage.store,
                    ArchiveDownloadReservationRecord.expires_at <= format_utc_timestamp(now),
                )
            )
        )
        if not expired:
            return
        usage.accounted_bytes += sum(current.reserved_bytes for current in expired)
        usage.updated_at = format_utc_timestamp(now)
        for current in expired:
            session.delete(current)
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


def _effective_limit(policy: ArchiveStoreConfig) -> int:
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
