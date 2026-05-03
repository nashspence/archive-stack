from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any

import typer

from contracts.operator import copy as operator_copy


def _copy_label(copy: Mapping[str, object]) -> str:
    copy_id = str(copy.get("id", "unknown"))
    volume_id = str(copy.get("volume_id", "unknown"))
    location = str(copy.get("location") or "unassigned")
    return f"{copy_id} ({volume_id} @ {location})"


def _collection_ids_text(collection_ids: object) -> str:
    if not isinstance(collection_ids, Sequence):
        return ""
    return ", ".join(str(item) for item in collection_ids)


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


def _image_next_actions(image: Mapping[str, object]) -> str:
    required = _int_value(image.get("physical_copies_required", 0))
    registered = _int_value(image.get("physical_copies_registered", 0))
    verified = _int_value(image.get("physical_copies_verified", 0))
    actions: list[str] = []
    if registered < required:
        actions.append("burn")
    if verified < required:
        actions.append("verify")
    return ", ".join(actions) if actions else "none"


def _find_collection_glacier_entry(
    collection_id: str,
    glacier_payload: Mapping[str, Any],
) -> Mapping[str, Any] | None:
    collections = glacier_payload.get("collections")
    if not isinstance(collections, Sequence):
        return None
    for collection in collections:
        if isinstance(collection, Mapping) and str(collection.get("id")) == collection_id:
            return collection
    return None


def _recovery_text(recovery: object, *, total_bytes: int) -> str:
    if not isinstance(recovery, Mapping):
        return "available=unknown"

    verified = recovery.get("verified_physical")
    glacier = recovery.get("glacier")
    available_items = recovery.get("available")
    available = (
        ",".join(str(item) for item in available_items)
        if isinstance(available_items, Sequence) and available_items
        else "none"
    )

    verified_state = (
        str(verified.get("state", "unknown")) if isinstance(verified, Mapping) else "unknown"
    )
    verified_bytes = _int_value(verified.get("bytes", 0)) if isinstance(verified, Mapping) else 0
    glacier_state = (
        str(glacier.get("state", "unknown")) if isinstance(glacier, Mapping) else "unknown"
    )
    glacier_bytes = _int_value(glacier.get("bytes", 0)) if isinstance(glacier, Mapping) else 0
    return (
        f"available={available} "
        f"verified_physical={verified_state} {verified_bytes}/{total_bytes} "
        f"glacier={glacier_state} {glacier_bytes}/{total_bytes}"
    )


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
        lines.extend(
            (
                "",
                operator_copy.pin_waiting_for_disc(
                    target=str(payload["target"]),
                    missing_bytes=None,
                ),
                "",
            )
        )
        lines.append(f"fetch: {fetch.get('id', 'unknown')} ({fetch.get('state', 'unknown')})")
        copies = fetch.get("copies")
        if isinstance(copies, Sequence):
            lines.append("available discs:")
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
        operator_copy.fetch_detail_pending(
            target=str(summary.get("target", "unknown")),
            pending_files=_int_value(summary.get("entries_pending"), default=len(pending)),
            partial_files=max(
                _int_value(summary.get("entries_partial"), default=len(partial)),
                1 if pending else 0,
            ),
        ),
        "",
        f"fetch: {summary.get('id', 'unknown')} ({summary.get('state', 'unknown')})",
        f"target: {summary.get('target', 'unknown')}",
        "Pending files:",
    ]
    lines.extend(pending or ["- none"])
    lines.append("Partly restored files:")
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
        lines.extend(
            [
                f"- {image.get('id', 'unknown')} ({image.get('filename', 'unknown')})",
                f"  finalized_at: {image.get('finalized_at', 'unknown')}",
                "  protection: "
                f"{image.get('physical_protection_state', 'unknown')} "
                f"registered={image.get('physical_copies_registered', 0)}/"
                f"{image.get('physical_copies_required', 0)} "
                f"verified={image.get('physical_copies_verified', 0)}/"
                f"{image.get('physical_copies_required', 0)}",
                f"  collections: {image.get('collections', 0)} "
                f"[{_collection_ids_text(image.get('collection_ids'))}]",
            ]
        )

    return "\n".join(lines)


def format_archive_status(
    ready_plan_payload: Mapping[str, Any],
    backlog_plan_payload: Mapping[str, Any],
    images_payload: Mapping[str, Any],
    unprotected_collections_payload: Mapping[str, Any],
    partially_protected_collections_payload: Mapping[str, Any],
    protected_collections_payload: Mapping[str, Any],
) -> str:
    lines = [
        "archive: "
        f"page={images_payload.get('page', 1)} "
        f"per_page={images_payload.get('per_page', 25)} "
        f"ready_to_finalize={ready_plan_payload.get('total', 0)} "
        f"waiting_for_future_iso={backlog_plan_payload.get('total', 0)} "
        f"unplanned_bytes={ready_plan_payload.get('unplanned_bytes', 0)}",
        "ready_to_finalize:",
    ]

    candidates = ready_plan_payload.get("candidates")
    if not isinstance(candidates, Sequence) or not candidates:
        lines.append("- none")
    else:
        for candidate in candidates:
            if not isinstance(candidate, Mapping):
                continue
            lines.append(
                f"- {candidate.get('candidate_id', 'unknown')} "
                f"fill={candidate.get('fill', 0)} "
                f"collections={candidate.get('collections', 0)} "
                f"[{_collection_ids_text(candidate.get('collection_ids'))}]"
            )

    lines.append("waiting_for_future_iso:")
    backlog_candidates = backlog_plan_payload.get("candidates")
    if not isinstance(backlog_candidates, Sequence) or not backlog_candidates:
        lines.append("- none")
    else:
        for candidate in backlog_candidates:
            if not isinstance(candidate, Mapping):
                continue
            lines.append(
                f"- {candidate.get('candidate_id', 'unknown')} "
                f"fill={candidate.get('fill', 0)} "
                f"collections={candidate.get('collections', 0)} "
                f"[{_collection_ids_text(candidate.get('collection_ids'))}]"
            )

    lines.append("finalized_images:")
    images = images_payload.get("images")
    if not isinstance(images, Sequence) or not images:
        lines.append("- none")
    else:
        for image in images:
            if not isinstance(image, Mapping):
                continue
            lines.extend(
                [
                    f"- {image.get('id', 'unknown')} ({image.get('filename', 'unknown')})",
                    f"  next: {_image_next_actions(image)}",
                    "  protection: "
                    f"{image.get('physical_protection_state', 'unknown')} "
                    f"registered={image.get('physical_copies_registered', 0)}/"
                    f"{image.get('physical_copies_required', 0)} "
                    f"verified={image.get('physical_copies_verified', 0)}/"
                    f"{image.get('physical_copies_required', 0)}",
                    f"  collections: {image.get('collections', 0)} "
                    f"[{_collection_ids_text(image.get('collection_ids'))}]",
                ]
            )

    lines.append("noncompliant_collections:")
    noncompliant_collections: list[Mapping[str, Any]] = []
    for payload in (
        unprotected_collections_payload,
        partially_protected_collections_payload,
    ):
        collections = payload.get("collections")
        if not isinstance(collections, Sequence):
            continue
        noncompliant_collections.extend(
            collection for collection in collections if isinstance(collection, Mapping)
        )
    if not noncompliant_collections:
        lines.append("- none")
    else:
        for collection in noncompliant_collections:
            total_bytes = _int_value(collection.get("bytes", 0))
            lines.append(
                f"- {collection.get('id', 'unknown')} "
                f"state={collection.get('protection_state', 'unknown')} "
                f"protected_bytes={collection.get('protected_bytes', 0)}/{total_bytes} "
                f"recovery={_recovery_text(collection.get('recovery'), total_bytes=total_bytes)}"
            )

    lines.append("fully_protected_collections:")
    collections = protected_collections_payload.get("collections")
    if not isinstance(collections, Sequence) or not collections:
        lines.append("- none")
    else:
        for collection in collections:
            if not isinstance(collection, Mapping):
                continue
            lines.append(
                f"- {collection.get('id', 'unknown')} "
                f"protected_bytes={collection.get('protected_bytes', 0)}/"
                f"{collection.get('bytes', 0)}"
            )
    return "\n".join(lines)


def format_collection_summary(
    payload: Mapping[str, Any],
    glacier_payload: Mapping[str, Any],
) -> str:
    collection_id = str(payload.get("id", "unknown"))
    lines = [
        f"collection: {collection_id}",
        "protection: "
        f"{payload.get('protection_state', 'unknown')} "
        f"protected_bytes={payload.get('protected_bytes', 0)}/{payload.get('bytes', 0)}",
        "storage: "
        f"files={payload.get('files', 0)} "
        f"hot_bytes={payload.get('hot_bytes', 0)} "
        f"archived_bytes={payload.get('archived_bytes', 0)} "
        f"pending_bytes={payload.get('pending_bytes', 0)}",
    ]
    lines.append(
        "recovery: "
        + _recovery_text(
            payload.get("recovery"),
            total_bytes=_int_value(payload.get("bytes", 0)),
        )
    )

    collection_glacier = _find_collection_glacier_entry(collection_id, glacier_payload)
    direct_glacier = payload.get("glacier")
    if isinstance(direct_glacier, Mapping):
        lines.append(
            "glacier: "
            f"{direct_glacier.get('state', 'unknown')} "
            f"stored_bytes={direct_glacier.get('stored_bytes', 0)} "
            f"backend={direct_glacier.get('backend') or 'unknown'} "
            f"storage_class={direct_glacier.get('storage_class') or 'unknown'}"
        )
        if direct_glacier.get("object_path"):
            lines.append(f"glacier_path: {direct_glacier.get('object_path')}")
        if direct_glacier.get("failure"):
            lines.append(f"glacier_failure: {direct_glacier.get('failure')}")

    archive_manifest = payload.get("archive_manifest")
    if isinstance(archive_manifest, Mapping):
        lines.append(
            "archive_manifest: "
            f"{archive_manifest.get('object_path') or 'missing'} "
            f"sha256={archive_manifest.get('sha256') or 'unknown'}"
        )
        ots_state = "uploaded" if archive_manifest.get("ots_object_path") else "missing"
        lines.append(
            f"ots: {ots_state} "
            f"path={archive_manifest.get('ots_object_path') or 'missing'}"
        )

    disc_coverage = payload.get("disc_coverage")
    if isinstance(disc_coverage, Mapping):
        lines.append(
            "disc_coverage="
            f"{disc_coverage.get('state', 'unknown')} "
            f"verified_physical_bytes={disc_coverage.get('verified_physical_bytes', 0)}"
        )

    if isinstance(collection_glacier, Mapping):
        estimated_cost = collection_glacier.get("estimated_monthly_cost_usd", 0.0)
        lines.append(
            "glacier_footprint: "
            f"bytes={collection_glacier.get('bytes', 0)} "
            f"measured_storage_bytes={collection_glacier.get('measured_storage_bytes', 0)} "
            f"estimated_billable_bytes={collection_glacier.get('estimated_billable_bytes', 0)} "
            f"estimated_monthly_cost_usd={estimated_cost}"
        )

    lines.append("coverage:")
    images = payload.get("image_coverage")
    if not isinstance(images, Sequence) or not images:
        lines.append("- none")
        return "\n".join(lines)

    image_costs: dict[str, Mapping[str, Any]] = {}
    if isinstance(collection_glacier, Mapping):
        contributions = collection_glacier.get("images")
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
        protection_state = image.get("physical_protection_state", "unknown")
        covered_paths = ", ".join(str(path) for path in image.get("covered_paths", [])) or "none"
        lines.extend(
            [
                f"- {image_id} ({image.get('filename', 'unknown')})",
                "  protection: "
                f"{protection_state} "
                f"registered={image.get('physical_copies_registered', 0)}/"
                f"{image.get('physical_copies_required', 0)} "
                f"verified={image.get('physical_copies_verified', 0)}/"
                f"{image.get('physical_copies_required', 0)}",
                f"  paths: {covered_paths}",
            ]
        )
        contribution = image_costs.get(image_id)
        if isinstance(contribution, Mapping):
            lines.append(
                "  collection_archive_contribution: "
                f"represented_bytes={contribution.get('represented_bytes', 0)}"
            )
        copies = image.get("copies")
        lines.append("  copies:")
        if not isinstance(copies, Sequence) or not copies:
            lines.append("  - none")
        else:
            for copy in copies:
                if not isinstance(copy, Mapping):
                    continue
                lines.append(
                    "  - "
                    f"{copy.get('id', 'unknown')} "
                    f"label={copy.get('label_text', 'unknown')} "
                    f"location={copy.get('location') or 'unassigned'} "
                    f"state={copy.get('state', 'unknown')} "
                    f"verification={copy.get('verification_state', 'unknown')}"
                )
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
            f"collections={totals.get('collections', 0)} "
            f"uploaded_collections={totals.get('uploaded_collections', 0)} "
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
            glacier = collection.get("glacier")
            glacier_state = (
                glacier.get("state", "unknown") if isinstance(glacier, Mapping) else "unknown"
            )
            manifest = collection.get("archive_manifest")
            ots_state = (
                "uploaded"
                if isinstance(manifest, Mapping) and manifest.get("ots_object_path")
                else "missing"
            )
            lines.append(
                f"- {collection.get('id', 'unknown')} "
                f"bytes={collection.get('bytes', 0)} "
                f"glacier={glacier_state} "
                f"ots={ots_state} "
                f"measured_storage_bytes={collection.get('measured_storage_bytes', 0)} "
                f"estimated_billable_bytes={collection.get('estimated_billable_bytes', 0)} "
                f"estimated_monthly_cost_usd={collection.get('estimated_monthly_cost_usd', 0.0)}"
            )
            if isinstance(glacier, Mapping) and glacier.get("object_path"):
                lines.append(f"  glacier_path: {glacier.get('object_path')}")
            if isinstance(manifest, Mapping) and manifest.get("object_path"):
                lines.append(f"  archive_manifest: {manifest.get('object_path')}")

    billing = payload.get("billing")
    lines.append("billing:")
    if not isinstance(billing, Mapping):
        lines.append("- unavailable")
    else:
        actuals = billing.get("actuals")
        lines.append("  actuals:")
        if not isinstance(actuals, Mapping):
            lines.append("  - unavailable")
        else:
            lines.append(
                "  - "
                f"source={actuals.get('source', 'unknown')} "
                f"scope={actuals.get('scope', 'unknown')} "
                f"filter={actuals.get('filter_label') or 'none'} "
                f"granularity={actuals.get('granularity') or 'unknown'}"
            )
            if actuals.get("billing_view_arn"):
                lines.append(f"    billing_view_arn: {actuals.get('billing_view_arn')}")
            periods = actuals.get("periods")
            if isinstance(periods, Sequence):
                for actual in periods:
                    if not isinstance(actual, Mapping):
                        continue
                    lines.append(
                        "    period: "
                        f"{actual.get('start', 'unknown')}..{actual.get('end', 'unknown')} "
                        f"estimated={actual.get('estimated', False)} "
                        f"unblended_cost_usd={actual.get('unblended_cost_usd', 0.0)} "
                        f"usage_quantity={actual.get('usage_quantity', 0.0)} "
                        f"usage_unit={actual.get('usage_unit') or 'unknown'}"
                    )
            notes = actuals.get("notes")
            if isinstance(notes, Sequence):
                for note in notes:
                    lines.append(f"    note: {note}")

        forecast = billing.get("forecast")
        lines.append("  forecast:")
        if not isinstance(forecast, Mapping):
            lines.append("  - unavailable")
        else:
            lines.append(
                "  - "
                f"source={forecast.get('source', 'unknown')} "
                f"scope={forecast.get('scope', 'unknown')} "
                f"filter={forecast.get('filter_label') or 'none'} "
                f"granularity={forecast.get('granularity') or 'unknown'}"
            )
            periods = forecast.get("periods")
            if isinstance(periods, Sequence):
                for period in periods:
                    if not isinstance(period, Mapping):
                        continue
                    lines.append(
                        "    period: "
                        f"{period.get('start', 'unknown')}..{period.get('end', 'unknown')} "
                        f"mean_cost_usd={period.get('mean_cost_usd', 0.0)} "
                        f"lower_bound_cost_usd={period.get('lower_bound_cost_usd', 0.0)} "
                        f"upper_bound_cost_usd={period.get('upper_bound_cost_usd', 0.0)}"
                    )
            notes = forecast.get("notes")
            if isinstance(notes, Sequence):
                for note in notes:
                    lines.append(f"    note: {note}")

        exports = billing.get("exports")
        lines.append("  exports:")
        if not isinstance(exports, Mapping):
            lines.append("  - unavailable")
        else:
            lines.append(
                "  - "
                f"source={exports.get('source', 'unknown')} "
                f"scope={exports.get('scope', 'unknown')} "
                f"filter={exports.get('filter_label') or 'none'} "
                f"object={exports.get('object_key') or 'none'}"
            )
            if exports.get("export_arn"):
                lines.append(f"    export_arn: {exports.get('export_arn')}")
            if exports.get("export_name"):
                lines.append(f"    export_name: {exports.get('export_name')}")
            if exports.get("execution_id"):
                lines.append(f"    execution_id: {exports.get('execution_id')}")
            if exports.get("manifest_key"):
                lines.append(f"    manifest_key: {exports.get('manifest_key')}")
            if exports.get("billing_period"):
                lines.append(f"    billing_period: {exports.get('billing_period')}")
            lines.append(f"    files_read: {exports.get('files_read', 0)}")
            breakdowns = exports.get("breakdowns")
            if isinstance(breakdowns, Sequence):
                for breakdown in breakdowns:
                    if not isinstance(breakdown, Mapping):
                        continue
                    lines.append(
                        "    breakdown: "
                        f"usage_type={breakdown.get('usage_type') or 'unknown'} "
                        f"operation={breakdown.get('operation') or 'unknown'} "
                        f"resource_id={breakdown.get('resource_id') or 'unknown'} "
                        f"tag_value={breakdown.get('tag_value') or 'unknown'} "
                        f"unblended_cost_usd={breakdown.get('unblended_cost_usd', 0.0)}"
                    )
            notes = exports.get("notes")
            if isinstance(notes, Sequence):
                for note in notes:
                    lines.append(f"    note: {note}")

        invoices = billing.get("invoices")
        lines.append("  invoices:")
        if not isinstance(invoices, Mapping):
            lines.append("  - unavailable")
        else:
            lines.append(
                "  - "
                f"source={invoices.get('source', 'unknown')} "
                f"scope={invoices.get('scope', 'unknown')} "
                f"account_id={invoices.get('account_id') or 'unknown'}"
            )
            items = invoices.get("invoices")
            if isinstance(items, Sequence):
                for invoice in items:
                    if not isinstance(invoice, Mapping):
                        continue
                    lines.append(
                        "    invoice: "
                        f"id={invoice.get('invoice_id') or 'unknown'} "
                        f"period={invoice.get('billing_period_start') or 'unknown'}.."
                        f"{invoice.get('billing_period_end') or 'unknown'} "
                        f"base_total_amount={invoice.get('base_total_amount', 0.0)} "
                        f"payment_total_amount={invoice.get('payment_total_amount', 0.0)}"
                    )
            notes = invoices.get("notes")
            if isinstance(notes, Sequence):
                for note in notes:
                    lines.append(f"    note: {note}")

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
                f"uploaded_collections={item.get('uploaded_collections', 0)} "
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
        glacier = collection.get("glacier")
        if isinstance(glacier, Mapping):
            lines.append(f"glacier: {glacier.get('state', 'unknown')}")
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
