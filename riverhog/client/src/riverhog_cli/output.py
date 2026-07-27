from __future__ import annotations

from collections.abc import Mapping, Sequence

from cli_support.output import (
    human_bytes as _bytes,
)
from cli_support.output import (
    mapping_items as _items,
)
from cli_support.output import (
    page_line as _page_line,
)

ENTITY_ID_STYLE = "bold cyan"
FIELD_STYLE = "dim"
ATTENTION_STYLE = "bold yellow"


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
        lines.append(f"- {file.get('file_ref', 'unknown')}  {_bytes(file.get('bytes'))}")
    return "\n".join(lines)


def format_collections(payload: Mapping[str, object]) -> str:
    lines = [_page_line(payload, "collections")]
    for collection in _items(payload, "collections"):
        tags = collection.get("tags")
        tag_text = ",".join(str(tag) for tag in tags) if isinstance(tags, Sequence) else ""
        lines.append(
            f"- {collection.get('id', 'unknown')}  "
            f"created={collection.get('created_at', 'unknown')}  "
            f"files={collection.get('files', 0)}  "
            f"bytes={_bytes(collection.get('bytes'))}  "
            f"tags={tag_text or 'none'}  "
            f"archive={_archive_copy_states(collection)}"
        )
    return "\n".join(lines)


def format_collection_summary(
    payload: Mapping[str, object],
) -> str:
    tags = payload.get("tags")
    tag_text = ", ".join(str(tag) for tag in tags) if isinstance(tags, Sequence) else ""
    lines = [
        f"collection {payload.get('id', 'unknown')}",
        f"created: {payload.get('created_at', 'unknown')}",
        f"tags: {tag_text or 'none'}",
        f"files: {payload.get('files', 0)}",
        f"bytes: {_bytes(payload.get('bytes'))}",
        f"remote storage: {_bytes(payload.get('remote_storage_bytes'))}",
        f"archive copies: {_archive_copy_states(payload)}",
    ]
    for archive in _items(payload, "archive_copies"):
        store = archive.get("store", "unknown")
        if archive.get("storage_class"):
            lines.append(f"{store} storage class: {archive['storage_class']}")
        if archive.get("last_verified_at"):
            lines.append(f"{store} verified: {archive['last_verified_at']}")
    return "\n".join(lines)


def format_collection_deletion_plan(payload: Mapping[str, object]) -> str:
    lines = [
        str(payload.get("warning", "DANGER: This collection deletion is permanent.")),
        "",
        f"collection deletion plan: {payload.get('collection_id', 'unknown')}",
        f"status: {payload.get('status', 'unknown')}",
        f"files: {payload.get('file_count', 0)} ({_bytes(payload.get('bytes'))})",
        f"remote storage: {_bytes(payload.get('remote_storage_bytes'))}",
        f"archive objects: {payload.get('archive_object_count', 0)}",
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
    lines = [
        str(payload.get("warning", "DANGER: This archive copy retirement is permanent.")),
        "",
        f"archive copy retirement plan: {payload.get('collection_id', 'unknown')}",
        f"store: {payload.get('store', 'unknown')}",
        f"status: {payload.get('status', 'unknown')}",
        f"remote storage: {_bytes(target_copy.get('remote_storage_bytes'))}",
        f"archive objects: {target_copy.get('object_count', 0)}",
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


def _quota_limit(value: object) -> str:
    if value is None:
        return "unlimited"
    try:
        current = int(str(value))
    except (TypeError, ValueError):
        return "unknown"
    return "blocked" if current == 0 else _bytes(current)


def format_app_access(payload: Mapping[str, object]) -> str:
    lines = [
        f"app: {payload.get('app', 'unknown')}",
        f"key: {payload.get('key_id', 'unknown')}",
        _page_line(payload, "access"),
    ]
    lines.extend(
        f"- {grant.get('permission', 'unknown')}  resource={grant.get('resource', 'unknown')}"
        for grant in _items(payload, "access")
    )
    return "\n".join(lines)


def format_app_access_set(payload: Mapping[str, object]) -> str:
    values = [
        (
            str(item.get("permission", "unknown"))
            if item.get("resource") == "*"
            else f"{item.get('permission', 'unknown')}={item.get('resource', 'unknown')}"
        )
        for item in _items(payload, "access")
    ]
    return "\n".join(
        [
            "application access replaced",
            f"app: {payload.get('app', 'unknown')}",
            f"key: {payload.get('key_id', 'unknown')}",
            "access: " + ", ".join(values),
        ]
    )


def format_tag(payload: Mapping[str, object]) -> str:
    return "\n".join(
        [
            f"tag: {payload.get('id', 'unknown')}",
            f"created by: {payload.get('created_by_app', 'unknown')}/"
            f"{payload.get('created_by_key_id') or 'bootstrap'}",
            f"created: {payload.get('created_at', 'unknown')}",
            f"collections: {payload.get('collections', 0)}",
        ]
    )


def format_tags(payload: Mapping[str, object]) -> str:
    lines = [_page_line(payload, "tags")]
    for tag in _items(payload, "tags"):
        lines.append(
            f"- {tag.get('id', 'unknown')}  collections={tag.get('collections', 0)}  "
            f"created={tag.get('created_at', 'unknown')}"
        )
    return "\n".join(lines)


def format_collection_tags(payload: Mapping[str, object]) -> str:
    tags = payload.get("tags")
    values = [str(tag) for tag in tags] if isinstance(tags, list) else []
    lines = [
        f"collection {payload.get('collection_id', 'unknown')}",
        f"metadata revision: {payload.get('metadata_revision', 'unknown')}",
        f"record etag: {payload.get('record_etag', 'unknown')}",
        "tags:",
    ]
    lines.extend(f"- {tag}" for tag in values)
    if not values:
        lines.append("- none")
    return "\n".join(lines)


def format_download_quota(payload: Mapping[str, object]) -> str:
    return "\n".join(
        [
            f"app: {payload.get('app', 'unknown')}",
            f"key: {payload.get('key_id', 'unknown')} ({payload.get('key_status', 'unknown')})",
            f"monthly remote-download quota: {_quota_limit(payload.get('monthly_bytes'))}",
            f"accounted: {_bytes(payload.get('accounted_bytes'))}",
            f"reserved: {_bytes(payload.get('reserved_bytes'))}",
            f"remaining: {_quota_limit(payload.get('remaining_bytes'))}",
            f"resets: {payload.get('resets_at', 'unknown')}",
        ]
    )


def format_download_quotas(payload: Mapping[str, object]) -> str:
    lines = [_page_line(payload, "quotas")]
    for quota in _items(payload, "quotas"):
        lines.append(
            f"- {quota.get('app', 'unknown')}/{quota.get('key_id', 'unknown')}  "
            f"status={quota.get('key_status', 'unknown')}  "
            f"quota={_quota_limit(quota.get('monthly_bytes'))}  "
            f"used={_bytes(quota.get('accounted_bytes'))}  "
            f"reserved={_bytes(quota.get('reserved_bytes'))}  "
            f"remaining={_quota_limit(quota.get('remaining_bytes'))}  "
            f"resets={quota.get('resets_at', 'unknown')}"
        )
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


def format_archive_store(payload: Mapping[str, object]) -> str:
    lines = [
        f"archive store {payload.get('store', 'unknown')}",
        f"backend: {payload.get('backend', 'unknown')}",
        f"storage class: {payload.get('storage_class', 'unknown')}",
        f"read mode: {payload.get('read_mode', 'unknown')}",
        f"write target: {'yes' if payload.get('write_target') else 'no'}",
        f"collections: {payload.get('collections', 0)}",
        f"objects: {payload.get('objects', 0)}",
        f"stored: {_bytes(payload.get('stored_bytes'))}",
    ]
    allowance = payload.get("download_allowance")
    if isinstance(allowance, Mapping):
        lines.extend(
            (
                f"download allowance: {_bytes(allowance.get('accounted_bytes'))} used + "
                f"{_bytes(allowance.get('reserved_bytes'))} reserved / "
                f"{_bytes(allowance.get('effective_limit_bytes'))}",
                f"download allowance resets: {allowance.get('resets_at', 'unknown')}",
            )
        )
    return "\n".join(lines)


def format_archive_stores(payload: Mapping[str, object]) -> str:
    lines = [_page_line(payload, "stores")]
    for store in _items(payload, "stores"):
        lines.append(
            f"- {store.get('store', 'unknown')}  "
            f"backend={store.get('backend', 'unknown')}  "
            f"class={store.get('storage_class', 'unknown')}  "
            f"read={store.get('read_mode', 'unknown')}  "
            f"write={'yes' if store.get('write_target') else 'no'}  "
            f"collections={store.get('collections', 0)}  "
            f"objects={store.get('objects', 0)}  "
            f"stored={_bytes(store.get('stored_bytes'))}"
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
    if payload.get("initiated_by_app"):
        initiator = str(payload["initiated_by_app"])
        if payload.get("initiated_by_key_id"):
            initiator += f"/{payload['initiated_by_key_id']}"
        lines.append(f"initiator: {initiator}")
    if payload.get("completed_at"):
        lines.append(f"completed: {payload['completed_at']}")
    if payload.get("failure"):
        lines.append(f"failure: {payload['failure']}")
    return "\n".join(lines)


def format_archive_copy_jobs(payload: Mapping[str, object]) -> str:
    lines = [_page_line(payload, "copies")]
    for copy in _items(payload, "copies"):
        lines.append(
            f"- {copy.get('collection_id', 'unknown')}  "
            f"{copy.get('source_store', 'automatic')} -> "
            f"{copy.get('destination_store', 'unknown')}  "
            f"state={copy.get('state', 'unknown')}  "
            f"requested={copy.get('requested_at', 'unknown')}"
        )
    return "\n".join(lines)


def format_file_selectors(
    payload: Mapping[str, object],
    key: str = "files",
) -> str:
    return "\n".join(
        f"{item['collection_id']}::{item['path']}"
        for item in _items(payload, key)
        if item.get("collection_id") not in {None, ""} and item.get("path") not in {None, ""}
    )
