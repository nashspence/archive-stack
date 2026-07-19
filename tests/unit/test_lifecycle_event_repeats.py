from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from lifecycle_events.repeats import (
    event_repeat_due,
    next_event_repeat_at,
    normalize_event_repeat_time,
    parse_event_repeat_interval_seconds,
)


def test_daily_event_repeats_snap_to_configured_local_time() -> None:
    emitted = datetime(2026, 6, 29, 8, 28, tzinfo=UTC)

    assert next_event_repeat_at(
        emitted,
        interval=timedelta(hours=24),
        repeat_time="14:00",
        repeat_timezone="America/Los_Angeles",
    ) == datetime(2026, 6, 30, 21, 0, tzinfo=UTC)


def test_short_event_repeat_intervals_remain_exact() -> None:
    emitted = datetime(2026, 6, 29, 8, 28, tzinfo=UTC)

    assert next_event_repeat_at(
        emitted,
        interval=timedelta(seconds=30),
        repeat_time="14:00",
        repeat_timezone="America/Los_Angeles",
    ) == datetime(2026, 6, 29, 8, 28, 30, tzinfo=UTC)


def test_event_repeat_due_uses_configured_time_slot() -> None:
    emitted = datetime(2026, 6, 29, 8, 28, tzinfo=UTC)

    assert not event_repeat_due(
        last_emitted_at=emitted,
        current=datetime(2026, 6, 30, 20, 59, tzinfo=UTC),
        interval=timedelta(hours=24),
        repeat_time="14:00",
        repeat_timezone="America/Los_Angeles",
    )
    assert event_repeat_due(
        last_emitted_at=emitted,
        current=datetime(2026, 6, 30, 21, 0, tzinfo=UTC),
        interval=timedelta(hours=24),
        repeat_time="14:00",
        repeat_timezone="America/Los_Angeles",
    )


def test_parse_event_repeat_policy_values() -> None:
    assert normalize_event_repeat_time("2:03") == "02:03"
    assert normalize_event_repeat_time("14:03:04") == "14:03:04"
    assert parse_event_repeat_interval_seconds("24h") == 86_400
    assert parse_event_repeat_interval_seconds("1h30m") == 5_400
    assert parse_event_repeat_interval_seconds("45") == 45

    with pytest.raises(ValueError, match="invalid event repeat time"):
        normalize_event_repeat_time("tomorrow")
    with pytest.raises(ValueError, match="invalid event repeat interval"):
        parse_event_repeat_interval_seconds("daily")
