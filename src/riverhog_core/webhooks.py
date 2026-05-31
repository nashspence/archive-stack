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


def fetch_summary_path(fetch_id: str) -> str:
    return f"/v1/fetches/{fetch_id}"


def fetch_manifest_path(fetch_id: str) -> str:
    return f"/v1/fetches/{fetch_id}/manifest"


def fetch_summary_url(base_url: str, fetch_id: str) -> str:
    return f"{base_url.rstrip('/')}{fetch_summary_path(fetch_id)}"


def fetch_manifest_url(base_url: str, fetch_id: str) -> str:
    return f"{base_url.rstrip('/')}{fetch_manifest_path(fetch_id)}"


def collection_path(collection_id: str) -> str:
    return f"/v1/collections/{collection_id}"


def collection_upload_path(collection_id: str) -> str:
    return f"/v1/collection-uploads/{collection_id}"


def collection_url(base_url: str, collection_id: str) -> str:
    return f"{base_url.rstrip('/')}{collection_path(collection_id)}"


def collection_upload_url(base_url: str, collection_id: str) -> str:
    return f"{base_url.rstrip('/')}{collection_upload_path(collection_id)}"


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


def build_recovery_ready_payload(
    *,
    config: WebhookConfig,
    session_id: str,
    restore_expires_at: str | None,
    images: list[dict[str, str]],
    delivered_at: datetime,
    reminder_count: int,
    reminder: bool,
    recovery_type: str = "image_rebuild",
    collections: list[dict[str, str]] | None = None,
) -> dict[str, object]:
    action, message = _recovery_operator_guidance(
        stage="ready",
        recovery_type=recovery_type,
    )
    payload = _base_recovery_payload(
        config=config,
        event="glacier_recovery.ready.reminder" if reminder else "glacier_recovery.ready",
        session_id=session_id,
        recovery_type=recovery_type,
        delivered_at=delivered_at,
        images=images,
        collections=collections or [],
    )
    payload.update(
        {
            "restore_expires_at": restore_expires_at,
            "reminder_count": reminder_count + (1 if reminder else 0),
            "reminder_interval_seconds": config.reminder_interval_seconds,
            "operator_urgency": "time_sensitive",
            "operator_action": action,
            "operator_message": message,
        }
    )
    return payload


def build_recovery_started_payload(
    *,
    config: WebhookConfig,
    session_id: str,
    recovery_type: str,
    retrieval_tier: str,
    estimated_ready_at: str | None,
    images: list[dict[str, str]],
    collections: list[dict[str, str]],
    delivered_at: datetime,
) -> dict[str, object]:
    action, message = _recovery_operator_guidance(
        stage="started",
        recovery_type=recovery_type,
    )
    payload = _base_recovery_payload(
        config=config,
        event="glacier_recovery.started",
        session_id=session_id,
        recovery_type=recovery_type,
        delivered_at=delivered_at,
        images=images,
        collections=collections,
    )
    payload.update(
        {
            "retrieval_tier": retrieval_tier,
            "estimated_ready_at": estimated_ready_at,
            "operator_urgency": "time_sensitive",
            "operator_action": action,
            "operator_message": message,
        }
    )
    return payload


def build_recovery_completed_payload(
    *,
    config: WebhookConfig,
    session_id: str,
    recovery_type: str,
    images: list[dict[str, str]],
    collections: list[dict[str, str]],
    delivered_at: datetime,
) -> dict[str, object]:
    action, message = _recovery_operator_guidance(
        stage="completed",
        recovery_type=recovery_type,
    )
    payload = _base_recovery_payload(
        config=config,
        event="glacier_recovery.completed",
        session_id=session_id,
        recovery_type=recovery_type,
        delivered_at=delivered_at,
        images=images,
        collections=collections,
    )
    payload.update(
        {
            "operator_urgency": "time_sensitive",
            "operator_action": action,
            "operator_message": message,
        }
    )
    return payload


def build_fetch_waiting_payload(
    *,
    config: WebhookConfig,
    fetch_id: str,
    target: str,
    files: int,
    bytes: int,
    copies: list[dict[str, str]],
    delivered_at: datetime,
    reminder_count: int,
    reminder: bool,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "event": "fetches.waiting_media.reminder" if reminder else "fetches.waiting_media",
        "type": "fetch_waiting_media",
        "fetch_id": fetch_id,
        "target": target,
        "delivered_at": isoformat_z(delivered_at),
        "reminder_count": reminder_count + (1 if reminder else 0),
        "reminder_interval_seconds": config.reminder_interval_seconds,
        "files": files,
        "bytes": bytes,
        "copies": copies,
        "operator_urgency": "time_sensitive",
        "operator_action": f"Run djdan fetch {fetch_id}",
        "operator_message": (
            "Riverhog is waiting for optical-media recovery before this pinned target "
            "can be hot again."
        ),
    }
    if config.base_url:
        payload["fetch_url"] = fetch_summary_url(config.base_url, fetch_id)
        payload["manifest_url"] = fetch_manifest_url(config.base_url, fetch_id)
    return payload


def _base_recovery_payload(
    *,
    config: WebhookConfig,
    event: str,
    session_id: str,
    recovery_type: str,
    delivered_at: datetime,
    images: list[dict[str, str]],
    collections: list[dict[str, str]],
) -> dict[str, object]:
    payload: dict[str, object] = {
        "event": event,
        "type": recovery_type,
        "session_id": session_id,
        "delivered_at": isoformat_z(delivered_at),
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
        "collections": [
            {
                "collection_id": collection["collection_id"],
                **(
                    {
                        "collection_url": collection_url(
                            config.base_url,
                            collection["collection_id"],
                        )
                    }
                    if config.base_url
                    else {}
                ),
            }
            for collection in collections
        ],
    }
    if config.base_url:
        payload["session_url"] = recovery_session_url(config.base_url, session_id)
    return payload


def _recovery_operator_guidance(*, stage: str, recovery_type: str) -> tuple[str, str]:
    if stage == "started":
        if recovery_type == "collection_restore":
            return (
                "Wait for Riverhog to restore missing pinned files automatically",
                (
                    "Glacier recovery has started for missing pinned hot files. "
                    "This is rare, expected to take a long time, and means Riverhog "
                    "is recovering the safely archived collection data."
                ),
            )
        return (
            "Wait for the recovery-ready notification",
            (
                "Glacier recovery has started for lost or damaged disc media. "
                "This is rare, expected to take a long time, and means Riverhog "
                "is recovering safely archived collection data so replacement media "
                "can be rebuilt."
            ),
        )
    if stage == "completed":
        if recovery_type == "collection_restore":
            return (
                "No operator action required",
                "Glacier recovery completed and the missing pinned files are hot again.",
            )
        return (
            "No operator action required",
            "Glacier recovery completed and temporary restored archive data was cleaned up.",
        )
    if recovery_type == "collection_restore":
        return (
            "Wait for Riverhog to finish materializing files",
            (
                "Glacier recovery data is ready. Riverhog will materialize missing "
                "pinned files automatically before cleanup."
            ),
        )
    return (
        "Rebuild and burn replacement media before the restore expires",
        (
            "Glacier recovery data is ready for replacement media. Complete the "
            "rebuild and burn workflow before the temporary restore window expires."
        ),
    )


def build_collection_lifecycle_payload(
    *,
    config: WebhookConfig,
    event: str,
    collection_id: str,
    delivered_at: datetime,
    details: dict[str, object] | None = None,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "event": event,
        "type": "collection_lifecycle",
        "collection_id": collection_id,
        "delivered_at": isoformat_z(delivered_at),
    }
    if config.base_url:
        payload["collection_url"] = collection_url(config.base_url, collection_id)
        payload["upload_url"] = collection_upload_url(config.base_url, collection_id)
    if details:
        payload.update(details)
    return payload


def build_copy_label_needed_payload(
    *,
    config: WebhookConfig,
    image_id: str,
    copy_id: str,
    label_text: str,
    delivered_at: datetime,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "event": "images.copy_label_needed",
        "type": "copy_lifecycle",
        "image_id": image_id,
        "copy_id": copy_id,
        "label_text": label_text,
        "delivered_at": isoformat_z(delivered_at),
    }
    if config.base_url:
        payload["image_url"] = image_summary_url(config.base_url, image_id)
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
