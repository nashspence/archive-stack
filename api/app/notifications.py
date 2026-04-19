from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
from sqlalchemy import select

from .config import (
    API_BASE_URL,
    CONTAINER_FINALIZATION_REMINDER_INTERVAL_SECONDS,
    CONTAINER_FINALIZATION_WEBHOOK_URL,
    CONTAINER_WEBHOOK_DISPATCH_INTERVAL_SECONDS,
    CONTAINER_WEBHOOK_RETRY_SECONDS,
    CONTAINER_WEBHOOK_TIMEOUT_SECONDS,
)
from .db import SessionLocal
from .models import Container

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


def _webhook_enabled() -> bool:
    return CONTAINER_FINALIZATION_WEBHOOK_URL is not None


def _iso_available(container: Container) -> bool:
    return bool(container.iso_abs_path and Path(container.iso_abs_path).exists())


def schedule_container_finalization_notification(session, container_id: str) -> bool:
    if not _webhook_enabled():
        return False

    container = session.get(Container, container_id)
    if container is None or container.burn_confirmed_at is not None:
        return False

    if container.finalization_status == "completed":
        return False

    if container.finalization_next_attempt_at is None:
        container.finalization_status = "pending"
        container.finalization_next_attempt_at = utcnow()
        container.finalization_completed_at = None
        container.finalization_last_error = None
        return True
    return False


def backfill_pending_container_finalization_notifications(session) -> int:
    if not _webhook_enabled():
        return 0

    containers = session.execute(
        select(Container)
        .where(Container.burn_confirmed_at.is_(None))
        .order_by(Container.created_at.asc())
    ).scalars().all()
    created = 0
    for container in containers:
        if schedule_container_finalization_notification(session, container.id):
            created += 1
    return created


def complete_container_finalization_notifications(session, container_id: str) -> bool:
    container = session.get(Container, container_id)
    if container is None:
        return False

    container.finalization_status = "completed"
    container.finalization_next_attempt_at = None
    container.finalization_completed_at = utcnow()
    container.finalization_last_error = None
    return True


def _build_container_finalization_payload(
    container: Container,
    *,
    delivered_at: datetime,
) -> dict[str, object]:
    is_reminder = container.finalization_initial_sent_at is not None
    return {
        "event": CONTAINER_FINALIZED_REMINDER_EVENT if is_reminder else CONTAINER_FINALIZED_EVENT,
        "container_id": container.id,
        "download_url": container_iso_download_url(container.id),
        "request_burn_image_url": container_iso_create_url(container.id),
        "iso_available": _iso_available(container),
        "burn_confirmed_at": isoformat_z(container.burn_confirmed_at),
        "delivered_at": isoformat_z(delivered_at),
        "reminder_interval_seconds": CONTAINER_FINALIZATION_REMINDER_INTERVAL_SECONDS,
        "reminder_count": container.finalization_reminder_count + (1 if is_reminder else 0),
    }


def _post_webhook(url: str, payload: dict[str, object]) -> None:
    with httpx.Client(timeout=CONTAINER_WEBHOOK_TIMEOUT_SECONDS) as client:
        response = client.post(url, json=payload)
        response.raise_for_status()


def deliver_due_container_finalization_notifications(*, now: datetime | None = None, limit: int = 100) -> int:
    if not _webhook_enabled():
        return 0

    delivered_count = 0
    session = SessionLocal()
    try:
        backfill_pending_container_finalization_notifications(session)
        current_time = now or utcnow()
        pending = session.execute(
            select(Container)
            .where(
                Container.burn_confirmed_at.is_(None),
                Container.finalization_next_attempt_at.is_not(None),
                Container.finalization_next_attempt_at <= current_time,
            )
            .order_by(Container.finalization_next_attempt_at.asc(), Container.created_at.asc())
            .limit(limit)
        ).scalars().all()

        for container in pending:
            try:
                payload = _build_container_finalization_payload(container, delivered_at=current_time)
                _post_webhook(CONTAINER_FINALIZATION_WEBHOOK_URL or "", payload)
            except Exception as exc:
                container.finalization_status = "pending"
                container.finalization_last_error = str(exc)
                container.finalization_next_attempt_at = current_time + timedelta(
                    seconds=max(1.0, CONTAINER_WEBHOOK_RETRY_SECONDS)
                )
                logger.warning(
                    "container finalization webhook delivery failed",
                    extra={"container_id": container.id, "error": str(exc)},
                )
                continue

            is_reminder = container.finalization_initial_sent_at is not None
            if not is_reminder:
                container.finalization_initial_sent_at = current_time
            else:
                container.finalization_reminder_count += 1
            container.finalization_last_sent_at = current_time
            container.finalization_last_error = None
            if CONTAINER_FINALIZATION_REMINDER_INTERVAL_SECONDS:
                container.finalization_status = "active"
                container.finalization_next_attempt_at = current_time + timedelta(
                    seconds=CONTAINER_FINALIZATION_REMINDER_INTERVAL_SECONDS
                )
            else:
                container.finalization_status = "completed"
                container.finalization_next_attempt_at = None
                container.finalization_completed_at = current_time
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
