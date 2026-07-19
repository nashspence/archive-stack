from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta

_DURATION_RE = re.compile(r"^(?:(\d+)d)?(?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s)?$")


def parse_duration(value: str) -> timedelta:
    match = _DURATION_RE.match(value.strip())
    if not match or not any(match.groups()):
        raise ValueError(
            f"invalid duration {value!r}: expected format like '2d', '24h', '30m', '90s'"
        )
    days, hours, minutes, seconds = (int(item or 0) for item in match.groups())
    return timedelta(days=days, hours=hours, minutes=minutes, seconds=seconds)


def utc_now() -> datetime:
    return datetime.now(UTC)


def format_utc_timestamp(value: datetime) -> str:
    if value.tzinfo is None:
        raise ValueError("UTC timestamps require timezone-aware datetimes")
    return value.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def parse_utc_timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("UTC timestamps require a timezone offset")
    return parsed.astimezone(UTC)


def utc_timestamp_now() -> str:
    return format_utc_timestamp(utc_now())
