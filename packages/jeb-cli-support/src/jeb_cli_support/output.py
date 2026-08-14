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
        f"batch: {payload.get('batch_id', 'unknown')}",
        f"attempt number: {payload.get('attempt_number', 'unknown')}",
        f"source: {payload.get('source_id', 'unknown')}",
        f"target: {payload.get('target_name', 'unknown')}",
        f"state: {payload.get('state', 'unknown')}",
        f"files: {payload.get('claimed_file_count', 0)}/{payload.get('file_count', 0)}",
        f"bytes: {_bytes(payload.get('total_bytes'))}",
        f"cleanup: {payload.get('cleanup', 'unknown')}",
        f"run: {payload.get('run_id', 'unknown')}",
        f"created: {payload.get('created_at', 'unknown')}",
        f"updated: {payload.get('updated_at', 'unknown')}",
    ]
    if payload.get("target_submission_id"):
        lines.append(f"target submission: {payload['target_submission_id']}")
    if payload.get("last_error"):
        lines.append(f"error: {payload['last_error']}")
    return "\n".join(lines)


def format_attempt_transition(payload: Mapping[str, object]) -> str:
    return (
        f"Jeb attempt {payload.get('attempt_id', 'unknown')}: "
        f"{payload.get('state', 'unknown')}  "
        f"files={payload.get('claimed_file_count', 0)}/{payload.get('file_count', 0)}"
    )


def format_upload_receipt(payload: Mapping[str, object]) -> str:
    return "\n".join(
        (
            f"Jeb ingress {payload.get('status', 'unknown')}",
            f"upload: {payload.get('upload_id', 'unknown')}",
            f"path: {payload.get('path', 'unknown')}",
            f"bytes: {_bytes(payload.get('bytes'))}",
            f"payload sha256: {payload.get('payload_sha256', 'unknown')}",
            f"provenance: {payload.get('provenance_identity', 'unknown')}",
        )
    )


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


def _target_config_text(payload: Mapping[str, object]) -> str:
    value = payload.get("target_config")
    if not isinstance(value, Mapping):
        return "{}"
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _integer(value: object) -> int:
    try:
        return int(str(value))
    except ValueError:
        return 0


def format_source(payload: Mapping[str, object]) -> str:
    raw_adapters = payload.get("adapters")
    adapters = (
        ",".join(str(adapter) for adapter in raw_adapters)
        if isinstance(raw_adapters, Sequence) and not isinstance(raw_adapters, (str, bytes))
        else "none"
    )
    raw_extensions = payload.get("include_extensions")
    extensions = (
        ",".join(str(extension) for extension in raw_extensions)
        if isinstance(raw_extensions, Sequence)
        and not isinstance(raw_extensions, (str, bytes))
        and raw_extensions
        else "all"
    )
    return "\n".join(
        [
            f"Jeb source {payload.get('id', 'unknown')}",
            f"state: {'enabled' if payload.get('enabled') else 'disabled'}",
            f"adapters: {adapters}",
            f"stable: {payload.get('stable_seconds', 'unknown')} seconds",
            f"extensions: {extensions}",
            f"target: {payload.get('target', 'unknown')}",
            f"target config: {_target_config_text(payload)}",
            f"threshold: {_bytes(payload.get('threshold_bytes'))}",
            f"cleanup: {payload.get('cleanup', 'unknown')}",
            "schedule: "
            f"{payload.get('cadence', 'unknown')} weekday={payload.get('weekday', 'unknown')} "
            f"at={_integer(payload.get('hour')):02d}:{_integer(payload.get('minute')):02d}",
        ]
    )


def format_source_result(payload: Mapping[str, object]) -> str:
    source = payload.get("source")
    rendered = format_source(source) if isinstance(source, Mapping) else format_source(payload)
    credential = payload.get("credential")
    if credential is not None:
        rendered += f"\ncredential: {credential}"
    return rendered


def format_source_removal(payload: Mapping[str, object]) -> str:
    return "\n".join(
        [
            f"removed Jeb source {payload.get('source', 'unknown')}",
            f"purged: {str(bool(payload.get('purged'))).lower()}",
            f"files removed: {payload.get('files', 0)}",
            f"bytes removed: {_bytes(payload.get('bytes'))}",
        ]
    )


def format_status(payload: Mapping[str, object]) -> str:
    sources = _items(payload, "sources")
    batches = payload.get("batches")
    unresolved_attempts = payload.get("unresolved_attempts")
    attempt_count = 0
    if isinstance(unresolved_attempts, Mapping):
        attempt_count = int(unresolved_attempts.get("total") or 0)
    lines = [f"Jeb status: sources={len(sources)} unresolved_attempts={attempt_count}"]
    for source in sources:
        lines.append(
            f"- {source.get('id', source.get('source_id', 'unknown'))}  "
            f"state={'enabled' if source.get('enabled') else 'disabled'}"
        )
    if isinstance(batches, Mapping):
        lines.append(
            f"batches: total={batches.get('total', 0)} "
            f"unresolved={batches.get('unresolved', 0)} resolved={batches.get('resolved', 0)}"
        )
    incomplete = payload.get("incomplete_tus_uploads")
    if isinstance(incomplete, Mapping):
        lines.append(
            "TUS incomplete: "
            f"{incomplete.get('total', 0)} ({_bytes(incomplete.get('bytes'))}), "
            f"stale={incomplete.get('stale', 0)}, "
            f"oldest={incomplete.get('oldest_age_seconds', 0)}s"
        )
    publications = payload.get("ingress_publications")
    if isinstance(publications, Mapping):
        lines.append(
            "ingress publications: "
            f"pending={publications.get('pending', 0)} "
            f"accepted={publications.get('accepted', 0)} "
            f"rejected={publications.get('rejected', 0)}"
        )
    return "\n".join(lines)


def format_archive_plan(payload: Mapping[str, object]) -> str:
    lines = [
        f"Jeb archive plan: {payload.get('source', payload.get('source_id', 'unknown'))}",
        f"status: {payload.get('status', 'unknown')}",
        f"eligible files: {payload.get('file_count', 0)}",
        f"eligible bytes: {payload.get('total_bytes', 0)}",
    ]
    target_preflight = payload.get("target_preflight")
    if isinstance(target_preflight, Mapping) and target_preflight.get("error"):
        lines.append(f"error: {target_preflight['error']}")
    if payload.get("period_start") or payload.get("period_end"):
        lines.append(f"period: {payload.get('period_start')} — {payload.get('period_end')}")
    return "\n".join(lines)


def archive_plan_exit_code(payload: Mapping[str, object]) -> int:
    return 0 if str(payload.get("status") or "").startswith("would_") else 1


def format_config_check(payload: Mapping[str, object]) -> str:
    return f"Jeb config: {payload.get('status', payload.get('state', 'unknown'))}"


def format_operation(payload: Mapping[str, object], *, title: str) -> str:
    nested = payload.get("operation")
    operation = nested if isinstance(nested, Mapping) else payload
    text = f"{title}: {payload.get('status', operation.get('state', 'complete'))}"
    if operation.get("id"):
        text += f"  operation={operation['id']}"
    return text


def format_operations(payload: Mapping[str, object]) -> str:
    lines = [_page_line(payload, "Jeb operations")]
    for operation in _items(payload, "operations"):
        lines.append(
            f"- {operation.get('id', 'unknown')}  "
            f"operation={operation.get('operation', 'unknown')}  "
            f"state={operation.get('state', 'unknown')}  "
            f"started={operation.get('started_at', 'unknown')}"
        )
    return "\n".join(lines)


def format_operation_detail(payload: Mapping[str, object]) -> str:
    lines = [
        f"Jeb operation {payload.get('id', 'unknown')}",
        f"operation: {payload.get('operation', 'unknown')}",
        f"state: {payload.get('state', 'unknown')}",
        f"started: {payload.get('started_at', 'unknown')}",
    ]
    if payload.get("source"):
        lines.append(f"source: {payload['source']}")
    if payload.get("attempt_id"):
        lines.append(f"attempt: {payload['attempt_id']}")
    if payload.get("completed_at"):
        lines.append(f"completed: {payload['completed_at']}")
    if payload.get("failure"):
        lines.append(f"failure: {payload['failure']}")
    return "\n".join(lines)


__all__ = [
    "archive_plan_exit_code",
    "format_archive_plan",
    "format_attempt",
    "format_attempt_transition",
    "format_attempts",
    "format_config_check",
    "format_operation",
    "format_operation_detail",
    "format_operations",
    "format_source",
    "format_source_removal",
    "format_source_result",
    "format_sources",
    "format_status",
]
