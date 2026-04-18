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
    CONTAINER_WEBHOOK_DISPATCH_INTERVAL_SECONDS,
    CONTAINER_WEBHOOK_RETRY_SECONDS,
    CONTAINER_WEBHOOK_TIMEOUT_SECONDS,
)
from .db import SessionLocal
from .models import Container, ContainerFinalizationNotification, ContainerFinalizationWebhookSubscription

logger = logging.getLogger(__name__)

CONTAINER_FINALIZED_EVENT = "container.finalized"
CONTAINER_FINALIZED_REMINDER_EVENT = "container.finalized.reminder"


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def isoformat_z(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def container_iso_download_path(container_id: str) -> str:
    return f"/v1/containers/{container_id}/iso/content"


def container_iso_download_url(container_id: str) -> str:
    return f"{API_BASE_URL}{container_iso_download_path(container_id)}"


def container_iso_create_url(container_id: str) -> str:
    return f"{API_BASE_URL}/v1/containers/{container_id}/iso/create"


def _iso_available(container: Container) -> bool:
    return bool(container.iso_abs_path and Path(container.iso_abs_path).exists())


def create_container_finalization_notifications_for_container(session, container_id: str) -> int:
    container = session.get(Container, container_id)
    if container is None or container.burn_confirmed_at is not None:
        return 0

    subscriptions = session.execute(
        select(ContainerFinalizationWebhookSubscription).where(
            ContainerFinalizationWebhookSubscription.active.is_(True)
        )
    ).scalars().all()
    created = 0
    now = utcnow()
    for subscription in subscriptions:
        existing = session.execute(
            select(ContainerFinalizationNotification).where(
                ContainerFinalizationNotification.subscription_id == subscription.id,
                ContainerFinalizationNotification.container_id == container_id,
            )
        ).scalar_one_or_none()
        if existing is not None:
            continue
        session.add(
            ContainerFinalizationNotification(
                subscription_id=subscription.id,
                container_id=container_id,
                status="pending",
                next_attempt_at=now,
            )
        )
        created += 1
    return created


def backfill_container_finalization_notifications_for_subscription(session, subscription_id: str) -> int:
    container_ids = session.execute(
        select(Container.id).where(Container.burn_confirmed_at.is_(None)).order_by(Container.created_at.asc())
    ).scalars().all()
    created = 0
    now = utcnow()
    for container_id in container_ids:
        existing = session.execute(
            select(ContainerFinalizationNotification).where(
                ContainerFinalizationNotification.subscription_id == subscription_id,
                ContainerFinalizationNotification.container_id == container_id,
            )
        ).scalar_one_or_none()
        if existing is not None:
            continue
        session.add(
            ContainerFinalizationNotification(
                subscription_id=subscription_id,
                container_id=container_id,
                status="pending",
                next_attempt_at=now,
            )
        )
        created += 1
    return created


def complete_container_finalization_notifications(session, container_id: str) -> int:
    notifications = session.execute(
        select(ContainerFinalizationNotification).where(
            ContainerFinalizationNotification.container_id == container_id,
            ContainerFinalizationNotification.status.in_(("pending", "active")),
        )
    ).scalars().all()
    finished_at = utcnow()
    for notification in notifications:
        notification.status = "completed"
        notification.next_attempt_at = None
        notification.completed_at = finished_at
        notification.last_error = None
    return len(notifications)


def _build_container_finalization_payload(
    notification: ContainerFinalizationNotification,
    *,
    delivered_at: datetime,
) -> dict[str, object]:
    is_reminder = notification.initial_sent_at is not None
    container = notification.container
    subscription = notification.subscription
    return {
        "event": CONTAINER_FINALIZED_REMINDER_EVENT if is_reminder else CONTAINER_FINALIZED_EVENT,
        "container_id": container.id,
        "download_url": container_iso_download_url(container.id),
        "request_burn_image_url": container_iso_create_url(container.id),
        "iso_available": _iso_available(container),
        "burn_confirmed_at": isoformat_z(container.burn_confirmed_at),
        "delivered_at": isoformat_z(delivered_at),
        "reminder_interval_seconds": subscription.reminder_interval_seconds,
        "reminder_count": notification.reminder_count + (1 if is_reminder else 0),
    }


def _post_webhook(url: str, payload: dict[str, object]) -> None:
    with httpx.Client(timeout=CONTAINER_WEBHOOK_TIMEOUT_SECONDS) as client:
        response = client.post(url, json=payload)
        response.raise_for_status()


def _failure_retry_delay_seconds(notification: ContainerFinalizationNotification) -> float:
    interval = notification.subscription.reminder_interval_seconds
    if interval is not None:
        return float(min(interval, max(1, int(CONTAINER_WEBHOOK_RETRY_SECONDS))))
    return float(CONTAINER_WEBHOOK_RETRY_SECONDS)


def deliver_due_container_finalization_notifications(*, now: datetime | None = None, limit: int = 100) -> int:
    delivered_count = 0
    session = SessionLocal()
    try:
        current_time = now or utcnow()
        pending = session.execute(
            select(ContainerFinalizationNotification)
            .where(
                ContainerFinalizationNotification.status.in_(("pending", "active")),
                ContainerFinalizationNotification.next_attempt_at.is_not(None),
                ContainerFinalizationNotification.next_attempt_at <= current_time,
            )
            .order_by(ContainerFinalizationNotification.next_attempt_at.asc(), ContainerFinalizationNotification.created_at.asc())
            .limit(limit)
            .options(
                selectinload(ContainerFinalizationNotification.subscription),
                selectinload(ContainerFinalizationNotification.container),
            )
        ).scalars().all()

        for notification in pending:
            container = notification.container
            if container.burn_confirmed_at is not None:
                notification.status = "completed"
                notification.next_attempt_at = None
                notification.completed_at = current_time
                notification.last_error = None
                continue

            try:
                payload = _build_container_finalization_payload(notification, delivered_at=current_time)
                _post_webhook(notification.subscription.webhook_url, payload)
            except Exception as exc:
                notification.last_error = str(exc)
                notification.next_attempt_at = current_time + timedelta(
                    seconds=_failure_retry_delay_seconds(notification)
                )
                logger.warning(
                    "container finalization webhook delivery failed",
                    extra={
                        "subscription_id": notification.subscription_id,
                        "container_id": notification.container_id,
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


async def run_container_finalization_notifier() -> None:
    while True:
        try:
            deliver_due_container_finalization_notifications()
        except Exception:
            logger.exception("container finalization notification loop failed")
        await asyncio.sleep(CONTAINER_WEBHOOK_DISPATCH_INTERVAL_SECONDS)
