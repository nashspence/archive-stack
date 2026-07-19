from __future__ import annotations

import math
import re
from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

_SECONDS_PER_DAY = 86_400.0
_DURATION_RE = re.compile(r"^(?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s)?$")


def parse_event_repeat_interval_seconds(value: str | None, *, default: int = 86_400) -> int:
    raw = (value or "").strip().lower()
    if not raw:
        return default
    if raw.isdigit():
        seconds = int(raw)
    else:
        match = _DURATION_RE.fullmatch(raw)
        if not match or not any(match.groups()):
            raise ValueError(
                f"invalid event repeat interval {value!r}: expected seconds or duration like '24h'"
            )
        seconds = (
            int(match.group(1) or 0) * 3600
            + int(match.group(2) or 0) * 60
            + int(match.group(3) or 0)
        )
    if seconds < 0:
        raise ValueError("event repeat interval must be non-negative")
    return seconds


def parse_event_repeat_time(value: str | None) -> time | None:
    raw = (value or "").strip()
    if not raw:
        return None
    parts = raw.split(":")
    if len(parts) not in {2, 3}:
        raise ValueError(f"invalid event repeat time {value!r}: expected HH:MM or HH:MM:SS")
    try:
        hour = int(parts[0])
        minute = int(parts[1])
        second = int(parts[2]) if len(parts) == 3 else 0
        return time(hour=hour, minute=minute, second=second)
    except ValueError as exc:
        raise ValueError(
            f"invalid event repeat time {value!r}: expected HH:MM or HH:MM:SS"
        ) from exc


def normalize_event_repeat_time(value: str | None) -> str | None:
    parsed = parse_event_repeat_time(value)
    if parsed is None:
        return None
    if parsed.second:
        return parsed.strftime("%H:%M:%S")
    return parsed.strftime("%H:%M")


def event_repeat_zone(value: str | None) -> ZoneInfo:
    name = (value or "UTC").strip() or "UTC"
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError as exc:
        raise ValueError(f"invalid event repeat timezone {value!r}") from exc


def next_event_repeat_at(
    current: datetime,
    *,
    interval: timedelta | float | int,
    repeat_time: str | None = None,
    repeat_timezone: str = "UTC",
) -> datetime | None:
    seconds = interval.total_seconds() if isinstance(interval, timedelta) else float(interval)
    if seconds <= 0:
        return None
    current_utc = current.astimezone(UTC)
    target_time = parse_event_repeat_time(repeat_time)
    if target_time is None or seconds < _SECONDS_PER_DAY:
        return current_utc + timedelta(seconds=seconds)

    zone = event_repeat_zone(repeat_timezone)
    local_current = current_utc.astimezone(zone)
    days = max(1, math.ceil(seconds / _SECONDS_PER_DAY))
    candidate_date = local_current.date() + timedelta(days=days)
    candidate_local = datetime.combine(candidate_date, target_time, tzinfo=zone)
    if candidate_local <= local_current:
        candidate_local += timedelta(days=1)
    return candidate_local.astimezone(UTC)


def event_repeat_due(
    *,
    last_emitted_at: datetime,
    current: datetime,
    interval: timedelta | float | int,
    repeat_time: str | None = None,
    repeat_timezone: str = "UTC",
) -> bool:
    due_at = next_event_repeat_at(
        last_emitted_at,
        interval=interval,
        repeat_time=repeat_time,
        repeat_timezone=repeat_timezone,
    )
    return due_at is not None and current.astimezone(UTC) >= due_at


__all__ = [
    "event_repeat_due",
    "event_repeat_zone",
    "next_event_repeat_at",
    "normalize_event_repeat_time",
    "parse_event_repeat_interval_seconds",
    "parse_event_repeat_time",
]
