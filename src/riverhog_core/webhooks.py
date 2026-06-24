from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from functools import lru_cache
from importlib import resources
from pathlib import Path
from typing import Any, Protocol

import httpx

_OPERATOR_CONTRACT_PATH = Path("contracts/webhooks/operator-notifications.v1.json")
_FALLBACK_NOTIFICATION_TEMPLATE: dict[str, str] = {
    "actor": "riverhog",
    "title_template": "{emoji} {subject_40}",
    "body_template": "Riverhog has an operator notification.",
}


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
    event = "images.ready.reminder" if is_reminder else "images.ready"
    images = [
        {
            "image_id": image.image_id,
            "filename": image.filename,
            "iso_available": image.iso_available,
            "download_url": image_iso_download_url(config.base_url, image.image_id),
        }
        for image in batch.images
    ]
    return {
        "event": event,
        "batch_id": batch.batch_id,
        "delivered_at": isoformat_z(delivered_at),
        "operator_urgency": _operator_event_field(event=event, field="operator_urgency"),
        "operator_action": _operator_event_field(event=event, field="operator_action"),
        "reminder_count": batch.reminder_count + (1 if is_reminder else 0),
        "reminder_interval_seconds": config.reminder_interval_seconds,
        "images": images,
        "notification": _images_ready_notification(event=event, images=images),
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
    payload["notification"] = _recovery_notification(
        event=str(payload["event"]),
        recovery_type=recovery_type,
        session_id=session_id,
        images=images,
        collections=collections or [],
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
    payload["notification"] = _recovery_notification(
        event="glacier_recovery.started",
        recovery_type=recovery_type,
        session_id=session_id,
        images=images,
        collections=collections,
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
    payload["notification"] = _recovery_notification(
        event="glacier_recovery.completed",
        recovery_type=recovery_type,
        session_id=session_id,
        images=images,
        collections=collections,
    )
    return payload


def build_recovery_canceled_payload(
    *,
    config: WebhookConfig,
    session_id: str,
    recovery_type: str,
    images: list[dict[str, str]],
    collections: list[dict[str, str]],
    delivered_at: datetime,
) -> dict[str, object]:
    payload = _base_recovery_payload(
        config=config,
        event="glacier_recovery.canceled",
        session_id=session_id,
        recovery_type=recovery_type,
        delivered_at=delivered_at,
        images=images,
        collections=collections,
    )
    payload.update(
        {
            "operator_urgency": "passive",
            "operator_action": "none",
            "operator_message": "Glacier recovery was canceled by the operator.",
        }
    )
    payload["notification"] = _recovery_notification(
        event="glacier_recovery.canceled",
        recovery_type=recovery_type,
        session_id=session_id,
        images=images,
        collections=collections,
    )
    return payload


def build_recovery_paused_reminder_payload(
    *,
    config: WebhookConfig,
    session_id: str,
    images: list[dict[str, str]],
    collections: list[dict[str, str]],
    delivered_at: datetime,
    reminder_count: int,
    reminder_interval_seconds: float,
) -> dict[str, object]:
    payload = _base_recovery_payload(
        config=config,
        event="glacier_recovery.paused.reminder",
        session_id=session_id,
        recovery_type="image_rebuild",
        delivered_at=delivered_at,
        images=images,
        collections=collections,
    )
    payload.update(
        {
            "reminder_count": reminder_count + 1,
            "reminder_interval_seconds": reminder_interval_seconds,
            "operator_urgency": "time_sensitive",
            "operator_action": f"Run `djdan disc rebuild resume {session_id}` when ready",
            "operator_message": (
                "Image rebuild recovery is paused. Resume it when ready to rebuild the image "
                "and restore disc coverage."
            ),
        }
    )
    payload["notification"] = _recovery_notification(
        event="glacier_recovery.paused.reminder",
        recovery_type="image_rebuild",
        session_id=session_id,
        images=images,
        collections=collections,
    )
    return payload


def build_recovery_retrying_payload(
    *,
    config: WebhookConfig,
    session_id: str,
    recovery_type: str,
    images: list[dict[str, str]],
    collections: list[dict[str, str]],
    delivered_at: datetime,
    attempts: int,
    failed_at: str,
    next_retry_at: str | None,
    retry_delay_seconds: float,
    error: str,
) -> dict[str, object]:
    payload = _base_recovery_payload(
        config=config,
        event="glacier_recovery.retrying",
        session_id=session_id,
        recovery_type=recovery_type,
        delivered_at=delivered_at,
        images=images,
        collections=collections,
    )
    payload.update(
        {
            "operator_urgency": "time_sensitive",
            "operator_action": "wait unless failures persist beyond normal connectivity trouble",
            "operator_message": (
                "Glacier recovery hit a retryable issue. Riverhog will keep retrying "
                "without operator action."
            ),
            "attempts": attempts,
            "failed_at": failed_at,
            "next_retry_at": next_retry_at,
            "retry_delay_seconds": retry_delay_seconds,
            "error": error,
        }
    )
    payload["notification"] = _recovery_notification(
        event="glacier_recovery.retrying",
        recovery_type=recovery_type,
        session_id=session_id,
        images=images,
        collections=collections,
        values={
            "attempts": attempts,
            "failed_at": failed_at,
            "next_retry_at": next_retry_at or "unknown",
            "retry_delay_seconds": retry_delay_seconds,
            "error": error,
        },
    )
    return payload


def build_recovery_failed_payload(
    *,
    config: WebhookConfig,
    session_id: str,
    recovery_type: str,
    images: list[dict[str, str]],
    collections: list[dict[str, str]],
    delivered_at: datetime,
    attempts: int,
    failed_at: str,
    error: str,
) -> dict[str, object]:
    payload = _base_recovery_payload(
        config=config,
        event="glacier_recovery.failed",
        session_id=session_id,
        recovery_type=recovery_type,
        delivered_at=delivered_at,
        images=images,
        collections=collections,
    )
    payload.update(
        {
            "operator_urgency": "critical",
            "operator_action": "inspect Riverhog recovery logs and session state",
            "operator_message": (
                "Glacier recovery stopped on a non-retryable issue. Riverhog will not "
                "continue this session automatically."
            ),
            "attempts": attempts,
            "failed_at": failed_at,
            "error": error,
        }
    )
    payload["notification"] = _recovery_notification(
        event="glacier_recovery.failed",
        recovery_type=recovery_type,
        session_id=session_id,
        images=images,
        collections=collections,
        values={
            "attempts": attempts,
            "failed_at": failed_at,
            "error": error,
        },
    )
    return payload


def build_fetch_queued_payload(
    *,
    config: WebhookConfig,
    fetch_id: str,
    name: str,
    files: int,
    bytes: int,
    copies: list[dict[str, str]],
    delivered_at: datetime,
    reminder_count: int,
    reminder: bool,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "event": "fetches.queued_djdan.reminder" if reminder else "fetches.queued_djdan",
        "type": "fetch_queued_djdan",
        "fetch_id": fetch_id,
        "name": name,
        "delivered_at": isoformat_z(delivered_at),
        "reminder_count": reminder_count + (1 if reminder else 0),
        "reminder_interval_seconds": config.reminder_interval_seconds,
        "files": files,
        "bytes": bytes,
        "copies": copies,
        "operator_urgency": "time_sensitive",
        "operator_action": f"Run `djdan fetch {fetch_id}`",
        "operator_message": (
            "Riverhog has a fetch queued for optical-media recovery before these files "
            "can be hot again."
        ),
        "notification": _fetch_queued_notification(
            reminder=reminder,
            name=name,
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
                "Wait for Riverhog to finish cloud-fetch recovery",
                (
                    "Cloud-fetch recovery has started for missing fetch-selected files. "
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
                "Cloud-fetch recovery completed and the missing fetch files are hot again.",
            )
        return (
            "No operator action required",
            "Glacier recovery completed and temporary restored archive data was cleaned up.",
        )
    if recovery_type == "collection_restore":
        return (
            "Wait for Riverhog to finish cloud-fetch materialization",
            (
                "Cloud-fetch recovery data is ready. Riverhog will materialize missing "
                "fetch files automatically before cleanup."
            ),
        )
    return (
        "Rebuild and burn replacement media before the restore expires",
        (
            "Glacier recovery data is ready for replacement media. Complete the "
            "rebuild and burn workflow before the temporary restore window expires."
        ),
    )


@lru_cache(maxsize=1)
def _operator_notification_contract() -> dict[str, Any]:
    for path in _operator_notification_contract_paths():
        if path.exists():
            payload = json.loads(path.read_text(encoding="utf-8"))
            return payload if isinstance(payload, dict) else {}
    try:
        resource = resources.files("riverhog_core").joinpath(
            "contracts",
            "webhooks",
            "operator-notifications.v1.json",
        )
        payload = json.loads(resource.read_text(encoding="utf-8"))
        return payload if isinstance(payload, dict) else {}
    except (FileNotFoundError, ModuleNotFoundError, json.JSONDecodeError):
        return {}


def _operator_notification_contract_paths() -> list[Path]:
    source_root = Path(__file__).resolve().parents[2]
    return [
        Path.cwd() / _OPERATOR_CONTRACT_PATH,
        source_root / _OPERATOR_CONTRACT_PATH,
    ]


@lru_cache(maxsize=1)
def _operator_notification_events() -> dict[str, Mapping[str, Any]]:
    events = _operator_notification_contract().get("events", [])
    if not isinstance(events, list):
        return {}
    return {
        str(event["event"]): event
        for event in events
        if isinstance(event, dict) and isinstance(event.get("event"), str)
    }


def _canonical_notification_from_contract(
    *,
    event: str,
    subject: str,
    notification_type: str | None = None,
    values: Mapping[str, object] | None = None,
) -> dict[str, str]:
    template = _notification_template(event=event, notification_type=notification_type)
    actor = str(template.get("actor", _FALLBACK_NOTIFICATION_TEMPLATE["actor"]))
    emoji = _notification_emoji(actor)
    subject_limit = _notification_int("subject_max_chars", default=40)
    body_limit = _notification_int("body_max_chars", default=150)
    normalized_subject = _normalize_space(subject)
    render_values = {
        "emoji": emoji,
        "subject": normalized_subject,
        "subject_40": _truncate(normalized_subject, subject_limit),
    }
    if values:
        for key, value in values.items():
            text = _normalize_space(str(value))
            render_values[str(key)] = text
            render_values[f"{key}_40"] = _truncate(text, 40)
            render_values[f"{key}_80"] = _truncate(text, 80)
            render_values[f"{key}_120"] = _truncate(text, 120)
    title = _render_template(
        str(template.get("title_template", _FALLBACK_NOTIFICATION_TEMPLATE["title_template"])),
        render_values,
    ).strip()
    body = _render_template(
        str(template.get("body_template", _FALLBACK_NOTIFICATION_TEMPLATE["body_template"])),
        render_values,
    )
    return {
        "title": title or emoji,
        "body": _truncate(body, body_limit),
    }


def _notification_template(*, event: str, notification_type: str | None) -> Mapping[str, Any]:
    event_contract = _operator_notification_events().get(event, {})
    type_templates = event_contract.get("canonical_notification_by_type")
    if notification_type and isinstance(type_templates, dict):
        template = type_templates.get(notification_type)
        if isinstance(template, dict):
            return template
    template = event_contract.get("canonical_notification")
    if isinstance(template, dict):
        return template
    return _FALLBACK_NOTIFICATION_TEMPLATE


def _operator_event_field(*, event: str, field: str, default: str = "") -> str:
    value = _operator_notification_events().get(event, {}).get(field)
    if isinstance(value, str):
        return value
    return default


def _notification_emoji(actor: str) -> str:
    rendering = _operator_notification_contract().get("receiver_rendering", {})
    actors = rendering.get("actors") if isinstance(rendering, dict) else None
    if isinstance(actors, dict) and isinstance(actors.get(actor), str):
        return str(actors[actor])
    return {"riverhog": "🐷", "djdan": "👨🏻‍🎤"}.get(actor, actor)


def _notification_int(key: str, *, default: int) -> int:
    rendering = _operator_notification_contract().get("receiver_rendering", {})
    if isinstance(rendering, dict):
        value = rendering.get(key)
        if isinstance(value, int) and value > 0:
            return value
    return default


def _render_template(template: str, values: Mapping[str, str]) -> str:
    try:
        return template.format(**values)
    except (KeyError, ValueError):
        return template


def _normalize_space(value: str) -> str:
    return " ".join(str(value).split())


def _truncate(value: str, limit: int) -> str:
    normalized = _normalize_space(value)
    if len(normalized) <= limit:
        return normalized
    if limit <= 3:
        return normalized[:limit]
    return f"{normalized[: limit - 3].rstrip()}..."


def _collection_subject(collection_id: str) -> str:
    leaf = collection_id.rstrip("/").split("/")[-1] or collection_id
    if "__" in leaf:
        return leaf.split("__", 1)[1]
    return leaf


def _target_subject(target: str) -> str:
    leaf = target.rstrip("/").split("/")[-1] or target
    if "__" in leaf:
        return leaf.split("__", 1)[1]
    return leaf


def _image_subject(images: Sequence[Mapping[str, object]]) -> str:
    if not images:
        return "disc image"
    first = images[0]
    subject = str(first.get("filename") or first.get("image_id") or "disc image")
    if len(images) > 1:
        subject = f"{subject} +{len(images) - 1}"
    return subject


def _recovery_subject(
    *,
    session_id: str,
    images: Sequence[Mapping[str, object]],
    collections: list[dict[str, str]],
) -> str:
    if collections:
        return _collection_subject(collections[0]["collection_id"])
    if images:
        return _image_subject(list(images))
    return session_id


def _collection_notification(
    *,
    event: str,
    collection_id: str,
    values: Mapping[str, object] | None = None,
) -> dict[str, str]:
    return _canonical_notification_from_contract(
        event=event,
        subject=_collection_subject(collection_id),
        values=values,
    )


def _munchy_job_subject(job: Mapping[str, object]) -> str:
    collection_slug = str(job.get("collection_slug") or "")
    if collection_slug:
        return _collection_subject(collection_slug)
    job_id = str(job.get("job_id") or "")
    if job_id:
        return _target_subject(job_id)
    return "munchy job"


def _jeb_batch_subject(batch: Mapping[str, object]) -> str:
    source_id = str(batch.get("source_id") or "")
    if str(batch.get("collection_slug") or "") == "jeb-held-signatures" and source_id:
        return source_id
    collection_slug = str(batch.get("collection_slug") or "")
    if collection_slug:
        return _collection_subject(collection_slug)
    if source_id:
        return source_id
    batch_id = str(batch.get("id") or "")
    if batch_id:
        return _target_subject(batch_id)
    return "jeb batch"


def _munchy_operator_urgency(*, event: str, severity: str) -> str:
    if event == "job.issue":
        if severity == "critical":
            return "critical"
        if severity in {"error", "warning"}:
            return "time_sensitive"
    return _operator_event_field(event=event, field="operator_urgency", default="passive")


def _munchy_operator_action(*, event: str, severity: str) -> str:
    if event == "job.issue" and severity == "critical":
        return "inspect Munchy job details immediately"
    return _operator_event_field(event=event, field="operator_action")


def _jeb_operator_urgency(*, event: str, severity: str) -> str:
    if event == "jeb.issue":
        if severity == "critical":
            return "critical"
        if severity in {"error", "warning"}:
            return "time_sensitive"
    return _operator_event_field(event=event, field="operator_urgency", default="passive")


def _jeb_operator_action(*, event: str, severity: str) -> str:
    if event == "jeb.issue" and severity == "critical":
        return "inspect Jeb batch details immediately"
    if event == "jeb.issue" and severity in {"error", "warning"}:
        return "review held Jeb capture signatures"
    return _operator_event_field(event=event, field="operator_action")


def _munchy_job_notification(
    *,
    event: str,
    job: Mapping[str, object],
    values: Mapping[str, object] | None = None,
) -> dict[str, str]:
    return _canonical_notification_from_contract(
        event=event,
        subject=_munchy_job_subject(job),
        values=values,
    )


def _jeb_batch_notification(
    *,
    event: str,
    batch: Mapping[str, object],
    values: Mapping[str, object] | None = None,
) -> dict[str, str]:
    return _canonical_notification_from_contract(
        event=event,
        subject=_jeb_batch_subject(batch),
        values=values,
    )


def _images_ready_notification(
    *,
    event: str,
    images: Sequence[Mapping[str, object]],
) -> dict[str, str]:
    return _canonical_notification_from_contract(
        event=event,
        subject=_image_subject(images),
    )


def _copy_label_needed_notification(*, label_text: str) -> dict[str, str]:
    return _canonical_notification_from_contract(
        event="images.copy_label_needed",
        subject=label_text,
    )


def _fetch_queued_notification(*, reminder: bool, name: str) -> dict[str, str]:
    return _canonical_notification_from_contract(
        event="fetches.queued_djdan.reminder" if reminder else "fetches.queued_djdan",
        subject=_target_subject(name),
    )


def _recovery_notification(
    *,
    event: str,
    recovery_type: str,
    session_id: str,
    images: Sequence[Mapping[str, object]],
    collections: list[dict[str, str]],
    values: Mapping[str, object] | None = None,
) -> dict[str, str]:
    subject = _recovery_subject(
        session_id=session_id,
        images=images,
        collections=collections,
    )
    return _canonical_notification_from_contract(
        event=event,
        subject=subject,
        notification_type=recovery_type,
        values=values,
    )


def build_collection_lifecycle_payload(
    *,
    config: WebhookConfig,
    event: str,
    collection_id: str,
    delivered_at: datetime,
    details: dict[str, object] | None = None,
) -> dict[str, object]:
    detail_values = details or {}
    payload: dict[str, object] = {
        "event": event,
        "type": "collection_lifecycle",
        "collection_id": collection_id,
        "delivered_at": isoformat_z(delivered_at),
        "operator_urgency": _operator_event_field(event=event, field="operator_urgency"),
        "operator_action": _operator_event_field(event=event, field="operator_action"),
        "notification": _collection_notification(
            event=event,
            collection_id=collection_id,
            values=detail_values,
        ),
    }
    if config.base_url:
        payload["collection_url"] = collection_url(config.base_url, collection_id)
        payload["upload_url"] = collection_upload_url(config.base_url, collection_id)
    if details:
        payload.update(details)
    return payload


def build_munchy_job_payload(
    *,
    event: str,
    job: Mapping[str, object],
    message: str,
    severity: str,
    delivered_at: datetime,
    recipient: str | None = None,
    details: dict[str, object] | None = None,
) -> dict[str, object]:
    detail_values = dict(details or {})
    detail_values.setdefault("message", message)
    if "error" not in detail_values and severity in {"warning", "error", "critical"}:
        detail_values["error"] = message
    payload: dict[str, object] = {
        "event": event,
        "type": "munchy_job",
        "source": "munchy",
        "actor": "munchy",
        "delivered_at": isoformat_z(delivered_at),
        "operator_urgency": _munchy_operator_urgency(event=event, severity=severity),
        "operator_action": _munchy_operator_action(event=event, severity=severity),
        "severity": severity,
        "message": message,
        "job_id": str(job.get("job_id") or ""),
        "collection_slug": str(job.get("collection_slug") or ""),
        "collection_timestamp": str(job.get("collection_timestamp") or ""),
        "phase": str(job.get("phase") or ""),
        "state": str(job.get("state") or ""),
        "notification": _munchy_job_notification(
            event=event,
            job=job,
            values=detail_values,
        ),
    }
    if recipient:
        payload["recipient"] = recipient
    if details:
        payload.update(details)
    return payload


def build_jeb_event_payload(
    *,
    event: str,
    batch: Mapping[str, object],
    message: str,
    severity: str,
    delivered_at: datetime,
    recipient: str | None = None,
    details: dict[str, object] | None = None,
) -> dict[str, object]:
    detail_values = dict(details or {})
    detail_values.setdefault("message", message)
    if "error" not in detail_values and severity in {"warning", "error", "critical"}:
        detail_values["error"] = message
    payload: dict[str, object] = {
        "event": event,
        "type": "jeb_batch",
        "source": "jeb",
        "actor": "jeb",
        "delivered_at": isoformat_z(delivered_at),
        "operator_urgency": _jeb_operator_urgency(event=event, severity=severity),
        "operator_action": _jeb_operator_action(event=event, severity=severity),
        "severity": severity,
        "message": message,
        "batch_id": str(batch.get("id") or ""),
        "source_id": str(batch.get("source_id") or ""),
        "target_name": str(batch.get("target_name") or ""),
        "target_type": str(batch.get("target_type") or ""),
        "collection_slug": str(batch.get("collection_slug") or ""),
        "collection_timestamp": str(batch.get("collection_timestamp") or ""),
        "state": str(batch.get("state") or ""),
        "notification": _jeb_batch_notification(
            event=event,
            batch=batch,
            values=detail_values,
        ),
    }
    if recipient:
        payload["recipient"] = recipient
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
        "operator_urgency": _operator_event_field(
            event="images.copy_label_needed",
            field="operator_urgency",
        ),
        "operator_action": _operator_event_field(
            event="images.copy_label_needed",
            field="operator_action",
        ),
        "notification": _copy_label_needed_notification(label_text=label_text),
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
