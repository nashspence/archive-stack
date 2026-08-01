from __future__ import annotations

import json
from collections.abc import Mapping, Sequence

from riverhog_cli_support.output import (
    human_bytes as _bytes,
)
from riverhog_cli_support.output import (
    mapping_items as _items,
)
from riverhog_cli_support.output import (
    page_line as _page_line,
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


def format_attempt(payload: Mapping[str, object]) -> str:
    lines = [
        f"Jeb attempt {payload.get('attempt_id', 'unknown')}",
        f"source: {payload.get('source_id', 'unknown')}",
        f"target: {payload.get('target_name', 'unknown')}",
        f"state: {payload.get('state', 'unknown')}",
        f"files: {payload.get('staged_file_count', 0)}/{payload.get('file_count', 0)}",
        f"bytes: {_bytes(payload.get('total_bytes'))}",
        f"created: {payload.get('created_at', 'unknown')}",
        f"updated: {payload.get('updated_at', 'unknown')}",
    ]
    if payload.get("target_submission_id"):
        lines.append(f"target submission: {payload['target_submission_id']}")
    if payload.get("last_error"):
        lines.append(f"error: {payload['last_error']}")
    return "\n".join(lines)


def format_sources(payload: Mapping[str, object]) -> str:
    lines = [_page_line(payload, "Jeb sources")]
    for source in _items(payload, "sources"):
        raw_adapters = source.get("adapters")
        adapters = (
            ",".join(str(adapter) for adapter in raw_adapters)
            if isinstance(raw_adapters, Sequence) and not isinstance(raw_adapters, (str, bytes))
            else "none"
        )
        target = str(source.get("target", "unknown"))
        raw_target_config = source.get("target_config")
        if isinstance(raw_target_config, Mapping) and raw_target_config:
            rendered_config = ",".join(
                f"{name}={json.dumps(value, separators=(',', ':'))}"
                for name, value in sorted(raw_target_config.items())
            )
            target = f"{target}({rendered_config})"
        lines.append(
            f"- {source.get('id', 'unknown')}  "
            f"state={'enabled' if source.get('enabled') else 'disabled'}  "
            f"adapters={adapters}  target={target}"
        )
    return "\n".join(lines)


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
        f"eligible bytes: {payload.get('total_bytes', 0)}",
    ]
    if payload.get("period_start") or payload.get("period_end"):
        lines.append(f"period: {payload.get('period_start')} — {payload.get('period_end')}")
    return "\n".join(lines)


def format_config_check(payload: Mapping[str, object]) -> str:
    return f"Jeb config: {payload.get('status', payload.get('state', 'unknown'))}"


def format_operation(payload: Mapping[str, object], *, title: str) -> str:
    return f"{title}: {payload.get('status', payload.get('state', 'complete'))}"


__all__ = [
    "format_archive_plan",
    "format_attempt",
    "format_attempts",
    "format_config_check",
    "format_operation",
    "format_sources",
    "format_status",
]
