from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import lru_cache
from importlib import resources
from pathlib import Path
from typing import Any

import httpx

from riverhog_core.operator_reminders import next_operator_reminder_at

_OPERATOR_CONTRACT_PATH = Path("contracts/webhooks/operator-notifications.v1.json")
_FALLBACK_NOTIFICATION_TEMPLATE: dict[str, str] = {
    "actor": "riverhog",
    "title_template": "{emoji} {subject_40}",
    "body_template": "Riverhog has an operator notification.",
}


@dataclass(frozen=True)
class WebhookConfig:
    url: str
    base_url: str
    timeout_seconds: float = 10.0
    retry_seconds: float = 60.0
    reminder_interval_seconds: float = 3600.0
    reminder_time: str | None = None
    reminder_timezone: str = "UTC"

    def next_reminder_at(self, current: datetime) -> datetime | None:
        return next_operator_reminder_at(
            current,
            interval=self.reminder_interval_seconds,
            reminder_time=self.reminder_time,
            reminder_timezone=self.reminder_timezone,
        )


def utcnow() -> datetime:
    return datetime.now(UTC)


def isoformat_z(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def archive_restore_path(restore_id: str) -> str:
    return f"/v1/archive-restores/{restore_id}"


def archive_restore_url(base_url: str, restore_id: str) -> str:
    return f"{base_url.rstrip('/')}{archive_restore_path(restore_id)}"


def fetch_summary_path(fetch_id: str) -> str:
    return f"/v1/fetches/{fetch_id}"


def fetch_summary_url(base_url: str, fetch_id: str) -> str:
    return f"{base_url.rstrip('/')}{fetch_summary_path(fetch_id)}"


def collection_path(collection_id: str) -> str:
    return f"/v1/collections/{collection_id}"


def collection_upload_path(collection_id: str) -> str:
    return f"/v1/collection-uploads/{collection_id}"


def collection_url(base_url: str, collection_id: str) -> str:
    return f"{base_url.rstrip('/')}{collection_path(collection_id)}"


def collection_upload_url(base_url: str, collection_id: str) -> str:
    return f"{base_url.rstrip('/')}{collection_upload_path(collection_id)}"


def build_archive_restore_ready_payload(
    *,
    config: WebhookConfig,
    restore_id: str,
    expires_at: str | None,
    collections: list[dict[str, str]],
    delivered_at: datetime,
) -> dict[str, object]:
    payload = _base_archive_restore_payload(
        config=config,
        event="archive_restore.ready",
        restore_id=restore_id,
        delivered_at=delivered_at,
        collections=collections,
    )
    payload.update(
        {
            "expires_at": expires_at,
            "operator_urgency": "time_sensitive",
            "operator_action": "wait for automatic materialization",
            "operator_message": "Archive retrieval is ready and materialization is starting.",
            "notification": _archive_restore_notification(
                event="archive_restore.ready", restore_id=restore_id, collections=collections
            ),
        }
    )
    return payload


def build_archive_restore_started_payload(
    *,
    config: WebhookConfig,
    restore_id: str,
    retrieval_tier: str,
    estimated_ready_at: str | None,
    collections: list[dict[str, str]],
    delivered_at: datetime,
) -> dict[str, object]:
    payload = _base_archive_restore_payload(
        config=config,
        event="archive_restore.started",
        restore_id=restore_id,
        delivered_at=delivered_at,
        collections=collections,
    )
    payload.update(
        {
            "retrieval_tier": retrieval_tier,
            "estimated_ready_at": estimated_ready_at,
            "operator_urgency": "time_sensitive",
            "operator_action": "wait for automatic materialization",
            "operator_message": "Archive retrieval has started for selected files.",
            "notification": _archive_restore_notification(
                event="archive_restore.started",
                restore_id=restore_id,
                collections=collections,
            ),
        }
    )
    return payload


def build_archive_restore_completed_payload(
    *,
    config: WebhookConfig,
    restore_id: str,
    collections: list[dict[str, str]],
    delivered_at: datetime,
) -> dict[str, object]:
    payload = _base_archive_restore_payload(
        config=config,
        event="archive_restore.completed",
        restore_id=restore_id,
        delivered_at=delivered_at,
        collections=collections,
    )
    payload.update(
        {
            "operator_urgency": "passive",
            "operator_action": "none",
            "operator_message": "Selected files are verified and available in hot storage.",
            "notification": _archive_restore_notification(
                event="archive_restore.completed",
                restore_id=restore_id,
                collections=collections,
            ),
        }
    )
    return payload


def build_archive_restore_canceled_payload(
    *,
    config: WebhookConfig,
    restore_id: str,
    collections: list[dict[str, str]],
    delivered_at: datetime,
) -> dict[str, object]:
    payload = _base_archive_restore_payload(
        config=config,
        event="archive_restore.canceled",
        restore_id=restore_id,
        delivered_at=delivered_at,
        collections=collections,
    )
    payload.update(
        {
            "operator_urgency": "passive",
            "operator_action": "none",
            "operator_message": "Archive restore was canceled.",
            "notification": _archive_restore_notification(
                event="archive_restore.canceled",
                restore_id=restore_id,
                collections=collections,
            ),
        }
    )
    return payload


def build_archive_restore_retrying_payload(
    *,
    config: WebhookConfig,
    restore_id: str,
    collections: list[dict[str, str]],
    delivered_at: datetime,
    attempts: int,
    failed_at: str,
    next_retry_at: str | None,
    retry_delay_seconds: float,
    error: str,
) -> dict[str, object]:
    payload = _base_archive_restore_payload(
        config=config,
        event="archive_restore.retrying",
        restore_id=restore_id,
        delivered_at=delivered_at,
        collections=collections,
    )
    payload.update(
        {
            "operator_urgency": "time_sensitive",
            "operator_action": "wait unless failures persist",
            "operator_message": "Archive retrieval hit a retryable issue.",
            "attempts": attempts,
            "failed_at": failed_at,
            "next_retry_at": next_retry_at,
            "retry_delay_seconds": retry_delay_seconds,
            "error": error,
            "notification": _archive_restore_notification(
                event="archive_restore.retrying",
                restore_id=restore_id,
                collections=collections,
                values={
                    "attempts": attempts,
                    "failed_at": failed_at,
                    "next_retry_at": next_retry_at or "unknown",
                    "retry_delay_seconds": retry_delay_seconds,
                    "error": error,
                },
            ),
        }
    )
    return payload


def build_archive_restore_failed_payload(
    *,
    config: WebhookConfig,
    restore_id: str,
    collections: list[dict[str, str]],
    delivered_at: datetime,
    attempts: int,
    failed_at: str,
    error: str,
) -> dict[str, object]:
    payload = _base_archive_restore_payload(
        config=config,
        event="archive_restore.failed",
        restore_id=restore_id,
        delivered_at=delivered_at,
        collections=collections,
    )
    payload.update(
        {
            "operator_urgency": "time_sensitive",
            "operator_action": "inspect Riverhog archive retrieval logs",
            "operator_message": "Archive retrieval stopped on a non-retryable issue.",
            "attempts": attempts,
            "failed_at": failed_at,
            "error": error,
            "notification": _archive_restore_notification(
                event="archive_restore.failed",
                restore_id=restore_id,
                collections=collections,
                values={"attempts": attempts, "failed_at": failed_at, "error": error},
            ),
        }
    )
    return payload


def _base_archive_restore_payload(
    *,
    config: WebhookConfig,
    event: str,
    restore_id: str,
    delivered_at: datetime,
    collections: list[dict[str, str]],
) -> dict[str, object]:
    payload: dict[str, object] = {
        "event": event,
        "type": "archive_restore",
        "restore_id": restore_id,
        "delivered_at": isoformat_z(delivered_at),
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
        payload["restore_url"] = archive_restore_url(config.base_url, restore_id)
    return payload


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
    title_limit = _notification_int("title_max_chars", default=48)
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
        "title": _truncate(title or emoji, title_limit),
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
    return {"riverhog": "🐷"}.get(actor, actor)


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


def _archive_restore_subject(
    *,
    restore_id: str,
    collections: list[dict[str, str]],
) -> str:
    if collections:
        return _collection_subject(collections[0]["collection_id"])
    return restore_id


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


def _jeb_issue_subject(context: Mapping[str, object]) -> str:
    account_id = str(context.get("account_id") or "")
    if account_id:
        return account_id
    collection_slug = str(context.get("collection_slug") or "")
    if collection_slug:
        return _collection_subject(collection_slug)
    context_id = str(context.get("id") or "")
    if context_id:
        return _target_subject(context_id)
    return "Jeb issue"


def _munchy_operator_urgency(*, event: str, severity: str) -> str:
    if event == "job.issue":
        if severity in {"critical", "error", "warning"}:
            return "time_sensitive"
    return _operator_event_field(event=event, field="operator_urgency", default="passive")


def _munchy_operator_action(*, event: str, severity: str) -> str:
    return _operator_event_field(event=event, field="operator_action")


def _jeb_operator_urgency(*, event: str, severity: str) -> str:
    if event == "jeb.issue":
        if severity in {"critical", "error", "warning"}:
            return "time_sensitive"
    return _operator_event_field(event=event, field="operator_urgency", default="passive")


def _jeb_operator_action(*, event: str, severity: str, component: str = "") -> str:
    if event == "jeb.issue" and component == "routing":
        return "fix Munchy routing, then run Jeb archive-now for the account"
    if event == "jeb.issue" and component == "munchy_preflight":
        return "repair Munchy routing preflight, then run Jeb archive-now for the account"
    if event == "jeb.issue" and severity in {"critical", "error", "warning"}:
        return "inspect Jeb issue details"
    return _operator_event_field(event=event, field="operator_action")


def _jeb_notification_summary(*, component: str, error: str) -> str:
    text = _normalize_space(error)
    if text and "\n" not in error and len(text) <= 100:
        return text
    if "metadata projection requires valid GPS coordinates" in text:
        return "Missing GPS metadata. Next: fix metadata projection config, then retry Jeb archive."
    if component == "routing":
        return _truncate(text or "Munchy routing preflight failed. Next: fix routes.", 120)
    if component == "munchy_preflight":
        return _truncate(text or "Munchy preflight failed. Next: repair Munchy.", 120)
    if component == "cleanup":
        return "Jeb cleanup failed. Next: inspect file permissions and source cleanup state."
    if component == "target":
        return "Munchy target failed. Next: inspect job details, fix the issue, then retry archive."
    return _truncate(text or "Jeb issue needs operator review. Next: inspect Jeb details.", 120)


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


def _jeb_issue_notification(
    *,
    event: str,
    context: Mapping[str, object],
    values: Mapping[str, object] | None = None,
) -> dict[str, str]:
    return _canonical_notification_from_contract(
        event=event,
        subject=_jeb_issue_subject(context),
        values=values,
    )


def _archive_restore_notification(
    *,
    event: str,
    restore_id: str,
    collections: list[dict[str, str]],
    values: Mapping[str, object] | None = None,
) -> dict[str, str]:
    subject = _archive_restore_subject(
        restore_id=restore_id,
        collections=collections,
    )
    return _canonical_notification_from_contract(
        event=event,
        subject=subject,
        notification_type="archive_restore",
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
    component = str(detail_values.get("component") or "")
    detail_error = str(detail_values.get("error") or message)
    notification_summary = _jeb_notification_summary(
        component=component,
        error=detail_error,
    )
    detail_values.setdefault("message", notification_summary)
    detail_values.setdefault("detailed_message", message)
    detail_values.setdefault("notification_summary", notification_summary)
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
    context: Mapping[str, object],
    message: str,
    severity: str,
    delivered_at: datetime,
    recipient: str | None = None,
    details: dict[str, object] | None = None,
) -> dict[str, object]:
    detail_values = dict(details or {})
    component = str(detail_values.get("component") or "")
    detail_error = str(detail_values.get("error") or message)
    notification_summary = _jeb_notification_summary(
        component=component,
        error=detail_error,
    )
    detail_values.setdefault("message", notification_summary)
    detail_values.setdefault("detailed_message", message)
    detail_values.setdefault("notification_summary", notification_summary)
    if "error" not in detail_values and severity in {"warning", "error", "critical"}:
        detail_values["error"] = message
    payload: dict[str, object] = {
        "event": event,
        "type": "jeb_issue",
        "source": "jeb",
        "actor": "jeb",
        "delivered_at": isoformat_z(delivered_at),
        "operator_urgency": _jeb_operator_urgency(event=event, severity=severity),
        "operator_action": _jeb_operator_action(
            event=event,
            severity=severity,
            component=component,
        ),
        "severity": severity,
        "message": notification_summary,
        "detailed_message": message,
        "attempt_id": str(context.get("id") or "") if context.get("batch_id") else "",
        "batch_id": str(context.get("batch_id") or ""),
        "account_id": str(context.get("account_id") or ""),
        "target_name": str(context.get("target_name") or ""),
        "target_type": str(context.get("target_type") or ""),
        "collection_slug": str(context.get("collection_slug") or ""),
        "collection_timestamp": str(context.get("collection_timestamp") or ""),
        "state": str(context.get("state") or ""),
        "notification": _jeb_issue_notification(
            event=event,
            context=context,
            values=detail_values,
        ),
    }
    if recipient:
        payload["recipient"] = recipient
    if details:
        payload.update(details)
        payload["message"] = notification_summary
        payload["detailed_message"] = message
    return payload


def post_webhook(*, config: WebhookConfig, payload: dict[str, object]) -> None:
    with httpx.Client(timeout=config.timeout_seconds) as client:
        response = client.post(config.url, json=payload)
        response.raise_for_status()
