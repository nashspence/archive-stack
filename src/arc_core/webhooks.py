from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol

import httpx


@dataclass(frozen=True)
class ReadyImage:
    image_id: str
    filename: str
    iso_available: bool


@dataclass(frozen=True)
class ImagesReadyBatch:
    batch_id: str
    images: list[ReadyImage]
    reminder_count: int = 0
    initial_sent_at: datetime | None = None
    next_attempt_at: datetime | None = None


class ImageReadyReminderStore(Protocol):
    def list_due(self, *, now: datetime, limit: int) -> list[ImagesReadyBatch]: ...
    def mark_delivered(
        self, batch_id: str, *, delivered_at: datetime, next_attempt_at: datetime | None
    ) -> None: ...
    def mark_failed(self, batch_id: str, *, error: str, next_attempt_at: datetime) -> None: ...


@dataclass(frozen=True)
class WebhookConfig:
    url: str
    base_url: str
    timeout_seconds: float = 10.0
    retry_seconds: float = 60.0
    reminder_interval_seconds: float = 3600.0


def utcnow() -> datetime:
    return datetime.now(UTC)


def isoformat_z(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def image_iso_download_path(image_id: str) -> str:
    return f"/v1/images/{image_id}/iso"


def image_summary_path(image_id: str) -> str:
    return f"/v1/images/{image_id}"


def image_iso_download_url(base_url: str, image_id: str) -> str:
    return f"{base_url.rstrip('/')}{image_iso_download_path(image_id)}"


def image_summary_url(base_url: str, image_id: str) -> str:
    return f"{base_url.rstrip('/')}{image_summary_path(image_id)}"


def recovery_session_path(session_id: str) -> str:
    return f"/v1/recovery-sessions/{session_id}"


def recovery_session_url(base_url: str, session_id: str) -> str:
    return f"{base_url.rstrip('/')}{recovery_session_path(session_id)}"


def build_images_ready_payload(
    *, config: WebhookConfig, batch: ImagesReadyBatch, delivered_at: datetime
) -> dict[str, object]:
    is_reminder = batch.initial_sent_at is not None
    return {
        "event": "images.ready.reminder" if is_reminder else "images.ready",
        "batch_id": batch.batch_id,
        "delivered_at": isoformat_z(delivered_at),
        "reminder_count": batch.reminder_count + (1 if is_reminder else 0),
        "reminder_interval_seconds": config.reminder_interval_seconds,
        "images": [
            {
                "image_id": image.image_id,
                "filename": image.filename,
                "iso_available": image.iso_available,
                "download_url": image_iso_download_url(config.base_url, image.image_id),
            }
            for image in batch.images
        ],
    }


def build_glacier_upload_failed_payload(
    *,
    config: WebhookConfig,
    image_id: str,
    error: str,
    attempts: int,
    failed_at: str,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "event": "images.glacier_upload.failed",
        "image_id": image_id,
        "failed_at": failed_at,
        "attempts": attempts,
        "error": error,
    }
    if config.base_url:
        payload["image_url"] = image_summary_url(config.base_url, image_id)
        payload["download_url"] = image_iso_download_url(config.base_url, image_id)
    return payload


def build_recovery_ready_payload(
    *,
    config: WebhookConfig,
    session_id: str,
    restore_expires_at: str | None,
    images: list[dict[str, str]],
    delivered_at: datetime,
    reminder_count: int,
    reminder: bool,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "event": "images.recovery_ready.reminder" if reminder else "images.recovery_ready",
        "session_id": session_id,
        "delivered_at": isoformat_z(delivered_at),
        "restore_expires_at": restore_expires_at,
        "reminder_count": reminder_count + (1 if reminder else 0),
        "reminder_interval_seconds": config.reminder_interval_seconds,
        "images": [
            {
                "image_id": image["image_id"],
                "filename": image["filename"],
                **(
                    {"image_url": image_summary_url(config.base_url, image["image_id"])}
                    if config.base_url
                    else {}
                ),
            }
            for image in images
        ],
    }
    if config.base_url:
        payload["session_url"] = recovery_session_url(config.base_url, session_id)
    return payload


def post_webhook(*, config: WebhookConfig, payload: dict[str, object]) -> None:
    with httpx.Client(timeout=config.timeout_seconds) as client:
        response = client.post(config.url, json=payload)
        response.raise_for_status()


class ImagesReadyReminderService:
    def __init__(self, *, store: ImageReadyReminderStore, config: WebhookConfig) -> None:
        self.store = store
        self.config = config

    def deliver_due(self, *, now: datetime | None = None, limit: int = 100) -> int:
        current = now or utcnow()
        delivered = 0
        for batch in self.store.list_due(now=current, limit=limit):
            try:
                payload = build_images_ready_payload(
                    config=self.config, batch=batch, delivered_at=current
                )
                post_webhook(config=self.config, payload=payload)
            except Exception as exc:
                self.store.mark_failed(
                    batch.batch_id,
                    error=str(exc),
                    next_attempt_at=current
                    + timedelta(seconds=max(1.0, self.config.retry_seconds)),
                )
                continue
            next_attempt = None
            if self.config.reminder_interval_seconds > 0:
                next_attempt = current + timedelta(seconds=self.config.reminder_interval_seconds)
            self.store.mark_delivered(
                batch.batch_id, delivered_at=current, next_attempt_at=next_attempt
            )
            delivered += 1
        return delivered

    async def run_forever(self, *, interval_seconds: float = 30.0) -> None:
        while True:
            self.deliver_due()
            await asyncio.sleep(interval_seconds)
