from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Final, Literal

from pydantic import BaseModel, ConfigDict, Field

MUNCHY_WEBHOOK_EMOJI: Final = "🤤"
MunchyNotificationSeverity = Literal["info", "warning", "error"]


class MunchyNotification(BaseModel):
    model_config = ConfigDict(extra="allow", frozen=True)

    source: Literal["munchy"] = "munchy"
    emoji: str = MUNCHY_WEBHOOK_EMOJI
    event: str = Field(min_length=1)
    severity: MunchyNotificationSeverity = "info"
    message: str = Field(min_length=1)
    job_id: str | None = None
    collection_slug: str | None = None
    collection_timestamp: str | None = None
    recipients: tuple[str, ...] = ()


def notification_payload(
    *,
    event: str,
    message: str,
    severity: MunchyNotificationSeverity = "info",
    recipients: tuple[str, ...] = (),
    extra: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    payload = dict(extra or {})
    notification = MunchyNotification(
        event=event,
        message=message,
        severity=severity,
        recipients=recipients,
        **payload,
    )
    return notification.model_dump(exclude_none=True, mode="json")
