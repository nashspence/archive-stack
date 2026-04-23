from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any

import typer


def _copy_label(copy: Mapping[str, object]) -> str:
    copy_id = str(copy.get("id", "unknown"))
    volume_id = str(copy.get("volume_id", "unknown"))
    location = str(copy.get("location", "unknown"))
    return f"{copy_id} ({volume_id} @ {location})"


def format_pin(payload: Mapping[str, Any]) -> str:
    lines = [
        f'target: {payload["target"]}',
        f'pin: {"true" if payload.get("pin") else "false"}',
    ]

    hot = payload.get("hot")
    if isinstance(hot, Mapping):
        lines.append(
            "hot: "
            f'{hot.get("state", "unknown")} '
            f'(present={hot.get("present_bytes", 0)} missing={hot.get("missing_bytes", 0)})'
        )

    fetch = payload.get("fetch")
    if isinstance(fetch, Mapping):
        lines.append(f'fetch: {fetch.get("id", "unknown")} ({fetch.get("state", "unknown")})')
        copies = fetch.get("copies")
        if isinstance(copies, Sequence):
            lines.append("candidate copies:")
            if copies:
                lines.extend(
                    f"- {_copy_label(copy)}"
                    for copy in copies
                    if isinstance(copy, Mapping)
                )
            else:
                lines.append("- none")

    return "\n".join(lines)


def format_fetch(summary: Mapping[str, Any], manifest: Mapping[str, Any]) -> str:
    pending: list[str] = []
    partial: list[str] = []

    entries = manifest.get("entries")
    if isinstance(entries, Sequence):
        for entry in entries:
            if not isinstance(entry, Mapping):
                continue
            path = str(entry.get("path", "unknown"))
            total_bytes = int(entry.get("recovery_bytes", entry.get("bytes", 0)))
            uploaded_bytes = int(entry.get("uploaded_bytes", 0))
            upload_state = str(entry.get("upload_state", "pending"))
            expires_at = str(entry.get("upload_state_expires_at", "n/a"))

            if upload_state == "uploaded" or (total_bytes > 0 and uploaded_bytes >= total_bytes):
                continue
            if upload_state == "partial" or uploaded_bytes > 0:
                partial.append(f"- {path} ({uploaded_bytes}/{total_bytes} bytes, expires {expires_at})")
                continue
            pending.append(f"- {path}")

    lines = [
        f'fetch: {summary.get("id", "unknown")} ({summary.get("state", "unknown")})',
        f'target: {summary.get("target", "unknown")}',
        "pending:",
    ]
    lines.extend(pending or ["- none"])
    lines.append("partial:")
    lines.extend(partial or ["- none", "expires: n/a"])
    return "\n".join(lines)


def format_images(payload: Mapping[str, Any]) -> str:
    lines = [
        "images: "
        f'page {payload.get("page", 1)}/{payload.get("pages", 0)} '
        f'per_page={payload.get("per_page", 25)} '
        f'total={payload.get("total", 0)} '
        f'sort={payload.get("sort", "finalized_at")} '
        f'order={payload.get("order", "desc")}'
    ]

    images = payload.get("images")
    if not isinstance(images, Sequence) or not images:
        lines.append("- none")
        return "\n".join(lines)

    for image in images:
        if not isinstance(image, Mapping):
            continue
        collection_ids = image.get("collection_ids")
        collection_text = ", ".join(str(item) for item in collection_ids) if isinstance(collection_ids, Sequence) else ""
        lines.extend(
            [
                f'- {image.get("id", "unknown")} ({image.get("filename", "unknown")})',
                f'  finalized_at: {image.get("finalized_at", "unknown")}',
                f'  copies: {image.get("copy_count", 0)}',
                f'  collections: {image.get("collections", 0)} [{collection_text}]',
            ]
        )

    return "\n".join(lines)


def format_plan(payload: Mapping[str, Any]) -> str:
    lines = [
        "plan: "
        f'page {payload.get("page", 1)}/{payload.get("pages", 0)} '
        f'per_page={payload.get("per_page", 25)} '
        f'total={payload.get("total", 0)} '
        f'sort={payload.get("sort", "fill")} '
        f'order={payload.get("order", "desc")}',
        "planner: "
        f'ready={payload.get("ready", False)} '
        f'target_bytes={payload.get("target_bytes", 0)} '
        f'min_fill_bytes={payload.get("min_fill_bytes", 0)} '
        f'unplanned_bytes={payload.get("unplanned_bytes", 0)}',
    ]

    candidates = payload.get("candidates")
    if not isinstance(candidates, Sequence) or not candidates:
        lines.append("- none")
        return "\n".join(lines)

    for candidate in candidates:
        if not isinstance(candidate, Mapping):
            continue
        collection_ids = candidate.get("collection_ids")
        collection_text = ", ".join(str(item) for item in collection_ids) if isinstance(collection_ids, Sequence) else ""
        lines.extend(
            [
                f'- {candidate.get("candidate_id", "unknown")}',
                f'  fill: {candidate.get("fill", 0)}',
                f'  iso_ready: {candidate.get("iso_ready", False)}',
                f'  collections: {candidate.get("collections", 0)} [{collection_text}]',
            ]
        )

    return "\n".join(lines)


def emit(payload: Any, *, json_mode: bool) -> None:
    if json_mode:
        typer.echo(json.dumps(payload, indent=2, sort_keys=True))
        return
    if isinstance(payload, str):
        typer.echo(payload)
        return
    if isinstance(payload, dict):
        typer.echo(json.dumps(payload, indent=2, sort_keys=True))
        return
    typer.echo(str(payload))
