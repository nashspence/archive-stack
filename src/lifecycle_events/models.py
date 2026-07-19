from __future__ import annotations

import json
import uuid
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

CLOUDEVENTS_JSON_CONTENT_TYPE = "application/cloudevents+json"
MAX_EVENT_CONTEXT_BYTES = 4096


def event_time(value: datetime | None = None) -> str:
    current = (value or datetime.now(UTC)).astimezone(UTC)
    return current.isoformat(timespec="microseconds").replace("+00:00", "Z")


class CloudEvent(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    specversion: Literal["1.0"] = "1.0"
    id: str = Field(min_length=1)
    source: str = Field(min_length=1)
    type: str = Field(min_length=1)
    subject: str | None = Field(default=None, min_length=1)
    time: str
    datacontenttype: Literal["application/json"] = "application/json"
    data: dict[str, Any] = Field(default_factory=dict)

    @field_validator("time")
    @classmethod
    def validate_time(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized.endswith("Z"):
            raise ValueError("CloudEvent time must be a UTC timestamp ending in Z")
        datetime.fromisoformat(normalized.removesuffix("Z") + "+00:00")
        return normalized


class EventPage(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    events: list[CloudEvent]
    next_cursor: str
    has_more: bool


def cloud_event(
    *,
    source: str,
    type: str,
    data: Mapping[str, Any] | None = None,
    subject: str | None = None,
    occurred_at: datetime | None = None,
    event_id: str | None = None,
) -> CloudEvent:
    return CloudEvent(
        id=event_id or str(uuid.uuid4()),
        source=source,
        type=type,
        subject=subject,
        time=event_time(occurred_at),
        data=dict(data or {}),
    )


def caused_event(
    *,
    cause: CloudEvent,
    source: str,
    type: str,
    data: Mapping[str, Any] | None = None,
    subject: str | None = None,
    occurred_at: datetime | None = None,
) -> CloudEvent:
    payload = dict(data or {})
    payload["cause"] = {
        "id": cause.id,
        "source": cause.source,
        "type": cause.type,
        **({"subject": cause.subject} if cause.subject is not None else {}),
    }
    event_id = str(
        uuid.uuid5(
            uuid.NAMESPACE_URL,
            "\x1f".join(
                (
                    cause.source,
                    cause.id,
                    source,
                    type,
                    subject or "",
                )
            ),
        )
    )
    return cloud_event(
        source=source,
        type=type,
        subject=subject,
        data=payload,
        occurred_at=occurred_at,
        event_id=event_id,
    )


def normalize_event_context(
    value: Mapping[str, Any] | None,
    *,
    max_bytes: int = MAX_EVENT_CONTEXT_BYTES,
) -> dict[str, Any] | None:
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise ValueError("event_context must be a JSON object")
    try:
        encoded = json.dumps(
            dict(value),
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ValueError("event_context must contain only JSON values") from exc
    if len(encoded) > max_bytes:
        raise ValueError(f"event_context must be at most {max_bytes} UTF-8 JSON bytes")
    decoded = json.loads(encoded)
    if not isinstance(decoded, dict):  # pragma: no cover - guarded by Mapping above
        raise ValueError("event_context must be a JSON object")
    return {str(key): item for key, item in decoded.items()}


__all__ = [
    "CLOUDEVENTS_JSON_CONTENT_TYPE",
    "MAX_EVENT_CONTEXT_BYTES",
    "CloudEvent",
    "EventPage",
    "caused_event",
    "cloud_event",
    "event_time",
    "normalize_event_context",
]
