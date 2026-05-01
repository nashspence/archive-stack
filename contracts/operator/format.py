from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from shlex import quote


def plural(count: int, singular: str, plural_form: str | None = None) -> str:
    if count == 1:
        return singular
    return plural_form or f"{singular}s"


def count_noun(count: int, singular: str, plural_form: str | None = None) -> str:
    return f"{count} {plural(count, singular, plural_form)}"


def bytes_amount(num_bytes: int | None) -> str:
    if num_bytes is None:
        return "unknown size"

    value = float(num_bytes)
    units: Sequence[str] = ("B", "KB", "MB", "GB", "TB", "PB")
    unit = "B"

    for candidate in units:
        unit = candidate
        if abs(value) < 1024 or candidate == units[-1]:
            break
        value /= 1024

    if unit == "B":
        return f"{value:.0f} {unit}"
    if abs(value) >= 100:
        return f"{value:.0f} {unit}"
    if abs(value) >= 10:
        return f"{value:.1f} {unit}"
    return f"{value:.2f} {unit}"


def money_usd(amount: Decimal | float | int | str | None) -> str:
    if amount is None:
        return "unknown cost"

    try:
        value = Decimal(str(amount)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    except (InvalidOperation, ValueError):
        return "unknown cost"
    if not value.is_finite():
        return "unknown cost"

    return f"${value}"


def percent(value: float | int | None, *, digits: int = 0) -> str:
    if value is None:
        return "unknown"
    return f"{float(value):.{digits}f}%"


def raw_command(*parts: object) -> str:
    return " ".join(
        quote(str(part))
        for part in parts
        if part is not None and str(part) != ""
    )


def command(*parts: object) -> str:
    return raw_command(*parts)


def truncate(value: object, *, max_chars: int = 96) -> str:
    text = "" if value is None else str(value)
    if len(text) <= max_chars:
        return text
    if max_chars <= 3:
        return text[:max_chars]
    return f"{text[: max_chars - 3]}..."


def when(value: datetime | str | None) -> str:
    if value is None:
        return "unknown time"
    if isinstance(value, str):
        return value

    dt = value
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)

    return dt.astimezone(UTC).strftime("%Y-%m-%d %H:%M UTC")


def deadline(value: datetime | str | None) -> str:
    return f"before {when(value)}"


def list_sentence(items: Iterable[object], *, max_items: int = 3) -> str:
    values = [str(item) for item in items if str(item)]
    if not values:
        return "none"

    shown = values[:max_items]
    remaining = len(values) - len(shown)
    if remaining > 0:
        shown.append(f"{remaining} more")

    if len(shown) == 1:
        return shown[0]
    if len(shown) == 2:
        return f"{shown[0]} and {shown[1]}"
    return f"{', '.join(shown[:-1])}, and {shown[-1]}"
