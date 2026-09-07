from __future__ import annotations

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

ENTITY_ID_STYLE = "bold cyan"
FIELD_STYLE = "dim"
ATTENTION_STYLE = "bold yellow"


def format_find(payload: Mapping[str, object]) -> str:
    lines = [_page_line(payload, "files")]
    for file in _items(payload, "files"):
        lines.append(f"- {file.get('file_ref', 'unknown')}  {_bytes(file.get('bytes'))}")
    return "\n".join(lines)


def format_collections(payload: Mapping[str, object]) -> str:
    lines = [_page_line(payload, "collections")]
    for collection in _items(payload, "collections"):
        lines.append(
            f"- {collection.get('id', 'unknown')}  "
            f"created={collection.get('created_at', 'unknown')}  "
            f"files={collection.get('files', 0)}  "
            f"bytes={_bytes(collection.get('bytes'))}  "
            f"encryption={collection.get('encryption_format', 'unknown')}:"
            f"{collection.get('passphrase_id', 'unknown')}  "
            f"archive-copies={collection.get('archive_copy_count', 0)}"
        )
    return "\n".join(lines)


def format_local_collections(payload: Mapping[str, object]) -> str:
    lines = [_page_line(payload, "local collections")]
    for collection in _items(payload, "collections"):
        lines.append(
            f"- {collection.get('collection_id', 'unknown')}  "
            f"status={collection.get('status', 'unknown')}  "
            f"created={collection.get('created_at', 'unknown')}  "
            f"files={collection.get('files', 0)}  "
            f"bytes={_bytes(collection.get('bytes'))}"
        )
    return "\n".join(lines)


def format_local_collection(payload: Mapping[str, object]) -> str:
    return "\n".join(
        [
            f"local collection {payload.get('collection_id', 'unknown')}",
            f"status: {payload.get('status', 'unknown')}",
            f"created: {payload.get('created_at', 'unknown')}",
            f"files: {payload.get('files', 0)}",
            f"bytes: {_bytes(payload.get('bytes'))}",
        ]
    )


def format_collection_summary(
    payload: Mapping[str, object],
) -> str:
    lines = [
        f"collection {payload.get('id', 'unknown')}",
        f"created: {payload.get('created_at', 'unknown')}",
        f"files: {payload.get('files', 0)}",
        f"bytes: {_bytes(payload.get('bytes'))}",
        f"encryption: {payload.get('encryption_format', 'unknown')}:"
        f"{payload.get('passphrase_id', 'unknown')}",
        f"remote storage: {_bytes(payload.get('remote_storage_bytes'))}",
        f"archive copies: {payload.get('archive_copy_count', 0)}",
    ]
    return "\n".join(lines)


def format_collection_archive_copies(payload: Mapping[str, object]) -> str:
    lines = [_page_line(payload, "archive copies")]
    for copy in _items(payload, "copies"):
        lines.append(
            f"- {copy.get('store', 'unknown')}  "
            f"state={copy.get('state', 'unknown')}  "
            f"objects={copy.get('object_count', 0)}  "
            f"stored={_bytes(copy.get('stored_bytes'))}"
        )
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


def _upload_custody(
    payload: Mapping[str, object],
    *,
    files_key: str,
    bytes_key: str,
) -> tuple[str, object, object]:
    custody = payload.get("custody")
    if not isinstance(custody, Mapping):
        return "unknown", 0, 0
    state = str(custody.get("state", "unknown"))
    if state == "complete":
        return state, payload.get(files_key, 0), payload.get(bytes_key, 0)
    return state, custody.get("files", 0), custody.get("bytes", 0)


def format_collection_upload(payload: Mapping[str, object]) -> str:
    custody_state, custody_files, custody_bytes = _upload_custody(
        payload,
        files_key="files_total",
        bytes_key="bytes_total",
    )
    lines = [
        f"collection upload {payload.get('collection_id', 'unknown')}",
        f"state: {payload.get('state', 'unknown')}",
        f"custody state: {custody_state}",
        f"custodied files: {custody_files}/{payload.get('files_total', 0)}",
        f"custodied bytes: {_bytes(custody_bytes)}/{_bytes(payload.get('bytes_total'))}",
        f"custody: {payload.get('custody_mode', 'unknown')}",
        f"encryption: {payload.get('encryption_format', 'unknown')}:"
        f"{payload.get('passphrase_id', 'unknown')}",
    ]
    if payload.get("archive_phase"):
        lines.append(f"archive phase: {payload['archive_phase']}")
    if payload.get("latest_failure"):
        lines.append(f"failure: {payload['latest_failure']}")
    if payload.get("archive_next_attempt_at"):
        lines.append(f"archive next attempt: {payload['archive_next_attempt_at']}")
    if payload.get("upload_state_expires_at"):
        lines.append(f"lease expires: {payload['upload_state_expires_at']}")
    if payload.get("orphaned_at"):
        lines.append(f"orphaned: {payload['orphaned_at']}")
    return "\n".join(lines)


def format_collection_upload_discard_plan(payload: Mapping[str, object]) -> str:
    custody_state, custody_files, custody_bytes = _upload_custody(
        payload,
        files_key="files",
        bytes_key="bytes",
    )
    lines = [
        f"collection upload discard: {payload.get('status', 'unknown')}",
        f"collection: {payload.get('collection_id', 'unknown')}",
        f"state: {payload.get('state', 'unknown')}",
        f"files: {payload.get('files', 0)}",
        f"bytes: {_bytes(payload.get('bytes'))}",
        f"custody state: {custody_state}",
        f"custodied files: {custody_files}",
        f"custodied bytes: {_bytes(custody_bytes)}",
        f"archive objects: {payload.get('archive_objects', 0)}",
        f"warning: {payload.get('warning', 'unknown')}",
    ]
    blockers = payload.get("blockers")
    if isinstance(blockers, Sequence) and blockers:
        lines.extend(f"blocked: {item}" for item in blockers)
    if payload.get("expires_at"):
        lines.append(f"plan expires: {payload['expires_at']}")
    if payload.get("challenge"):
        lines.append(f"confirmation challenge: {payload['challenge']}")
    return "\n".join(lines)


def format_collection_upload_discard_result(payload: Mapping[str, object]) -> str:
    custody_state, custody_files, custody_bytes = _upload_custody(
        payload,
        files_key="files",
        bytes_key="bytes",
    )
    return "\n".join(
        [
            f"collection upload discard: {payload.get('status', 'unknown')}",
            f"collection: {payload.get('collection_id', 'unknown')}",
            f"files: {payload.get('files', 0)}",
            f"bytes: {_bytes(payload.get('bytes'))}",
            f"custody state: {custody_state}",
            f"custodied files: {custody_files}",
            f"custodied bytes: {_bytes(custody_bytes)}",
            f"archive objects: {payload.get('archive_objects', 0)}",
        ]
    )


def format_collection_uploads(payload: Mapping[str, object]) -> str:
    lines = [_page_line(payload, "uploads")]
    for upload in _items(payload, "uploads"):
        custody_state, custody_files, custody_bytes = _upload_custody(
            upload,
            files_key="files",
            bytes_key="bytes",
        )
        lines.append(
            f"- {upload.get('collection_id', 'unknown')}  "
            f"state={upload.get('state', 'unknown')}  "
            f"created={upload.get('created_at', 'unknown')}  "
            f"files={upload.get('files', 0)}  "
            f"bytes={_bytes(upload.get('bytes'))}  "
            f"custody={custody_state}:{custody_files}/"
            f"{upload.get('files', 0)}:{_bytes(custody_bytes)}  "
            f"mode={upload.get('custody_mode', 'unknown')}  "
            f"encryption={upload.get('encryption_format', 'unknown')}:"
            f"{upload.get('passphrase_id', 'unknown')}"
        )
    return "\n".join(lines)


def format_collection_upload_files(payload: Mapping[str, object]) -> str:
    lines = [_page_line(payload, "files")]
    for file in _items(payload, "files"):
        receipt = file.get("custody_receipt")
        custody = "custodied" if isinstance(receipt, Mapping) else "pending"
        lines.append(
            f"- {file.get('path', 'unknown')}  "
            f"custody={custody}  "
            f"bytes={_bytes(file.get('bytes'))}  "
            f"sha256={file.get('sha256', 'unknown')}"
        )
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
    lines = [_page_line(payload, "access")]
    lines.extend(
        f"- {grant.get('app', 'unknown')}/{grant.get('key_id', 'unknown')}  "
        f"status={grant.get('key_status', 'unknown')}  "
        f"permission={grant.get('permission', 'unknown')}  "
        f"resource={grant.get('resource', 'unknown')}"
        for grant in _items(payload, "access")
    )
    return "\n".join(lines)


def format_app_access_selectors(payload: Mapping[str, object]) -> str:
    selectors: list[str] = []
    for grant in _items(payload, "access"):
        permission = str(grant.get("permission", ""))
        resource = str(grant.get("resource", ""))
        allow = permission if resource == "*" else f"{permission}={resource}"
        if grant.get("app") and grant.get("key_id") and permission and resource:
            selectors.append(f"{grant['app']}::{grant['key_id']}::{allow}")
    return "\n".join(selectors)


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
            "application access updated",
            f"app: {payload.get('app', 'unknown')}",
            f"key: {payload.get('key_id', 'unknown')}",
            "access: " + ", ".join(values),
        ]
    )


def format_collection_access_groups(payload: Mapping[str, object]) -> str:
    lines = [_page_line(payload, "groups")]
    for group in _items(payload, "groups"):
        lines.append(
            f"- {group.get('id', 'unknown')}  status={group.get('status', 'unknown')}  "
            f"collections={group.get('collection_count', 0)}  "
            f"label={group.get('display_label') or 'none'}"
        )
    return "\n".join(lines)


def format_collection_access_group_members(payload: Mapping[str, object]) -> str:
    lines = [_page_line(payload, "members")]
    lines.extend(
        f"- {member.get('collection_id', 'unknown')}  added={member.get('added_at', 'unknown')}"
        for member in _items(payload, "members")
    )
    return "\n".join(lines)


def format_collection_access_groups_for_collection(payload: Mapping[str, object]) -> str:
    return format_collection_access_groups(payload)


def format_collection_access_group_membership(payload: Mapping[str, object]) -> str:
    return "\n".join(
        [
            f"access group: {payload.get('group_id', 'unknown')}",
            f"collection: {payload.get('collection_id', 'unknown')}",
            f"present: {payload.get('present', False)}",
            f"changed: {payload.get('changed', False)}",
            f"authorization revision: {payload.get('authorization_revision', 'unknown')}",
        ]
    )


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
    observer = payload.get("provenance_observer")
    if isinstance(observer, Mapping):
        lines.append(
            f"provenance observer: {observer.get('name', 'unknown')}  "
            f"contract={observer.get('contract_id', 'unknown')}@"
            f"{observer.get('contract_sha256', 'unknown')}"
        )
    return "\n".join(lines)


def format_archive_store(payload: Mapping[str, object]) -> str:
    lines = [
        f"archive store {payload.get('store', 'unknown')}",
        f"read mode: {payload.get('read_mode', 'unknown')}",
        f"read priority: {payload.get('read_priority', 'unknown')}",
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
            f"read={store.get('read_mode', 'unknown')}  "
            f"read-priority={store.get('read_priority', 'unknown')}  "
            f"write={'yes' if store.get('write_target') else 'no'}  "
            f"collections={store.get('collections', 0)}  "
            f"objects={store.get('objects', 0)}  "
            f"stored={_bytes(store.get('stored_bytes'))}"
        )
    return "\n".join(lines)


def format_retrieval_cache_status(payload: Mapping[str, object]) -> str:
    policy = payload.get("policy")
    values = policy if isinstance(policy, Mapping) else {}
    lines = [
        "retrieval cache",
        f"configured: {'yes' if payload.get('configured') else 'no'}",
        f"new archive insertion: {'enabled' if payload.get('new_archive_enabled') else 'disabled'}",
        f"objects: {payload.get('objects', 0)}",
        f"stored: {_bytes(payload.get('stored_bytes'))}",
        f"protected: {payload.get('protected_objects', 0)}",
        f"unleased: {payload.get('unleased_objects', 0)}",
        f"new archive lease: {values.get('new_archive_lease_seconds', 0)}s",
        f"retrieval lease: {values.get('retrieval_default_lease_seconds', 0)}s default, "
        f"{values.get('retrieval_max_lease_seconds', 0)}s maximum",
        f"pending timeout: {values.get('pending_timeout_seconds', 0)}s",
        f"sweep interval: {values.get('sweep_interval_seconds', 0)}s",
        f"restore poll interval: {values.get('restore_poll_interval_seconds', 0)}s",
    ]
    stores = payload.get("stores")
    if isinstance(stores, Sequence) and not isinstance(stores, (str, bytes)):
        lines.append("stores:")
        for store in stores:
            if not isinstance(store, Mapping):
                continue
            budget = store.get("admission_budget_bytes")
            lines.append(
                f"- {store.get('cache_store', 'unknown')}  "
                f"priority={store.get('priority', 'unknown')}  "
                f"admission={'enabled' if store.get('admission_enabled') else 'disabled'}  "
                f"budget={_bytes(budget) if budget is not None else 'adapter-decided'}  "
                f"reserved={_bytes(store.get('reserved_bytes'))}  "
                f"committed={_bytes(store.get('committed_bytes'))}"
            )
    return "\n".join(lines)


def format_retrieval_cache_objects(payload: Mapping[str, object]) -> str:
    lines = [_page_line(payload, "cache objects")]
    for current in _items(payload, "objects"):
        categories = current.get("lease_categories")
        leases = (
            ",".join(str(value) for value in categories)
            if isinstance(categories, Sequence) and not isinstance(categories, (str, bytes))
            else ""
        )
        lines.append(
            f"- {current.get('collection_id', 'unknown')}::"
            f"{current.get('source_store', 'unknown')}::"
            f"{current.get('object_id', 'unknown')}  "
            f"cache={current.get('cache_store', 'unknown')}  "
            f"state={current.get('state', 'unknown')}  "
            f"stored={_bytes(current.get('stored_bytes'))}  "
            f"cached={current.get('cached_at', 'unknown')}  "
            f"protected-until={current.get('protected_until') or 'unleased'}  "
            f"leases={leases or 'none'}"
        )
    return "\n".join(lines)


def format_retrieval_cache_selectors(payload: Mapping[str, object]) -> str:
    return "\n".join(
        f"{current['collection_id']}::{current['source_store']}::{current['object_id']}"
        for current in _items(payload, "objects")
        if current.get("collection_id") not in {None, ""}
        and current.get("source_store") not in {None, ""}
        and current.get("object_id") not in {None, ""}
    )


def format_retrieval_cache_object(payload: Mapping[str, object]) -> str:
    categories = payload.get("lease_categories")
    leases = (
        ", ".join(str(value) for value in categories)
        if isinstance(categories, Sequence) and not isinstance(categories, (str, bytes))
        else "none"
    )
    return "\n".join(
        [
            f"retrieval cache object {payload.get('collection_id', 'unknown')}::"
            f"{payload.get('source_store', 'unknown')}::"
            f"{payload.get('object_id', 'unknown')}",
            f"stored: {_bytes(payload.get('stored_bytes'))}",
            f"state: {payload.get('state', 'unknown')}",
            f"cache store: {payload.get('cache_store', 'unknown')}",
            f"stored sha256: {payload.get('stored_sha256', 'unknown')}",
            f"cached: {payload.get('cached_at', 'unknown')}",
            f"verified: {payload.get('verified_at', 'unknown')}",
            f"protected until: {payload.get('protected_until') or 'unleased'}",
            f"new archive lease expires: {payload.get('new_archive_expires_at') or 'none'}",
            f"lease categories: {leases}",
            f"retrieval job leases: {payload.get('retrieval_job_leases', 0)}",
        ]
    )


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


def format_archive_copy_selectors(payload: Mapping[str, object]) -> str:
    return "\n".join(
        f"{item['collection_id']}::{item['destination_store']}"
        for item in _items(payload, "copies")
        if item.get("collection_id") not in {None, ""}
        and item.get("destination_store") not in {None, ""}
    )


def format_file_selectors(
    payload: Mapping[str, object],
    key: str = "files",
) -> str:
    return "\n".join(
        f"{item['collection_id']}::{item['path']}"
        for item in _items(payload, key)
        if item.get("collection_id") not in {None, ""} and item.get("path") not in {None, ""}
    )


def format_provenance_files(payload: Mapping[str, object]) -> str:
    lines = [_page_line(payload, "files")]
    for file in _items(payload, "files"):
        provenance = file.get("provenance")
        status = (
            provenance.get("status", "unknown") if isinstance(provenance, Mapping) else "unknown"
        )
        journal = provenance.get("journal_id", "") if isinstance(provenance, Mapping) else ""
        lines.append(
            f"- {file.get('path', 'unknown')}  status={status}  "
            f"bytes={_bytes(file.get('bytes'))}" + (f"  journal={journal}" if journal else "")
        )
    return "\n".join(lines)


def format_file_provenance(payload: Mapping[str, object]) -> str:
    provenance = payload.get("provenance")
    current = provenance if isinstance(provenance, Mapping) else {}
    lines = [
        (
            f"collection file {payload.get('collection_id', 'unknown')}::"
            f"{payload.get('path', 'unknown')}"
        ),
        f"payload: {_bytes(payload.get('bytes'))} sha256={payload.get('sha256', 'unknown')}",
        f"provenance: {current.get('status', 'unknown')}",
    ]
    if current.get("journal_id"):
        lines.append(f"journal: {current['journal_id']}")
        lines.append(f"current state: {current.get('current_state_id', 'unknown')}")
    if current.get("omission_reason"):
        lines.append(f"omission: {current['omission_reason']}")
    return "\n".join(lines)


def format_provenance_trace(payload: Mapping[str, object]) -> str:
    lines = []
    if payload.get("path") is not None:
        lines.extend(format_file_provenance(payload).splitlines())
    lines.append(_page_line(payload, "trace items"))
    for item in _items(payload, "items"):
        kind = item.get("kind")
        if kind == "journal":
            value = item.get("journal")
            journal = value if isinstance(value, Mapping) else {}
            lines.append(
                f"- journal {journal.get('journal_id', 'unknown')}  "
                f"entries={journal.get('entries', 0)}  "
                f"current={journal.get('current_state_id', 'unknown')}"
            )
        elif kind == "external_state_reference":
            value = item.get("reference")
            reference = value if isinstance(value, Mapping) else {}
            lines.append(
                f"- reference {reference.get('from_journal_id', 'unknown')} -> "
                f"{reference.get('to_journal_id', 'unknown')}  "
                f"state={reference.get('state_id', 'unknown')}"
            )
    return "\n".join(lines)


def format_provenance_journal_agents(payload: Mapping[str, object]) -> str:
    lines = [_page_line(payload, "agents")]
    lines.extend(f"- {item.get('agent_id', 'unknown')}" for item in _items(payload, "agents"))
    return "\n".join(lines)


def format_provenance_verification(payload: Mapping[str, object]) -> str:
    return "\n".join(
        [
            f"collection provenance {payload.get('collection_id', 'unknown')}: "
            f"{'valid' if payload.get('valid') else 'invalid'}",
            f"mode: {payload.get('provenance_mode', 'unknown')}",
            f"identity: {payload.get('provenance_identity') or 'omitted'}",
            f"files: {payload.get('files', 0)}",
            f"journals: {payload.get('journals', 0)}",
            f"projected entities: {payload.get('entities', 0)}",
        ]
    )


def format_provenance_verification_job(payload: Mapping[str, object]) -> str:
    result = payload.get("result")
    if payload.get("state") == "succeeded" and isinstance(result, Mapping):
        return format_provenance_verification(result)
    lines = [
        f"collection provenance verification {payload.get('collection_id', 'unknown')}",
        f"state: {payload.get('state', 'unknown')}",
        f"attempts: {payload.get('attempts', 0)}",
    ]
    if payload.get("failure"):
        lines.append(f"failure: {payload['failure']}")
    return "\n".join(lines)
