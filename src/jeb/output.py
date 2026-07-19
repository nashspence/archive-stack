from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any


def emit(payload: Any, *, json_mode: bool) -> None:
    if json_mode:
        print(json.dumps(payload, sort_keys=True, separators=(",", ":")))
    else:
        print(payload)


def _bytes(value: object) -> str:
    if not isinstance(value, (int, float, str)):
        return str(value)
    try:
        amount = float(value or 0)
    except (TypeError, ValueError):
        return str(value)
    if amount < 1000:
        return f"{int(amount)} B"
    for unit in ("KB", "MB", "GB", "TB", "PB"):
        amount /= 1000
        if amount < 1000 or unit == "PB":
            return f"{amount:.1f} {unit}"
    raise AssertionError("unreachable")


def _items(payload: Mapping[str, object], key: str) -> list[Mapping[str, object]]:
    value = payload.get(key)
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return []
    return [item for item in value if isinstance(item, Mapping)]


def _page_line(payload: Mapping[str, object], noun: str) -> str:
    return (
        f"{noun}: {payload.get('total', 0)} "
        f"(page {payload.get('page', 1)}/{payload.get('pages', 0)})"
    )


def format_attempts(payload: Mapping[str, object]) -> str:
    lines = [_page_line(payload, "Jeb attempts")]
    for attempt in _items(payload, "attempts"):
        lines.append(
            f"- {attempt.get('id', attempt.get('attempt_id', 'unknown'))}  "
            f"source={attempt.get('source_id', attempt.get('source', 'unknown'))}  "
            f"state={attempt.get('state', 'unknown')}"
        )
    return "\n".join(lines)


def format_sources(payload: Mapping[str, object]) -> str:
    lines = [_page_line(payload, "Jeb sources")]
    for source in _items(payload, "sources"):
        raw_adapters = source.get("adapters")
        adapters = (
            ",".join(str(adapter) for adapter in raw_adapters)
            if isinstance(raw_adapters, Sequence)
            and not isinstance(raw_adapters, (str, bytes))
            else "none"
        )
        lines.append(
            f"- {source.get('id', 'unknown')}  "
            f"state={'enabled' if source.get('enabled') else 'disabled'}  "
            f"adapters={adapters}  target={source.get('target', 'unknown')}"
        )
    return "\n".join(lines)


def format_list_ids(
    payload: Mapping[str, object],
    key: str,
    *,
    id_key: str = "id",
) -> str:
    return "\n".join(
        str(item[id_key])
        for item in _items(payload, key)
        if item.get(id_key) is not None and item.get(id_key) != ""
    )


def format_status(payload: Mapping[str, object]) -> str:
    sources = _items(payload, "sources")
    batches = payload.get("batches")
    active_attempts = payload.get("active_attempts")
    attempt_count = 0
    if isinstance(active_attempts, Mapping):
        attempt_count = int(active_attempts.get("total") or 0)
    lines = [f"Jeb status: sources={len(sources)} active_attempts={attempt_count}"]
    for source in sources:
        lines.append(
            f"- {source.get('id', source.get('source_id', 'unknown'))}  "
            f"state={'enabled' if source.get('enabled') else 'disabled'}"
        )
    if isinstance(batches, Mapping):
        lines.append(f"batches: total={batches.get('total', 0)} active={batches.get('active', 0)}")
    incomplete = payload.get("incomplete_tus_uploads")
    if isinstance(incomplete, Mapping):
        lines.append(
            "TUS incomplete: "
            f"{incomplete.get('total', 0)} ({_bytes(incomplete.get('bytes'))}), "
            f"stale={incomplete.get('stale', 0)}, "
            f"oldest={incomplete.get('oldest_age_seconds', 0)}s"
        )
    return "\n".join(lines)


def format_archive_plan(payload: Mapping[str, object]) -> str:
    lines = [
        f"Jeb archive plan: {payload.get('source', payload.get('source_id', 'unknown'))}",
        f"eligible files: {payload.get('file_count', 0)}",
        f"eligible bytes: {payload.get('bytes', 0)}",
    ]
    if payload.get("period_start") or payload.get("period_end"):
        lines.append(f"period: {payload.get('period_start')} — {payload.get('period_end')}")
    return "\n".join(lines)


def format_config_check(payload: Mapping[str, object]) -> str:
    return f"Jeb config: {payload.get('status', payload.get('state', 'unknown'))}"


def format_operation(payload: Mapping[str, object], *, title: str) -> str:
    return f"{title}: {payload.get('status', payload.get('state', 'complete'))}"


__all__ = [
    "emit",
    "format_archive_plan",
    "format_attempts",
    "format_config_check",
    "format_list_ids",
    "format_operation",
    "format_sources",
    "format_status",
]
