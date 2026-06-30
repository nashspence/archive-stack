from __future__ import annotations

import math
import re
from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

_SECONDS_PER_DAY = 86_400.0
_DURATION_RE = re.compile(r"^(?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s)?$")


def parse_reminder_interval_seconds(value: str | None, *, default: int = 86_400) -> int:
    raw = (value or "").strip().lower()
    if not raw:
        return default
    if raw.isdigit():
        seconds = int(raw)
    else:
        match = _DURATION_RE.fullmatch(raw)
        if not match or not any(match.groups()):
            raise ValueError(
                f"invalid reminder interval {value!r}: expected seconds or duration like '24h'"
            )
        seconds = (
            int(match.group(1) or 0) * 3600
            + int(match.group(2) or 0) * 60
            + int(match.group(3) or 0)
        )
    if seconds < 0:
        raise ValueError("reminder interval must be non-negative")
    return seconds


def parse_reminder_time(value: str | None) -> time | None:
    raw = (value or "").strip()
    if not raw:
        return None
    parts = raw.split(":")
    if len(parts) not in {2, 3}:
        raise ValueError(f"invalid reminder time {value!r}: expected HH:MM or HH:MM:SS")
    try:
        hour = int(parts[0])
        minute = int(parts[1])
        second = int(parts[2]) if len(parts) == 3 else 0
        return time(hour=hour, minute=minute, second=second)
    except ValueError as exc:
        raise ValueError(f"invalid reminder time {value!r}: expected HH:MM or HH:MM:SS") from exc


def normalize_reminder_time(value: str | None) -> str | None:
    parsed = parse_reminder_time(value)
    if parsed is None:
        return None
    if parsed.second:
        return parsed.strftime("%H:%M:%S")
    return parsed.strftime("%H:%M")


def reminder_zone(value: str | None) -> ZoneInfo:
    name = (value or "UTC").strip() or "UTC"
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError as exc:
        raise ValueError(f"invalid reminder timezone {value!r}") from exc


def next_operator_reminder_at(
    current: datetime,
    *,
    interval: timedelta | float | int,
    reminder_time: str | None = None,
    reminder_timezone: str = "UTC",
) -> datetime | None:
    seconds = interval.total_seconds() if isinstance(interval, timedelta) else float(interval)
    if seconds <= 0:
        return None
    current_utc = current.astimezone(UTC)
    target_time = parse_reminder_time(reminder_time)
    if target_time is None or seconds < _SECONDS_PER_DAY:
        return current_utc + timedelta(seconds=seconds)

    zone = reminder_zone(reminder_timezone)
    local_current = current_utc.astimezone(zone)
    days = max(1, math.ceil(seconds / _SECONDS_PER_DAY))
    candidate_date = local_current.date() + timedelta(days=days)
    candidate_local = datetime.combine(candidate_date, target_time, tzinfo=zone)
    if candidate_local <= local_current:
        candidate_local += timedelta(days=1)
    return candidate_local.astimezone(UTC)


def operator_reminder_due(
    *,
    last_sent_at: datetime,
    current: datetime,
    interval: timedelta | float | int,
    reminder_time: str | None = None,
    reminder_timezone: str = "UTC",
) -> bool:
    due_at = next_operator_reminder_at(
        last_sent_at,
        interval=interval,
        reminder_time=reminder_time,
        reminder_timezone=reminder_timezone,
    )
    return due_at is not None and current.astimezone(UTC) >= due_at
