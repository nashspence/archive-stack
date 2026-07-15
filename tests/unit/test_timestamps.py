from datetime import UTC, datetime, timedelta, timezone

import pytest

from riverhog_core.timestamps import format_utc_timestamp, parse_utc_timestamp


def test_utc_timestamp_has_fixed_fractional_precision() -> None:
    assert (
        format_utc_timestamp(datetime(2026, 7, 15, 16, 2, 3, tzinfo=UTC))
        == "2026-07-15T16:02:03.000000Z"
    )


def test_utc_timestamp_normalizes_offsets() -> None:
    eastern = timezone(-timedelta(hours=4))
    value = datetime(2026, 7, 15, 12, 2, 3, 456789, tzinfo=eastern)

    assert format_utc_timestamp(value) == "2026-07-15T16:02:03.456789Z"


def test_parse_utc_timestamp_returns_utc() -> None:
    assert parse_utc_timestamp("2026-07-15T12:02:03.456789-04:00") == datetime(
        2026, 7, 15, 16, 2, 3, 456789, tzinfo=UTC
    )


def test_utc_timestamp_requires_timezone_context() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        format_utc_timestamp(datetime(2026, 7, 15, 16, 2, 3))

    with pytest.raises(ValueError, match="timezone offset"):
        parse_utc_timestamp("2026-07-15T16:02:03.000000")
