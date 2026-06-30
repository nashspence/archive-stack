from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from riverhog_core.operator_reminders import (
    next_operator_reminder_at,
    normalize_reminder_time,
    operator_reminder_due,
    parse_reminder_interval_seconds,
)


def test_daily_reminders_snap_to_configured_local_time() -> None:
    delivered = datetime(2026, 6, 29, 8, 28, tzinfo=UTC)

    assert next_operator_reminder_at(
        delivered,
        interval=timedelta(hours=24),
        reminder_time="14:00",
        reminder_timezone="America/Los_Angeles",
    ) == datetime(2026, 6, 30, 21, 0, tzinfo=UTC)


def test_short_intervals_remain_exact_for_retry_like_test_cadence() -> None:
    delivered = datetime(2026, 6, 29, 8, 28, tzinfo=UTC)

    assert next_operator_reminder_at(
        delivered,
        interval=timedelta(seconds=30),
        reminder_time="14:00",
        reminder_timezone="America/Los_Angeles",
    ) == datetime(2026, 6, 29, 8, 28, 30, tzinfo=UTC)


def test_operator_reminder_due_uses_configured_time_slot() -> None:
    delivered = datetime(2026, 6, 29, 8, 28, tzinfo=UTC)

    assert not operator_reminder_due(
        last_sent_at=delivered,
        current=datetime(2026, 6, 30, 20, 59, tzinfo=UTC),
        interval=timedelta(hours=24),
        reminder_time="14:00",
        reminder_timezone="America/Los_Angeles",
    )
    assert operator_reminder_due(
        last_sent_at=delivered,
        current=datetime(2026, 6, 30, 21, 0, tzinfo=UTC),
        interval=timedelta(hours=24),
        reminder_time="14:00",
        reminder_timezone="America/Los_Angeles",
    )


def test_parse_reminder_policy_values() -> None:
    assert normalize_reminder_time("2:03") == "02:03"
    assert normalize_reminder_time("14:03:04") == "14:03:04"
    assert parse_reminder_interval_seconds("24h") == 86_400
    assert parse_reminder_interval_seconds("1h30m") == 5_400
    assert parse_reminder_interval_seconds("45") == 45

    with pytest.raises(ValueError, match="invalid reminder time"):
        normalize_reminder_time("tomorrow")
    with pytest.raises(ValueError, match="invalid reminder interval"):
        parse_reminder_interval_seconds("daily")
