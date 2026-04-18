from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from .config import (
    API_BASE_URL,
    DISC_WEBHOOK_DISPATCH_INTERVAL_SECONDS,
    DISC_WEBHOOK_RETRY_SECONDS,
    DISC_WEBHOOK_TIMEOUT_SECONDS,
)
from .db import SessionLocal
from .models import Disc, DiscFinalizationNotification, DiscFinalizationWebhookSubscription

logger = logging.getLogger(__name__)

DISC_FINALIZED_EVENT = "disc.finalized"
DISC_FINALIZED_REMINDER_EVENT = "disc.finalized.reminder"


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def isoformat_z(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def disc_iso_download_path(disc_id: str) -> str:
    return f"/v1/discs/{disc_id}/iso/content"


def disc_iso_download_url(disc_id: str) -> str:
    return f"{API_BASE_URL}{disc_iso_download_path(disc_id)}"


def disc_iso_create_url(disc_id: str) -> str:
    return f"{API_BASE_URL}/v1/discs/{disc_id}/iso/create"


def _iso_available(disc: Disc) -> bool:
    return bool(disc.iso_abs_path and Path(disc.iso_abs_path).exists())


def create_disc_finalization_notifications_for_disc(session, disc_id: str) -> int:
    disc = session.get(Disc, disc_id)
    if disc is None or disc.burn_confirmed_at is not None:
        return 0

    subscriptions = session.execute(
        select(DiscFinalizationWebhookSubscription).where(
            DiscFinalizationWebhookSubscription.active.is_(True)
        )
    ).scalars().all()
    created = 0
    now = utcnow()
    for subscription in subscriptions:
        existing = session.execute(
            select(DiscFinalizationNotification).where(
                DiscFinalizationNotification.subscription_id == subscription.id,
                DiscFinalizationNotification.disc_id == disc_id,
            )
        ).scalar_one_or_none()
        if existing is not None:
            continue
        session.add(
            DiscFinalizationNotification(
                subscription_id=subscription.id,
                disc_id=disc_id,
                status="pending",
                next_attempt_at=now,
            )
        )
        created += 1
    return created


def backfill_disc_finalization_notifications_for_subscription(session, subscription_id: str) -> int:
    disc_ids = session.execute(
        select(Disc.id).where(Disc.burn_confirmed_at.is_(None)).order_by(Disc.created_at.asc())
    ).scalars().all()
    created = 0
    now = utcnow()
    for disc_id in disc_ids:
        existing = session.execute(
            select(DiscFinalizationNotification).where(
                DiscFinalizationNotification.subscription_id == subscription_id,
                DiscFinalizationNotification.disc_id == disc_id,
            )
        ).scalar_one_or_none()
        if existing is not None:
            continue
        session.add(
            DiscFinalizationNotification(
                subscription_id=subscription_id,
                disc_id=disc_id,
                status="pending",
                next_attempt_at=now,
            )
        )
        created += 1
    return created


def complete_disc_finalization_notifications(session, disc_id: str) -> int:
    notifications = session.execute(
        select(DiscFinalizationNotification).where(
            DiscFinalizationNotification.disc_id == disc_id,
            DiscFinalizationNotification.status.in_(("pending", "active")),
        )
    ).scalars().all()
    finished_at = utcnow()
    for notification in notifications:
        notification.status = "completed"
        notification.next_attempt_at = None
        notification.completed_at = finished_at
        notification.last_error = None
    return len(notifications)


def _build_disc_finalization_payload(
    notification: DiscFinalizationNotification,
    *,
    delivered_at: datetime,
) -> dict[str, object]:
    is_reminder = notification.initial_sent_at is not None
    disc = notification.disc
    subscription = notification.subscription
    return {
        "event": DISC_FINALIZED_REMINDER_EVENT if is_reminder else DISC_FINALIZED_EVENT,
        "disc_id": disc.id,
        "download_url": disc_iso_download_url(disc.id),
        "request_burn_image_url": disc_iso_create_url(disc.id),
        "iso_available": _iso_available(disc),
        "burn_confirmed_at": isoformat_z(disc.burn_confirmed_at),
        "delivered_at": isoformat_z(delivered_at),
        "reminder_interval_seconds": subscription.reminder_interval_seconds,
        "reminder_count": notification.reminder_count + (1 if is_reminder else 0),
    }


def _post_webhook(url: str, payload: dict[str, object]) -> None:
    with httpx.Client(timeout=DISC_WEBHOOK_TIMEOUT_SECONDS) as client:
        response = client.post(url, json=payload)
        response.raise_for_status()


def _failure_retry_delay_seconds(notification: DiscFinalizationNotification) -> float:
    interval = notification.subscription.reminder_interval_seconds
    if interval is not None:
        return float(min(interval, max(1, int(DISC_WEBHOOK_RETRY_SECONDS))))
    return float(DISC_WEBHOOK_RETRY_SECONDS)


def deliver_due_disc_finalization_notifications(*, now: datetime | None = None, limit: int = 100) -> int:
    delivered_count = 0
    session = SessionLocal()
    try:
        current_time = now or utcnow()
        pending = session.execute(
            select(DiscFinalizationNotification)
            .where(
                DiscFinalizationNotification.status.in_(("pending", "active")),
                DiscFinalizationNotification.next_attempt_at.is_not(None),
                DiscFinalizationNotification.next_attempt_at <= current_time,
            )
            .order_by(DiscFinalizationNotification.next_attempt_at.asc(), DiscFinalizationNotification.created_at.asc())
            .limit(limit)
            .options(
                selectinload(DiscFinalizationNotification.subscription),
                selectinload(DiscFinalizationNotification.disc),
            )
        ).scalars().all()

        for notification in pending:
            disc = notification.disc
            if disc.burn_confirmed_at is not None:
                notification.status = "completed"
                notification.next_attempt_at = None
                notification.completed_at = current_time
                notification.last_error = None
                continue

            try:
                payload = _build_disc_finalization_payload(notification, delivered_at=current_time)
                _post_webhook(notification.subscription.webhook_url, payload)
            except Exception as exc:
                notification.last_error = str(exc)
                notification.next_attempt_at = current_time + timedelta(
                    seconds=_failure_retry_delay_seconds(notification)
                )
                logger.warning(
                    "disc finalization webhook delivery failed",
                    extra={
                        "subscription_id": notification.subscription_id,
                        "disc_id": notification.disc_id,
                        "error": str(exc),
                    },
                )
                continue

            is_reminder = notification.initial_sent_at is not None
            if not is_reminder:
                notification.initial_sent_at = current_time
            else:
                notification.reminder_count += 1
            notification.last_sent_at = current_time
            notification.last_error = None
            if notification.subscription.reminder_interval_seconds:
                notification.status = "active"
                notification.next_attempt_at = current_time + timedelta(
                    seconds=notification.subscription.reminder_interval_seconds
                )
            else:
                notification.status = "completed"
                notification.next_attempt_at = None
                notification.completed_at = current_time
            delivered_count += 1

        session.commit()
        return delivered_count
    finally:
        session.close()


async def run_disc_finalization_notifier() -> None:
    while True:
        try:
            deliver_due_disc_finalization_notifications()
        except Exception:
            logger.exception("disc finalization notification loop failed")
        await asyncio.sleep(DISC_WEBHOOK_DISPATCH_INTERVAL_SECONDS)
