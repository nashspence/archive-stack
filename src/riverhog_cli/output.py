from __future__ import annotations

import json
import os
import sys
from collections.abc import Mapping, Sequence
from typing import Any, Literal, cast

import typer

RichConsole: Any
RichGroup: Any
RichTable: Any
RichText: Any

FIELD_STYLE = "bold #c0ad6c"
ENTITY_ID_STYLE = "bold #8ec9cc"
ATTENTION_STYLE = "bold #ff8933"
_ATTENTION_TOKENS = {"partial"}
_FETCH_SHOW_FILE_PREVIEW_LIMIT = 8

try:
    from rich.console import Console as RichConsole
    from rich.console import Group as RichGroup
    from rich.table import Table as RichTable
    from rich.text import Text as RichText
except ModuleNotFoundError:  # pragma: no cover - exercised only in stripped environments
    RichConsole = None
    RichGroup = None
    RichTable = None
    RichText = None


def _plain_requested() -> bool:
    raw_value = os.getenv("RIVERHOG_CLI_PLAIN", "").strip().casefold()
    return raw_value in {"1", "true", "yes", "on"} or os.getenv("TERM") == "dumb"


def _rich_enabled() -> bool:
    return (
        RichConsole is not None
        and RichGroup is not None
        and RichTable is not None
        and RichText is not None
        and not _plain_requested()
    )


def _console() -> Any:
    if RichConsole is None:
        return None
    color_system: Literal["auto"] | None = "auto" if sys.stdout.isatty() else None
    return RichConsole(file=sys.stdout, color_system=color_system, highlight=False)


def _page_text(kind: str, payload: Mapping[str, Any]) -> Any:
    pages = payload.get("pages", 0)
    page = payload.get("page", 1)
    per_page = payload.get("per_page", 25)
    total = payload.get("total", 0)
    text = f"{kind} page {page}/{pages}  per_page={per_page}  total={total}"
    if RichText is None:
        return text
    return RichText(text, style="bold")


def _styled_text(value: object, style: str) -> Any:
    text = str(value)
    if RichText is None:
        return text
    return RichText(text, style=style)


def _entity_text(value: object) -> Any:
    return _styled_text(value, ENTITY_ID_STYLE)


def _attention_text(value: object) -> Any:
    text = str(value)
    normalized = text.casefold().replace("-", "_")
    if any(token in normalized for token in _ATTENTION_TOKENS):
        return _styled_text(text, ATTENTION_STYLE)
    return text


def _bytes_text(value: object) -> str:
    byte_count = _int_value(value)
    if byte_count < 1000:
        return f"{byte_count} B"
    scaled = float(byte_count)
    for unit in ("KB", "MB", "GB", "TB", "PB"):
        scaled /= 1000.0
        if scaled < 1000.0 or unit == "PB":
            return f"{scaled:.1f} {unit}"
    raise AssertionError("unreachable")


def _ratio_text(numerator: object, denominator: object) -> str:
    total = _int_value(denominator)
    value = _int_value(numerator)
    if total <= 0:
        return f"{_bytes_text(value)} / {_bytes_text(total)}"
    return f"{_bytes_text(value)} / {_bytes_text(total)} ({value / total * 100:.0f}%)"


def _count_ratio_text(numerator: object, denominator: object) -> str:
    total = _int_value(denominator)
    value = _int_value(numerator)
    if total <= 0:
        return f"{value}/{total}"
    return f"{value}/{total} ({value / total * 100:.0f}%)"


def _attention_if_nonzero(value: object) -> Any:
    text = str(_int_value(value))
    if _int_value(value) > 0:
        return _styled_text(text, ATTENTION_STYLE)
    return text


def _coverage_bool_text(value: object) -> Any:
    text = str(bool(value)).lower()
    if not bool(value):
        return _styled_text(text, ATTENTION_STYLE)
    return text


def _quiet_table(*columns: str) -> Any:
    table = RichTable(box=None, show_edge=False, padding=(0, 2), collapse_padding=True)
    for index, column in enumerate(columns):
        table.add_column(column, no_wrap=index == 0, header_style=FIELD_STYLE)
    return table


def _detail_table() -> Any:
    table = RichTable(
        box=None,
        show_edge=False,
        show_header=False,
        padding=(0, 2),
        collapse_padding=True,
    )
    table.add_column("Field", style=FIELD_STYLE, no_wrap=True)
    table.add_column("Value")
    return table


def _preview_lines(items: Sequence[str], *, limit: int = 5) -> str:
    if not items:
        return "none"
    shown = list(items[:limit])
    if len(items) > limit:
        shown.append(f"... {len(items) - limit} more")
    return "\n".join(shown)


def _preview_lines_with_total(
    items: Sequence[str],
    *,
    limit: int = 5,
    total: int | None = None,
) -> str:
    if not items:
        if total and total > 0:
            return f"... {total} paths"
        return "none"
    shown = list(items[:limit])
    remaining = (total if total is not None else len(items)) - len(shown)
    if remaining > 0:
        shown.append(f"... {remaining} more")
    return "\n".join(shown)


def _string_items(value: object) -> list[str]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        return []
    return [str(item) for item in value]


def _disc_label(disc: Mapping[str, object]) -> str:
    disc_id = str(disc.get("disc_id", "unknown"))
    image_id = str(disc.get("image_id", "unknown"))
    location = str(disc.get("location") or "unassigned")
    return f"{disc_id} ({image_id} @ {location})"


def _disc_lines(value: object, *, limit: int = 5) -> str:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        return "none"
    discs = [disc for disc in value if isinstance(disc, Mapping)]
    if not discs:
        return "none"
    lines = [
        f"{disc.get('label_text') or disc.get('disc_id', 'unknown')} @ "
        f"{disc.get('location') or 'unassigned'} "
        f"({disc.get('verification_state', disc.get('state', 'unknown'))})"
        for disc in discs[:limit]
    ]
    if len(discs) > limit:
        lines.append(f"... {len(discs) - limit} more")
    return "\n".join(lines)


def _collection_ids_text(collection_ids: object) -> str:
    if not isinstance(collection_ids, Sequence):
        return ""
    return ", ".join(str(item) for item in collection_ids)


def _collection_ids_lines(collection_ids: object) -> str:
    if not isinstance(collection_ids, Sequence) or isinstance(
        collection_ids, (str, bytes, bytearray)
    ):
        return "none"
    items = [str(item) for item in collection_ids]
    return "\n".join(items) if items else "none"


def _int_value(value: object, *, default: int = 0) -> int:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        return int(value)
    return default


def _partial_disc_coverage(verified: object, registered: object, required: object) -> bool:
    required_count = _int_value(required)
    return required_count > 0 and (
        _int_value(verified) < required_count or _int_value(registered) < required_count
    )


def _disc_coverage_text(verified: object, registered: object, required: object) -> Any:
    text = f"{verified}/{registered}/{required}"
    if _partial_disc_coverage(verified, registered, required):
        return _styled_text(text, ATTENTION_STYLE)
    return text


def _disc_detail_coverage_text(image: Mapping[str, Any]) -> Any:
    verified = image.get("discs_verified", 0)
    registered = image.get("discs_registered", 0)
    required = image.get("discs_required", 0)
    text = f"verified={verified}/{required} registered={registered}/{required}"
    if _partial_disc_coverage(verified, registered, required):
        return _styled_text(text, ATTENTION_STYLE)
    return text


def _find_collection_archive_entry(
    collection_id: str,
    archive_payload: Mapping[str, Any],
) -> Mapping[str, Any] | None:
    collections = archive_payload.get("collections")
    if not isinstance(collections, Sequence):
        return None
    for collection in collections:
        if isinstance(collection, Mapping) and str(collection.get("id")) == collection_id:
            return collection
    return None


def _targets_lines(value: object, *, limit: int = 8) -> str:
    targets = _string_items(value)
    return _preview_lines(targets, limit=limit)


def _fetch_status_lines(manifest: Mapping[str, Any]) -> tuple[list[str], list[str], list[str]]:
    pending: list[str] = []
    partial: list[str] = []
    byte_complete: list[str] = []

    entries = manifest.get("entries")
    if isinstance(entries, Sequence):
        for entry in entries:
            if not isinstance(entry, Mapping):
                continue
            path = str(entry.get("path", "unknown"))
            collection_id = str(entry.get("collection_id", "")).strip()
            label = f"{collection_id}/{path}" if collection_id else path
            total_bytes = int(entry.get("recovery_bytes", entry.get("bytes", 0)))
            uploaded_bytes = int(entry.get("uploaded_bytes", 0))
            upload_state = str(entry.get("upload_state", "pending"))
            expires_at = str(entry.get("upload_state_expires_at", "n/a"))

            if upload_state == "uploaded":
                continue
            if upload_state == "byte_complete" or (
                total_bytes > 0 and uploaded_bytes >= total_bytes
            ):
                byte_complete.append(f"- {label} ({uploaded_bytes}/{total_bytes} bytes)")
                continue
            if upload_state == "partial" or uploaded_bytes > 0:
                partial.append(
                    f"- {label} ({uploaded_bytes}/{total_bytes} bytes, expires {expires_at})"
                )
                continue
            pending.append(f"- {label}")

    return pending, partial, byte_complete


def _format_fetch_plain(summary: Mapping[str, Any], manifest: Mapping[str, Any]) -> str:
    pending, partial, byte_complete = _fetch_status_lines(manifest)

    lines = [
        f"fetch: {summary.get('id', 'unknown')} ({summary.get('state', 'unknown')})",
        f"name: {summary.get('name', 'unknown')}",
        "targets:",
        *[f"- {target}" for target in _string_items(summary.get("targets"))],
        "scope: "
        f"files={summary.get('files', 0)} "
        f"bytes={summary.get('bytes', 0)} "
        f"hot={summary.get('hot_files', 0)}/{summary.get('files', 0)} "
        f"disc={summary.get('disc_files', 0)}/{summary.get('files', 0)} "
        f"missing_bytes={summary.get('missing_bytes', 0)}",
    ]
    if summary.get("next_action"):
        lines.append(
            f"next: {summary.get('next_action')} - {summary.get('next_action_reason', '')}"
        )
    target_summaries = summary.get("target_summaries")
    if isinstance(target_summaries, Sequence) and target_summaries:
        lines.append("target summaries:")
        for target in target_summaries:
            if not isinstance(target, Mapping):
                continue
            lines.append(
                f"- {target.get('target', 'unknown')} "
                f"files={target.get('files', 0)} "
                f"bytes={target.get('bytes', 0)} "
                f"hot={target.get('hot_files', 0)}/{target.get('files', 0)} "
                f"missing={target.get('missing_files', 0)} "
                f"disc_missing={target.get('missing_with_disc_files', 0)} "
                f"archive_missing={target.get('missing_without_disc_files', 0)}"
            )
    archive_lines = _fetch_archive_restore_lines(summary)
    if archive_lines:
        lines.extend(archive_lines)
    files_preview = summary.get("files_preview")
    if isinstance(files_preview, Sequence) and files_preview:
        lines.append("files preview:")
        for file in files_preview[:_FETCH_SHOW_FILE_PREVIEW_LIMIT]:
            if isinstance(file, Mapping):
                lines.extend(_fetch_file_plain_lines(file))
        remaining = _int_value(summary.get("files", 0)) - _FETCH_SHOW_FILE_PREVIEW_LIMIT
        if remaining > 0:
            lines.append(f"... {remaining} more; use riverhog hot fetch files {summary.get('id')}")
    if not pending and not partial and not byte_complete:
        lines.append("entries: none")
        return "\n".join(lines)
    if pending:
        lines.append("pending:")
        lines.extend(pending)
    if partial:
        lines.append("partial:")
        lines.extend(partial)
    if byte_complete:
        lines.append("byte-complete:")
        lines.extend(byte_complete)
    return "\n".join(lines)


def _fetch_file_plain_lines(file: Mapping[str, Any]) -> list[str]:
    return [
        f"- {file.get('target', 'unknown')}",
        f"  bytes: {file.get('bytes', 0)}",
        f"  hot: {str(file.get('hot', False)).lower()}",
        f"  disc: {str(file.get('disc_coverage', False)).lower()}",
    ]


def _fetch_file_record_text(file: Mapping[str, Any], *, primary: bool) -> Any:
    title_style = ENTITY_ID_STYLE if primary else ""
    target = RichText(str(file.get("target", "unknown")), style=title_style)
    meta = RichText("  ")
    for index, (label, value) in enumerate(
        (
            ("bytes", _bytes_text(file.get("bytes", 0))),
            ("hot", _coverage_bool_text(file.get("hot", False))),
            ("disc", _coverage_bool_text(file.get("disc_coverage", False))),
        )
    ):
        if index:
            meta.append("   ")
        meta.append(f"{label}:", style=FIELD_STYLE)
        if isinstance(value, RichText):
            meta.append(" ")
            meta.append_text(value)
        else:
            meta.append(f" {value}")
    return RichGroup(target, meta)


def format_fetch(summary: Mapping[str, Any], manifest: Mapping[str, Any]) -> Any:
    if not _rich_enabled():
        return _format_fetch_plain(summary, manifest)

    pending, partial, byte_complete = _fetch_status_lines(manifest)
    table = _detail_table()
    table.add_row("fetch", _entity_text(summary.get("id", "unknown")))
    table.add_row("name", str(summary.get("name", "unknown")))
    table.add_row("targets", _targets_lines(summary.get("targets")))
    table.add_row("state", _attention_text(summary.get("state", "unknown")))
    table.add_row("files", str(summary.get("files", 0)))
    table.add_row("bytes", _bytes_text(summary.get("bytes", 0)))
    table.add_row("hot", _count_ratio_text(summary.get("hot_files", 0), summary.get("files", 0)))
    table.add_row(
        "disc",
        _count_ratio_text(summary.get("disc_files", 0), summary.get("files", 0)),
    )
    table.add_row("missing", _bytes_text(summary.get("missing_bytes", 0)))
    if summary.get("next_action"):
        table.add_row(
            "next",
            f"{summary.get('next_action')} - {summary.get('next_action_reason', '')}",
        )
    table.add_row("discs", _disc_lines(summary.get("discs")))

    renderables: list[Any] = [RichText("fetch", style="bold"), table]
    target_summaries = summary.get("target_summaries")
    if isinstance(target_summaries, Sequence) and target_summaries:
        target_table = _quiet_table(
            "Target",
            "Files",
            "Bytes",
            "Hot",
            "Missing",
            "Disc Missing",
            "Archive Missing",
        )
        for target in target_summaries:
            if not isinstance(target, Mapping):
                continue
            target_table.add_row(
                str(target.get("target", "unknown")),
                str(target.get("files", 0)),
                _bytes_text(target.get("bytes", 0)),
                _count_ratio_text(target.get("hot_files", 0), target.get("files", 0)),
                _attention_if_nonzero(target.get("missing_files", 0)),
                _attention_if_nonzero(target.get("missing_with_disc_files", 0)),
                _attention_if_nonzero(target.get("missing_without_disc_files", 0)),
            )
        renderables.extend([RichText("targets", style="bold"), target_table])
    archive_restores = _fetch_archive_restores_payload(summary)
    if archive_restores is not None and _int_value(archive_restores.get("total", 0)) > 0:
        restore_table = _quiet_table("Restore", "State", "Collections", "Paths", "Ready", "Expires")
        restores = archive_restores.get("restores")
        if isinstance(restores, Sequence):
            for restore in restores:
                if not isinstance(restore, Mapping):
                    continue
                restore_table.add_row(
                    str(restore.get("id", "unknown")),
                    _restore_state_text(restore.get("state", "unknown")),
                    _preview_lines(_restore_related_ids(restore, "collections"), limit=3),
                    _restore_paths_text(restore),
                    str(restore.get("ready_at") or "unknown"),
                    str(restore.get("expires_at") or "unknown"),
                )
        if not restore_table.rows:
            restore_table.add_row("none", "", "", "", "", "")
        renderables.extend(
            [
                RichText("archive restores", style="bold"),
                _fetch_archive_restores_scope_text(archive_restores),
                restore_table,
            ]
        )
    files_preview = summary.get("files_preview")
    if isinstance(files_preview, Sequence) and files_preview:
        renderables.append(RichText("files preview", style="bold"))
        for file in files_preview[:_FETCH_SHOW_FILE_PREVIEW_LIMIT]:
            if isinstance(file, Mapping):
                renderables.extend(["", _fetch_file_record_text(file, primary=False)])
        remaining = _int_value(summary.get("files", 0)) - _FETCH_SHOW_FILE_PREVIEW_LIMIT
        if remaining > 0:
            renderables.append(
                f"... {remaining} more; use riverhog hot fetch files {summary.get('id')}"
            )
    if pending or partial or byte_complete:
        status_table = _quiet_table("Status", "Items")
        if pending:
            status_table.add_row("pending", _preview_lines(pending, limit=8))
        if partial:
            status_table.add_row("partial", _preview_lines(partial, limit=8))
        if byte_complete:
            status_table.add_row("byte-complete", _preview_lines(byte_complete, limit=8))
        renderables.extend([RichText("entries", style="bold"), status_table])

    return RichGroup(*renderables)


def format_fetch_start_plan(payload: Mapping[str, Any]) -> Any:
    if not _rich_enabled():
        return "\n".join(
            [
                "hot fetch start dry-run",
                f"status: {payload.get('status', 'unknown')}",
                f"fetch: {payload.get('id', 'unknown')}",
                f"name: {payload.get('name', 'unknown')}",
                f"current state: {payload.get('state', 'unknown')}",
                f"queued state: {payload.get('queued_state', 'unknown')}",
                f"archive: {str(bool(payload.get('archive'))).lower()}",
                f"archive restore: {str(bool(payload.get('will_create_archive_restore'))).lower()}",
                f"files: {payload.get('files', 0)}",
                f"bytes: {_bytes_text(payload.get('bytes', 0))}",
                f"missing bytes: {_bytes_text(payload.get('missing_bytes', 0))}",
                f"targets: {', '.join(_string_items(payload.get('targets')))}",
            ]
        )

    table = _detail_table()
    table.add_row("status", _attention_text(payload.get("status", "unknown")))
    table.add_row("fetch", _entity_text(payload.get("id", "unknown")))
    table.add_row("name", str(payload.get("name", "unknown")))
    table.add_row("current state", str(payload.get("state", "unknown")))
    table.add_row("queued state", str(payload.get("queued_state", "unknown")))
    table.add_row("archive", str(bool(payload.get("archive"))).lower())
    table.add_row(
        "archive restore",
        str(bool(payload.get("will_create_archive_restore"))).lower(),
    )
    table.add_row("files", str(payload.get("files", 0)))
    table.add_row("bytes", _bytes_text(payload.get("bytes", 0)))
    table.add_row("missing bytes", _bytes_text(payload.get("missing_bytes", 0)))
    table.add_row("targets", _targets_lines(payload.get("targets")))
    return RichGroup(RichText("hot fetch start dry-run", style="bold"), table)


def _fetch_archive_restores_payload(summary: Mapping[str, Any]) -> Mapping[str, Any] | None:
    payload = summary.get("archive_restores")
    if not isinstance(payload, Mapping):
        return None
    return payload


def _fetch_archive_restore_lines(summary: Mapping[str, Any]) -> list[str]:
    archive_restores = _fetch_archive_restores_payload(summary)
    if archive_restores is None or _int_value(archive_restores.get("total", 0)) <= 0:
        return []
    returned = archive_restores.get(
        "restores_returned", len(_archive_restore_items(archive_restores))
    )
    lines = [
        "archive restores:",
        f"restores: {returned}/{archive_restores.get('total', 0)}",
    ]
    restores = archive_restores.get("restores")
    if not isinstance(restores, Sequence) or not restores:
        lines.append("- none")
        return lines
    for restore in restores:
        if not isinstance(restore, Mapping):
            continue
        lines.extend(
            [
                f"- {restore.get('id', 'unknown')} state={restore.get('state', 'unknown')}",
                "  collections: "
                f"{_collection_ids_text(_restore_related_ids(restore, 'collections'))}",
                f"  paths: {_restore_paths_text(restore)}",
                f"  ready: {restore.get('ready_at') or 'unknown'}",
                f"  expires: {restore.get('expires_at') or 'unknown'}",
            ]
        )
        if restore.get("latest_message"):
            lines.append(f"  message: {restore.get('latest_message')}")
    return lines


def _archive_restore_items(archive_restores: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    restores = archive_restores.get("restores")
    if not isinstance(restores, Sequence) or isinstance(restores, (str, bytes, bytearray)):
        return []
    return [restore for restore in restores if isinstance(restore, Mapping)]


def _restore_paths_text(restore: Mapping[str, Any]) -> str:
    paths = restore.get("paths")
    if paths is None:
        return "all"
    return _preview_lines(_string_items(paths), limit=3)


def _fetch_archive_restores_scope_text(archive_restores: Mapping[str, Any]) -> Any:
    text = RichText()
    returned = len(_archive_restore_items(archive_restores))
    fields: list[tuple[str, object]] = [
        ("restores", f"{returned}/{archive_restores.get('total', 0)}")
    ]
    for label in ("state", "type"):
        if archive_restores.get(label) is not None:
            fields.append((label, archive_restores.get(label)))
    for index, (label, value) in enumerate(fields):
        if index:
            text.append("  ")
        text.append(f"{label}:", style=FIELD_STYLE)
        text.append(f" {value}")
    return text


def _format_fetch_files_plain(payload: Mapping[str, Any]) -> str:
    lines = [
        "fetch files: "
        f"fetch={payload.get('fetch_id', 'unknown')} "
        f"page {payload.get('page', 1)}/{payload.get('pages', 0)} "
        f"per_page={payload.get('per_page', 25)} "
        f"total={payload.get('total', 0)} "
        f"sort={payload.get('sort', 'target')} "
        f"order={payload.get('order', 'asc')}",
    ]
    if payload.get("query") is not None:
        lines.append(f"query: {payload.get('query')}")
    if payload.get("hot") is not None:
        lines.append(f"hot: {str(payload.get('hot')).lower()}")
    if payload.get("disc_coverage") is not None:
        lines.append(f"disc: {str(payload.get('disc_coverage')).lower()}")
    files = payload.get("files")
    if not isinstance(files, Sequence) or not files:
        lines.append("- none")
        return "\n".join(lines)
    for file in files:
        if isinstance(file, Mapping):
            lines.extend(_fetch_file_plain_lines(file))
    return "\n".join(lines)


def _fetch_files_scope_text(payload: Mapping[str, Any]) -> Any:
    text = RichText()
    fields: list[tuple[str, object]] = [
        ("fetch", payload.get("fetch_id", "unknown")),
        ("sort", f"{payload.get('sort', 'target')} {payload.get('order', 'asc')}"),
    ]
    if payload.get("query") is not None:
        fields.insert(1, ("query", payload.get("query")))
    if payload.get("hot") is not None:
        fields.append(("hot", str(payload.get("hot")).lower()))
    if payload.get("disc_coverage") is not None:
        fields.append(("disc", str(payload.get("disc_coverage")).lower()))
    for index, (label, value) in enumerate(fields):
        if index:
            text.append("  ")
        text.append(f"{label}:", style=FIELD_STYLE)
        if label == "fetch":
            text.append(" ")
            text.append_text(_entity_text(value))
        else:
            text.append(f" {value}")
    return text


def format_fetch_files(payload: Mapping[str, Any]) -> Any:
    if not _rich_enabled():
        return _format_fetch_files_plain(payload)
    renderables: list[Any] = [_page_text("fetch files", payload), _fetch_files_scope_text(payload)]
    files = payload.get("files")
    if not isinstance(files, Sequence) or not files:
        return RichGroup(*renderables, "none")
    for file in files:
        if isinstance(file, Mapping):
            renderables.extend(["", _fetch_file_record_text(file, primary=True)])
    return RichGroup(*renderables)


def _restore_state_text(value: object) -> Any:
    text = str(value)
    normalized = text.casefold().replace("-", "_")
    if normalized in {"expired", "failed"}:
        return _styled_text(text, ATTENTION_STYLE)
    return _attention_text(text)


def _restore_related_ids(
    restore: Mapping[str, Any],
    key: str,
    *,
    id_key: str = "id",
) -> list[str]:
    payload = restore.get(key)
    if not isinstance(payload, Sequence) or isinstance(payload, (str, bytes, bytearray)):
        return []
    return [str(item.get(id_key, "unknown")) for item in payload if isinstance(item, Mapping)]


def _format_archive_restores_plain(payload: Mapping[str, Any]) -> str:
    lines = [
        "archive restores: "
        f"page {payload.get('page', 1)}/{payload.get('pages', 0)} "
        f"per_page={payload.get('per_page', 25)} "
        f"total={payload.get('total', 0)} "
        f"sort={payload.get('sort', 'created_at')} "
        f"order={payload.get('order', 'desc')}",
    ]
    for label in ("terminal", "type", "state", "collection", "image"):
        if payload.get(label) is not None:
            lines.append(f"{label}: {payload.get(label)}")
    restores = payload.get("restores")
    if not isinstance(restores, Sequence) or not restores:
        lines.append("- none")
        return "\n".join(lines)
    for restore in restores:
        if not isinstance(restore, Mapping):
            continue
        lines.extend(
            [
                f"- {restore.get('id', 'unknown')} "
                f"type={restore.get('type', 'unknown')} "
                f"state={restore.get('state', 'unknown')}",
                "  collections: "
                f"{_collection_ids_text(_restore_related_ids(restore, 'collections'))}",
                f"  images: {_collection_ids_text(_restore_related_ids(restore, 'images'))}",
                f"  ready: {restore.get('ready_at') or 'unknown'}",
                f"  expires: {restore.get('expires_at') or 'unknown'}",
            ]
        )
        if restore.get("latest_message"):
            lines.append(f"  message: {restore.get('latest_message')}")
    return "\n".join(lines)


def _archive_restore_scope_text(payload: Mapping[str, Any]) -> Any:
    text = RichText()
    fields: list[tuple[str, object]] = [
        ("sort", f"{payload.get('sort', 'created_at')} {payload.get('order', 'desc')}")
    ]
    for label in ("terminal", "type", "state", "collection", "image"):
        if payload.get(label) is not None:
            fields.append((label, payload.get(label)))
    for index, (label, value) in enumerate(fields):
        if index:
            text.append("  ")
        text.append(f"{label}:", style=FIELD_STYLE)
        text.append(f" {value}")
    return text


def format_archive_restores(payload: Mapping[str, Any]) -> Any:
    if not _rich_enabled():
        return _format_archive_restores_plain(payload)
    table = _quiet_table(
        "Restore",
        "State",
        "Type",
        "Collections",
        "Images",
        "Ready",
        "Expires",
    )
    restores = payload.get("restores")
    if isinstance(restores, Sequence):
        for restore in restores:
            if not isinstance(restore, Mapping):
                continue
            table.add_row(
                _entity_text(restore.get("id", "unknown")),
                _restore_state_text(restore.get("state", "unknown")),
                str(restore.get("type", "unknown")),
                _preview_lines(_restore_related_ids(restore, "collections"), limit=3),
                _preview_lines(_restore_related_ids(restore, "images"), limit=3),
                str(restore.get("ready_at") or "unknown"),
                str(restore.get("expires_at") or "unknown"),
            )
    if not table.rows:
        table.add_row("none", "", "", "", "", "", "")
    return RichGroup(
        _page_text("archive restores", payload),
        _archive_restore_scope_text(payload),
        table,
    )


def _format_archive_restore_plain(payload: Mapping[str, Any]) -> str:
    lines = [
        f"archive restore: {payload.get('id', 'unknown')}",
        f"type: {payload.get('type', 'unknown')}",
        f"state: {payload.get('state', 'unknown')}",
        f"created_at: {payload.get('created_at', 'unknown')}",
        f"requested_at: {payload.get('requested_at') or 'none'}",
        f"ready_at: {payload.get('ready_at') or 'none'}",
        f"expires_at: {payload.get('expires_at') or 'none'}",
        f"completed_at: {payload.get('completed_at') or 'none'}",
        f"canceled_at: {payload.get('canceled_at') or 'none'}",
        f"paused_at: {payload.get('paused_at') or 'none'}",
    ]
    if payload.get("paused_from_state"):
        lines.append(f"paused_from_state: {payload.get('paused_from_state')}")
    if payload.get("type") == "fetch_materialization":
        paths = _string_items(payload.get("paths"))
        lines.append("paths:")
        if paths:
            lines.extend(f"- {path}" for path in paths)
        else:
            lines.append("- all")
    if payload.get("latest_message"):
        lines.append(f"latest_message: {payload.get('latest_message')}")

    progress = payload.get("progress")
    if isinstance(progress, Mapping):
        lines.append(
            "progress: "
            f"archive_verification={progress.get('archive_verification', 'unknown')} "
            f"extraction={progress.get('extraction', 'unknown')} "
            f"materialization={progress.get('materialization', 'unknown')}"
        )

    notification = payload.get("notification")
    if isinstance(notification, Mapping) and notification.get("last_failure"):
        lines.append(
            "last_failure: "
            f"{notification.get('last_failure')} "
            f"at={notification.get('last_failure_at') or 'unknown'} "
            f"count={notification.get('failure_count', 0)}"
        )

    collections = payload.get("collections")
    lines.append("collections:")
    if not isinstance(collections, Sequence) or not collections:
        lines.append("- none")
    else:
        for collection in collections:
            if not isinstance(collection, Mapping):
                continue
            archive = collection.get("archive")
            archive_state = (
                archive.get("state", "unknown") if isinstance(archive, Mapping) else "unknown"
            )
            manifest = collection.get("collection_manifest")
            manifest_state = "uploaded" if isinstance(manifest, Mapping) else "missing"
            lines.append(
                f"- {collection.get('id', 'unknown')} "
                f"archive={archive_state} "
                f"stored_bytes={collection.get('stored_bytes', 0)} "
                f"manifest={manifest_state}"
            )

    images = payload.get("images")
    lines.append("images:")
    if not isinstance(images, Sequence) or not images:
        lines.append("- none")
    else:
        for image in images:
            if not isinstance(image, Mapping):
                continue
            lines.append(
                f"- {image.get('id', 'unknown')} "
                f"rebuild_state={image.get('rebuild_state', 'unknown')} "
                f"filename={image.get('filename', 'unknown')}"
            )
            lines.append(
                f"  collections: {_collection_ids_text(_string_items(image.get('collection_ids')))}"
            )

    warnings = _string_items(payload.get("warnings"))
    if warnings:
        lines.append("warnings:")
        lines.extend(f"- {warning}" for warning in warnings)
    return "\n".join(lines)


def _archive_restore_collections_table(payload: Mapping[str, Any]) -> Any:
    table = _quiet_table("Collection", "Archive", "Stored", "Manifest", "OTS")
    collections = payload.get("collections")
    if isinstance(collections, Sequence):
        for collection in collections:
            if not isinstance(collection, Mapping):
                continue
            archive = collection.get("archive")
            archive_state = (
                str(archive.get("state", "unknown")) if isinstance(archive, Mapping) else "unknown"
            )
            manifest = collection.get("collection_manifest")
            manifest_path = (
                str(manifest.get("object_path") or "missing")
                if isinstance(manifest, Mapping)
                else "missing"
            )
            ots_state = (
                str(manifest.get("ots_state", "missing"))
                if isinstance(manifest, Mapping)
                else "missing"
            )
            table.add_row(
                str(collection.get("id", "unknown")),
                _attention_text(archive_state),
                _bytes_text(collection.get("stored_bytes", 0)),
                manifest_path,
                _attention_text(ots_state),
            )
    if not table.rows:
        table.add_row("none", "", "", "", "")
    return table


def _archive_restore_images_table(payload: Mapping[str, Any]) -> Any:
    table = _quiet_table("Image", "Rebuild", "Filename", "Collections")
    images = payload.get("images")
    if isinstance(images, Sequence):
        for image in images:
            if not isinstance(image, Mapping):
                continue
            table.add_row(
                str(image.get("id", "unknown")),
                _restore_state_text(image.get("rebuild_state", "unknown")),
                str(image.get("filename", "unknown")),
                _collection_ids_lines(image.get("collection_ids")),
            )
    if not table.rows:
        table.add_row("none", "", "", "")
    return table


def format_archive_restore(payload: Mapping[str, Any]) -> Any:
    if not _rich_enabled():
        return _format_archive_restore_plain(payload)

    overview = _detail_table()
    overview.add_row("restore", _entity_text(payload.get("id", "unknown")))
    overview.add_row("type", str(payload.get("type", "unknown")))
    overview.add_row("state", _restore_state_text(payload.get("state", "unknown")))
    overview.add_row("created", str(payload.get("created_at", "unknown")))
    overview.add_row("restore requested", str(payload.get("requested_at") or "none"))
    overview.add_row("ready", str(payload.get("ready_at") or "none"))
    overview.add_row("expires", str(payload.get("expires_at") or "none"))
    overview.add_row("completed", str(payload.get("completed_at") or "none"))
    if payload.get("canceled_at"):
        overview.add_row("canceled", str(payload.get("canceled_at")))
    if payload.get("paused_at"):
        overview.add_row("paused", str(payload.get("paused_at")))
    if payload.get("paused_from_state"):
        overview.add_row("paused from", str(payload.get("paused_from_state")))
    if payload.get("type") == "fetch_materialization":
        paths = _string_items(payload.get("paths"))
        overview.add_row("restore paths", _preview_lines(paths, limit=8) if paths else "all")
    if payload.get("latest_message"):
        overview.add_row("message", str(payload.get("latest_message")))
    notification = payload.get("notification")
    if isinstance(notification, Mapping) and notification.get("last_failure"):
        overview.add_row(
            "last failure",
            (
                f"{notification.get('last_failure')} "
                f"at {notification.get('last_failure_at') or 'unknown'} "
                f"({notification.get('failure_count', 0)} attempts)"
            ),
        )

    progress = payload.get("progress")
    if isinstance(progress, Mapping):
        progress_table = _detail_table()
        progress_table.add_row(
            "archive verification",
            _restore_state_text(progress.get("archive_verification", "unknown")),
        )
        progress_table.add_row(
            "extraction",
            _restore_state_text(progress.get("extraction", "unknown")),
        )
        progress_table.add_row(
            "materialization",
            _restore_state_text(progress.get("materialization", "unknown")),
        )
    else:
        progress_table = None

    renderables: list[Any] = [
        RichText(f"archive restore {payload.get('id', 'unknown')}", style="bold"),
        overview,
        RichText("collections", style="bold"),
        _archive_restore_collections_table(payload),
        RichText("images", style="bold"),
        _archive_restore_images_table(payload),
    ]
    if progress_table is not None:
        renderables.extend([RichText("progress", style="bold"), progress_table])
    warnings = _string_items(payload.get("warnings"))
    if warnings:
        renderables.extend([RichText("warnings", style="bold"), _preview_lines(warnings, limit=5)])
    return RichGroup(*renderables)


def _format_collections_plain(payload: Mapping[str, Any]) -> str:
    lines = [
        "collections: "
        f"page {payload.get('page', 1)}/{payload.get('pages', 0)} "
        f"per_page={payload.get('per_page', 25)} "
        f"total={payload.get('total', 0)}",
    ]
    collections = payload.get("collections")
    if not isinstance(collections, Sequence) or not collections:
        lines.append("- none")
        return "\n".join(lines)
    for collection in collections:
        if not isinstance(collection, Mapping):
            continue
        disc_coverage = collection.get("disc_coverage")
        disc_redundancy = collection.get("disc_redundancy")
        archive = collection.get("archive")
        coverage_text = "unknown"
        redundancy_text = "unknown"
        archive_text = "unknown"
        if isinstance(disc_coverage, Mapping):
            coverage_text = (
                f"{disc_coverage.get('state', 'unknown')} {disc_coverage.get('bytes', 0)}"
            )
        if isinstance(disc_redundancy, Mapping):
            redundancy_text = (
                f"{disc_redundancy.get('state', 'unknown')} {disc_redundancy.get('bytes', 0)}"
            )
        if isinstance(archive, Mapping):
            archive_text = str(archive.get("state", "unknown"))
        lines.append(
            f"- {collection.get('id', 'unknown')} "
            f"files={collection.get('files', 0)} "
            f"bytes={collection.get('bytes', 0)} "
            f"hot={collection.get('hot_bytes', 0)}/{collection.get('bytes', 0)} "
            f"archive={archive_text} "
            f"disc={coverage_text} "
            f"redundancy={redundancy_text}"
        )
    return "\n".join(lines)


def format_collections(payload: Mapping[str, Any]) -> Any:
    if not _rich_enabled():
        return _format_collections_plain(payload)
    table = _quiet_table(
        "Collection slug",
        "Files",
        "Bytes",
        "Hot",
        "Archive",
        "Disc coverage",
        "Disc redundancy",
    )
    collections = payload.get("collections")
    if isinstance(collections, Sequence):
        for collection in collections:
            if not isinstance(collection, Mapping):
                continue
            disc_coverage = collection.get("disc_coverage")
            disc_redundancy = collection.get("disc_redundancy")
            archive = collection.get("archive")
            if isinstance(disc_coverage, Mapping):
                disc_text = (
                    f"{disc_coverage.get('state', 'unknown')} "
                    f"{_bytes_text(disc_coverage.get('bytes', 0))}"
                )
            else:
                disc_text = "unknown"
            redundancy_text = "unknown"
            if isinstance(disc_redundancy, Mapping):
                redundancy_text = (
                    f"{disc_redundancy.get('state', 'unknown')} "
                    f"{_bytes_text(disc_redundancy.get('bytes', 0))}"
                )
            archive_text = (
                str(archive.get("state", "unknown")) if isinstance(archive, Mapping) else "unknown"
            )
            table.add_row(
                _entity_text(collection.get("id", "unknown")),
                str(collection.get("files", 0)),
                _bytes_text(collection.get("bytes", 0)),
                _ratio_text(collection.get("hot_bytes", 0), collection.get("bytes", 0)),
                _attention_text(archive_text),
                _attention_text(disc_text),
                _attention_text(redundancy_text),
            )
    if not table.rows:
        table.add_row("none", "", "", "", "", "", "")
    return RichGroup(_page_text("collections", payload), table)


def _format_fetches_plain(payload: Mapping[str, Any]) -> str:
    lines = [
        "fetches: "
        f"page {payload.get('page', 1)}/{payload.get('pages', 0)} "
        f"per_page={payload.get('per_page', 25)} "
        f"total={payload.get('total', 0)}"
    ]
    fetches = payload.get("fetches")
    if not isinstance(fetches, Sequence) or not fetches:
        lines.append("- none")
        return "\n".join(lines)
    for fetch in fetches:
        if not isinstance(fetch, Mapping):
            continue
        lines.append(
            f"- {fetch.get('id', 'unknown')} "
            f"name={fetch.get('name', 'unknown')} "
            f"state={fetch.get('state', 'unknown')} "
            f"files={fetch.get('files', 0)} "
            f"bytes={fetch.get('bytes', 0)} "
            f"missing={fetch.get('missing_bytes', 0)}"
        )
        targets = _string_items(fetch.get("targets"))
        if targets:
            lines.append(f"  targets: {', '.join(targets)}")
    return "\n".join(lines)


def format_fetches(payload: Mapping[str, Any]) -> Any:
    if not _rich_enabled():
        return _format_fetches_plain(payload)
    table = _quiet_table("Fetch", "Name", "State", "Missing", "Files", "Bytes", "Targets")
    fetches = payload.get("fetches")
    if isinstance(fetches, Sequence):
        for fetch in fetches:
            if not isinstance(fetch, Mapping):
                continue
            table.add_row(
                _entity_text(fetch.get("id", "unknown")),
                str(fetch.get("name", "unknown")),
                _attention_text(fetch.get("state", "unknown")),
                _bytes_text(fetch.get("missing_bytes", 0)),
                str(fetch.get("files", 0)),
                _bytes_text(fetch.get("bytes", 0)),
                _targets_lines(fetch.get("targets"), limit=3),
            )
    if not table.rows:
        table.add_row("none", "", "", "", "", "", "")
    return RichGroup(_page_text("fetches", payload), table)


def _jeb_preview(value: object, *, limit: int = 160) -> str:
    text = str(value or "").replace("\n", " ").strip()
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."


def _mapping_items(value: object) -> list[Mapping[str, Any]]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        return []
    return [cast(Mapping[str, Any], item) for item in value if isinstance(item, Mapping)]


def _optional_mapping(value: object) -> Mapping[str, Any]:
    return cast(Mapping[str, Any], value) if isinstance(value, Mapping) else {}


def _jeb_account_text(payload: Mapping[str, Any]) -> str:
    return str(payload.get("account_id") or "-")


def _jeb_state_counts_text(counts: Mapping[str, Any]) -> str:
    if not counts:
        return "none"
    return ", ".join(f"{state}={counts[state]}" for state in sorted(counts))


def _jeb_filters_text(payload: Mapping[str, Any]) -> str:
    filters = _optional_mapping(payload.get("filters"))
    active_filters = [
        f"{key}={value}" for key, value in filters.items() if value not in (None, [], "")
    ]
    return "  ".join(active_filters)


def _jeb_attempt_plain_line(attempt: Mapping[str, Any]) -> str:
    parts = [
        str(attempt.get("attempt_id") or "unknown"),
        f"account={_jeb_account_text(attempt)}",
        f"collection_slug={attempt.get('collection_slug') or '-'}",
        f"state={attempt.get('state') or 'unknown'}",
        f"files={attempt.get('file_count', 0)}",
        f"bytes={_bytes_text(attempt.get('total_bytes', 0))}",
        f"cleanup={attempt.get('cleanup') or '-'}",
    ]
    if attempt.get("job_id"):
        parts.append(f"job={attempt.get('job_id')}")
    if attempt.get("updated_at"):
        parts.append(f"updated={attempt.get('updated_at')}")
    if attempt.get("last_error"):
        parts.append(f"error={_jeb_preview(attempt.get('last_error'))}")
    return "  ".join(parts)


def _format_jeb_attempts_plain(payload: Mapping[str, Any]) -> str:
    header = (
        f"jeb attempts: page {payload.get('page', 1)}/{payload.get('pages', 0)} "
        f"per_page={payload.get('per_page', 25)} "
        f"total={payload.get('total', 0)} "
        f"sort={payload.get('sort', 'updated_at')} "
        f"order={payload.get('order', 'desc')} "
        f"terminal={payload.get('terminal', 'active')}"
    )
    if payload.get("query"):
        header += f" query={payload.get('query')}"
    filters_text = _jeb_filters_text(payload)
    if filters_text:
        header += f" {filters_text}"
    lines = [header]
    attempts = _mapping_items(payload.get("attempts"))
    if not attempts:
        lines.append("- none")
        return "\n".join(lines)
    lines.extend(f"- {_jeb_attempt_plain_line(attempt)}" for attempt in attempts)
    return "\n".join(lines)


def format_jeb_attempts(payload: Mapping[str, Any]) -> Any:
    if not _rich_enabled():
        return _format_jeb_attempts_plain(payload)
    renderables: list[Any] = [_page_text("jeb attempts", payload)]
    scope_items = [
        f"sort={payload.get('sort', 'updated_at')}",
        f"order={payload.get('order', 'desc')}",
        f"terminal={payload.get('terminal', 'active')}",
    ]
    if payload.get("query"):
        scope_items.append(f"query={payload.get('query')}")
    filters_text = _jeb_filters_text(payload)
    if filters_text:
        scope_items.append(filters_text)
    renderables.append("  ".join(scope_items))

    table = _quiet_table(
        "Attempt",
        "Account",
        "Collection",
        "State",
        "Files",
        "Bytes",
        "Cleanup",
        "Updated",
    )
    for attempt in _mapping_items(payload.get("attempts")):
        table.add_row(
            _entity_text(attempt.get("attempt_id") or "unknown"),
            _jeb_account_text(attempt),
            str(attempt.get("collection_slug") or "-"),
            _attention_text(attempt.get("state") or "unknown"),
            str(attempt.get("file_count", 0)),
            _bytes_text(attempt.get("total_bytes", 0)),
            str(attempt.get("cleanup") or "-"),
            str(attempt.get("updated_at") or "-"),
        )
    if not table.rows:
        table.add_row("none", "", "", "", "", "", "", "")
    renderables.append(table)
    return RichGroup(*renderables)


def _jeb_account_plain_line(account: Mapping[str, Any]) -> str:
    enabled = "enabled" if account.get("enabled") else "disabled"
    path_state = "present" if account.get("path_exists") else "missing"
    routing = "failed" if account.get("routing_preflight_failed") else "ok"
    parts = [
        str(account.get("id") or "unknown"),
        enabled,
        f"path={path_state}",
        f"routing_preflight={routing}",
        f"collection_slug={account.get('collection_slug') or '-'}",
    ]
    if "eligible_files" in account:
        parts.append(f"eligible={account.get('eligible_files')} files")
        parts.append(f"bytes={_bytes_text(account.get('eligible_bytes', 0))}")
    if account.get("eligible_error"):
        parts.append(f"eligible_error={_jeb_preview(account.get('eligible_error'))}")
    return "  ".join(parts)


def _format_jeb_status_plain(payload: Mapping[str, Any]) -> str:
    accounts = _mapping_items(payload.get("accounts"))
    batch_counts = _optional_mapping(payload.get("batches"))
    preflight = _optional_mapping(payload.get("routing_preflight_failures"))
    lines = [
        "jeb status",
        "accounts: "
        f"{sum(1 for account in accounts if account.get('enabled'))}/{len(accounts)} enabled",
        (
            f"batches: total={batch_counts.get('total', 0)} "
            f"active={batch_counts.get('active', 0)} "
            f"terminal={batch_counts.get('terminal', 0)}"
        ),
        f"states: {_jeb_state_counts_text(_optional_mapping(batch_counts.get('states')))}",
        f"routing preflight failures: {preflight.get('total', 0)}",
    ]
    active_operation = _optional_mapping(payload.get("active_operation"))
    if active_operation:
        lines.append(
            "active operation: "
            f"{active_operation.get('operation', 'unknown')} "
            f"id={active_operation.get('id', 'unknown')} "
            f"account={active_operation.get('account') or '-'} "
            f"attempt={active_operation.get('attempt_id') or '-'}"
        )

    lines.append("accounts:")
    if accounts:
        lines.extend(f"- {_jeb_account_plain_line(account)}" for account in accounts)
    else:
        lines.append("- none")

    active_attempts = _optional_mapping(payload.get("active_attempts"))
    active_attempt_rows = _mapping_items(active_attempts.get("attempts"))
    lines.append("active attempts:")
    if active_attempt_rows:
        lines.extend(f"- {_jeb_attempt_plain_line(attempt)}" for attempt in active_attempt_rows)
        total_active_listed = _int_value(active_attempts.get("total", 0))
        if total_active_listed > len(active_attempt_rows):
            lines.append(f"- ... {total_active_listed - len(active_attempt_rows)} more")
    else:
        lines.append("- none")

    recent_failures = _optional_mapping(payload.get("recent_failures"))
    failure_attempts = _mapping_items(recent_failures.get("attempts"))
    lines.append("recent failures:")
    if failure_attempts:
        lines.extend(f"- {_jeb_attempt_plain_line(attempt)}" for attempt in failure_attempts)
    else:
        lines.append("- none")

    failures = _mapping_items(preflight.get("failures"))
    if failures:
        lines.append("routing preflight:")
        lines.extend(
            (
                f"- {failure.get('account_id')} "
                f"kind={failure.get('failure_kind')} "
                f"unmatched={failure.get('unmatched_count')}/{failure.get('file_count')} "
                f"updated={failure.get('updated_at')} "
                f"message={_jeb_preview(failure.get('message'))}"
            )
            for failure in failures
        )
    return "\n".join(lines)


def _jeb_attempt_table(title: str, attempts: Sequence[Mapping[str, Any]]) -> Any:
    table = _quiet_table(
        "Attempt", "Account", "Collection slug", "State", "Files", "Bytes", "Updated"
    )
    for attempt in attempts:
        table.add_row(
            _entity_text(attempt.get("attempt_id") or "unknown"),
            _jeb_account_text(attempt),
            str(attempt.get("collection_slug") or "-"),
            _attention_text(attempt.get("state") or "unknown"),
            str(attempt.get("file_count", 0)),
            _bytes_text(attempt.get("total_bytes", 0)),
            str(attempt.get("updated_at") or "-"),
        )
    if not table.rows:
        table.add_row("none", "", "", "", "", "", "")
    return RichGroup(RichText(title, style="bold"), table)


def format_jeb_status(payload: Mapping[str, Any]) -> Any:
    if not _rich_enabled():
        return _format_jeb_status_plain(payload)
    accounts = _mapping_items(payload.get("accounts"))
    batch_counts = _optional_mapping(payload.get("batches"))
    preflight = _optional_mapping(payload.get("routing_preflight_failures"))

    summary = _detail_table()
    summary.add_row(
        "accounts",
        f"{sum(1 for account in accounts if account.get('enabled'))}/{len(accounts)} enabled",
    )
    summary.add_row(
        "batches",
        (
            f"total={batch_counts.get('total', 0)}  "
            f"active={batch_counts.get('active', 0)}  "
            f"terminal={batch_counts.get('terminal', 0)}"
        ),
    )
    summary.add_row("states", _jeb_state_counts_text(_optional_mapping(batch_counts.get("states"))))
    summary.add_row("routing preflight failures", str(preflight.get("total", 0)))
    active_operation = _optional_mapping(payload.get("active_operation"))
    if active_operation:
        summary.add_row(
            "active operation",
            (
                f"{active_operation.get('operation', 'unknown')}  "
                f"id={active_operation.get('id', 'unknown')}  "
                f"account={active_operation.get('account') or '-'}  "
                f"attempt={active_operation.get('attempt_id') or '-'}"
            ),
        )

    account_table = _quiet_table(
        "Account",
        "Enabled",
        "Path",
        "Routing",
        "Backlog",
        "Bytes",
        "Collection slug",
    )
    for account in accounts:
        account_table.add_row(
            _entity_text(account.get("id") or "unknown"),
            str(bool(account.get("enabled"))).lower(),
            "present" if account.get("path_exists") else _attention_text("missing"),
            _attention_text("failed") if account.get("routing_preflight_failed") else "ok",
            str(account.get("eligible_files", "-")),
            _bytes_text(account.get("eligible_bytes", 0)) if "eligible_bytes" in account else "-",
            str(account.get("collection_slug") or "-"),
        )
    if not account_table.rows:
        account_table.add_row("none", "", "", "", "", "", "")

    renderables: list[Any] = [RichText("jeb status", style="bold"), summary, account_table]
    active_attempts = _mapping_items(
        _optional_mapping(payload.get("active_attempts")).get("attempts")
    )
    renderables.append(_jeb_attempt_table("active attempts", active_attempts))
    failed_attempts = _mapping_items(
        _optional_mapping(payload.get("recent_failures")).get("attempts")
    )
    renderables.append(_jeb_attempt_table("recent failures", failed_attempts))

    failures = _mapping_items(preflight.get("failures"))
    if failures:
        preflight_table = _quiet_table("Account", "Kind", "Unmatched", "Updated", "Message")
        for failure in failures:
            preflight_table.add_row(
                _entity_text(failure.get("account_id") or "unknown"),
                str(failure.get("failure_kind") or "unknown"),
                f"{failure.get('unmatched_count', 0)}/{failure.get('file_count', 0)}",
                str(failure.get("updated_at") or "-"),
                _jeb_preview(failure.get("message")),
            )
        renderables.extend([RichText("routing preflight", style="bold"), preflight_table])
    return RichGroup(*renderables)


def format_jeb_config_check(payload: Mapping[str, Any]) -> Any:
    if not _rich_enabled():
        accounts = _string_items(payload.get("accounts"))
        lines = [
            "jeb config",
            f"status: {payload.get('status', 'unknown')}",
            f"accounts: {payload.get('account_count', 0)}",
        ]
        if accounts:
            lines.append("account ids:")
            lines.extend(f"- {account}" for account in accounts)
        return "\n".join(lines)
    table = _detail_table()
    table.add_row("status", _attention_text(payload.get("status", "unknown")))
    table.add_row("accounts", str(payload.get("account_count", 0)))
    table.add_row("account ids", _preview_lines(_string_items(payload.get("accounts")), limit=8))
    return RichGroup(RichText("jeb config", style="bold"), table)


def _operation_summary(payload: Mapping[str, Any]) -> Mapping[str, Any]:
    return _optional_mapping(payload.get("operation"))


def format_jeb_operation(payload: Mapping[str, Any], *, title: str) -> Any:
    operation = _operation_summary(payload)
    if not _rich_enabled():
        lines = [title, f"status: {payload.get('status', 'unknown')}"]
        if payload.get("account"):
            lines.append(f"account: {payload.get('account')}")
        if payload.get("batch_id"):
            lines.append(f"batch: {payload.get('batch_id')}")
        if payload.get("attempt_id"):
            lines.append(f"attempt: {payload.get('attempt_id')}")
        if operation:
            lines.append(f"operation: {operation.get('operation', 'unknown')}")
            if operation.get("id"):
                lines.append(f"operation id: {operation.get('id')}")
            if operation.get("started_at"):
                lines.append(f"started: {operation.get('started_at')}")
        return "\n".join(lines)
    table = _detail_table()
    table.add_row("status", _attention_text(payload.get("status", "unknown")))
    if payload.get("account"):
        table.add_row("account", str(payload.get("account")))
    if payload.get("batch_id"):
        table.add_row("batch", _entity_text(payload.get("batch_id", "unknown")))
    if payload.get("attempt_id"):
        table.add_row("attempt", _entity_text(payload.get("attempt_id", "unknown")))
    if operation:
        table.add_row("operation", str(operation.get("operation", "unknown")))
        if operation.get("id"):
            table.add_row("operation id", _entity_text(operation.get("id", "unknown")))
        if operation.get("started_at"):
            table.add_row("started", str(operation.get("started_at")))
    return RichGroup(RichText(title, style="bold"), table)


def format_jeb_archive_plan(payload: Mapping[str, Any]) -> Any:
    preflight = _optional_mapping(payload.get("routing_preflight"))
    if not _rich_enabled():
        lines = [
            "jeb archive dry-run",
            f"status: {payload.get('status', 'unknown')}",
            f"account: {payload.get('account', 'unknown')}",
            f"collection slug: {payload.get('collection_slug', 'unknown')}",
            f"target: {payload.get('target_name', 'unknown')}",
            f"upload root: {payload.get('upload_root', '-')}",
            f"files: {payload.get('file_count', 0)}",
            f"bytes: {_bytes_text(payload.get('total_bytes', 0))}",
            f"cleanup: {payload.get('cleanup', '-')}",
            f"process: {str(bool(payload.get('process'))).lower()}",
        ]
        if payload.get("batch_id"):
            lines.append(f"batch: {payload.get('batch_id')}")
        if payload.get("job_id"):
            lines.append(f"job: {payload.get('job_id')}")
        lines.append(
            "routing preflight: "
            f"{preflight.get('status', 'unknown')} "
            f"ok={str(preflight.get('ok')).lower()} "
            f"unmatched={preflight.get('unmatched_count', 0)} "
            f"left={preflight.get('left_count', 0)}"
        )
        if preflight.get("error"):
            lines.append(f"preflight error: {_jeb_preview(preflight.get('error'))}")
        return "\n".join(lines)

    table = _detail_table()
    table.add_row("status", _attention_text(payload.get("status", "unknown")))
    table.add_row("account", str(payload.get("account", "unknown")))
    table.add_row("collection slug", str(payload.get("collection_slug", "unknown")))
    table.add_row("target", str(payload.get("target_name", "unknown")))
    table.add_row("upload root", str(payload.get("upload_root", "-")))
    table.add_row("files", str(payload.get("file_count", 0)))
    table.add_row("bytes", _bytes_text(payload.get("total_bytes", 0)))
    table.add_row("cleanup", str(payload.get("cleanup", "-")))
    table.add_row("process", str(bool(payload.get("process"))).lower())
    if payload.get("batch_id"):
        table.add_row("batch", _entity_text(payload.get("batch_id", "unknown")))
    if payload.get("job_id"):
        table.add_row("job", _entity_text(payload.get("job_id", "unknown")))
    table.add_row(
        "routing preflight",
        (
            f"{preflight.get('status', 'unknown')}  "
            f"ok={str(preflight.get('ok')).lower()}  "
            f"unmatched={preflight.get('unmatched_count', 0)}  "
            f"left={preflight.get('left_count', 0)}"
        ),
    )
    if preflight.get("error"):
        table.add_row("preflight error", _jeb_preview(preflight.get("error")))
    return RichGroup(RichText("jeb archive dry-run", style="bold"), table)


def _format_hot_evict_plain(payload: Mapping[str, Any]) -> str:
    dry_run = bool(payload.get("dry_run"))
    action_label = "would evict" if dry_run else "evicted"
    return "\n".join(
        [
            "hot evict dry-run" if dry_run else "hot evict",
            f"status: {payload.get('status', 'unknown')}",
            f"targets: {', '.join(_string_items(payload.get('targets')))}",
            f"selected: {payload.get('files', 0)} files {payload.get('bytes', 0)} bytes",
            f"{action_label}: "
            f"{payload.get('would_evict_files' if dry_run else 'evicted_files', 0)} files "
            f"{payload.get('would_evict_bytes' if dry_run else 'evicted_bytes', 0)} bytes",
        ]
    )


def format_hot_evict(payload: Mapping[str, Any]) -> Any:
    if not _rich_enabled():
        return _format_hot_evict_plain(payload)
    dry_run = bool(payload.get("dry_run"))
    table = _detail_table()
    table.add_row("status", _attention_text(payload.get("status", "unknown")))
    table.add_row("targets", _targets_lines(payload.get("targets")))
    table.add_row("selected files", str(payload.get("files", 0)))
    table.add_row("selected bytes", _bytes_text(payload.get("bytes", 0)))
    table.add_row(
        "would evict files" if dry_run else "evicted files",
        str(payload.get("would_evict_files" if dry_run else "evicted_files", 0)),
    )
    table.add_row(
        "would evict bytes" if dry_run else "evicted bytes",
        _bytes_text(payload.get("would_evict_bytes" if dry_run else "evicted_bytes", 0)),
    )
    return RichGroup(RichText("hot evict dry-run" if dry_run else "hot evict", style="bold"), table)


def _format_images_plain(payload: Mapping[str, Any]) -> str:
    lines = [
        "images: "
        f"page {payload.get('page', 1)}/{payload.get('pages', 0)} "
        f"per_page={payload.get('per_page', 25)} "
        f"total={payload.get('total', 0)} "
        f"sort={payload.get('sort', 'finalized_at')} "
        f"order={payload.get('order', 'desc')}"
    ]

    images = payload.get("images")
    if not isinstance(images, Sequence) or not images:
        lines.append("- none")
        return "\n".join(lines)

    for image in images:
        if not isinstance(image, Mapping):
            continue
        lines.extend(
            [
                f"- {image.get('id', 'unknown')} ({image.get('filename', 'unknown')})",
                f"  finalized_at: {image.get('finalized_at', 'unknown')}",
                f"  bytes: {image.get('bytes', 0)} "
                f"target_bytes={image.get('target_bytes', 0)} "
                f"fill={image.get('fill', 0)}",
                "  redundancy: "
                f"{image.get('disc_redundancy_state', 'unknown')} "
                f"registered={image.get('discs_registered', 0)}/"
                f"{image.get('discs_required', 0)} "
                f"verified={image.get('discs_verified', 0)}/"
                f"{image.get('discs_required', 0)}",
                f"  collections: {image.get('collections', 0)} "
                f"[{_collection_ids_text(image.get('collection_ids'))}]",
            ]
        )

    return "\n".join(lines)


def format_images(payload: Mapping[str, Any]) -> Any:
    if not _rich_enabled():
        return _format_images_plain(payload)
    table = _quiet_table("Image", "Redundancy", "Discs", "Files", "Bytes", "Fill", "Collections")
    images = payload.get("images")
    if isinstance(images, Sequence):
        for image in images:
            if not isinstance(image, Mapping):
                continue
            required = image.get("discs_required", 0)
            table.add_row(
                _entity_text(image.get("id", "unknown")),
                _attention_text(image.get("disc_redundancy_state", "unknown")),
                _disc_coverage_text(
                    image.get("discs_verified", 0),
                    image.get("discs_registered", 0),
                    required,
                ),
                str(image.get("files", 0)),
                _bytes_text(image.get("bytes", 0)),
                f"{float(image.get('fill', 0) or 0) * 100:.1f}%",
                str(image.get("collections", 0)),
            )
    if not table.rows:
        table.add_row("none", "", "", "", "", "", "")
    return RichGroup(_page_text("images", payload), table)


def _format_image_plain(image: Mapping[str, Any]) -> str:
    collection_ids = _collection_ids_lines(image.get("collection_ids"))
    collection_lines = (
        "\n".join(f"  {line}" for line in collection_ids.splitlines())
        if collection_ids != "none"
        else "  none"
    )
    return "\n".join(
        [
            f"image: {image.get('id', 'unknown')} ({image.get('filename', 'unknown')})",
            f"finalized_at: {image.get('finalized_at', 'unknown')}",
            f"bytes: {image.get('bytes', 0)} "
            f"target_bytes={image.get('target_bytes', 0)} "
            f"fill={image.get('fill', 0)} "
            f"files={image.get('files', 0)}",
            "redundancy: "
            f"{image.get('disc_redundancy_state', 'unknown')} "
            f"registered={image.get('discs_registered', 0)}/"
            f"{image.get('discs_required', 0)} "
            f"verified={image.get('discs_verified', 0)}/"
            f"{image.get('discs_required', 0)}",
            f"collections: {image.get('collections', 0)}",
            collection_lines,
        ]
    )


def format_image(image: Mapping[str, Any]) -> Any:
    if not _rich_enabled():
        return _format_image_plain(image)
    table = _detail_table()
    table.add_row("image", _entity_text(image.get("id", "unknown")))
    table.add_row("filename", str(image.get("filename", "unknown")))
    table.add_row("finalized_at", str(image.get("finalized_at", "unknown")))
    table.add_row("files", str(image.get("files", 0)))
    table.add_row("bytes", _bytes_text(image.get("bytes", 0)))
    table.add_row("target", _bytes_text(image.get("target_bytes", 0)))
    table.add_row("fill", f"{float(image.get('fill', 0) or 0) * 100:.1f}%")
    table.add_row(
        "redundancy",
        _attention_text(image.get("disc_redundancy_state", "unknown")),
    )
    table.add_row(
        "discs",
        _disc_detail_coverage_text(image),
    )
    table.add_row("collections", _collection_ids_lines(image.get("collection_ids")))
    return RichGroup(RichText("image", style="bold"), table)


def _disc_payload(payload: Mapping[str, Any]) -> Mapping[str, Any]:
    disc_payload = payload.get("disc")
    if isinstance(disc_payload, Mapping):
        return disc_payload
    return payload


def _format_disc_plain(payload: Mapping[str, Any]) -> str:
    disc_payload = _disc_payload(payload)
    lines = [
        f"disc: {disc_payload.get('disc_id', 'unknown')}",
        f"image: {payload.get('image_id', disc_payload.get('image_id', 'unknown'))}",
        f"label: {disc_payload.get('label_text', 'unknown')}",
        f"location: {disc_payload.get('location') or 'unassigned'}",
        f"state: {disc_payload.get('state', 'unknown')}",
        f"verification: {disc_payload.get('verification_state', 'unknown')}",
    ]
    history = disc_payload.get("history")
    if isinstance(history, Sequence):
        lines.append(f"history: {len(history)} event(s)")
    archive_restore = payload.get("archive_restore")
    if isinstance(archive_restore, Mapping):
        lines.append(
            "rebuild: "
            f"{archive_restore.get('id', 'unknown')} "
            f"state={archive_restore.get('state', 'unknown')}"
        )
        if archive_restore.get("latest_message"):
            lines.append(f"rebuild_message: {archive_restore.get('latest_message')}")
    return "\n".join(lines)


def format_disc(payload: Mapping[str, Any]) -> Any:
    if not _rich_enabled():
        return _format_disc_plain(payload)

    disc_payload = _disc_payload(payload)
    table = _detail_table()
    table.add_row("disc", _entity_text(disc_payload.get("disc_id", "unknown")))
    table.add_row(
        "image",
        str(payload.get("image_id", disc_payload.get("image_id", "unknown"))),
    )
    table.add_row("label", str(disc_payload.get("label_text", "unknown")))
    table.add_row("location", str(disc_payload.get("location") or "unassigned"))
    table.add_row("state", _attention_text(disc_payload.get("state", "unknown")))
    table.add_row(
        "verification",
        _attention_text(disc_payload.get("verification_state", "unknown")),
    )
    if disc_payload.get("created_at"):
        table.add_row("created", str(disc_payload.get("created_at")))

    archive_restore = payload.get("archive_restore")
    if isinstance(archive_restore, Mapping):
        table.add_row("rebuild", _entity_text(archive_restore.get("id", "unknown")))
        table.add_row(
            "rebuild state",
            _restore_state_text(archive_restore.get("state", "unknown")),
        )
        if archive_restore.get("latest_message"):
            table.add_row("rebuild message", str(archive_restore.get("latest_message")))

    renderables: list[Any] = [RichText("disc", style="bold"), table]
    history = disc_payload.get("history")
    if isinstance(history, Sequence) and history:
        history_table = _quiet_table("At", "Event", "State", "Verification", "Location")
        for item in history:
            if not isinstance(item, Mapping):
                continue
            history_table.add_row(
                str(item.get("at", "unknown")),
                str(item.get("event", "unknown")),
                _attention_text(item.get("state", "unknown")),
                _attention_text(item.get("verification_state", "unknown")),
                str(item.get("location") or "unassigned"),
            )
        renderables.extend([RichText("history", style="bold"), history_table])
    return RichGroup(*renderables)


def _disc_items(payload: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    items = payload.get("discs")
    if isinstance(items, Sequence):
        return [item for item in items if isinstance(item, Mapping)]
    discs = payload.get("discs")
    image_id = payload.get("image_id")
    if not isinstance(discs, Sequence):
        return []
    return [{**disc, "image_id": image_id} for disc in discs if isinstance(disc, Mapping)]


def _format_discs_plain(payload: Mapping[str, Any]) -> str:
    discs = _disc_items(payload)
    if not discs:
        return "discs:\n- none"
    lines = ["discs:"]
    for disc in discs:
        lines.append(
            f"- {disc.get('disc_id', 'unknown')} "
            f"image={disc.get('image_id', 'unknown')} "
            f"state={disc.get('state', 'unknown')} "
            f"verification={disc.get('verification_state', 'unknown')} "
            f"location={disc.get('location') or 'unassigned'}"
        )
    return "\n".join(lines)


def format_discs(payload: Mapping[str, Any]) -> Any:
    if not _rich_enabled():
        return _format_discs_plain(payload)
    table = _quiet_table("Disc", "Image", "State", "Verification", "Location")
    for item in _disc_items(payload):
        table.add_row(
            _entity_text(item.get("disc_id", "unknown")),
            str(item.get("image_id", "unknown")),
            _attention_text(item.get("state", "unknown")),
            _attention_text(item.get("verification_state", "unknown")),
            str(item.get("location") or "unassigned"),
        )
    if not table.rows:
        table.add_row("none", "", "", "", "")
    renderables: list[Any] = []
    if "page" in payload:
        renderables.append(_page_text("discs", payload))
    else:
        renderables.append(RichText("discs", style="bold"))
    renderables.append(table)
    return RichGroup(*renderables)


def _format_collection_summary_plain(
    payload: Mapping[str, Any],
    archive_payload: Mapping[str, Any],
) -> str:
    collection_id = str(payload.get("id", "unknown"))
    disc_coverage = payload.get("disc_coverage")
    disc_redundancy = payload.get("disc_redundancy")
    lines = [
        f"collection: {collection_id}",
        f"storage: files={payload.get('files', 0)} hot_bytes={payload.get('hot_bytes', 0)}",
    ]
    if isinstance(disc_coverage, Mapping):
        lines.append(
            "disc coverage: "
            f"{disc_coverage.get('state', 'unknown')} "
            f"bytes={disc_coverage.get('bytes', 0)}"
        )
    if isinstance(disc_redundancy, Mapping):
        lines.append(
            "disc redundancy: "
            f"{disc_redundancy.get('state', 'unknown')} bytes={disc_redundancy.get('bytes', 0)}"
        )
    collection_archive = _find_collection_archive_entry(collection_id, archive_payload)
    direct_archive = payload.get("archive")
    if isinstance(direct_archive, Mapping):
        lines.append(
            "archive: "
            f"{direct_archive.get('state', 'unknown')} "
            f"stored_bytes={direct_archive.get('stored_bytes', 0)} "
            f"backend={direct_archive.get('backend') or 'unknown'} "
            f"storage_class={direct_archive.get('storage_class') or 'unknown'}"
        )
        if direct_archive.get("object_path"):
            lines.append(f"archive_path: {direct_archive.get('object_path')}")
        if direct_archive.get("failure"):
            lines.append(f"archive_failure: {direct_archive.get('failure')}")

    collection_manifest = payload.get("collection_manifest")
    if isinstance(collection_manifest, Mapping):
        lines.append(
            "collection_manifest: "
            f"{collection_manifest.get('object_path') or 'missing'} "
            f"sha256={collection_manifest.get('sha256') or 'unknown'}"
        )
        ots_state = "uploaded" if collection_manifest.get("ots_object_path") else "missing"
        lines.append(
            f"ots: {ots_state} path={collection_manifest.get('ots_object_path') or 'missing'}"
        )

    if isinstance(collection_archive, Mapping):
        lines.append(
            "archive_footprint: "
            f"bytes={collection_archive.get('bytes', 0)} "
            f"measured_storage_bytes={collection_archive.get('measured_storage_bytes', 0)}"
        )

    lines.append("coverage:")
    images = payload.get("image_coverage")
    if not isinstance(images, Sequence) or not images:
        lines.append("- none")
        return "\n".join(lines)

    image_costs: dict[str, Mapping[str, Any]] = {}
    if isinstance(collection_archive, Mapping):
        contributions = collection_archive.get("images")
        if isinstance(contributions, Sequence):
            image_costs = {
                str(item.get("image_id")): item
                for item in contributions
                if isinstance(item, Mapping)
            }

    for image in images:
        if not isinstance(image, Mapping):
            continue
        image_id = str(image.get("id", "unknown"))
        disc_redundancy_state = image.get("disc_redundancy_state", "unknown")
        covered_paths = ", ".join(str(path) for path in image.get("covered_paths", [])) or "none"
        lines.extend(
            [
                f"- {image_id} ({image.get('filename', 'unknown')})",
                "  redundancy: "
                f"{disc_redundancy_state} "
                f"registered={image.get('discs_registered', 0)}/"
                f"{image.get('discs_required', 0)} "
                f"verified={image.get('discs_verified', 0)}/"
                f"{image.get('discs_required', 0)}",
                f"  paths: {covered_paths}",
            ]
        )
        contribution = image_costs.get(image_id)
        if isinstance(contribution, Mapping):
            lines.append(
                "  collection_archive_contribution: "
                f"represented_bytes={contribution.get('represented_bytes', 0)}"
            )
        discs = image.get("discs")
        lines.append("  discs:")
        if not isinstance(discs, Sequence) or not discs:
            lines.append("  - none")
        else:
            for disc in discs:
                if not isinstance(disc, Mapping):
                    continue
                lines.append(
                    "  - "
                    f"{disc.get('disc_id', 'unknown')} "
                    f"label={disc.get('label_text', 'unknown')} "
                    f"location={disc.get('location') or 'unassigned'} "
                    f"state={disc.get('state', 'unknown')} "
                    f"verification={disc.get('verification_state', 'unknown')}"
                )
    return "\n".join(lines)


def _collection_image_costs(
    collection_archive: Mapping[str, Any] | None,
) -> dict[str, Mapping[str, Any]]:
    if not isinstance(collection_archive, Mapping):
        return {}
    contributions = collection_archive.get("images")
    if not isinstance(contributions, Sequence):
        return {}
    return {str(item.get("image_id")): item for item in contributions if isinstance(item, Mapping)}


def _collection_coverage_table(
    payload: Mapping[str, Any],
    collection_archive: Mapping[str, Any] | None,
) -> Any:
    table = _quiet_table("Image", "Redundancy", "Counts", "Archive", "Discs", "Paths")
    image_costs = _collection_image_costs(collection_archive)
    images = payload.get("image_coverage")
    if isinstance(images, Sequence):
        for image in images:
            if not isinstance(image, Mapping):
                continue
            image_id = str(image.get("id", "unknown"))
            required = image.get("discs_required", 0)
            contribution = image_costs.get(image_id)
            archive_text = (
                _bytes_text(contribution.get("represented_bytes", 0))
                if isinstance(contribution, Mapping)
                else "unknown"
            )
            table.add_row(
                _entity_text(image_id),
                _attention_text(image.get("disc_redundancy_state", "unknown")),
                _disc_coverage_text(
                    image.get("discs_verified", 0),
                    image.get("discs_registered", 0),
                    required,
                ),
                archive_text,
                _disc_lines(image.get("discs"), limit=4),
                _preview_lines_with_total(
                    _string_items(image.get("covered_paths")),
                    limit=4,
                    total=(
                        _int_value(image.get("covered_paths_total"))
                        if image.get("covered_paths_total") is not None
                        else None
                    ),
                ),
            )
    if not table.rows:
        table.add_row("none", "", "", "", "", "")
    return table


def format_collection_summary(
    payload: Mapping[str, Any],
    archive_payload: Mapping[str, Any],
) -> Any:
    if not _rich_enabled():
        return _format_collection_summary_plain(payload, archive_payload)

    collection_id = str(payload.get("id", "unknown"))
    total_bytes = _int_value(payload.get("bytes", 0))
    collection_archive = _find_collection_archive_entry(collection_id, archive_payload)

    overview = _detail_table()
    overview.add_row("files", str(payload.get("files", 0)))
    overview.add_row("bytes", _bytes_text(total_bytes))
    overview.add_row("hot", _ratio_text(payload.get("hot_bytes", 0), total_bytes))

    disc_coverage = payload.get("disc_coverage")
    if isinstance(disc_coverage, Mapping):
        overview.add_row(
            "disc coverage",
            _attention_text(
                f"{disc_coverage.get('state', 'unknown')} "
                f"{_ratio_text(disc_coverage.get('bytes', 0), total_bytes)}"
            ),
        )
    disc_redundancy = payload.get("disc_redundancy")
    if isinstance(disc_redundancy, Mapping):
        overview.add_row(
            "disc redundancy",
            _attention_text(
                f"{disc_redundancy.get('state', 'unknown')} "
                f"{_ratio_text(disc_redundancy.get('bytes', 0), total_bytes)}"
            ),
        )

    direct_archive = payload.get("archive")
    if isinstance(direct_archive, Mapping):
        overview.add_row(
            "archive",
            f"{direct_archive.get('state', 'unknown')} "
            f"{_bytes_text(direct_archive.get('stored_bytes', 0))} stored "
            f"{direct_archive.get('backend') or 'unknown'} "
            f"{direct_archive.get('storage_class') or 'unknown'}",
        )
        if direct_archive.get("object_path"):
            overview.add_row("archive path", str(direct_archive.get("object_path")))
        if direct_archive.get("failure"):
            overview.add_row("archive failure", str(direct_archive.get("failure")))

    collection_manifest = payload.get("collection_manifest")
    if isinstance(collection_manifest, Mapping):
        overview.add_row(
            "manifest",
            f"{collection_manifest.get('object_path') or 'missing'} "
            f"sha256={collection_manifest.get('sha256') or 'unknown'}",
        )
        overview.add_row(
            "ots",
            (
                f"{collection_manifest.get('ots_state') or 'uploaded'} "
                f"{collection_manifest.get('ots_object_path')}"
                if collection_manifest.get("ots_object_path")
                else "missing"
            ),
        )

    if isinstance(collection_archive, Mapping):
        overview.add_row(
            "footprint",
            f"{_bytes_text(collection_archive.get('bytes', 0))} logical, "
            f"{_bytes_text(collection_archive.get('measured_storage_bytes', 0))} measured",
        )

    return RichGroup(
        RichText(f"collection {collection_id}", style="bold"),
        overview,
        RichText("coverage", style="bold"),
        _collection_coverage_table(payload, collection_archive),
    )


def format_archive_report(payload: Mapping[str, Any]) -> str:
    totals = payload.get("totals")
    lines = [
        "archive: "
        f"scope={payload.get('scope', 'all')} "
        f"measured_at={payload.get('measured_at', 'unknown')}",
    ]
    if isinstance(totals, Mapping):
        lines.append(
            "totals: "
            f"collections={totals.get('collections', 0)} "
            f"uploaded_collections={totals.get('uploaded_collections', 0)} "
            f"measured_storage_bytes={totals.get('measured_storage_bytes', 0)}"
        )

    images = payload.get("images")
    lines.append("images:")
    if not isinstance(images, Sequence) or not images:
        lines.append("- none")
    else:
        for image in images:
            if not isinstance(image, Mapping):
                continue
            lines.append(
                f"- {image.get('id', 'unknown')} ({image.get('filename', 'unknown')}) "
                f"collections=[{_collection_ids_text(image.get('collection_ids'))}]"
            )

    collections = payload.get("collections")
    lines.append("collections:")
    if not isinstance(collections, Sequence) or not collections:
        lines.append("- none")
    else:
        for collection in collections:
            if not isinstance(collection, Mapping):
                continue
            archive = collection.get("archive")
            archive_state = (
                archive.get("state", "unknown") if isinstance(archive, Mapping) else "unknown"
            )
            manifest = collection.get("collection_manifest")
            ots_state = (
                "uploaded"
                if isinstance(manifest, Mapping) and manifest.get("ots_object_path")
                else "missing"
            )
            lines.append(
                f"- {collection.get('id', 'unknown')} "
                f"bytes={collection.get('bytes', 0)} "
                f"archive={archive_state} "
                f"ots={ots_state} "
                f"measured_storage_bytes={collection.get('measured_storage_bytes', 0)}"
            )
            if isinstance(archive, Mapping) and archive.get("object_path"):
                lines.append(f"  archive_path: {archive.get('object_path')}")
            if isinstance(manifest, Mapping) and manifest.get("object_path"):
                lines.append(f"  collection_manifest: {manifest.get('object_path')}")

    history = payload.get("history")
    if isinstance(history, Sequence) and history:
        lines.append("history:")
        for item in history:
            if not isinstance(item, Mapping):
                continue
            lines.append(
                f"- {item.get('captured_at', 'unknown')} "
                f"uploaded_collections={item.get('uploaded_collections', 0)} "
                f"measured_storage_bytes={item.get('measured_storage_bytes', 0)}"
            )
    return "\n".join(lines)


def _format_plan_plain(payload: Mapping[str, Any]) -> str:
    lines = [
        "plan: "
        f"page {payload.get('page', 1)}/{payload.get('pages', 0)} "
        f"per_page={payload.get('per_page', 25)} "
        f"total={payload.get('total', 0)} "
        f"sort={payload.get('sort', 'fill')} "
        f"order={payload.get('order', 'desc')}",
        "planner: "
        f"ready={payload.get('ready', False)} "
        f"target_bytes={payload.get('target_bytes', 0)} "
        f"min_fill_bytes={payload.get('min_fill_bytes', 0)} "
        f"unplanned_bytes={payload.get('unplanned_bytes', 0)}",
    ]

    candidates = payload.get("candidates")
    if not isinstance(candidates, Sequence) or not candidates:
        lines.append("- none")
        return "\n".join(lines)

    for candidate in candidates:
        if not isinstance(candidate, Mapping):
            continue
        collection_ids = candidate.get("collection_ids")
        collection_text = (
            ", ".join(str(item) for item in collection_ids)
            if isinstance(collection_ids, Sequence)
            else ""
        )
        lines.extend(
            [
                f"- {candidate.get('candidate_id', 'unknown')}",
                f"  fill: {candidate.get('fill', 0)} "
                f"bytes={candidate.get('bytes', 0)} "
                f"target_bytes={candidate.get('target_bytes', payload.get('target_bytes', 0))}",
                f"  iso_ready: {candidate.get('iso_ready', False)}",
                f"  collections: {candidate.get('collections', 0)} [{collection_text}]",
            ]
        )

    return "\n".join(lines)


def format_plan(payload: Mapping[str, Any]) -> Any:
    if not _rich_enabled():
        return _format_plan_plain(payload)
    planner = RichText(
        "planner "
        f"ready={payload.get('ready', False)}  "
        f"target={_bytes_text(payload.get('target_bytes', 0))}  "
        f"min_fill={_bytes_text(payload.get('min_fill_bytes', 0))}  "
        f"unplanned={_bytes_text(payload.get('unplanned_bytes', 0))}",
        style="bold",
    )
    table = _quiet_table("Candidate", "Ready", "Fill", "Files", "Bytes", "Collections")
    candidates = payload.get("candidates")
    if isinstance(candidates, Sequence):
        for candidate in candidates:
            if not isinstance(candidate, Mapping):
                continue
            table.add_row(
                _entity_text(candidate.get("candidate_id", "unknown")),
                "yes" if candidate.get("iso_ready", False) else "no",
                f"{float(candidate.get('fill', 0) or 0) * 100:.1f}%",
                str(candidate.get("files", 0)),
                _bytes_text(candidate.get("bytes", 0)),
                str(candidate.get("collections", 0)),
            )
    if not table.rows:
        table.add_row("none", "", "", "", "", "")
    return RichGroup(_page_text("plan", payload), planner, table)


def _format_find_plain(payload: Mapping[str, Any]) -> str:
    lines = [
        "find: "
        f"page {payload.get('page', 1)}/{payload.get('pages', 0)} "
        f"per_page={payload.get('per_page', 25)} "
        f"total={payload.get('total', 0)} "
        f"sort={payload.get('sort', 'target')} "
        f"order={payload.get('order', 'asc')}",
    ]
    if payload.get("query") is not None:
        lines.append(f"query: {payload.get('query')}")
    if payload.get("collection") is not None:
        lines.append(f"collection: {payload.get('collection')}")
    if payload.get("hot") is not None:
        lines.append(f"hot: {str(payload.get('hot')).lower()}")
    if payload.get("disc_coverage") is not None:
        lines.append(f"disc: {str(payload.get('disc_coverage')).lower()}")
    files = payload.get("files")
    if not isinstance(files, Sequence) or not files:
        lines.append("- none")
        return "\n".join(lines)
    for file in files:
        if not isinstance(file, Mapping):
            continue
        lines.extend(
            [
                f"- {file.get('target', 'unknown')}",
                f"  bytes: {file.get('bytes', 0)}",
                f"  hot: {str(file.get('hot', False)).lower()}",
                f"  disc: {str(file.get('disc_coverage', False)).lower()}",
            ]
        )
    return "\n".join(lines)


def _find_scope_text(payload: Mapping[str, Any]) -> Any:
    text = RichText()
    fields: list[tuple[str, object]] = [
        ("sort", f"{payload.get('sort', 'target')} {payload.get('order', 'asc')}")
    ]
    if payload.get("query") is not None:
        fields.insert(0, ("query", payload.get("query")))
    if payload.get("collection") is not None:
        fields.append(("collection", payload.get("collection")))
    if payload.get("hot") is not None:
        fields.append(("hot", str(payload.get("hot")).lower()))
    if payload.get("disc_coverage") is not None:
        fields.append(("disc", str(payload.get("disc_coverage")).lower()))
    for index, (label, value) in enumerate(fields):
        if index:
            text.append("  ")
        text.append(f"{label}:", style=FIELD_STYLE)
        text.append(f" {value}")
    return text


def _find_record_text(file: Mapping[str, Any]) -> Any:
    target = RichText(str(file.get("target", "unknown")), style=ENTITY_ID_STYLE)
    meta = RichText("  ")
    for index, (label, value) in enumerate(
        (
            ("bytes", _bytes_text(file.get("bytes", 0))),
            ("hot", str(file.get("hot", False)).lower()),
            ("disc", str(file.get("disc_coverage", False)).lower()),
        )
    ):
        if index:
            meta.append("   ")
        meta.append(f"{label}:", style=FIELD_STYLE)
        meta.append(f" {value}")
    return RichGroup(target, meta)


def format_find(payload: Mapping[str, Any]) -> Any:
    if not _rich_enabled():
        return _format_find_plain(payload)
    renderables: list[Any] = [_page_text("files", payload), _find_scope_text(payload)]
    files = payload.get("files")
    if not isinstance(files, Sequence) or not files:
        return RichGroup(*renderables, "none")
    if isinstance(files, Sequence):
        for file in files:
            if not isinstance(file, Mapping):
                continue
            renderables.extend(["", _find_record_text(file)])
    return RichGroup(*renderables)


def _upload_state_text(value: object) -> Any:
    text = str(value)
    normalized = text.casefold().replace("-", "_")
    if normalized in {"failed", "partial", "timeout"} or "failure" in normalized:
        return _styled_text(text, ATTENTION_STYLE)
    return text


def _upload_ratio_text(numerator: object, denominator: object) -> Any:
    total = _int_value(denominator)
    value = _int_value(numerator)
    text = _ratio_text(value, total)
    if total > 0 and value < total:
        return _styled_text(text, ATTENTION_STYLE)
    return text


def _upload_ratio_with_suffix(numerator: object, denominator: object, suffix: str) -> Any:
    value = _upload_ratio_text(numerator, denominator)
    if RichText is not None and isinstance(value, RichText):
        value.append(suffix)
        return value
    return f"{value}{suffix}"


def format_collection_upload_plan(payload: Mapping[str, Any]) -> Any:
    raw_files_preview = payload.get("files_preview")
    files_preview = [
        item
        for item in (raw_files_preview if isinstance(raw_files_preview, Sequence) else [])
        if isinstance(item, Mapping)
    ]
    if not _rich_enabled():
        lines = [
            "collection upload dry-run",
            f"status: {payload.get('status', 'unknown')}",
            f"root: {payload.get('root', 'unknown')}",
            f"slug: {payload.get('slug', 'unknown')}",
            f"normalized slug: {payload.get('normalized_slug', 'unknown')}",
            f"collection: {payload.get('collection_id') or 'server-assigned'}",
            f"timestamp: {payload.get('upload_timestamp') or 'server-assigned'}",
            f"files: {payload.get('files_total', 0)}",
            f"bytes: {_bytes_text(payload.get('bytes_total', 0))}",
            f"mode: {'session' if payload.get('session') else 'manifest'}",
            f"wait: {payload.get('wait_mode', 'unknown')}",
            f"server validation: {payload.get('server_validation', 'not_run')}",
        ]
        if files_preview:
            lines.append("files preview:")
            for item in files_preview[:5]:
                lines.append(f"- {item.get('path', 'unknown')} {_bytes_text(item.get('bytes', 0))}")
            remaining = _int_value(payload.get("files_total", 0)) - len(files_preview[:5])
            if remaining > 0:
                lines.append(f"... {remaining} more")
        return "\n".join(lines)

    table = _detail_table()
    table.add_row("status", _attention_text(payload.get("status", "unknown")))
    table.add_row("root", str(payload.get("root", "unknown")))
    table.add_row("slug", str(payload.get("slug", "unknown")))
    table.add_row("normalized slug", str(payload.get("normalized_slug", "unknown")))
    table.add_row("collection", str(payload.get("collection_id") or "server-assigned"))
    table.add_row("timestamp", str(payload.get("upload_timestamp") or "server-assigned"))
    table.add_row("files", str(payload.get("files_total", 0)))
    table.add_row("bytes", _bytes_text(payload.get("bytes_total", 0)))
    table.add_row("mode", "session" if payload.get("session") else "manifest")
    table.add_row("wait", str(payload.get("wait_mode", "unknown")))
    table.add_row("server validation", str(payload.get("server_validation", "not_run")))
    renderables: list[Any] = [RichText("collection upload dry-run", style="bold"), table]
    if files_preview:
        preview_table = _quiet_table("Path", "Bytes")
        for item in files_preview[:5]:
            preview_table.add_row(
                str(item.get("path", "unknown")),
                _bytes_text(item.get("bytes", 0)),
            )
        remaining = _int_value(payload.get("files_total", 0)) - len(files_preview[:5])
        if remaining > 0:
            preview_table.add_row(f"... {remaining} more", "")
        renderables.extend([RichText("files preview", style="bold"), preview_table])
    return RichGroup(*renderables)


def format_collection_upload(payload: Mapping[str, Any]) -> Any:
    if _rich_enabled():
        collection_id = str(payload.get("collection_id", "unknown"))
        table = _detail_table()
        table.add_row("State", _upload_state_text(payload.get("state", "unknown")))
        table.add_row(
            "Files",
            _upload_ratio_text(payload.get("files_uploaded", 0), payload.get("files_total", 0)),
        )
        table.add_row(
            "Bytes",
            _upload_ratio_text(payload.get("uploaded_bytes", 0), payload.get("bytes_total", 0)),
        )
        hot_promoted_files = payload.get("hot_promoted_files")
        hot_promoted_bytes = payload.get("hot_promoted_bytes")
        if hot_promoted_files is not None or hot_promoted_bytes is not None:
            hot_bytes = _upload_ratio_text(hot_promoted_bytes or 0, payload.get("bytes_total", 0))
            if RichText is not None and isinstance(hot_bytes, RichText):
                hot_value = RichText(
                    f"{hot_promoted_files or 0}/{payload.get('files_total', 0)} files, "
                )
                hot_value.append_text(hot_bytes)
            else:
                hot_value = (
                    f"{hot_promoted_files or 0}/{payload.get('files_total', 0)} files, {hot_bytes}"
                )
            table.add_row(
                "Hot",
                hot_value,
            )
        archive_total = payload.get("archive_total_bytes")
        archive_uploaded = payload.get("archive_uploaded_bytes")
        if archive_total is not None or archive_uploaded is not None:
            archive_value = _upload_ratio_text(archive_uploaded or 0, archive_total or 0)
            archive_phase = payload.get("archive_phase")
            if archive_phase:
                archive_value = _upload_ratio_with_suffix(
                    archive_uploaded or 0,
                    archive_total or 0,
                    f" ({archive_phase})",
                )
            table.add_row("Deep Archive", archive_value)
        latest_failure = payload.get("latest_failure") or payload.get("archive_failure")
        if latest_failure:
            table.add_row("Latest Failure", _styled_text(latest_failure, ATTENTION_STYLE))

        title = RichText("collection upload ", style="bold")
        title.append(collection_id, style=ENTITY_ID_STYLE)
        renderables: list[Any] = [title, table]

        collection = payload.get("collection")
        if isinstance(collection, Mapping):
            finalized = _detail_table()
            finalized.add_row("Files", str(collection.get("files", 0)))
            finalized.add_row("Bytes", _bytes_text(collection.get("bytes", 0)))
            archive = collection.get("archive")
            if isinstance(archive, Mapping):
                finalized.add_row("Archive", str(archive.get("state", "unknown")))
            renderables.extend([RichText("finalized", style="bold"), finalized])
            return RichGroup(*renderables)

        files = payload.get("files")
        if isinstance(files, Sequence):
            pending = [
                file
                for file in files
                if isinstance(file, Mapping) and file.get("upload_state") != "uploaded"
            ]
            if pending:
                pending_table = _quiet_table("Path", "State", "Bytes")
                for file in pending[:10]:
                    pending_table.add_row(
                        str(file.get("path", "unknown")),
                        _upload_state_text(file.get("upload_state", "unknown")),
                        _upload_ratio_text(file.get("uploaded_bytes", 0), file.get("bytes", 0)),
                    )
                if len(pending) > 10:
                    pending_table.add_row(f"... {len(pending) - 10} more", "", "")
                renderables.extend([RichText("pending", style="bold"), pending_table])
        return RichGroup(*renderables)

    lines = [
        f"collection: {payload.get('collection_id', 'unknown')}",
        f"state: {payload.get('state', 'unknown')}",
        "upload: "
        f"{payload.get('files_uploaded', 0)}/{payload.get('files_total', 0)} files "
        f"{payload.get('uploaded_bytes', 0)}/{payload.get('bytes_total', 0)} bytes",
    ]
    collection = payload.get("collection")
    if isinstance(collection, Mapping):
        lines.append(
            f"finalized: {collection.get('files', 0)} files {collection.get('bytes', 0)} bytes"
        )
        archive = collection.get("archive")
        if isinstance(archive, Mapping):
            lines.append(f"archive: {archive.get('state', 'unknown')}")
        return "\n".join(lines)

    files = payload.get("files")
    if isinstance(files, Sequence):
        pending = [
            file
            for file in files
            if isinstance(file, Mapping) and file.get("upload_state") != "uploaded"
        ]
        lines.append("pending:")
        if not pending:
            lines.append("- none")
        for file in pending:
            lines.append(
                f"- {file.get('path', 'unknown')} "
                f"({file.get('uploaded_bytes', 0)}/{file.get('bytes', 0)} bytes)"
            )
    return "\n".join(lines)


def format_files(payload: Mapping[str, Any]) -> str:
    files = payload.get("files")
    if not isinstance(files, Sequence) or not files:
        return (
            "files: "
            f"page {payload.get('page', 1)}/{payload.get('pages', 0)} "
            f"per_page={payload.get('per_page', 25)} "
            f"total={payload.get('total', 0)}\n"
            "target: "
            f"{payload.get('target', 'unknown')}\n"
            "- none"
        )
    lines = [
        "files: "
        f"page {payload.get('page', 1)}/{payload.get('pages', 0)} "
        f"per_page={payload.get('per_page', 25)} "
        f"total={payload.get('total', 0)}",
        f"target: {payload.get('target', 'unknown')}",
    ]
    for file in files:
        if not isinstance(file, Mapping):
            continue
        lines.extend(
            [
                f"- {file.get('target', 'unknown')}",
                f"  bytes: {file.get('bytes', 0)}",
                f"  hot: {str(file.get('hot', False)).lower()}",
                f"  disc: {str(file.get('disc_coverage', False)).lower()}",
            ]
        )
    return "\n".join(lines)


def emit(payload: Any, *, json_mode: bool) -> None:
    if json_mode:
        typer.echo(json.dumps(payload, sort_keys=True, separators=(",", ":")))
        return
    if isinstance(payload, str):
        typer.echo(payload)
        return
    if isinstance(payload, dict):
        typer.echo(json.dumps(payload, sort_keys=True, separators=(",", ":")))
        return
    console = _console()
    if console is None:
        typer.echo(str(payload))
        return
    console.print(payload)
