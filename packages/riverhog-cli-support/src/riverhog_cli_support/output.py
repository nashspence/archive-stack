"""Shared structured CLI output helpers for Riverhog-family applications."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any


def json_text(payload: object) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def emit(payload: Any, *, json_mode: bool) -> None:
    print(json_text(payload) if json_mode else payload)


def human_bytes(value: object) -> str:
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


def mapping_items(
    payload: Mapping[str, object],
    key: str,
) -> list[Mapping[str, object]]:
    value = payload.get(key)
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        return []
    return [item for item in value if isinstance(item, Mapping)]


def page_line(payload: Mapping[str, object], noun: str) -> str:
    return (
        f"{noun}: {payload.get('total', 0)} "
        f"(page {payload.get('page', 1)}/{payload.get('pages', 0)})"
    )


def format_list_ids(
    payload: Mapping[str, object],
    key: str,
    *,
    id_key: str = "id",
) -> str:
    return "\n".join(
        str(item[id_key])
        for item in mapping_items(payload, key)
        if item.get(id_key) is not None and item.get(id_key) != ""
    )


__all__ = [
    "emit",
    "format_list_ids",
    "human_bytes",
    "json_text",
    "mapping_items",
    "page_line",
]
