from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any

ENTITY_ID_STYLE = "bold cyan"
FIELD_STYLE = "dim"
ATTENTION_STYLE = "bold yellow"


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


def _archive_copy_states(payload: Mapping[str, object]) -> str:
    copies = _items(payload, "archive_copies")
    if not copies:
        return "none"
    return ", ".join(
        f"{copy.get('store', 'unknown')}={copy.get('state', 'pending')}" for copy in copies
    )


def format_find(payload: Mapping[str, object]) -> str:
    lines = [_page_line(payload, "files")]
    for file in _items(payload, "files"):
        lines.append(f"- {file.get('logical_path', 'unknown')}  {_bytes(file.get('bytes'))}")
    return "\n".join(lines)


def format_collections(payload: Mapping[str, object]) -> str:
    lines = [_page_line(payload, "collections")]
    for collection in _items(payload, "collections"):
        lines.append(
            f"- {collection.get('id', 'unknown')}  files={collection.get('files', 0)}  "
            f"bytes={_bytes(collection.get('bytes'))}  "
            f"archive={_archive_copy_states(collection)}"
        )
    return "\n".join(lines)


def format_apps(payload: Mapping[str, object]) -> str:
    lines = [_page_line(payload, "apps")]
    for application in _items(payload, "apps"):
        lines.append(
            f"- {application.get('name', 'unknown')}  "
            f"keys={application.get('active_keys', 0)}/{application.get('keys', 0)}  "
            f"last_used={application.get('last_used_at') or 'never'}"
        )
    return "\n".join(lines)


def format_app_keys(payload: Mapping[str, object]) -> str:
    lines = [
        f"app: {payload.get('app', 'unknown')}",
        _page_line(payload, "keys"),
    ]
    for key in _items(payload, "keys"):
        lines.append(
            f"- {key.get('id', 'unknown')}  status={key.get('status', 'unknown')}  "
            f"created={key.get('created_at', 'unknown')}  "
            f"expires={key.get('expires_at') or 'never'}  "
            f"last_used={key.get('last_used_at') or 'never'}"
        )
    return "\n".join(lines)


def format_app_key_created(payload: Mapping[str, object]) -> str:
    return "\n".join(
        [
            "app key created",
            f"app: {payload.get('app', 'unknown')}",
            f"key: {payload.get('id', 'unknown')}",
            f"expires: {payload.get('expires_at') or 'never'}",
            f"token: {payload.get('token', '')}",
            "Save this token now; Riverhog will not show it again.",
        ]
    )


def format_app_key_revoked(payload: Mapping[str, object]) -> str:
    return "\n".join(
        [
            "app key revoked",
            f"app: {payload.get('app', 'unknown')}",
            f"key: {payload.get('id', 'unknown')}",
            f"revoked: {payload.get('revoked_at', 'unknown')}",
        ]
    )


def format_collection_summary(
    payload: Mapping[str, object],
    archive_report: Mapping[str, object] | None = None,
) -> str:
    lines = [
        f"collection {payload.get('id', 'unknown')}",
        f"files: {payload.get('files', 0)}",
        f"bytes: {_bytes(payload.get('bytes'))}",
        f"archive copies: {_archive_copy_states(payload)}",
    ]
    for archive in _items(payload, "archive_copies"):
        store = archive.get("store", "unknown")
        if archive.get("storage_class"):
            lines.append(f"{store} storage class: {archive['storage_class']}")
        if archive.get("last_verified_at"):
            lines.append(f"{store} verified: {archive['last_verified_at']}")
    if archive_report is not None:
        totals = archive_report.get("totals")
        if isinstance(totals, Mapping):
            lines.append(f"remote storage: {_bytes(totals.get('measured_storage_bytes'))}")
    return "\n".join(lines)


def format_collection_deletion_plan(payload: Mapping[str, object]) -> str:
    archive_objects = payload.get("archive_objects")
    archive_object_count = (
        len(archive_objects)
        if isinstance(archive_objects, Sequence) and not isinstance(archive_objects, (str, bytes))
        else 0
    )
    lines = [
        str(payload.get("warning", "DANGER: This collection deletion is permanent.")),
        "",
        f"collection deletion plan: {payload.get('collection_id', 'unknown')}",
        f"status: {payload.get('status', 'unknown')}",
        f"files: {payload.get('file_count', 0)} ({_bytes(payload.get('bytes'))})",
        f"remote storage: {_bytes(payload.get('remote_storage_bytes'))}",
        f"archive objects: {archive_object_count}",
    ]
    blockers = payload.get("blockers")
    if isinstance(blockers, Sequence) and not isinstance(blockers, (str, bytes)):
        lines.extend(f"blocked: {blocker}" for blocker in blockers)
    if payload.get("billing_note"):
        lines.append(f"billing: {payload['billing_note']}")
    if payload.get("expires_at"):
        lines.append(f"plan expires: {payload['expires_at']}")
    if payload.get("challenge"):
        lines.append(f"confirmation challenge: {payload['challenge']}")
    return "\n".join(lines)


def format_collection_deletion_result(payload: Mapping[str, object]) -> str:
    return "\n".join(
        [
            f"collection deletion: {payload.get('status', 'unknown')}",
            f"collection: {payload.get('collection_id', 'unknown')}",
            f"files: {payload.get('files', 0)} ({_bytes(payload.get('bytes'))})",
            f"remote storage: {_bytes(payload.get('remote_storage_bytes'))}",
        ]
    )


def format_archive_copy_retirement_plan(payload: Mapping[str, object]) -> str:
    target = payload.get("target_copy")
    target_copy = target if isinstance(target, Mapping) else {}
    retained = _items(payload, "retained_copies")
    objects = target_copy.get("objects")
    object_count = (
        len(objects)
        if isinstance(objects, Sequence) and not isinstance(objects, (str, bytes))
        else 0
    )
    lines = [
        str(payload.get("warning", "DANGER: This archive copy retirement is permanent.")),
        "",
        f"archive copy retirement plan: {payload.get('collection_id', 'unknown')}",
        f"store: {payload.get('store', 'unknown')}",
        f"status: {payload.get('status', 'unknown')}",
        f"remote storage: {_bytes(target_copy.get('remote_storage_bytes'))}",
        f"archive objects: {object_count}",
        "retained copies: "
        + (
            ", ".join(str(copy.get("store", "unknown")) for copy in retained)
            if retained
            else "none"
        ),
    ]
    blockers = payload.get("blockers")
    if isinstance(blockers, Sequence) and not isinstance(blockers, (str, bytes)):
        lines.extend(f"blocked: {blocker}" for blocker in blockers)
    if payload.get("verification_note"):
        lines.append(f"verification: {payload['verification_note']}")
    if payload.get("billing_note"):
        lines.append(f"billing: {payload['billing_note']}")
    if payload.get("expires_at"):
        lines.append(f"plan expires: {payload['expires_at']}")
    if payload.get("challenge"):
        lines.append(f"confirmation challenge: {payload['challenge']}")
    return "\n".join(lines)


def format_archive_copy_retirement_result(payload: Mapping[str, object]) -> str:
    lines = [
        f"archive copy retirement: {payload.get('status', 'unknown')}",
        f"collection: {payload.get('collection_id', 'unknown')}",
        f"store: {payload.get('store', 'unknown')}",
        f"remote storage: {_bytes(payload.get('remote_storage_bytes'))}",
    ]
    if payload.get("verified_store"):
        lines.append(f"verified retained store: {payload['verified_store']}")
    return "\n".join(lines)


def format_collection_upload(payload: Mapping[str, object]) -> str:
    lines = [
        f"collection upload {payload.get('collection_id', 'unknown')}",
        f"state: {payload.get('state', 'unknown')}",
        f"files: {payload.get('files_uploaded', 0)}/{payload.get('files_total', 0)}",
        f"bytes: {_bytes(payload.get('uploaded_bytes'))}/{_bytes(payload.get('bytes_total'))}",
    ]
    if payload.get("archive_phase"):
        lines.append(f"archive phase: {payload['archive_phase']}")
    if payload.get("latest_failure"):
        lines.append(f"failure: {payload['latest_failure']}")
    return "\n".join(lines)


def format_collection_upload_plan(payload: Mapping[str, object]) -> str:
    collection_id = payload.get("collection_id")
    identity = str(collection_id) if collection_id else "server-assigned"
    lines = [
        f"collection upload dry-run: {identity}",
        f"files: {payload.get('files_total', 0)}",
        f"bytes: {_bytes(payload.get('bytes_total'))}",
    ]
    return "\n".join(lines)


def format_archive_report(payload: Mapping[str, object]) -> str:
    totals = payload.get("totals")
    totals = totals if isinstance(totals, Mapping) else {}
    lines = [
        f"archive report: {payload.get('scope', 'all')}",
        f"collections: {totals.get('uploaded_collections', 0)}/{totals.get('collections', 0)}",
        f"remote storage: {_bytes(totals.get('measured_storage_bytes'))}",
    ]
    for allowance in _items(payload, "download_allowances"):
        lines.append(
            f"download {allowance.get('store', 'unknown')}: "
            f"{_bytes(allowance.get('accounted_bytes'))} used + "
            f"{_bytes(allowance.get('reserved_bytes'))} reserved / "
            f"{_bytes(allowance.get('effective_limit_bytes'))}; "
            f"resets {allowance.get('resets_at', 'unknown')}"
        )
    return "\n".join(lines)


def format_archive_copy_job(payload: Mapping[str, object]) -> str:
    route = (
        f"{payload.get('source_store', 'automatic')} -> "
        f"{payload.get('destination_store', 'unknown')}"
    )
    lines = [
        f"archive copy {payload.get('collection_id', 'unknown')}",
        f"route: {route}",
        f"state: {payload.get('state', 'unknown')}",
    ]
    if payload.get("failure"):
        lines.append(f"failure: {payload['failure']}")
    return "\n".join(lines)


def format_jeb_attempts(payload: Mapping[str, object]) -> str:
    lines = [_page_line(payload, "Jeb attempts")]
    for attempt in _items(payload, "attempts"):
        lines.append(
            f"- {attempt.get('id', attempt.get('attempt_id', 'unknown'))}  "
            f"source={attempt.get('source_id', attempt.get('source', 'unknown'))}  "
            f"state={attempt.get('state', 'unknown')}"
        )
    return "\n".join(lines)


def format_jeb_sources(payload: Mapping[str, object]) -> str:
    lines = [_page_line(payload, "Jeb sources")]
    for source in _items(payload, "sources"):
        raw_adapters = source.get("adapters")
        adapters = (
            ",".join(str(adapter) for adapter in raw_adapters)
            if isinstance(raw_adapters, Sequence) and not isinstance(raw_adapters, (str, bytes))
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


def format_file_selectors(
    payload: Mapping[str, object],
    key: str = "files",
) -> str:
    return "\n".join(
        f"{item['collection_id']}::{item['collection_path']}"
        for item in _items(payload, key)
        if item.get("collection_id") not in {None, ""}
        and item.get("collection_path") not in {None, ""}
    )


def format_jeb_status(payload: Mapping[str, object]) -> str:
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


def format_jeb_archive_plan(payload: Mapping[str, object]) -> str:
    lines = [
        f"Jeb archive plan: {payload.get('source', payload.get('source_id', 'unknown'))}",
        f"eligible files: {payload.get('file_count', 0)}",
        f"eligible bytes: {payload.get('bytes', 0)}",
    ]
    if payload.get("period_start") or payload.get("period_end"):
        lines.append(f"period: {payload.get('period_start')} — {payload.get('period_end')}")
    return "\n".join(lines)


def format_jeb_config_check(payload: Mapping[str, object]) -> str:
    return f"Jeb config: {payload.get('status', payload.get('state', 'unknown'))}"


def format_jeb_operation(payload: Mapping[str, object], *, title: str) -> str:
    return f"{title}: {payload.get('status', payload.get('state', 'complete'))}"
