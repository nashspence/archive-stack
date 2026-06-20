from __future__ import annotations

import json
import os
import sys
from collections.abc import Mapping, Sequence
from typing import Any, Literal

import typer

RichConsole: Any
RichGroup: Any
RichTable: Any
RichText: Any

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


def _quiet_table(*columns: str) -> Any:
    table = RichTable(box=None, show_edge=False, padding=(0, 2), collapse_padding=True)
    for index, column in enumerate(columns):
        table.add_column(column, no_wrap=index == 0)
    return table


def _detail_table() -> Any:
    table = RichTable(
        box=None,
        show_edge=False,
        show_header=False,
        padding=(0, 2),
        collapse_padding=True,
    )
    table.add_column("Field", style="bold", no_wrap=True)
    table.add_column("Value")
    return table


def _preview_lines(items: Sequence[str], *, limit: int = 5) -> str:
    if not items:
        return "none"
    shown = list(items[:limit])
    if len(items) > limit:
        shown.append(f"... {len(items) - limit} more")
    return "\n".join(shown)


def _string_items(value: object) -> list[str]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        return []
    return [str(item) for item in value]


def _copy_label(copy: Mapping[str, object]) -> str:
    copy_id = str(copy.get("id", "unknown"))
    volume_id = str(copy.get("volume_id", "unknown"))
    location = str(copy.get("location") or "unassigned")
    return f"{copy_id} ({volume_id} @ {location})"


def _copy_lines(value: object, *, limit: int = 5) -> str:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        return "none"
    copies = [copy for copy in value if isinstance(copy, Mapping)]
    if not copies:
        return "none"
    lines = [
        f"{copy.get('label_text') or copy.get('id', 'unknown')} @ "
        f"{copy.get('location') or 'unassigned'} "
        f"({copy.get('verification_state', copy.get('state', 'unknown'))})"
        for copy in copies[:limit]
    ]
    if len(copies) > limit:
        lines.append(f"... {len(copies) - limit} more")
    return "\n".join(lines)


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


def _format_pin_plain(payload: Mapping[str, Any]) -> str:
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


def format_pin(payload: Mapping[str, Any]) -> Any:
    if not _rich_enabled():
        return _format_pin_plain(payload)

    table = _detail_table()
    table.add_row("target", str(payload.get("target", "unknown")))
    table.add_row("pin", "yes" if payload.get("pin") else "no")

    hot = payload.get("hot")
    if isinstance(hot, Mapping):
        table.add_row("hot", str(hot.get("state", "unknown")))
        table.add_row("present", _bytes_text(hot.get("present_bytes", 0)))
        table.add_row("missing", _bytes_text(hot.get("missing_bytes", 0)))

    fetch = payload.get("fetch")
    if isinstance(fetch, Mapping):
        table.add_row("fetch", str(fetch.get("id", "unknown")))
        table.add_row("fetch state", str(fetch.get("state", "unknown")))
        table.add_row("fetch files", str(fetch.get("files", 0)))
        table.add_row("fetch bytes", _bytes_text(fetch.get("bytes", 0)))
        table.add_row("fetch missing", _bytes_text(fetch.get("missing_bytes", 0)))
        table.add_row("candidate copies", _copy_lines(fetch.get("copies")))

    return RichGroup(RichText("hot pin", style="bold"), table)


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
        f"target: {summary.get('target', 'unknown')}",
        "pending:",
    ]
    lines.extend(pending or ["- none"])
    lines.append("partial:")
    lines.extend(partial or ["- none", "expires: n/a"])
    lines.append("byte-complete:")
    lines.extend(byte_complete or ["- none"])
    return "\n".join(lines)


def format_fetch(summary: Mapping[str, Any], manifest: Mapping[str, Any]) -> Any:
    if not _rich_enabled():
        return _format_fetch_plain(summary, manifest)

    pending, partial, byte_complete = _fetch_status_lines(manifest)
    table = _detail_table()
    table.add_row("fetch", str(summary.get("id", "unknown")))
    table.add_row("target", str(summary.get("target", "unknown")))
    table.add_row("state", str(summary.get("state", "unknown")))
    table.add_row("files", str(summary.get("files", 0)))
    table.add_row("bytes", _bytes_text(summary.get("bytes", 0)))
    table.add_row("missing", _bytes_text(summary.get("missing_bytes", 0)))
    table.add_row("copies", _copy_lines(summary.get("copies")))

    status_table = _quiet_table("Status", "Items")
    status_table.add_row("pending", _preview_lines(pending, limit=8))
    status_table.add_row("partial", _preview_lines(partial, limit=8))
    status_table.add_row("byte-complete", _preview_lines(byte_complete, limit=8))

    return RichGroup(
        RichText("fetch", style="bold"),
        table,
        RichText("entries", style="bold"),
        status_table,
    )


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
        disc_text = "unknown"
        if isinstance(disc_coverage, Mapping):
            disc_text = (
                f"{disc_coverage.get('state', 'unknown')} "
                f"{disc_coverage.get('verified_physical_bytes', 0)}"
            )
        lines.append(
            f"- {collection.get('id', 'unknown')} "
            f"protection={collection.get('protection_state', 'unknown')} "
            f"files={collection.get('files', 0)} "
            f"bytes={collection.get('bytes', 0)} "
            f"hot={collection.get('hot_bytes', 0)}/{collection.get('bytes', 0)} "
            f"archive={collection.get('archived_bytes', 0)}/{collection.get('bytes', 0)} "
            f"disc={disc_text}"
        )
    return "\n".join(lines)


def format_collections(payload: Mapping[str, Any]) -> Any:
    if not _rich_enabled():
        return _format_collections_plain(payload)
    table = _quiet_table(
        "Collection",
        "Protection",
        "Files",
        "Bytes",
        "Hot",
        "Archive",
        "Disc",
    )
    collections = payload.get("collections")
    if isinstance(collections, Sequence):
        for collection in collections:
            if not isinstance(collection, Mapping):
                continue
            disc_coverage = collection.get("disc_coverage")
            if isinstance(disc_coverage, Mapping):
                disc_text = (
                    f"{disc_coverage.get('state', 'unknown')} "
                    f"{_bytes_text(disc_coverage.get('verified_physical_bytes', 0))}"
                )
            else:
                disc_text = "unknown"
            table.add_row(
                str(collection.get("id", "unknown")),
                str(collection.get("protection_state", "unknown")),
                str(collection.get("files", 0)),
                _bytes_text(collection.get("bytes", 0)),
                _ratio_text(collection.get("hot_bytes", 0), collection.get("bytes", 0)),
                _ratio_text(collection.get("archived_bytes", 0), collection.get("bytes", 0)),
                disc_text,
            )
    if not table.rows:
        table.add_row("none", "", "", "", "", "", "")
    return RichGroup(_page_text("collections", payload), table)


def _format_hot_pins_plain(payload: Mapping[str, Any]) -> str:
    pins = payload.get("pins")
    if not isinstance(pins, Sequence) or not pins:
        return "hot pins:\n- none"
    lines = ["hot pins:"]
    for pin in pins:
        if not isinstance(pin, Mapping):
            continue
        fetch = pin.get("fetch")
        if isinstance(fetch, Mapping):
            copies = fetch.get("copies")
            copy_count = fetch.get(
                "copy_count",
                len(copies) if isinstance(copies, Sequence) else None,
            )
            lines.append(
                f"- {pin.get('target', 'unknown')} "
                f"fetch={fetch.get('id', 'unknown')} "
                f"state={fetch.get('state', 'unknown')} "
                f"files={fetch.get('files', 0)} "
                f"bytes={fetch.get('bytes', 0)} "
                f"missing={fetch.get('missing_bytes', 0)}"
                + (f" copies={copy_count}" if copy_count is not None else "")
            )
        else:
            lines.append(f"- {pin.get('target', 'unknown')} fetch=none")
    return "\n".join(lines)


def format_hot_pins(payload: Mapping[str, Any]) -> Any:
    if not _rich_enabled():
        return _format_hot_pins_plain(payload)
    table = _quiet_table("Target", "Fetch", "State", "Missing", "Files", "Bytes")
    pins = payload.get("pins")
    if isinstance(pins, Sequence):
        for pin in pins:
            if not isinstance(pin, Mapping):
                continue
            fetch = pin.get("fetch")
            fetch_id = "none"
            state = "unknown"
            if isinstance(fetch, Mapping):
                fetch_id = str(fetch.get("id", "unknown"))
                state = str(fetch.get("state", "unknown"))
                files = str(fetch.get("files", 0))
                bytes_text = _bytes_text(fetch.get("bytes", 0))
                missing = _bytes_text(fetch.get("missing_bytes", 0))
            else:
                files = "0"
                bytes_text = "0 B"
                missing = "0 B"
            table.add_row(
                str(pin.get("target", "unknown")),
                fetch_id,
                state,
                missing,
                files,
                bytes_text,
            )
    if not table.rows:
        table.add_row("none", "", "", "", "", "")
    return RichGroup(RichText("hot pins", style="bold"), table)


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


def format_images(payload: Mapping[str, Any]) -> Any:
    if not _rich_enabled():
        return _format_images_plain(payload)
    table = _quiet_table("Image", "Protection", "Discs", "Files", "Bytes", "Fill", "Collections")
    images = payload.get("images")
    if isinstance(images, Sequence):
        for image in images:
            if not isinstance(image, Mapping):
                continue
            required = image.get("physical_copies_required", 0)
            table.add_row(
                str(image.get("id", "unknown")),
                str(image.get("physical_protection_state", "unknown")),
                (
                    f"{image.get('physical_copies_verified', 0)}/"
                    f"{image.get('physical_copies_registered', 0)}/"
                    f"{required}"
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
    return "\n".join(
        [
            f"image: {image.get('id', 'unknown')} ({image.get('filename', 'unknown')})",
            f"finalized_at: {image.get('finalized_at', 'unknown')}",
            f"bytes: {image.get('bytes', 0)} "
            f"target_bytes={image.get('target_bytes', 0)} "
            f"fill={image.get('fill', 0)} "
            f"files={image.get('files', 0)}",
            "protection: "
            f"{image.get('physical_protection_state', 'unknown')} "
            f"registered={image.get('physical_copies_registered', 0)}/"
            f"{image.get('physical_copies_required', 0)} "
            f"verified={image.get('physical_copies_verified', 0)}/"
            f"{image.get('physical_copies_required', 0)}",
            f"collections: {image.get('collections', 0)} "
            f"[{_collection_ids_text(image.get('collection_ids'))}]",
        ]
    )


def format_image(image: Mapping[str, Any]) -> Any:
    if not _rich_enabled():
        return _format_image_plain(image)
    table = _quiet_table("Field", "Value")
    table.add_row("image", str(image.get("id", "unknown")))
    table.add_row("filename", str(image.get("filename", "unknown")))
    table.add_row("finalized_at", str(image.get("finalized_at", "unknown")))
    table.add_row("files", str(image.get("files", 0)))
    table.add_row("bytes", _bytes_text(image.get("bytes", 0)))
    table.add_row("target", _bytes_text(image.get("target_bytes", 0)))
    table.add_row("fill", f"{float(image.get('fill', 0) or 0) * 100:.1f}%")
    table.add_row("protection", str(image.get("physical_protection_state", "unknown")))
    table.add_row(
        "discs",
        (
            f"verified={image.get('physical_copies_verified', 0)}/"
            f"{image.get('physical_copies_required', 0)} "
            f"registered={image.get('physical_copies_registered', 0)}/"
            f"{image.get('physical_copies_required', 0)}"
        ),
    )
    table.add_row("collections", _collection_ids_text(image.get("collection_ids")))
    return RichGroup(RichText("image", style="bold"), table)


def _format_release_plain(payload: Mapping[str, Any]) -> str:
    return "\n".join(
        [
            f"target: {payload.get('target', 'unknown')}",
            f"pin: {'true' if payload.get('pin') else 'false'}",
        ]
    )


def format_release(payload: Mapping[str, Any]) -> Any:
    if not _rich_enabled():
        return _format_release_plain(payload)
    table = _detail_table()
    table.add_row("target", str(payload.get("target", "unknown")))
    table.add_row("pin", "yes" if payload.get("pin") else "no")
    return RichGroup(RichText("hot pin", style="bold"), table)


def _disc_copy_payload(payload: Mapping[str, Any]) -> Mapping[str, Any]:
    copy_payload = payload.get("copy")
    if isinstance(copy_payload, Mapping):
        return copy_payload
    return payload


def _format_disc_plain(payload: Mapping[str, Any]) -> str:
    copy_payload = _disc_copy_payload(payload)
    lines = [
        f"disc: {copy_payload.get('id', 'unknown')}",
        f"image: {payload.get('image_id', copy_payload.get('image_id', 'unknown'))}",
        f"volume: {copy_payload.get('volume_id', 'unknown')}",
        f"label: {copy_payload.get('label_text', 'unknown')}",
        f"location: {copy_payload.get('location') or 'unassigned'}",
        f"state: {copy_payload.get('state', 'unknown')}",
        f"verification: {copy_payload.get('verification_state', 'unknown')}",
    ]
    history = copy_payload.get("history")
    if isinstance(history, Sequence):
        lines.append(f"history: {len(history)} event(s)")
    return "\n".join(lines)


def format_disc(payload: Mapping[str, Any]) -> Any:
    if not _rich_enabled():
        return _format_disc_plain(payload)

    copy_payload = _disc_copy_payload(payload)
    table = _detail_table()
    table.add_row("disc", str(copy_payload.get("id", "unknown")))
    table.add_row("image", str(payload.get("image_id", copy_payload.get("image_id", "unknown"))))
    table.add_row("volume", str(copy_payload.get("volume_id", "unknown")))
    table.add_row("label", str(copy_payload.get("label_text", "unknown")))
    table.add_row("location", str(copy_payload.get("location") or "unassigned"))
    table.add_row("state", str(copy_payload.get("state", "unknown")))
    table.add_row("verification", str(copy_payload.get("verification_state", "unknown")))
    if copy_payload.get("created_at"):
        table.add_row("created", str(copy_payload.get("created_at")))

    renderables: list[Any] = [RichText("disc", style="bold"), table]
    history = copy_payload.get("history")
    if isinstance(history, Sequence) and history:
        history_table = _quiet_table("At", "Event", "State", "Verification", "Location")
        for item in history:
            if not isinstance(item, Mapping):
                continue
            history_table.add_row(
                str(item.get("at", "unknown")),
                str(item.get("event", "unknown")),
                str(item.get("state", "unknown")),
                str(item.get("verification_state", "unknown")),
                str(item.get("location") or "unassigned"),
            )
        renderables.extend([RichText("history", style="bold"), history_table])
    return RichGroup(*renderables)


def _disc_items(payload: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    items = payload.get("discs")
    if isinstance(items, Sequence):
        return [item for item in items if isinstance(item, Mapping)]
    copies = payload.get("copies")
    image_id = payload.get("image_id")
    if not isinstance(copies, Sequence):
        return []
    return [{**copy, "image_id": image_id} for copy in copies if isinstance(copy, Mapping)]


def _format_discs_plain(payload: Mapping[str, Any]) -> str:
    discs = _disc_items(payload)
    if not discs:
        return "discs:\n- none"
    lines = ["discs:"]
    for disc in discs:
        lines.append(
            f"- {disc.get('id', 'unknown')} "
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
            str(item.get("id", "unknown")),
            str(item.get("image_id", "unknown")),
            str(item.get("state", "unknown")),
            str(item.get("verification_state", "unknown")),
            str(item.get("location") or "unassigned"),
        )
    if not table.rows:
        table.add_row("none", "", "", "", "")
    return RichGroup(RichText("discs", style="bold"), table)


def _format_collection_summary_plain(
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

    collection_manifest = payload.get("collection_manifest")
    if isinstance(collection_manifest, Mapping):
        lines.append(
            "collection_manifest: "
            f"{collection_manifest.get('object_path') or 'missing'} "
            f"sha256={collection_manifest.get('sha256') or 'unknown'}"
        )
        ots_state = "uploaded" if collection_manifest.get("ots_object_path") else "missing"
        lines.append(
            f"ots: {ots_state} "
            f"path={collection_manifest.get('ots_object_path') or 'missing'}"
        )

    disc_coverage = payload.get("disc_coverage")
    if isinstance(disc_coverage, Mapping):
        lines.append(
            "disc_coverage="
            f"{disc_coverage.get('state', 'unknown')} "
            f"verified_physical_bytes={disc_coverage.get('verified_physical_bytes', 0)}"
        )

    if isinstance(collection_glacier, Mapping):
        lines.append(
            "glacier_footprint: "
            f"bytes={collection_glacier.get('bytes', 0)} "
            f"measured_storage_bytes={collection_glacier.get('measured_storage_bytes', 0)}"
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


def _collection_recovery_rows(
    table: Any,
    recovery: object,
    *,
    total_bytes: int,
) -> None:
    if not isinstance(recovery, Mapping):
        table.add_row("recovery", "unknown")
        return

    available = _string_items(recovery.get("available"))
    table.add_row("available", ", ".join(available) if available else "none")

    verified = recovery.get("verified_physical")
    if isinstance(verified, Mapping):
        table.add_row(
            "verified physical",
            f"{verified.get('state', 'unknown')} "
            f"{_ratio_text(verified.get('bytes', 0), total_bytes)}",
        )

    glacier = recovery.get("glacier")
    if isinstance(glacier, Mapping):
        table.add_row(
            "deep archive",
            f"{glacier.get('state', 'unknown')} "
            f"{_ratio_text(glacier.get('bytes', 0), total_bytes)}",
        )


def _collection_image_costs(
    collection_glacier: Mapping[str, Any] | None,
) -> dict[str, Mapping[str, Any]]:
    if not isinstance(collection_glacier, Mapping):
        return {}
    contributions = collection_glacier.get("images")
    if not isinstance(contributions, Sequence):
        return {}
    return {
        str(item.get("image_id")): item
        for item in contributions
        if isinstance(item, Mapping)
    }


def _collection_coverage_table(
    payload: Mapping[str, Any],
    collection_glacier: Mapping[str, Any] | None,
) -> Any:
    table = _quiet_table("Image", "Protection", "Discs", "Paths", "Copies", "Archive")
    image_costs = _collection_image_costs(collection_glacier)
    images = payload.get("image_coverage")
    if isinstance(images, Sequence):
        for image in images:
            if not isinstance(image, Mapping):
                continue
            image_id = str(image.get("id", "unknown"))
            required = image.get("physical_copies_required", 0)
            contribution = image_costs.get(image_id)
            archive_text = (
                _bytes_text(contribution.get("represented_bytes", 0))
                if isinstance(contribution, Mapping)
                else "unknown"
            )
            table.add_row(
                image_id,
                str(image.get("physical_protection_state", "unknown")),
                (
                    f"{image.get('physical_copies_verified', 0)}/"
                    f"{image.get('physical_copies_registered', 0)}/"
                    f"{required}"
                ),
                _preview_lines(_string_items(image.get("covered_paths")), limit=4),
                _copy_lines(image.get("copies"), limit=4),
                archive_text,
            )
    if not table.rows:
        table.add_row("none", "", "", "", "", "")
    return table


def format_collection_summary(
    payload: Mapping[str, Any],
    glacier_payload: Mapping[str, Any],
) -> Any:
    if not _rich_enabled():
        return _format_collection_summary_plain(payload, glacier_payload)

    collection_id = str(payload.get("id", "unknown"))
    total_bytes = _int_value(payload.get("bytes", 0))
    collection_glacier = _find_collection_glacier_entry(collection_id, glacier_payload)

    overview = _detail_table()
    overview.add_row("protection", str(payload.get("protection_state", "unknown")))
    overview.add_row("protected", _ratio_text(payload.get("protected_bytes", 0), total_bytes))
    overview.add_row("files", str(payload.get("files", 0)))
    overview.add_row("bytes", _bytes_text(total_bytes))
    overview.add_row("hot", _ratio_text(payload.get("hot_bytes", 0), total_bytes))
    overview.add_row("archive", _ratio_text(payload.get("archived_bytes", 0), total_bytes))
    overview.add_row("pending", _bytes_text(payload.get("pending_bytes", 0)))
    _collection_recovery_rows(overview, payload.get("recovery"), total_bytes=total_bytes)

    disc_coverage = payload.get("disc_coverage")
    if isinstance(disc_coverage, Mapping):
        overview.add_row(
            "disc",
            f"{disc_coverage.get('state', 'unknown')} "
            f"{_ratio_text(disc_coverage.get('verified_physical_bytes', 0), total_bytes)}",
        )

    direct_glacier = payload.get("glacier")
    if isinstance(direct_glacier, Mapping):
        overview.add_row(
            "glacier",
            f"{direct_glacier.get('state', 'unknown')} "
            f"{_bytes_text(direct_glacier.get('stored_bytes', 0))} stored "
            f"{direct_glacier.get('backend') or 'unknown'} "
            f"{direct_glacier.get('storage_class') or 'unknown'}",
        )
        if direct_glacier.get("object_path"):
            overview.add_row("glacier path", str(direct_glacier.get("object_path")))
        if direct_glacier.get("failure"):
            overview.add_row("glacier failure", str(direct_glacier.get("failure")))

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

    if isinstance(collection_glacier, Mapping):
        overview.add_row(
            "footprint",
            f"{_bytes_text(collection_glacier.get('bytes', 0))} logical, "
            f"{_bytes_text(collection_glacier.get('measured_storage_bytes', 0))} measured",
        )

    return RichGroup(
        RichText(f"collection {collection_id}", style="bold"),
        overview,
        RichText("coverage", style="bold"),
        _collection_coverage_table(payload, collection_glacier),
    )


def format_glacier_report(payload: Mapping[str, Any]) -> str:
    totals = payload.get("totals")
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
            glacier = collection.get("glacier")
            glacier_state = (
                glacier.get("state", "unknown") if isinstance(glacier, Mapping) else "unknown"
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
                f"glacier={glacier_state} "
                f"ots={ots_state} "
                f"measured_storage_bytes={collection.get('measured_storage_bytes', 0)}"
            )
            if isinstance(glacier, Mapping) and glacier.get("object_path"):
                lines.append(f"  glacier_path: {glacier.get('object_path')}")
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
                str(candidate.get("candidate_id", "unknown")),
                "yes" if candidate.get("iso_ready", False) else "no",
                f"{float(candidate.get('fill', 0) or 0) * 100:.1f}%",
                str(candidate.get("files", 0)),
                _bytes_text(candidate.get("bytes", 0)),
                str(candidate.get("collections", 0)),
            )
    if not table.rows:
        table.add_row("none", "", "", "", "", "")
    return RichGroup(_page_text("plan", payload), planner, table)


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
