from __future__ import annotations

import json
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import asdict
from datetime import datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import cast

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from arc_core.archive_compliance import (
    copy_counts_toward_protection,
    normalize_copy_state,
    normalize_glacier_state,
)
from arc_core.catalog_models import (
    FinalizedImageRecord,
    GlacierRecoverySessionImageRecord,
    GlacierRecoverySessionRecord,
    ImageCopyRecord,
)
from arc_core.domain.enums import CopyState, GlacierState, RecoverySessionState
from arc_core.domain.errors import Conflict, InvalidState, NotFound
from arc_core.domain.models import (
    GlacierArchiveStatus,
    RecoveryCostEstimate,
    RecoveryNotificationStatus,
    RecoverySessionImage,
    RecoverySessionSummary,
)
from arc_core.domain.types import ImageId
from arc_core.ports.archive_store import ArchiveRestoreStatus, ArchiveStore
from arc_core.runtime_config import RuntimeConfig
from arc_core.services.glacier_pricing import resolve_glacier_pricing
from arc_core.sqlite_db import make_session_factory, session_scope
from arc_core.webhooks import (
    WebhookConfig,
    build_recovery_ready_payload,
    post_webhook,
    utcnow,
)

_ACTIVE_RECOVERY_STATES = {
    RecoverySessionState.PENDING_APPROVAL.value,
    RecoverySessionState.RESTORE_REQUESTED.value,
    RecoverySessionState.READY.value,
}


class SqlAlchemyRecoverySessionService:
    def __init__(self, config: RuntimeConfig, archive_store: ArchiveStore) -> None:
        self._config = config
        self._archive_store = archive_store
        self._session_factory = make_session_factory(str(config.sqlite_path))

    def get(self, session_id: str) -> RecoverySessionSummary:
        with session_scope(self._session_factory) as session:
            record = session.get(GlacierRecoverySessionRecord, session_id)
            if record is None:
                raise NotFound(f"recovery session not found: {session_id}")
            return _session_summary(session, record, config=self._config)

    def get_for_image(self, image_id: str) -> RecoverySessionSummary:
        with session_scope(self._session_factory) as session:
            record = _latest_session_for_image(session, image_id)
            if record is None:
                raise NotFound(f"recovery session not found for image: {image_id}")
            return _session_summary(session, record, config=self._config)

    def create_or_resume_for_image(self, image_id: str) -> RecoverySessionSummary:
        with session_scope(self._session_factory) as session:
            image = _require_image(session, image_id)
            active = _active_session_for_image(session, image_id)
            if active is not None:
                return _session_summary(session, active, config=self._config)
            _require_glacier_uploaded(image)
            if _protected_copy_count(session, image_id) > 0:
                raise Conflict(
                    "image still has protected copies and does not require "
                    f"archive recovery: {image_id}"
                )
            reusable = _reusable_pending_approval_session(session)
            if reusable is not None:
                attached = _attach_image_to_session(
                    session,
                    record=reusable,
                    image=image,
                    config=self._config,
                )
                return _session_summary(session, attached, config=self._config)
            created = _create_recovery_session(session, config=self._config, image=image)
            return _session_summary(session, created, config=self._config)

    def approve(self, session_id: str) -> RecoverySessionSummary:
        current = utcnow()
        now = _isoformat_z(current)
        estimated_ready_at = _isoformat_z(current + self._config.glacier_recovery_restore_latency)
        with session_scope(self._session_factory) as session:
            record = session.get(GlacierRecoverySessionRecord, session_id)
            if record is None:
                raise NotFound(f"recovery session not found: {session_id}")
            if record.state == RecoverySessionState.EXPIRED.value:
                raise InvalidState(
                    "recovery session expired; re-initiate recovery to request restore"
                )
            if record.state != RecoverySessionState.PENDING_APPROVAL.value:
                raise InvalidState("recovery session is not waiting for approval")
            statuses = [
                self._archive_store.request_finalized_image_restore(
                    image_id=image.image_id,
                    object_path=_require_archive_object_path(image),
                    retrieval_tier=record.retrieval_tier,
                    hold_days=record.hold_days,
                    requested_at=now,
                    estimated_ready_at=estimated_ready_at,
                )
                for image in _session_images(session, record=record)
            ]
            record.state = RecoverySessionState.RESTORE_REQUESTED.value
            record.approved_at = now
            record.restore_requested_at = now
            record.restore_ready_at = _max_timestamp(
                status.ready_at for status in statuses if status.ready_at is not None
            ) or estimated_ready_at
            record.restore_expires_at = _min_timestamp(
                status.expires_at for status in statuses if status.expires_at is not None
            )
            record.restore_next_poll_at = _isoformat_z(
                current + self._config.glacier_recovery_sweep_interval
            )
            record.latest_message = (
                "Archive restore requested; wait for the ready notification before downloading or "
                "burning replacement media."
            )
            return _session_summary(session, record, config=self._config)

    def complete(self, session_id: str) -> RecoverySessionSummary:
        now = _isoformat_z(utcnow())
        with session_scope(self._session_factory) as session:
            record = session.get(GlacierRecoverySessionRecord, session_id)
            if record is None:
                raise NotFound(f"recovery session not found: {session_id}")
            if record.state not in {
                RecoverySessionState.READY.value,
                RecoverySessionState.EXPIRED.value,
            }:
                raise InvalidState("recovery session is not ready to complete")
            for image in _session_images(session, record=record):
                self._archive_store.cleanup_finalized_image_restore(
                    image_id=image.image_id,
                    object_path=_require_archive_object_path(image),
                )
            record.state = RecoverySessionState.COMPLETED.value
            record.completed_at = now
            record.next_reminder_at = None
            record.restore_expires_at = now
            record.latest_message = (
                "Recovery session completed and restored ISO cleanup was recorded."
            )
            return _session_summary(session, record, config=self._config)

    def iter_restored_iso(self, session_id: str, image_id: str) -> Iterator[bytes]:
        with session_scope(self._session_factory) as session:
            record = session.get(GlacierRecoverySessionRecord, session_id)
            if record is None:
                raise NotFound(f"recovery session not found: {session_id}")
            if record.state != RecoverySessionState.READY.value:
                raise InvalidState("recovery session is not ready for ISO download")
            images = {
                image.image_id: image
                for image in _session_images(session, record=record)
            }
            image = images.get(image_id)
            if image is None:
                raise NotFound(f"image not found in recovery session: {image_id}")
            object_path = _require_archive_object_path(image)
        yield from self._archive_store.iter_restored_finalized_image(
            image_id=image_id,
            object_path=object_path,
        )

    def process_due_sessions(self, *, limit: int = 100) -> int:
        if limit < 1:
            return 0

        current = utcnow()
        current_text = _isoformat_z(current)
        processed = 0
        with session_scope(self._session_factory) as session:
            due_ids = session.scalars(
                select(GlacierRecoverySessionRecord.session_id)
                .where(
                    or_(
                        (
                            (GlacierRecoverySessionRecord.state
                             == RecoverySessionState.RESTORE_REQUESTED.value)
                            & (
                                (
                                    GlacierRecoverySessionRecord.restore_next_poll_at.is_(None)
                                )
                                | (
                                    GlacierRecoverySessionRecord.restore_next_poll_at
                                    <= current_text
                                )
                                | (
                                    GlacierRecoverySessionRecord.restore_ready_at <= current_text
                                )
                            )
                        ),
                        (
                            (GlacierRecoverySessionRecord.state == RecoverySessionState.READY.value)
                            & (
                                (
                                    GlacierRecoverySessionRecord.restore_expires_at.is_not(None)
                                )
                                & (GlacierRecoverySessionRecord.restore_expires_at <= current_text)
                            )
                        ),
                        (
                            (GlacierRecoverySessionRecord.state == RecoverySessionState.READY.value)
                            & (
                                (
                                    GlacierRecoverySessionRecord.next_reminder_at.is_not(None)
                                )
                                & (GlacierRecoverySessionRecord.next_reminder_at <= current_text)
                            )
                        ),
                    )
                )
                .order_by(
                    GlacierRecoverySessionRecord.created_at,
                    GlacierRecoverySessionRecord.session_id,
                )
                .limit(limit)
            ).all()

        for session_id in due_ids:
            self._process_one(session_id=session_id)
            processed += 1
        return processed

    def _process_one(self, *, session_id: str) -> None:
        current = utcnow()
        current_text = _isoformat_z(current)
        with session_scope(self._session_factory) as session:
            record = session.get(GlacierRecoverySessionRecord, session_id)
            if record is None:
                return

            if (
                record.state == RecoverySessionState.RESTORE_REQUESTED.value
            ):
                status = self._session_restore_status(session, record=record, current=current)
                if status.state == "ready":
                    record.state = RecoverySessionState.READY.value
                    record.restore_ready_at = status.ready_at or current_text
                    record.restore_expires_at = status.expires_at or _isoformat_z(
                        current + self._config.glacier_recovery_ready_ttl
                    )
                    record.restore_next_poll_at = None
                    record.latest_message = (
                        "Restored ISO data is ready; reopen the session to complete download, "
                        "verify the ISO, and burn replacement media before cleanup."
                    )
                    _notify_recovery_ready(
                        session,
                        record=record,
                        config=self._config,
                        current=current,
                        reminder=False,
                    )
                    return
                if status.state == "expired":
                    record.state = RecoverySessionState.EXPIRED.value
                    record.next_reminder_at = None
                    record.restore_next_poll_at = None
                    record.latest_message = (
                        "Restored ISO data expired and cleanup was recorded; re-initiate "
                        "recovery to request a new restore."
                    )
                    return
                record.restore_next_poll_at = _isoformat_z(
                    current + self._config.glacier_recovery_sweep_interval
                )
                record.latest_message = (
                    status.message
                    or "Archive restore is still in progress; Riverhog will poll again."
                )
                return

            if (
                record.state == RecoverySessionState.READY.value
                and record.next_reminder_at is not None
                and record.next_reminder_at <= current_text
            ):
                initial_notification_succeeded = record.last_notified_at is not None
                _notify_recovery_ready(
                    session,
                    record=record,
                    config=self._config,
                    current=current,
                    reminder=initial_notification_succeeded,
                )
                return

            if (
                record.state == RecoverySessionState.READY.value
                and record.restore_expires_at is not None
                and record.restore_expires_at <= current_text
            ):
                for image in _session_images(session, record=record):
                    self._archive_store.cleanup_finalized_image_restore(
                        image_id=image.image_id,
                        object_path=_require_archive_object_path(image),
                    )
                record.state = RecoverySessionState.EXPIRED.value
                record.next_reminder_at = None
                record.restore_next_poll_at = None
                record.latest_message = (
                    "Restored ISO data expired and cleanup was recorded; re-initiate recovery to "
                    "request a new restore."
                )

    def _session_restore_status(
        self,
        session: Session,
        *,
        record: GlacierRecoverySessionRecord,
        current: datetime,
    ) -> ArchiveRestoreStatus:
        statuses = [
            self._archive_store.get_finalized_image_restore_status(
                image_id=image.image_id,
                object_path=_require_archive_object_path(image),
                requested_at=record.restore_requested_at or _isoformat_z(current),
                estimated_ready_at=record.restore_ready_at,
                estimated_expires_at=record.restore_expires_at,
            )
            for image in _session_images(session, record=record)
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
            message="Archive restore is still in progress; Riverhog will poll again.",
        )


def ensure_glacier_recovery_session_for_image(
    session: Session,
    *,
    config: RuntimeConfig,
    image_id: str,
) -> None:
    image = session.get(FinalizedImageRecord, image_id)
    if image is None:
        return
    if normalize_glacier_state(image.glacier_state) != GlacierState.UPLOADED:
        return
    if _protected_copy_count(session, image_id) > 0:
        return
    if not _has_recovery_triggering_copy_history(session, image_id):
        return
    if _active_session_for_image(session, image_id) is not None:
        return
    reusable = _reusable_pending_approval_session(session)
    if reusable is not None:
        _attach_image_to_session(session, record=reusable, image=image, config=config)
        return
    _create_recovery_session(session, config=config, image=image)


class StubRecoverySessionService:
    def get(self, session_id: str) -> RecoverySessionSummary:
        raise NotImplementedError("StubRecoverySessionService is not implemented yet")

    def get_for_image(self, image_id: str) -> RecoverySessionSummary:
        raise NotImplementedError("StubRecoverySessionService is not implemented yet")

    def create_or_resume_for_image(self, image_id: str) -> RecoverySessionSummary:
        raise NotImplementedError("StubRecoverySessionService is not implemented yet")

    def approve(self, session_id: str) -> RecoverySessionSummary:
        raise NotImplementedError("StubRecoverySessionService is not implemented yet")

    def complete(self, session_id: str) -> RecoverySessionSummary:
        raise NotImplementedError("StubRecoverySessionService is not implemented yet")

    def iter_restored_iso(self, session_id: str, image_id: str) -> Iterator[bytes]:
        raise NotImplementedError("StubRecoverySessionService is not implemented yet")

    def process_due_sessions(self, *, limit: int = 100) -> int:
        raise NotImplementedError("StubRecoverySessionService is not implemented yet")


def _create_recovery_session(
    session: Session,
    *,
    config: RuntimeConfig,
    image: FinalizedImageRecord,
) -> GlacierRecoverySessionRecord:
    existing_ids = session.scalars(
        select(GlacierRecoverySessionRecord.session_id)
        .join(
            GlacierRecoverySessionImageRecord,
            GlacierRecoverySessionImageRecord.session_id == GlacierRecoverySessionRecord.session_id,
        )
        .where(GlacierRecoverySessionImageRecord.image_id == image.image_id)
    ).all()
    session_id = _generated_recovery_session_id(image.image_id, existing_ids=existing_ids)
    created_at = _isoformat_z(utcnow())
    estimate = _estimate_recovery_costs(config=config, images=(image,))
    warnings = _build_warnings(config=config)
    record = GlacierRecoverySessionRecord(
        session_id=session_id,
        state=RecoverySessionState.PENDING_APPROVAL.value,
        created_at=created_at,
        approved_at=None,
        restore_requested_at=None,
        restore_ready_at=None,
        restore_next_poll_at=None,
        restore_expires_at=None,
        completed_at=None,
        latest_message=(
            "Approve the estimated restore cost before Riverhog requests archive restore."
        ),
        retrieval_tier=config.glacier_recovery_retrieval_tier,
        hold_days=max(int(config.glacier_recovery_ready_ttl.total_seconds() // 86400), 1),
        estimate_json=json.dumps(asdict(estimate), sort_keys=True),
        warnings_json=json.dumps(list(warnings)),
        reminder_count=0,
        next_reminder_at=None,
        last_notified_at=None,
    )
    session.add(record)
    session.flush()
    session.add(
        GlacierRecoverySessionImageRecord(
            session_id=session_id,
            image_id=image.image_id,
            image_order=0,
        )
    )
    session.flush()
    return record


def _attach_image_to_session(
    session: Session,
    *,
    record: GlacierRecoverySessionRecord,
    image: FinalizedImageRecord,
    config: RuntimeConfig,
) -> GlacierRecoverySessionRecord:
    existing_image_ids = {
        row.image_id
        for row in session.scalars(
            select(GlacierRecoverySessionImageRecord).where(
                GlacierRecoverySessionImageRecord.session_id == record.session_id
            )
        ).all()
    }
    if image.image_id in existing_image_ids:
        return record
    next_order = len(existing_image_ids)
    session.add(
        GlacierRecoverySessionImageRecord(
            session_id=record.session_id,
            image_id=image.image_id,
            image_order=next_order,
        )
    )
    session.flush()
    _refresh_recovery_session_metadata(session, record=record, config=config)
    return record


def _require_image(session: Session, image_id: str) -> FinalizedImageRecord:
    image = cast(FinalizedImageRecord | None, session.get(FinalizedImageRecord, image_id))
    if image is None:
        raise NotFound(f"image not found: {image_id}")
    return image


def _require_glacier_uploaded(image: FinalizedImageRecord) -> None:
    if normalize_glacier_state(image.glacier_state) != GlacierState.UPLOADED:
        raise InvalidState(
            f"image archive is not uploaded and cannot be restored yet: {image.image_id}"
        )


def _require_archive_object_path(image: FinalizedImageRecord) -> str:
    if not image.glacier_object_path:
        raise InvalidState(
            f"image archive object path is missing and cannot be restored: {image.image_id}"
        )
    return image.glacier_object_path


def _protected_copy_count(session: Session, image_id: str) -> int:
    rows = session.scalars(
        select(ImageCopyRecord.state).where(ImageCopyRecord.image_id == image_id)
    ).all()
    return sum(1 for state in rows if copy_counts_toward_protection(state))


def _has_recovery_triggering_copy_history(session: Session, image_id: str) -> bool:
    rows = session.scalars(
        select(ImageCopyRecord.state).where(ImageCopyRecord.image_id == image_id)
    ).all()
    return any(
        normalize_copy_state(state) not in {CopyState.NEEDED, CopyState.BURNING}
        for state in rows
    )


def _active_session_for_image(
    session: Session,
    image_id: str,
) -> GlacierRecoverySessionRecord | None:
    return cast(
        GlacierRecoverySessionRecord | None,
        session.scalar(
        select(GlacierRecoverySessionRecord)
        .join(
            GlacierRecoverySessionImageRecord,
            GlacierRecoverySessionImageRecord.session_id == GlacierRecoverySessionRecord.session_id,
        )
        .where(GlacierRecoverySessionImageRecord.image_id == image_id)
        .where(GlacierRecoverySessionRecord.state.in_(_ACTIVE_RECOVERY_STATES))
        .order_by(GlacierRecoverySessionRecord.created_at.desc())
        .limit(1)
    )
    )


def _latest_session_for_image(
    session: Session,
    image_id: str,
) -> GlacierRecoverySessionRecord | None:
    return cast(
        GlacierRecoverySessionRecord | None,
        session.scalar(
        select(GlacierRecoverySessionRecord)
        .join(
            GlacierRecoverySessionImageRecord,
            GlacierRecoverySessionImageRecord.session_id == GlacierRecoverySessionRecord.session_id,
        )
        .where(GlacierRecoverySessionImageRecord.image_id == image_id)
        .order_by(GlacierRecoverySessionRecord.created_at.desc())
        .limit(1)
    )
    )


def _reusable_pending_approval_session(session: Session) -> GlacierRecoverySessionRecord | None:
    return cast(
        GlacierRecoverySessionRecord | None,
        session.scalar(
        select(GlacierRecoverySessionRecord)
        .where(GlacierRecoverySessionRecord.state == RecoverySessionState.PENDING_APPROVAL.value)
        .order_by(GlacierRecoverySessionRecord.created_at.desc())
        .limit(1)
    )
    )


def _session_images(
    session: Session,
    *,
    record: GlacierRecoverySessionRecord,
) -> list[FinalizedImageRecord]:
    image_rows = session.scalars(
        select(GlacierRecoverySessionImageRecord)
        .where(GlacierRecoverySessionImageRecord.session_id == record.session_id)
        .order_by(GlacierRecoverySessionImageRecord.image_order)
    ).all()
    return [_require_image(session, image_row.image_id) for image_row in image_rows]


def _session_summary(
    session: Session,
    record: GlacierRecoverySessionRecord,
    *,
    config: RuntimeConfig,
) -> RecoverySessionSummary:
    images: list[RecoverySessionImage] = []
    for image in _session_images(session, record=record):
        images.append(
            RecoverySessionImage(
                id=ImageId(image.image_id),
                filename=image.filename,
                glacier=GlacierArchiveStatus(
                    state=normalize_glacier_state(image.glacier_state),
                    object_path=image.glacier_object_path,
                    stored_bytes=image.glacier_stored_bytes,
                    backend=image.glacier_backend,
                    storage_class=image.glacier_storage_class,
                    last_uploaded_at=image.glacier_last_uploaded_at,
                    last_verified_at=image.glacier_last_verified_at,
                    failure=image.glacier_failure,
                ),
                stored_bytes=int(image.glacier_stored_bytes or image.bytes),
            )
        )
    estimate = RecoveryCostEstimate(**json.loads(record.estimate_json))
    notification = RecoveryNotificationStatus(
        webhook_configured=bool(config.glacier_recovery_webhook_url),
        reminder_count=record.reminder_count,
        next_reminder_at=record.next_reminder_at,
        last_notified_at=record.last_notified_at,
    )
    warnings = tuple(str(item) for item in json.loads(record.warnings_json))
    return RecoverySessionSummary(
        id=record.session_id,
        state=RecoverySessionState(record.state),
        created_at=record.created_at,
        approved_at=record.approved_at,
        restore_requested_at=record.restore_requested_at,
        restore_ready_at=record.restore_ready_at,
        restore_expires_at=record.restore_expires_at,
        completed_at=record.completed_at,
        latest_message=record.latest_message,
        warnings=warnings,
        cost_estimate=estimate,
        notification=notification,
        images=tuple(images),
    )


def _refresh_recovery_session_metadata(
    session: Session,
    *,
    record: GlacierRecoverySessionRecord,
    config: RuntimeConfig,
) -> None:
    images = _session_images(session, record=record)
    estimate = _estimate_recovery_costs(config=config, images=images)
    record.estimate_json = json.dumps(asdict(estimate), sort_keys=True)
    record.warnings_json = json.dumps(list(_build_warnings(config=config)))
    record.hold_days = max(int(config.glacier_recovery_ready_ttl.total_seconds() // 86400), 1)
    record.retrieval_tier = config.glacier_recovery_retrieval_tier


def _estimate_recovery_costs(
    *,
    config: RuntimeConfig,
    images: Iterable[FinalizedImageRecord],
) -> RecoveryCostEstimate:
    pricing_basis = resolve_glacier_pricing(config)
    image_list = list(images)
    total_bytes = sum(int(image.glacier_stored_bytes or image.bytes) for image in image_list)
    total_gib = Decimal(total_bytes) / Decimal(1024**3)
    hold_days = max(int(config.glacier_recovery_ready_ttl.total_seconds() // 86400), 1)
    retrieval_rate, request_rate = _retrieval_rates(config)
    retrieval_cost = _usd(total_gib * Decimal(str(retrieval_rate)))
    restore_request_count = max(len(image_list), 1)
    request_fees = _usd(
        Decimal("0.001") * Decimal(str(request_rate)) * Decimal(restore_request_count)
    )
    temporary_storage_cost = _usd(
        total_gib
        * Decimal(str(pricing_basis.standard_storage_rate_usd_per_gib_month))
        * Decimal(hold_days)
        / Decimal(30)
    )
    return RecoveryCostEstimate(
        currency_code=pricing_basis.currency_code or "USD",
        retrieval_tier=config.glacier_recovery_retrieval_tier,
        hold_days=hold_days,
        image_count=len(image_list),
        total_bytes=total_bytes,
        restore_request_count=restore_request_count,
        retrieval_rate_usd_per_gib=retrieval_rate,
        request_rate_usd_per_1000=request_rate,
        standard_storage_rate_usd_per_gib_month=(
            pricing_basis.standard_storage_rate_usd_per_gib_month
        ),
        retrieval_cost_usd=float(retrieval_cost),
        request_fees_usd=float(request_fees),
        temporary_storage_cost_usd=float(temporary_storage_cost),
        total_estimated_cost_usd=float(
            _usd(retrieval_cost + request_fees + temporary_storage_cost)
        ),
        assumptions=(
            "Excludes network egress or operator-local media costs.",
            "Uses the configured ready-to-download cleanup window.",
            "Assumes one archive restore request per image.",
        ),
    )


def _build_warnings(config: RuntimeConfig) -> tuple[str, ...]:
    restore_latency = _format_timedelta(config.glacier_recovery_restore_latency)
    cleanup_window = _format_timedelta(config.glacier_recovery_ready_ttl)
    reminder = (
        "Riverhog will notify and remind the operator through the configured recovery webhook "
        "while restored ISO data is ready."
        if config.glacier_recovery_webhook_url
        else "No recovery webhook URL is configured; operators must poll the recovery session "
        "manually for readiness."
    )
    return (
        "Archive restore requests take time; the configured restore latency estimate "
        f"is {restore_latency}.",
        reminder,
        "Restored ISO data will be cleaned up after "
        f"{cleanup_window} if recovery is not completed sooner.",
    )


def _notify_recovery_ready(
    session: Session,
    *,
    record: GlacierRecoverySessionRecord,
    config: RuntimeConfig,
    current: datetime,
    reminder: bool,
) -> None:
    if not config.glacier_recovery_webhook_url:
        record.next_reminder_at = None
        return
    try:
        payload = build_recovery_ready_payload(
            config=_webhook_config(config),
            session_id=record.session_id,
            restore_expires_at=record.restore_expires_at,
            images=[
                {
                    "image_id": image.image_id,
                    "filename": _require_image(session, image.image_id).filename,
                }
                for image in session.scalars(
                    select(GlacierRecoverySessionImageRecord).where(
                        GlacierRecoverySessionImageRecord.session_id == record.session_id
                    )
                ).all()
            ],
            delivered_at=current,
            reminder_count=record.reminder_count,
            reminder=reminder,
        )
        post_webhook(
            config=_webhook_config(config),
            payload=payload,
        )
    except Exception as exc:
        record.latest_message = (
            "Ready notification failed and will retry: "
            f"{str(exc).strip() or exc.__class__.__name__}"
        )
        record.next_reminder_at = _isoformat_z(
            current + config.glacier_recovery_webhook_retry_delay
        )
        return

    record.last_notified_at = _isoformat_z(current)
    if reminder:
        record.reminder_count += 1
    interval = config.glacier_recovery_webhook_reminder_interval
    if interval.total_seconds() > 0:
        record.next_reminder_at = _isoformat_z(current + interval)
    else:
        record.next_reminder_at = None


def _webhook_config(config: RuntimeConfig) -> WebhookConfig:
    return WebhookConfig(
        url=config.glacier_recovery_webhook_url or "",
        base_url=config.public_base_url or "",
        timeout_seconds=config.glacier_recovery_webhook_timeout.total_seconds(),
        retry_seconds=config.glacier_recovery_webhook_retry_delay.total_seconds(),
        reminder_interval_seconds=config.glacier_recovery_webhook_reminder_interval.total_seconds(),
    )


def _generated_recovery_session_id(image_id: str, *, existing_ids: Sequence[str]) -> str:
    existing = set(existing_ids)
    ordinal = 1
    while True:
        candidate = f"rs-{image_id}-{ordinal}"
        ordinal += 1
        if candidate not in existing:
            return candidate


def _retrieval_rates(config: RuntimeConfig) -> tuple[float, float]:
    if config.glacier_recovery_retrieval_tier == "standard":
        return (
            config.glacier_standard_retrieval_rate_usd_per_gib,
            config.glacier_standard_request_rate_usd_per_1000,
        )
    return (
        config.glacier_bulk_retrieval_rate_usd_per_gib,
        config.glacier_bulk_request_rate_usd_per_1000,
    )


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


def _isoformat_z(value: datetime) -> str:
    return value.strftime("%Y-%m-%dT%H:%M:%SZ")


def _usd(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP)


def _max_timestamp(values: Iterable[str]) -> str | None:
    value_list = list(values)
    return max(value_list) if value_list else None


def _min_timestamp(values: Iterable[str]) -> str | None:
    value_list = list(values)
    return min(value_list) if value_list else None
