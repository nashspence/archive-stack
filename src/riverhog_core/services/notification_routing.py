from __future__ import annotations

import json
import logging
from collections.abc import Callable, Mapping, Sequence

from riverhog_core.domain.errors import BadRequest
from riverhog_core.runtime_config import RuntimeConfig
from riverhog_core.timestamps import utc_now
from riverhog_core.webhooks import WebhookConfig, build_collection_lifecycle_payload

PostWebhook = Callable[..., None]
_NOTIFY_RECIPIENT_CHARS = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.-")


def normalize_notify_recipient(raw: object) -> str:
    recipient = str(raw).strip()
    if not recipient:
        raise BadRequest("notify recipients must not be blank")
    if any(ch not in _NOTIFY_RECIPIENT_CHARS for ch in recipient):
        raise BadRequest(
            "notify recipients may contain only letters, digits, dots, underscores, and dashes"
        )
    return recipient


def normalize_collection_notify_config(
    notify: Mapping[str, object] | None,
) -> dict[str, object] | None:
    if notify is None:
        return None
    enabled = notify.get("enabled", True)
    if not isinstance(enabled, bool):
        raise BadRequest("notify.enabled must be a boolean")

    raw_recipients = notify.get("recipients", [])
    if isinstance(raw_recipients, str) or not isinstance(raw_recipients, Sequence):
        raise BadRequest("notify.recipients must be a list")
    recipients: list[str] = []
    for raw_recipient in raw_recipients:
        recipient = normalize_notify_recipient(raw_recipient)
        if recipient not in recipients:
            recipients.append(recipient)
    if enabled and not recipients:
        raise BadRequest("notify.recipients is required when notifications are enabled")
    return {"enabled": enabled, "recipients": recipients}


def collection_notify_json(notify: Mapping[str, object] | None) -> str | None:
    normalized = normalize_collection_notify_config(notify)
    if normalized is None:
        return None
    return json.dumps(normalized, sort_keys=True, separators=(",", ":"))


def decode_collection_notify_json(
    raw: str | None,
    *,
    log: logging.Logger,
) -> dict[str, object] | None:
    if not raw:
        return None
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        log.warning("collection upload notify_json is not valid JSON")
        return None
    if not isinstance(payload, dict):
        log.warning("collection upload notify_json is not an object")
        return None
    try:
        return normalize_collection_notify_config(
            {str(key): value for key, value in payload.items()}
        )
    except BadRequest:
        log.warning("collection upload notify_json is invalid")
        return None


def collection_notification_recipients(
    config: RuntimeConfig,
    notify: Mapping[str, object] | None,
) -> list[str]:
    if notify is None:
        return list(config.collection_webhook_default_recipients)
    if not bool(notify.get("enabled", True)):
        return []
    raw_recipients = notify.get("recipients") or []
    if isinstance(raw_recipients, str) or not isinstance(raw_recipients, Sequence):
        return []
    return [normalize_notify_recipient(item) for item in raw_recipients]


def post_collection_webhooks(
    config: RuntimeConfig,
    *,
    event: str,
    collection_id: str,
    details: dict[str, object] | None = None,
    notify: Mapping[str, object] | None = None,
    post: PostWebhook,
    log: logging.Logger,
) -> None:
    recipients = collection_notification_recipients(config, notify)
    for recipient in recipients:
        url = config.collection_webhook_urls.get(recipient)
        if not url:
            log.warning(
                "collection notification recipient %s has no configured webhook",
                recipient,
            )
            continue
        _post_collection_webhook_payload(
            url=url,
            config=config,
            event=event,
            collection_id=collection_id,
            details=details,
            recipient=recipient,
            post=post,
            log=log,
        )


def _post_collection_webhook_payload(
    *,
    url: str,
    config: RuntimeConfig,
    event: str,
    collection_id: str,
    details: dict[str, object] | None,
    recipient: str | None,
    post: PostWebhook,
    log: logging.Logger,
) -> None:
    try:
        webhook_config = WebhookConfig(
            url=url,
            base_url=config.public_base_url or "",
            timeout_seconds=config.webhook_timeout.total_seconds(),
        )
        payload = build_collection_lifecycle_payload(
            config=webhook_config,
            event=event,
            collection_id=collection_id,
            delivered_at=utc_now(),
            details=details,
        )
        if recipient:
            payload["recipient"] = recipient
        post(config=webhook_config, payload=payload)
    except Exception:
        log.warning("failed to deliver %s webhook for %s", event, collection_id, exc_info=True)
