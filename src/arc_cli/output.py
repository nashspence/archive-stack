from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any

import typer


def _copy_label(copy: Mapping[str, object]) -> str:
    copy_id = str(copy.get("id", "unknown"))
    volume_id = str(copy.get("volume_id", "unknown"))
    location = str(copy.get("location") or "unassigned")
    return f"{copy_id} ({volume_id} @ {location})"


def format_copy(payload: Mapping[str, Any]) -> str:
    history = payload.get("history")
    lines = [
        f"copy: {payload.get('id', 'unknown')}",
        f"volume: {payload.get('volume_id', 'unknown')}",
        f"label: {payload.get('label_text', 'unknown')}",
        f"location: {payload.get('location') or 'unassigned'}",
        f"state: {payload.get('state', 'unknown')}",
        f"verification: {payload.get('verification_state', 'unknown')}",
    ]
    if isinstance(history, Sequence):
        lines.append(f"history: {len(history)} event(s)")
    return "\n".join(lines)


def format_copies(payload: Mapping[str, Any]) -> str:
    copies = payload.get("copies")
    if not isinstance(copies, Sequence) or not copies:
        return "copies: none"
    lines = [f"copies: {len(copies)}"]
    for copy in copies:
        if not isinstance(copy, Mapping):
            continue
        lines.append(
            f"- {copy.get('id', 'unknown')} "
            f"state={copy.get('state', 'unknown')} "
            f"verification={copy.get('verification_state', 'unknown')} "
            f"location={copy.get('location') or 'unassigned'}"
        )
    return "\n".join(lines)


def format_pin(payload: Mapping[str, Any]) -> str:
    lines = [
        f"target: {payload['target']}",
        f"pin: {'true' if payload.get('pin') else 'false'}",
    ]

    hot = payload.get("hot")
    if isinstance(hot, Mapping):
        lines.append(
            "hot: "
            f"{hot.get('state', 'unknown')} "
            f"(present={hot.get('present_bytes', 0)} missing={hot.get('missing_bytes', 0)})"
        )

    fetch = payload.get("fetch")
    if isinstance(fetch, Mapping):
        lines.append(f"fetch: {fetch.get('id', 'unknown')} ({fetch.get('state', 'unknown')})")
        copies = fetch.get("copies")
        if isinstance(copies, Sequence):
            lines.append("candidate copies:")
            if copies:
                lines.extend(
                    f"- {_copy_label(copy)}" for copy in copies if isinstance(copy, Mapping)
                )
            else:
                lines.append("- none")

    return "\n".join(lines)


def format_fetch(summary: Mapping[str, Any], manifest: Mapping[str, Any]) -> str:
    pending: list[str] = []
    partial: list[str] = []
    byte_complete: list[str] = []

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

            if upload_state == "uploaded":
                continue
            if upload_state == "byte_complete" or (
                total_bytes > 0 and uploaded_bytes >= total_bytes
            ):
                byte_complete.append(f"- {path} ({uploaded_bytes}/{total_bytes} bytes)")
                continue
            if upload_state == "partial" or uploaded_bytes > 0:
                partial.append(
                    f"- {path} ({uploaded_bytes}/{total_bytes} bytes, expires {expires_at})"
                )
                continue
            pending.append(f"- {path}")

    lines = [
        f"fetch: {summary.get('id', 'unknown')} ({summary.get('state', 'unknown')})",
        f"target: {summary.get('target', 'unknown')}",
        "pending:",
    ]
    lines.extend(pending or ["- none"])
    lines.append("partial:")
    lines.extend(partial or ["- none", "expires: n/a"])
    lines.append("byte-complete:")
    lines.extend(byte_complete or ["- none"])
    return "\n".join(lines)


def format_images(payload: Mapping[str, Any]) -> str:
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
        collection_ids = image.get("collection_ids")
        glacier = image.get("glacier")
        glacier_state = (
            glacier.get("state", "unknown") if isinstance(glacier, Mapping) else "unknown"
        )
        collection_text = (
            ", ".join(str(item) for item in collection_ids)
            if isinstance(collection_ids, Sequence)
            else ""
        )
        lines.extend(
            [
                f"- {image.get('id', 'unknown')} ({image.get('filename', 'unknown')})",
                f"  finalized_at: {image.get('finalized_at', 'unknown')}",
                "  protection: "
                f"{image.get('protection_state', 'unknown')} "
                f"copies={image.get('physical_copies_registered', 0)}/"
                f"{image.get('physical_copies_required', 0)} "
                f"glacier={glacier_state}",
                f"  collections: {image.get('collections', 0)} [{collection_text}]",
            ]
        )
        if isinstance(glacier, Mapping) and glacier.get("object_path"):
            lines.append(f"  glacier_path: {glacier.get('object_path')}")
        if isinstance(glacier, Mapping) and glacier.get("failure"):
            lines.append(f"  glacier_failure: {glacier.get('failure')}")

    return "\n".join(lines)


def format_glacier_report(payload: Mapping[str, Any]) -> str:
    totals = payload.get("totals")
    pricing_basis = payload.get("pricing_basis")
    lines = [
        "glacier: "
        f"scope={payload.get('scope', 'all')} "
        f"measured_at={payload.get('measured_at', 'unknown')}",
    ]
    if isinstance(totals, Mapping):
        lines.append(
            "totals: "
            f"images={totals.get('images', 0)} "
            f"uploaded={totals.get('uploaded_images', 0)} "
            f"measured_storage_bytes={totals.get('measured_storage_bytes', 0)} "
            f"estimated_billable_bytes={totals.get('estimated_billable_bytes', 0)} "
            f"estimated_monthly_cost_usd={totals.get('estimated_monthly_cost_usd', 0.0)}"
        )
    if isinstance(pricing_basis, Mapping):
        lines.extend(
            [
                "pricing_basis: "
                f"{pricing_basis.get('label', 'unknown')} "
                f"source={pricing_basis.get('source', 'unknown')} "
                f"storage_class={pricing_basis.get('storage_class', 'unknown')} "
                f"region={pricing_basis.get('region_code') or 'unknown'} "
                f"effective_at={pricing_basis.get('effective_at') or 'unknown'} "
                f"glacier_rate={pricing_basis.get('glacier_storage_rate_usd_per_gib_month', 0.0)} "
                "standard_rate="
                f"{pricing_basis.get('standard_storage_rate_usd_per_gib_month', 0.0)}",
                "pricing_details: "
                f"archived_metadata_bytes_per_object="
                f"{pricing_basis.get('archived_metadata_bytes_per_object', 0)} "
                f"standard_metadata_bytes_per_object="
                f"{pricing_basis.get('standard_metadata_bytes_per_object', 0)} "
                f"minimum_storage_duration_days="
                f"{pricing_basis.get('minimum_storage_duration_days', 0)}",
            ]
        )

    images = payload.get("images")
    lines.append("images:")
    if not isinstance(images, Sequence) or not images:
        lines.append("- none")
    else:
        for image in images:
            if not isinstance(image, Mapping):
                continue
            glacier = image.get("glacier")
            glacier_state = (
                glacier.get("state", "unknown") if isinstance(glacier, Mapping) else "unknown"
            )
            lines.append(
                f"- {image.get('id', 'unknown')} ({image.get('filename', 'unknown')}) "
                f"glacier={glacier_state} "
                f"measured_storage_bytes={image.get('measured_storage_bytes', 0)} "
                f"estimated_billable_bytes={image.get('estimated_billable_bytes', 0)} "
                f"estimated_monthly_cost_usd={image.get('estimated_monthly_cost_usd', 0.0)}"
            )
            if isinstance(glacier, Mapping) and glacier.get("object_path"):
                lines.append(f"  glacier_path: {glacier.get('object_path')}")

    collections = payload.get("collections")
    lines.append("collections:")
    if not isinstance(collections, Sequence) or not collections:
        lines.append("- none")
    else:
        for collection in collections:
            if not isinstance(collection, Mapping):
                continue
            lines.append(
                f"- {collection.get('id', 'unknown')} "
                f"attribution={collection.get('attribution_state', 'unknown')} "
                f"represented_bytes={collection.get('represented_bytes', 0)} "
                f"derived_stored_bytes={collection.get('derived_stored_bytes', 0)} "
                f"estimated_monthly_cost_usd={collection.get('estimated_monthly_cost_usd', 0.0)}"
            )

    billing = payload.get("billing")
    lines.append("billing:")
    if not isinstance(billing, Mapping):
        lines.append("- unavailable")
    else:
        lines.append(
            "- "
            f"source={billing.get('source', 'unknown')} "
            f"scope={billing.get('scope', 'unknown')} "
            f"filter={billing.get('filter_label') or 'none'}"
        )
        actuals = billing.get("actuals")
        if isinstance(actuals, Sequence) and actuals:
            for actual in actuals:
                if not isinstance(actual, Mapping):
                    continue
                lines.append(
                    "  actual: "
                    f"{actual.get('start', 'unknown')}..{actual.get('end', 'unknown')} "
                    f"estimated={actual.get('estimated', False)} "
                    f"unblended_cost_usd={actual.get('unblended_cost_usd', 0.0)} "
                    f"usage_quantity={actual.get('usage_quantity', 0.0)} "
                    f"usage_unit={actual.get('usage_unit') or 'unknown'}"
                )
        forecast = billing.get("forecast")
        if isinstance(forecast, Sequence) and forecast:
            for period in forecast:
                if not isinstance(period, Mapping):
                    continue
                lines.append(
                    "  forecast: "
                    f"{period.get('start', 'unknown')}..{period.get('end', 'unknown')} "
                    f"mean_cost_usd={period.get('mean_cost_usd', 0.0)} "
                    f"lower_bound_cost_usd={period.get('lower_bound_cost_usd', 0.0)} "
                    f"upper_bound_cost_usd={period.get('upper_bound_cost_usd', 0.0)}"
                )
        notes = billing.get("notes")
        if isinstance(notes, Sequence):
            for note in notes:
                lines.append(f"  note: {note}")

    history = payload.get("history")
    if isinstance(history, Sequence) and history:
        lines.append("history:")
        for item in history:
            if not isinstance(item, Mapping):
                continue
            lines.append(
                f"- {item.get('captured_at', 'unknown')} "
                f"uploaded={item.get('uploaded_images', 0)} "
                f"measured_storage_bytes={item.get('measured_storage_bytes', 0)} "
                f"estimated_monthly_cost_usd={item.get('estimated_monthly_cost_usd', 0.0)}"
            )
    return "\n".join(lines)


def format_plan(payload: Mapping[str, Any]) -> str:
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
                f"  fill: {candidate.get('fill', 0)}",
                f"  iso_ready: {candidate.get('iso_ready', False)}",
                f"  collections: {candidate.get('collections', 0)} [{collection_text}]",
            ]
        )

    return "\n".join(lines)


def format_collection_files(payload: Mapping[str, Any]) -> str:
    lines = [
        f"collection: {payload.get('collection_id', 'unknown')}",
        "files: "
        f"page {payload.get('page', 1)}/{payload.get('pages', 0)} "
        f"per_page={payload.get('per_page', 25)} "
        f"total={payload.get('total', 0)}",
    ]
    files = payload.get("files")
    if not isinstance(files, Sequence) or not files:
        lines.append("- none")
        return "\n".join(lines)
    for file in files:
        if not isinstance(file, Mapping):
            continue
        lines.extend(
            [
                f"- {file.get('path', 'unknown')}",
                f"  bytes: {file.get('bytes', 0)}",
                f"  hot: {str(file.get('hot', False)).lower()}",
                f"  archived: {str(file.get('archived', False)).lower()}",
            ]
        )
    return "\n".join(lines)


def format_collection_upload(payload: Mapping[str, Any]) -> str:
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
                f"  archived: {str(file.get('archived', False)).lower()}",
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
