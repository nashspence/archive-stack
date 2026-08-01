from __future__ import annotations

import json
import logging
import os
import threading
import time
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import httpx
from lifecycle_events import CloudEvent, caused_event, normalize_event_context
from munchy_api_client.routing import (
    RoutingFile,
    routing_file_facts,
    routing_plan,
    sidecar_rules,
)
from pydantic import BaseModel, ConfigDict, field_validator
from riverhog_api_client import Conflict, HashMismatch, NotFound, ServiceUnavailable
from riverhog_api_client.client import ApiClient
from riverhog_api_client.ingress import iter_ingress_upload_parts
from riverhog_protocol.manifest import collection_content_etag
from time_formats import utc_timestamp_now

import munchy_core.domain.errors as domain_errors
import munchy_core.domain.models as domain_models
import munchy_core.persistence.lifecycle_events as lifecycle_store
import munchy_core.persistence.sqlite_state as state_store
import munchy_core.runtime.config as runtime_config
import munchy_core.services.events as event_service
import munchy_core.services.handoffs as handoff_service
import munchy_core.services.processing as processing_service
import munchy_core.services.routing as routing_service
import munchy_core.services.uploads as upload_service

log = logging.getLogger("munchy.server")

RIVERHOG_HANDOFF_ENABLED = os.getenv("MUNCHY_RIVERHOG_HANDOFF_ENABLED", "0").lower() in {
    "1",
    "true",
    "yes",
    "on",
}

RIVERHOG_HANDOFF_CHUNK_BYTES = int(
    os.getenv("MUNCHY_RIVERHOG_HANDOFF_CHUNK_BYTES", str(8 * 1024 * 1024))
)

RIVERHOG_HANDOFF_WORKERS = max(
    1,
    int(os.getenv("MUNCHY_RIVERHOG_HANDOFF_WORKERS", "8")),
)

RIVERHOG_HANDOFF_SINGLE_CHUNK_WORKERS = 2

RIVERHOG_EAGER_HANDOFF_FILES_PER_TICK = max(
    1,
    int(os.getenv("MUNCHY_RIVERHOG_EAGER_HANDOFF_FILES_PER_TICK", "128")),
)

RIVERHOG_EAGER_HANDOFF_BYTES_PER_TICK = max(
    1,
    int(os.getenv("MUNCHY_RIVERHOG_EAGER_HANDOFF_BYTES_PER_TICK", str(512 * 1024 * 1024))),
)

RIVERHOG_EAGER_HANDOFF_SECONDS_PER_TICK = max(
    0.25,
    float(os.getenv("MUNCHY_RIVERHOG_EAGER_HANDOFF_SECONDS_PER_TICK", "8")),
)

RIVERHOG_EAGER_HANDOFF_INTERVAL_SECONDS = max(
    0.25,
    float(os.getenv("MUNCHY_RIVERHOG_EAGER_HANDOFF_INTERVAL_SECONDS", "1")),
)

RIVERHOG_HANDOFF_SAVE_EVERY_FILES = max(
    1,
    int(os.getenv("MUNCHY_RIVERHOG_HANDOFF_SAVE_EVERY_FILES", "32")),
)

RIVERHOG_HANDOFF_SAVE_EVERY_SECONDS = max(
    0.25,
    float(os.getenv("MUNCHY_RIVERHOG_HANDOFF_SAVE_EVERY_SECONDS", "5")),
)

RIVERHOG_FINALIZE_POLL_SECONDS = max(
    1.0,
    float(os.getenv("MUNCHY_RIVERHOG_FINALIZE_POLL_SECONDS", "5")),
)

UPSTREAM_EVENT_POLL_SECONDS = max(
    1.0,
    float(os.getenv("MUNCHY_UPSTREAM_EVENT_POLL_SECONDS", "5")),
)

upstream_event_stop = threading.Event()

upstream_event_thread: threading.Thread | None = None

riverhog_upload_locks: dict[str, threading.RLock] = {}

riverhog_upload_locks_guard = threading.Lock()

riverhog_upload_call_locks: dict[str, threading.Lock] = {}

riverhog_upload_call_locks_guard = threading.Lock()


def riverhog_upload_lock(job_id: str) -> threading.RLock:
    with riverhog_upload_locks_guard:
        lock = riverhog_upload_locks.get(job_id)
        if lock is None:
            lock = threading.RLock()
            riverhog_upload_locks[job_id] = lock
        return lock


def riverhog_upload_call_lock(job_id: str) -> threading.Lock:
    with riverhog_upload_call_locks_guard:
        lock = riverhog_upload_call_locks.get(job_id)
        if lock is None:
            lock = threading.Lock()
            riverhog_upload_call_locks[job_id] = lock
        return lock


class RiverhogHandoffOptions(BaseModel):
    model_config = ConfigDict(extra="forbid")

    archive_store: str | None = None
    tags: list[str] | None = None

    @field_validator("tags")
    @classmethod
    def normalize_tags(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
        tags = list(dict.fromkeys(str(tag).strip() for tag in value))
        if any(not tag for tag in tags):
            raise ValueError("riverhog handoff tags must not contain blanks")
        return tags


RIVERHOG_FILE_STATE_RANK = {
    "": 0,
    "pending": 1,
    "registered": 2,
    "uploading": 3,
    "uploaded": 4,
    "deleted": 5,
}


def merge_riverhog_file_record(
    current_record: dict[str, Any],
    payload_record: dict[str, Any],
) -> dict[str, Any]:
    merged = {**current_record, **payload_record}
    for key in ("bytes", "uploaded_bytes"):
        merged[key] = max(int(current_record.get(key) or 0), int(payload_record.get(key) or 0))
    current_state = str(current_record.get("state") or "")
    payload_state = str(payload_record.get("state") or "")
    if RIVERHOG_FILE_STATE_RANK.get(current_state, 0) > RIVERHOG_FILE_STATE_RANK.get(
        payload_state,
        0,
    ):
        merged["state"] = current_state
    return merged


def merge_riverhog_files(
    current_files: Any,
    payload_files: Any,
) -> dict[str, Any]:
    current = current_files if isinstance(current_files, dict) else {}
    payload = payload_files if isinstance(payload_files, dict) else {}
    merged: dict[str, Any] = {}
    for rel_path in sorted(set(current) | set(payload)):
        current_record = current.get(rel_path)
        payload_record = payload.get(rel_path)
        if isinstance(current_record, dict) and isinstance(payload_record, dict):
            merged[str(rel_path)] = merge_riverhog_file_record(current_record, payload_record)
        elif isinstance(payload_record, dict):
            merged[str(rel_path)] = dict(payload_record)
        elif isinstance(current_record, dict):
            merged[str(rel_path)] = dict(current_record)
    return merged


def merge_last_eager_upload_metrics(
    merged: dict[str, Any],
    current_riverhog: dict[str, Any],
    payload_riverhog: dict[str, Any],
) -> None:
    current_at = state_store.safe_parse_timestamp(current_riverhog.get("last_eager_upload_at"))
    payload_at = state_store.safe_parse_timestamp(payload_riverhog.get("last_eager_upload_at"))
    if current_at is None and payload_at is None:
        return
    source = (
        payload_riverhog
        if current_at is None or (payload_at is not None and payload_at >= current_at)
        else current_riverhog
    )
    for key in (
        "last_eager_upload_at",
        "last_eager_upload_files",
        "last_eager_upload_bytes",
        "last_eager_upload_elapsed_seconds",
    ):
        if key in source:
            merged[key] = source[key]


def merge_riverhog_handoff_state(
    current_riverhog: dict[str, Any],
    payload_riverhog: dict[str, Any],
) -> dict[str, Any]:
    current_updated = state_store.safe_parse_timestamp(current_riverhog.get("updated_at"))
    payload_updated = state_store.safe_parse_timestamp(payload_riverhog.get("updated_at"))
    if current_updated and (payload_updated is None or current_updated > payload_updated):
        merged = {**payload_riverhog, **current_riverhog}
    else:
        merged = {**current_riverhog, **payload_riverhog}
    merge_last_eager_upload_metrics(merged, current_riverhog, payload_riverhog)
    merged["files"] = merge_riverhog_files(
        current_riverhog.get("files"),
        payload_riverhog.get("files"),
    )
    return merged


def expected_riverhog_primary_files_total(
    input_upload: dict[str, Any],
    groups: dict[str, dict[str, Any]],
) -> int:
    return sum(
        len(upload_service.primary_upload_files_for_groups(input_upload, {str(group_name)}))
        for group_name, group_config in groups.items()
        if routing_service.group_produces_primary_archive_output(group_config)
    )


def routing_path_resolvable(routing: Mapping[str, Any]) -> bool:
    for gate in state_store.dict_or_empty(routing.get("gates")).values():
        if isinstance(gate, Mapping) and routing_service.predicate_requires_non_path_facts(gate):
            return False
    pairings = routing.get("pairings")
    if isinstance(pairings, list):
        for pairing in pairings:
            if not isinstance(pairing, Mapping):
                return False
            key = str(pairing.get("key") or "exif.content_identifier")
            if not key.startswith("path."):
                return False
            for predicate_name in ("still", "movie"):
                predicate = pairing.get(predicate_name)
                if not isinstance(predicate, Mapping):
                    return False
                if routing_service.predicate_requires_non_path_facts(predicate):
                    return False
    for rule in sidecar_rules(routing):
        if isinstance(rule.get("facts"), Mapping):
            return False
    if not routing_service.sidecar_rules_are_path_resolvable(routing):
        return False
    for route in routing.get("routes") or []:
        if not isinstance(route, Mapping):
            return False
        when = route.get("when")
        if isinstance(when, Mapping) and routing_service.predicate_requires_non_path_facts(when):
            return False
    return True


def expected_riverhog_primary_files_total_from_path_routing(
    input_upload: dict[str, Any],
    groups: dict[str, dict[str, Any]],
    routing: Mapping[str, Any],
) -> int | None:
    if not routing_path_resolvable(routing):
        return None
    files = [
        RoutingFile(
            path=str(file_state.get("path") or ""),
            bytes=int(file_state.get("bytes") or 0),
            routing_facts=routing_file_facts(str(file_state.get("path") or "")),
        )
        for file_state in input_upload.get("files", [])
        if isinstance(file_state, Mapping) and str(file_state.get("path") or "")
    ]
    if not files:
        return None
    plan = routing_plan(routing, files, group_names=set(groups))
    if not plan.ok:
        return None
    primary_groups = {
        str(group_name)
        for group_name, group_config in groups.items()
        if routing_service.group_produces_primary_archive_output(group_config)
    }
    return sum(
        1
        for match in plan.matches
        if match.get("action") == "upload" and str(match.get("group") or "") in primary_groups
    )


RIVERHOG_CAUSAL_EVENT_TYPES = {
    "io.riverhog.riverhog.collection.upload_staged": "job.archive.upload_staged",
    "io.riverhog.riverhog.collection.archive_retry_scheduled": "job.archive.retry_scheduled",
    "io.riverhog.riverhog.collection.archive_failed": "job.archive.failed",
    "io.riverhog.riverhog.collection.finalized": "job.archive.finalized",
    "io.riverhog.riverhog.collection.deleted": "job.archive.deleted",
}


def job_for_riverhog_collection(
    collection_id: int,
    *,
    event_time: str | None = None,
) -> dict[str, Any] | None:
    with closing(state_store.state_db()) as conn:
        rows = conn.execute(
            """
            SELECT payload, updated_at
            FROM states
            WHERE kind = 'job'
              AND (
                    json_extract(payload, '$.handoff_adapter_state.collection_id') = ?
                 OR json_extract(payload, '$.handoff_receipt.external_id') = ?
                 OR json_extract(payload, '$.handoff_progress.external_id') = ?
              )
            ORDER BY updated_at DESC
            """,
            (collection_id, collection_id, collection_id),
        ).fetchall()
    if not rows:
        return None
    occurred_at = state_store.safe_parse_timestamp(event_time)
    candidates: list[tuple[datetime, dict[str, Any]]] = []
    for row in rows:
        payload = json.loads(str(row["payload"]))
        if not isinstance(payload, dict):
            continue
        created_at = state_store.safe_parse_timestamp(payload.get("created_at"))
        if occurred_at is not None and created_at is not None and created_at > occurred_at:
            continue
        rank = created_at or state_store.safe_parse_timestamp(row["updated_at"])
        if rank is not None:
            candidates.append((rank, payload))
    if not candidates:
        return None
    return max(candidates, key=lambda item: item[0])[1]


def translate_riverhog_event(event: CloudEvent) -> bool:
    mapped_type = RIVERHOG_CAUSAL_EVENT_TYPES.get(event.type)
    if mapped_type is None:
        return False
    raw_collection_id = event.data.get("collection_id") or event.subject
    if raw_collection_id is None:
        raise RuntimeError(f"Riverhog event {event.id} has no collection identity")
    collection_id = int(raw_collection_id)
    job = job_for_riverhog_collection(collection_id, event_time=event.time)
    if job is None:
        log.warning(
            "Riverhog event %s for collection %s has no owning Munchy job",
            event.id,
            collection_id,
        )
        return False
    job_id = str(job.get("job_id") or "")
    owner = str(job.get("initiated_by_app") or "munchy")
    details = {
        key: value
        for key, value in event.data.items()
        if key not in {"actor", "context", "initiator"}
    }
    details.update(
        {
            "actor": {"app": "munchy"},
            "initiator": {
                "app": owner,
                "key_id": job.get("initiated_by_key_id"),
            },
            "collection_id": collection_id,
            "job_id": job_id,
            "state": str(job.get("state") or "unknown"),
            "phase": str(job.get("phase") or "unknown"),
            "workflow_mode": str(job.get("workflow_mode") or ""),
        }
    )
    translated = caused_event(
        cause=event,
        source=runtime_config.EVENT_SOURCE,
        type=f"io.riverhog.munchy.{mapped_type}",
        subject=job_id or None,
        data=details,
    )
    context = normalize_event_context(job.get("event_context"))
    expiry = (
        event_service.event_context_expiry()
        if str(job.get("state") or "") in domain_models.TERMINAL_JOB_STATES
        else None
    )
    lifecycle_store.lifecycle_event_log().append_once(
        translated,
        owner=owner,
        context=context,
        context_expires_at=expiry,
    )
    return True


def consume_riverhog_events_once(api: ApiClient) -> int:
    cursors = lifecycle_store.lifecycle_event_cursors()
    cursor = cursors.cursor("riverhog")
    page = api.list_lifecycle_events(after=cursor, limit=100)
    translated = sum(1 for event in page.events if translate_riverhog_event(event))
    if page.next_cursor != cursor:
        cursors.advance("riverhog", page.next_cursor)
    return translated


def riverhog_event_loop() -> None:
    with ApiClient() as api:
        while not upstream_event_stop.is_set():
            try:
                translated = consume_riverhog_events_once(api)
                if translated:
                    continue
            except Exception:
                log.exception("Riverhog lifecycle event consumption failed")
            upstream_event_stop.wait(UPSTREAM_EVENT_POLL_SECONDS)


def riverhog_config_enabled(job: dict[str, Any]) -> bool:
    return str(state_store.dict_or_empty(job.get("handoff")).get("destination") or "") == "riverhog"


def riverhog_handoff_options(job: Mapping[str, Any]) -> dict[str, Any]:
    handoff = job.get("handoff")
    if not isinstance(handoff, Mapping):
        return {}
    return state_store.dict_or_empty(handoff.get("options"))


def riverhog_collection_id_for_job(job: dict[str, Any]) -> int | None:
    state = job.get("handoff_adapter_state")
    if isinstance(state, dict) and state.get("collection_id"):
        return int(state["collection_id"])
    receipt = job.get("handoff_receipt")
    if isinstance(receipt, dict) and receipt.get("external_id"):
        return int(receipt["external_id"])
    progress = job.get("handoff_progress")
    if isinstance(progress, dict) and progress.get("external_id"):
        return int(progress["external_id"])
    return None


def riverhog_session_state(job: dict[str, Any]) -> dict[str, Any]:
    state = job.setdefault("handoff_adapter_state", {})
    if not isinstance(state, dict):
        state = {}
        job["handoff_adapter_state"] = state
    state.setdefault("state", "not_started")
    state.setdefault("files", {})
    return state


def compact_riverhog_payload(payload: dict[str, Any]) -> dict[str, Any]:
    keep_keys = [
        "collection_id",
        "state",
        "files_total",
        "files_pending",
        "files_partial",
        "files_uploaded",
        "bytes_total",
        "uploaded_bytes",
        "missing_bytes",
        "upload_state_expires_at",
        "latest_failure",
        "archive_phase",
        "archive_phase_updated_at",
        "archive_uploaded_bytes",
        "archive_total_bytes",
        "archive_uploaded_parts",
        "archive_total_parts",
        "archive_store",
    ]
    return {key: payload[key] for key in keep_keys if key in payload}


def update_remote_state_from_payload(
    job: dict[str, Any],
    payload: dict[str, Any],
    *,
    authoritative_files: bool = False,
) -> dict[str, Any]:
    with riverhog_upload_lock(str(job.get("job_id") or "")):
        state = riverhog_session_state(job)
        if payload.get("collection_id"):
            state["collection_id"] = int(payload["collection_id"])
        if payload.get("state"):
            state["remote_state"] = str(payload["state"])
            state["state"] = str(payload["state"])
        state["last_payload"] = compact_riverhog_payload(payload)
        state["updated_at"] = utc_timestamp_now()

        files = state.setdefault("files", {})
        if isinstance(files, dict):
            file_items: list[dict[str, Any]] = []
            single_file = payload.get("file")
            if isinstance(single_file, dict):
                file_items.append(single_file)
            for item in payload.get("files") or []:
                if isinstance(item, dict):
                    file_items.append(item)
            for item in file_items:
                if not item.get("path"):
                    continue
                rel_path = str(item["path"])
                record = files.setdefault(rel_path, {"path": rel_path})
                if not isinstance(record, dict):
                    record = {"path": rel_path}
                    files[rel_path] = record
                record["bytes"] = int(item.get("bytes") or record.get("bytes") or 0)
                if item.get("sha256"):
                    record["sha256"] = str(item["sha256"])
                existing_uploaded = int(record.get("uploaded_bytes") or 0)
                incoming_uploaded = int(item.get("uploaded_bytes") or 0)
                record["uploaded_bytes"] = (
                    incoming_uploaded
                    if authoritative_files
                    else max(existing_uploaded, incoming_uploaded)
                )
                upload_state = str(item.get("upload_state") or "")
                record["upload_state"] = upload_state
                if authoritative_files and upload_state != "uploaded":
                    record["state"] = "missing"
                    record.pop("uploaded_at", None)
                if (
                    int(record.get("bytes") or 0) > 0
                    and int(record.get("uploaded_bytes") or 0) >= int(record.get("bytes") or 0)
                    and record.get("state") not in {"deleted", "uploaded"}
                ):
                    record["state"] = "uploaded"
                    record["uploaded_at"] = record.get("uploaded_at") or utc_timestamp_now()
        if authoritative_files:
            job["_replace_handoff_adapter_state"] = True
        return state


def sync_riverhog_session_from_remote(job: dict[str, Any], api: ApiClient) -> dict[str, Any] | None:
    collection_id = riverhog_collection_id_for_job(job)
    if not collection_id:
        return None
    state = riverhog_session_state(job)
    state["collection_id"] = collection_id
    payload = api.get_collection_upload_session(collection_id)
    update_remote_state_from_payload(job, payload)
    return payload


def refresh_riverhog_session_from_remote(job: dict[str, Any]) -> None:
    if not riverhog_config_enabled(job):
        return
    collection_id = riverhog_collection_id_for_job(job)
    if not collection_id:
        return
    state = riverhog_session_state(job)
    state["collection_id"] = collection_id
    api = ApiClient()
    try:
        sync_riverhog_session_from_remote(job, api)
        state_store.save_job(job)
    except Exception as exc:
        log.debug("riverhog session status refresh failed for %s: %s", job.get("job_id"), exc)
    finally:
        close = getattr(api, "close", None)
        if callable(close):
            close()


def touch_riverhog_session_state(job: dict[str, Any]) -> None:
    with riverhog_upload_lock(str(job.get("job_id") or "")):
        riverhog_session_state(job)["updated_at"] = utc_timestamp_now()


def ensure_riverhog_session(
    job: dict[str, Any],
    api: ApiClient,
    archive_dir: Path,
) -> int:
    if not riverhog_config_enabled(job):
        raise RuntimeError("riverhog upload is not enabled for this job")
    if not RIVERHOG_HANDOFF_ENABLED:
        raise RuntimeError(
            "riverhog upload requested, but Munchy server Riverhog upload is disabled"
        )
    lock = riverhog_upload_lock(str(job.get("job_id") or ""))
    with lock:
        state = riverhog_session_state(job)
        if state.get("collection_id"):
            return int(state["collection_id"])

        payload = api.create_or_resume_collection_upload_session(
            str(job.get("submission_id") or job["job_id"]),
            [str(tag) for tag in riverhog_handoff_options(job).get("tags") or []],
            ingest_source=str(archive_dir),
            archive_store=cast(
                str | None,
                riverhog_handoff_options(job).get("archive_store"),
            ),
            event_context=normalize_event_context(job.get("event_context")),
        )
        update_remote_state_from_payload(job, payload)
        state = riverhog_session_state(job)
        state["opened_at"] = state.get("opened_at") or utc_timestamp_now()
        state_store.save_job(job)
        collection_id = state.get("collection_id")
        if collection_id is None:
            raise RuntimeError("riverhog upload session did not return a collection_id")
        return int(collection_id)


def riverhog_file_record(
    job: dict[str, Any],
    archive_dir: Path,
    source_path: Path,
) -> dict[str, Any]:
    rel_path = source_path.relative_to(archive_dir).as_posix()
    if not source_path.exists():
        raise RuntimeError(f"riverhog upload source file disappeared before upload: {source_path}")
    stat = source_path.stat()
    lock = riverhog_upload_lock(str(job.get("job_id") or ""))
    with lock:
        state = riverhog_session_state(job)
        files = state.setdefault("files", {})
        if not isinstance(files, dict):
            files = {}
            state["files"] = files
        record = files.setdefault(rel_path, {"path": rel_path})
        if not isinstance(record, dict):
            record = {"path": rel_path}
            files[rel_path] = record
        if record.get("state") in {"uploaded", "deleted"}:
            return record
        existing_bytes = int(record.get("bytes") or 0)
        if existing_bytes and existing_bytes != stat.st_size:
            raise RuntimeError(f"riverhog upload file size changed after registration: {rel_path}")
        record["path"] = rel_path
        record["source"] = str(source_path)
        record["bytes"] = stat.st_size
        needs_sha256 = not record.get("sha256")
        record["state"] = record.get("state") or "pending"
        touch_riverhog_session_state(job)
    if needs_sha256:
        digest = upload_service.file_sha256(source_path)
        with lock:
            state = riverhog_session_state(job)
            files = state.setdefault("files", {})
            if not isinstance(files, dict):
                files = {}
                state["files"] = files
            record = files.setdefault(rel_path, {"path": rel_path})
            if isinstance(record, dict) and not record.get("sha256"):
                record["sha256"] = digest
                touch_riverhog_session_state(job)
    with lock:
        files = state_store.dict_or_empty(riverhog_session_state(job).get("files"))
        record = files.get(rel_path)
        if isinstance(record, dict):
            return cast(dict[str, Any], record)
    raise RuntimeError(f"missing Riverhog file record for {rel_path}")


def riverhog_upload_file_complete(record: dict[str, Any]) -> bool:
    bytes_total = int(record.get("bytes") or 0)
    return record.get("state") in {"uploaded", "deleted"} or (
        bytes_total > 0 and int(record.get("uploaded_bytes") or 0) >= bytes_total
    )


def riverhog_payload_confirms_file_uploaded(
    payload: dict[str, Any],
    rel_path: str,
    length: int,
) -> bool:
    items: list[dict[str, Any]] = []
    single_file = payload.get("file")
    if isinstance(single_file, dict):
        items.append(single_file)
    files = payload.get("files")
    if isinstance(files, list):
        items.extend(item for item in files if isinstance(item, dict))
    for item in items:
        if str(item.get("path") or "") != rel_path:
            continue
        uploaded = int(item.get("uploaded_bytes") or 0)
        state = str(item.get("upload_state") or "")
        return uploaded >= length and state == "uploaded"
    return False


def confirm_riverhog_artifact_uploaded(
    job: dict[str, Any],
    api: ApiClient,
    collection_id: int,
    file_payload: dict[str, object],
) -> dict[str, Any]:
    rel_path = str(file_payload["path"])
    length_value = file_payload.get("bytes")
    if not isinstance(length_value, int):
        length_value = int(str(length_value))
    length = length_value
    payload = api.create_or_resume_registered_collection_file_upload(collection_id, file_payload)
    update_remote_state_from_payload(job, payload)
    if not riverhog_payload_confirms_file_uploaded(payload, rel_path, length):
        raise RuntimeError(f"riverhog did not acknowledge completed upload for {rel_path}")
    return payload


def remove_uploaded_riverhog_artifact(
    job: dict[str, Any],
    archive_dir: Path,
    source_path: Path,
    record: dict[str, Any],
    *,
    persist: bool = True,
) -> None:
    if source_path.exists():
        source_path.unlink()
    parent = source_path.parent
    while parent != archive_dir and parent.is_dir():
        try:
            parent.rmdir()
        except OSError:
            break
        parent = parent.parent
    with riverhog_upload_lock(str(job.get("job_id") or "")):
        record["state"] = "deleted"
        record["deleted_at"] = record.get("deleted_at") or utc_timestamp_now()
        touch_riverhog_session_state(job)
    log.debug(
        "riverhog upload accepted; removed local artifact job=%s path=%s bytes=%s",
        job.get("job_id"),
        record.get("path"),
        processing_service.format_log_bytes(record.get("bytes")),
    )
    if persist:
        state_store.save_job(job)


def riverhog_upload_artifact(
    job: dict[str, Any],
    api: ApiClient,
    archive_dir: Path,
    source_path: Path,
    *,
    persist: bool = True,
) -> bool:
    job_id = str(job["job_id"])
    collection_id = ensure_riverhog_session(job, api, archive_dir)
    record = riverhog_file_record(job, archive_dir, source_path)
    rel_path = str(record["path"])
    length = int(record["bytes"])
    file_payload = {
        "path": rel_path,
        "bytes": length,
        "sha256": str(record["sha256"]),
    }
    if riverhog_upload_file_complete(record):
        if source_path.exists() and record.get("state") != "deleted":
            confirm_riverhog_artifact_uploaded(job, api, collection_id, file_payload)
            remove_uploaded_riverhog_artifact(
                job,
                archive_dir,
                source_path,
                record,
                persist=persist,
            )
            return True
        return False

    session = api.create_or_resume_registered_collection_file_upload(collection_id, file_payload)
    update_remote_state_from_payload(job, session)
    with riverhog_upload_lock(job_id):
        record = (
            riverhog_session_state(job)
            .setdefault("files", {})
            .setdefault(
                rel_path,
                {"path": rel_path},
            )
        )
        if not isinstance(record, dict):
            record = {"path": rel_path}
            riverhog_session_state(job).setdefault("files", {})[rel_path] = record
        record["registered_at"] = record.get("registered_at") or utc_timestamp_now()
        record["state"] = "registered"
        touch_riverhog_session_state(job)
    offset = int(session["offset"])
    ingress_length = int(session["length"])
    encryption = session.get("encryption")
    if not isinstance(encryption, Mapping):
        raise RuntimeError(f"riverhog upload encryption is missing for {rel_path}")
    if int(encryption.get("plaintext_bytes", -1)) != length:
        raise RuntimeError(f"riverhog upload plaintext length changed for {rel_path}")
    if int(encryption.get("ciphertext_bytes", -1)) != ingress_length:
        raise RuntimeError(f"riverhog upload ciphertext length changed for {rel_path}")
    if offset > ingress_length:
        raise RuntimeError(f"riverhog upload offset for {rel_path} is past expected length")
    with riverhog_upload_lock(job_id):
        record["state"] = "uploading" if offset < ingress_length else "uploaded"
        touch_riverhog_session_state(job)

    if offset >= ingress_length:
        confirm_riverhog_artifact_uploaded(job, api, collection_id, file_payload)
        with riverhog_upload_lock(job_id):
            record["uploaded_at"] = record.get("uploaded_at") or utc_timestamp_now()
        remove_uploaded_riverhog_artifact(
            job,
            archive_dir,
            source_path,
            record,
            persist=persist,
        )
        return True

    interruption_delay = 1.0
    while offset < ingress_length:
        part = next(
            iter_ingress_upload_parts(
                source_path,
                encryption,
                ciphertext_offset=offset,
                target_part_bytes=RIVERHOG_HANDOFF_CHUNK_BYTES,
            )
        )
        try:
            state_store.raise_if_job_canceled(job_id)
            next_offset = api.tus_client().patch_chunk(
                str(session["upload_url"]),
                offset=offset,
                checksum_algorithm=str(session["checksum_algorithm"]),
                content=part.ciphertext,
            )
        except (httpx.TransportError, Conflict, ServiceUnavailable) as exc:
            log.warning(
                "riverhog upload interrupted job=%s path=%s offset=%s: %s",
                job_id,
                rel_path,
                offset,
                exc,
            )
            session = api.create_or_resume_collection_file_upload(collection_id, rel_path)
            recovered_offset = int(session["offset"])
            if recovered_offset < offset:
                raise RuntimeError(
                    f"riverhog upload offset for {rel_path} moved backward to "
                    f"{recovered_offset}; expected at least {offset}"
                ) from exc
            if recovered_offset > ingress_length:
                raise RuntimeError(
                    f"riverhog upload offset for {rel_path} is past expected length"
                ) from exc
            if recovered_offset == offset:
                handoff_service.retry_sleep(interruption_delay, job_id=job_id)
                interruption_delay = min(30.0, interruption_delay * 2)
            else:
                interruption_delay = 1.0
            offset = recovered_offset
            continue
        if next_offset != offset + len(part.ciphertext):
            raise RuntimeError(f"riverhog upload offset advanced unexpectedly for {rel_path}")
        offset = next_offset
        interruption_delay = 1.0
        with riverhog_upload_lock(job_id):
            record["uploaded_bytes"] = max(
                int(record.get("uploaded_bytes") or 0),
                part.plaintext_start + part.plaintext_bytes,
            )
            record["state"] = "uploading"
            touch_riverhog_session_state(job)

    if offset != ingress_length:
        raise RuntimeError(
            f"riverhog upload for {rel_path} stopped at {offset} of {ingress_length} bytes"
        )
    confirm_riverhog_artifact_uploaded(job, api, collection_id, file_payload)
    with riverhog_upload_lock(job_id):
        record["uploaded_bytes"] = length
        record["state"] = "uploaded"
        record["uploaded_at"] = record.get("uploaded_at") or utc_timestamp_now()
        touch_riverhog_session_state(job)
    remove_uploaded_riverhog_artifact(
        job,
        archive_dir,
        source_path,
        record,
        persist=persist,
    )
    return True


def eager_riverhog_artifact_paths(job: dict[str, Any]) -> list[Path]:
    eager = job.get("eager_archive")
    if not isinstance(eager, dict):
        return []
    files = eager.get("files")
    if not isinstance(files, dict):
        return []
    paths: list[Path] = []
    for item in files.values():
        if not isinstance(item, dict) or item.get("state") != "encoded":
            continue
        output_value = item.get("output")
        if not output_value:
            continue
        output = Path(str(output_value))
        if output.exists():
            paths.append(output)
        sidecar = routing_service.source_artifact_sidecar_for_archive_output(output)
        if sidecar.exists():
            paths.append(sidecar)
    return paths


def uploaded_riverhog_paths(job: dict[str, Any]) -> set[str]:
    state = job.get("handoff_adapter_state")
    if not isinstance(state, dict):
        return set()
    files = state.get("files")
    if not isinstance(files, dict):
        return set()
    uploaded: set[str] = set()
    for rel_path, item in files.items():
        if isinstance(item, dict) and riverhog_upload_file_complete(item):
            uploaded.add(str(rel_path))
    return uploaded


def zero_riverhog_upload_metrics(started: float | None = None) -> dict[str, int | float]:
    elapsed = 0.0 if started is None else round(max(0.0, time.monotonic() - started), 6)
    return {
        "processed_files": 0,
        "uploaded_files": 0,
        "uploaded_bytes": 0,
        "elapsed_seconds": elapsed,
    }


def riverhog_artifact_paths(
    job: dict[str, Any],
    archive_dir: Path,
    *,
    final: bool,
) -> list[Path]:
    paths = (
        routing_service.archive_dir_artifact_paths(archive_dir)
        if final
        else eager_riverhog_artifact_paths(job)
    )
    seen: set[str] = set()
    ordered: list[Path] = []
    uploaded = uploaded_riverhog_paths(job)
    for path in sorted(paths):
        if not path.exists() or not path.is_file():
            continue
        rel_path = path.relative_to(archive_dir).as_posix()
        if rel_path in uploaded or rel_path in seen:
            continue
        seen.add(rel_path)
        ordered.append(path)
    return ordered


def upload_riverhog_artifacts(
    job: dict[str, Any],
    archive_dir: Path,
    *,
    final: bool,
    max_files: int | None = None,
    max_bytes: int | None = None,
    max_seconds: float | None = None,
) -> dict[str, int | float]:
    if not riverhog_config_enabled(job):
        return zero_riverhog_upload_metrics()
    job_id = str(job["job_id"])
    lock = riverhog_upload_call_lock(job_id)
    with lock:
        if not final:
            current = state_store.read_state("job", job_id)
            if isinstance(current, dict):
                if not riverhog_eager_upload_allowed(current):
                    return zero_riverhog_upload_metrics()
                job.clear()
                job.update(current)
        return _upload_riverhog_artifacts_unlocked(
            job,
            archive_dir,
            final=final,
            max_files=max_files,
            max_bytes=max_bytes,
            max_seconds=max_seconds,
        )


def _upload_riverhog_artifacts_unlocked(
    job: dict[str, Any],
    archive_dir: Path,
    *,
    final: bool,
    max_files: int | None = None,
    max_bytes: int | None = None,
    max_seconds: float | None = None,
) -> dict[str, int | float]:
    uploaded = 0
    processed = 0
    uploaded_bytes = 0
    started = time.monotonic()
    selected: list[tuple[Path, int]] = []
    for source_path in riverhog_artifact_paths(job, archive_dir, final=final):
        elapsed = time.monotonic() - started
        if max_files is not None and len(selected) >= max_files:
            break
        if max_seconds is not None and selected and elapsed >= max_seconds:
            break
        source_bytes = source_path.stat().st_size if source_path.exists() else 0
        if (
            max_bytes is not None
            and selected
            and sum(item[1] for item in selected) + source_bytes > max_bytes
        ):
            break
        selected.append((source_path, source_bytes))
    if not selected:
        return {
            "processed_files": 0,
            "uploaded_files": 0,
            "uploaded_bytes": 0,
            "elapsed_seconds": round(max(0.0, time.monotonic() - started), 6),
        }

    worker_count = min(max(1, RIVERHOG_HANDOFF_WORKERS), len(selected))
    single_chunk_slots = threading.BoundedSemaphore(
        min(RIVERHOG_HANDOFF_SINGLE_CHUNK_WORKERS, worker_count)
    )
    last_save_at = started

    def persist_progress_if_due(*, force: bool = False) -> None:
        nonlocal last_save_at
        now = time.monotonic()
        if (
            force
            or processed % RIVERHOG_HANDOFF_SAVE_EVERY_FILES == 0
            or now - last_save_at >= RIVERHOG_HANDOFF_SAVE_EVERY_SECONDS
        ):
            state_store.save_job(job)
            last_save_at = now

    worker_clients: list[ApiClient] = []
    worker_clients_lock = threading.Lock()
    worker_local = threading.local()

    def api_for_worker() -> ApiClient:
        worker_api = getattr(worker_local, "riverhog_api", None)
        if worker_api is None:
            worker_api = ApiClient()
            worker_local.riverhog_api = worker_api
            with worker_clients_lock:
                worker_clients.append(worker_api)
        return worker_api

    def upload_one(item: tuple[Path, int]) -> tuple[int, int, int]:
        source_path, source_bytes = item
        if source_bytes <= RIVERHOG_HANDOFF_CHUNK_BYTES:
            with single_chunk_slots:
                did_upload = riverhog_upload_artifact(
                    job,
                    api_for_worker(),
                    archive_dir,
                    source_path,
                    persist=False,
                )
        else:
            did_upload = riverhog_upload_artifact(
                job,
                api_for_worker(),
                archive_dir,
                source_path,
                persist=False,
            )
        return 1, 1 if did_upload else 0, source_bytes if did_upload else 0

    if worker_count > 1:
        try:
            with ThreadPoolExecutor(max_workers=worker_count) as executor:
                futures = [executor.submit(upload_one, item) for item in selected]
                for future in as_completed(futures):
                    item_processed, item_uploaded, item_bytes = future.result()
                    processed += item_processed
                    uploaded += item_uploaded
                    uploaded_bytes += item_bytes
                    persist_progress_if_due()
        finally:
            with worker_clients_lock:
                clients = list(worker_clients)
                worker_clients.clear()
            for worker_api in clients:
                worker_api.close()
            persist_progress_if_due(force=True)
        return {
            "processed_files": processed,
            "uploaded_files": uploaded,
            "uploaded_bytes": uploaded_bytes,
            "elapsed_seconds": round(max(0.0, time.monotonic() - started), 6),
        }

    api = ApiClient()
    try:
        ensure_riverhog_session(job, api, archive_dir)
        for source_path, source_bytes in selected:
            if riverhog_upload_artifact(
                job,
                api,
                archive_dir,
                source_path,
                persist=False,
            ):
                uploaded += 1
                uploaded_bytes += source_bytes
            processed += 1
            persist_progress_if_due()
        return {
            "processed_files": processed,
            "uploaded_files": uploaded,
            "uploaded_bytes": uploaded_bytes,
            "elapsed_seconds": round(max(0.0, time.monotonic() - started), 6),
        }
    finally:
        persist_progress_if_due(force=True)
        api.close()


def maybe_upload_riverhog_artifacts(job: dict[str, Any], archive_dir: Path) -> None:
    if not riverhog_config_enabled(job) or not RIVERHOG_HANDOFF_ENABLED:
        return
    try:
        result = upload_riverhog_artifacts(
            job,
            archive_dir,
            final=False,
            max_files=RIVERHOG_EAGER_HANDOFF_FILES_PER_TICK,
            max_bytes=RIVERHOG_EAGER_HANDOFF_BYTES_PER_TICK,
            max_seconds=RIVERHOG_EAGER_HANDOFF_SECONDS_PER_TICK,
        )
        if result["processed_files"]:
            state = riverhog_session_state(job)
            state["last_eager_upload_at"] = utc_timestamp_now()
            state["last_eager_upload_files"] = int(result["uploaded_files"])
            state["last_eager_upload_bytes"] = int(result["uploaded_bytes"])
            state["last_eager_upload_elapsed_seconds"] = float(result["elapsed_seconds"])
            touch_riverhog_session_state(job)
            state_store.save_job(job)
    except domain_errors.JobCanceled:
        raise
    except HashMismatch as exc:
        event_service.emit_job_issue(job, component="riverhog_upload", error=exc, severity="error")
        log.error("riverhog eager upload failed integrity check: %s", exc)
    except RuntimeError as exc:
        log.warning("riverhog eager upload failed; will retry later: %s", exc)
    except Exception as exc:
        log.warning("riverhog eager upload issue; will retry later: %s", exc)


RIVERHOG_EAGER_HANDOFF_BLOCKED_PHASES = {"metadata_projection", "handoff"}


def riverhog_eager_upload_allowed(job: dict[str, Any]) -> bool:
    if job.get("state") != "running" or job.get("cancel_requested"):
        return False
    if str(job.get("workflow_mode") or "collection_archive") != "collection_archive":
        return False
    if str(state_store.dict_or_empty(job.get("handoff")).get("destination") or "") != "riverhog":
        return False
    if not riverhog_config_enabled(job):
        return False
    if str(job.get("phase") or "") in RIVERHOG_EAGER_HANDOFF_BLOCKED_PHASES:
        return False
    state = job.get("handoff_adapter_state")
    return not (
        isinstance(state, dict) and state.get("state") in {"canceled", "archiving", "finalized"}
    )


def all_riverhog_session_files_uploaded(job: dict[str, Any]) -> bool:
    state = job.get("handoff_adapter_state")
    if not isinstance(state, dict):
        return False
    files = state.get("files")
    if not isinstance(files, dict) or not files:
        return False
    return all(
        isinstance(item, dict) and riverhog_upload_file_complete(item) for item in files.values()
    )


def can_resume_preserving_riverhog_session(job: dict[str, Any]) -> bool:
    state = job.get("handoff_adapter_state")
    if not riverhog_config_enabled(job) or not isinstance(state, dict):
        return False
    if state.get("canceled_at") or state.get("state") in {"canceled"}:
        return False
    return all_riverhog_session_files_uploaded(job)


def riverhog_session_visible_for_resume(job: dict[str, Any]) -> bool:
    if not RIVERHOG_HANDOFF_ENABLED:
        return True
    api = ApiClient()
    try:
        sync_riverhog_session_from_remote(job, api)
        return True
    except NotFound:
        return False
    except Exception as exc:
        log.warning(
            "could not verify riverhog session before preserving resume for %s: %s",
            job.get("job_id"),
            exc,
        )
        return True
    finally:
        api.close()


def complete_riverhog_session(
    job: dict[str, Any],
    api: ApiClient,
    archive_dir: Path,
) -> dict[str, Any]:
    collection_id = ensure_riverhog_session(job, api, archive_dir)
    try:
        files = state_store.dict_or_empty(riverhog_session_state(job).get("files"))
        manifest = [
            (str(record["path"]), int(record["bytes"]), str(record["sha256"]))
            for record in files.values()
            if isinstance(record, dict)
            and record.get("path")
            and record.get("bytes") is not None
            and record.get("sha256")
        ]
        if not manifest or len(manifest) != len(files):
            raise domain_errors.HandoffFailed("riverhog upload manifest is incomplete")
        payload = api.complete_collection_upload_session(
            collection_id,
            files_total=len(manifest),
            content_etag=collection_content_etag(manifest),
        )
    except Conflict:
        payload = api.get_collection_upload_session(collection_id)
        update_remote_state_from_payload(job, payload)
        page = 1
        while True:
            file_page = api.list_collection_upload_session_files(
                collection_id,
                page=page,
                per_page=100,
            )
            update_remote_state_from_payload(job, file_page, authoritative_files=True)
            pages = max(1, int(file_page.get("pages") or 1))
            if page >= pages:
                break
            page += 1
        files = state_store.dict_or_empty(riverhog_session_state(job).get("files"))
        missing_paths = [
            str(path)
            for path, record in files.items()
            if isinstance(record, dict) and not riverhog_upload_file_complete(record)
        ]
        unavailable = sum(1 for path in missing_paths if not (archive_dir / path).is_file())
        state_store.save_job(job)
        if unavailable:
            raise domain_errors.HandoffFailed(
                f"riverhog reports missing ingress objects ({len(missing_paths)}); "
                f"local handoff artifacts unavailable ({unavailable})"
            ) from None
        raise
    update_remote_state_from_payload(job, payload)
    state = riverhog_session_state(job)
    state["completed_at"] = state.get("completed_at") or utc_timestamp_now()
    state_store.save_job(job)
    return payload


def compact_riverhog_progress_metrics(progress: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(progress, dict):
        return {}
    keys = (
        "primary_files_uploaded",
        "primary_files_total",
        "artifact_files_uploaded",
        "artifact_files_known",
        "artifact_files_registered",
        "artifact_files_deleted",
        "uploaded_bytes",
        "bytes_total",
        "percent_bytes",
        "percent_files",
        "state",
        "archive_phase",
        "archive_uploaded_bytes",
        "archive_total_bytes",
        "archive_uploaded_parts",
        "archive_total_parts",
        "finalized",
        "safe_to_delete",
    )
    return {key: progress[key] for key in keys if key in progress}


def wait_for_riverhog_finalized(
    job: dict[str, Any],
    api: ApiClient,
    collection_id: int,
) -> dict[str, Any]:
    while True:
        state_store.raise_if_job_canceled(str(job["job_id"]))
        payload = api.get_collection_upload_session(collection_id)
        update_remote_state_from_payload(job, payload)
        state_store.save_job(job)
        if str(payload.get("state") or "") == "finalized":
            state = riverhog_session_state(job)
            state["remote_state"] = "finalized"
            state["state"] = "finalized"
            state["finalized_at"] = state.get("finalized_at") or utc_timestamp_now()
            state["last_payload"] = compact_riverhog_payload(payload)
            state_store.save_job(job)
            return payload
        if str(payload.get("state") or "") == "failed":
            raise RuntimeError(
                f"riverhog collection upload failed: {payload.get('latest_failure')}"
            )
        handoff_service.retry_sleep(RIVERHOG_FINALIZE_POLL_SECONDS, job_id=str(job["job_id"]))


def upload_to_riverhog(job: dict[str, Any], archive_dir: Path) -> dict[str, Any] | None:
    if not riverhog_config_enabled(job):
        return None
    event_service.emit_job_event(
        job,
        "archive.handoff",
        "Archive collection is complete; handing off to Riverhog.",
        extra={"archive_dir": str(archive_dir), "method": "session"},
    )

    def operation() -> dict[str, Any]:
        if not RIVERHOG_HANDOFF_ENABLED:
            raise RuntimeError(
                "riverhog upload requested, but Munchy server Riverhog upload is disabled"
            )
        api = ApiClient()
        metrics: dict[str, Any] = {"started_at": utc_timestamp_now()}
        job["handoff_metrics"] = metrics
        state_store.save_job(job)
        try:
            metrics["final_sweep_started_at"] = utc_timestamp_now()
            metrics["final_sweep_before"] = compact_riverhog_progress_metrics(
                riverhog_handoff_progress(job)
            )
            final_sweep = upload_riverhog_artifacts(job, archive_dir, final=True)
            metrics["final_sweep_finished_at"] = utc_timestamp_now()
            metrics["final_sweep_elapsed_seconds"] = final_sweep["elapsed_seconds"]
            metrics["final_sweep_processed_files"] = final_sweep["processed_files"]
            metrics["final_sweep_uploaded_files"] = final_sweep["uploaded_files"]
            metrics["final_sweep_uploaded_bytes"] = final_sweep["uploaded_bytes"]
            sync_riverhog_session_from_remote(job, api)
            metrics["final_sweep_after"] = compact_riverhog_progress_metrics(
                riverhog_handoff_progress(job)
            )
            state_store.save_job(job)
            if not all_riverhog_session_files_uploaded(job):
                raise RuntimeError("riverhog upload did not upload every registered file")
            complete_started = time.monotonic()
            metrics["session_complete_started_at"] = utc_timestamp_now()
            state_store.save_job(job)
            payload = complete_riverhog_session(job, api, archive_dir)
            metrics["session_complete_finished_at"] = utc_timestamp_now()
            metrics["session_complete_elapsed_seconds"] = round(
                max(0.0, time.monotonic() - complete_started),
                6,
            )
            raw_collection_id = payload.get("collection_id")
            collection_id = int(raw_collection_id) if raw_collection_id is not None else None
            if collection_id is not None:
                finalize_started = time.monotonic()
                metrics["wait_finalized_started_at"] = utc_timestamp_now()
                state_store.save_job(job)
                payload = wait_for_riverhog_finalized(job, api, collection_id)
                metrics["wait_finalized_finished_at"] = utc_timestamp_now()
                metrics["wait_finalized_elapsed_seconds"] = round(
                    max(0.0, time.monotonic() - finalize_started),
                    6,
                )
            metrics["finished_at"] = utc_timestamp_now()
            state_store.save_job(job)
            return {
                "destination": "riverhog",
                "external_id": collection_id,
                "metrics": dict(metrics),
                "payload": compact_riverhog_payload(payload),
            }
        except domain_errors.JobCanceled:
            metrics["canceled_at"] = utc_timestamp_now()
            state_store.save_job(job)
            raise
        except Exception as exc:
            metrics["failed_at"] = utc_timestamp_now()
            metrics["error"] = str(exc)
            state_store.save_job(job)
            raise
        finally:
            api.close()

    return handoff_service.retry_handoff_until_success(
        job,
        result_key="handoff_receipt",
        phase="handoff",
        action="Riverhog handoff",
        component="riverhog_handoff",
        operation=operation,
    )


def cancel_riverhog_upload_session(job: dict[str, Any], *, reason: str) -> None:
    if not riverhog_config_enabled(job):
        return
    state = job.get("handoff_adapter_state")
    if not isinstance(state, dict):
        state = riverhog_session_state(job)
    collection_id = riverhog_collection_id_for_job(job)
    if collection_id is None:
        return
    state["collection_id"] = collection_id
    if state.get("state") in {"canceled", "archiving", "finalized"} or state.get("canceled_at"):
        return
    state["cancel_reason"] = reason
    if not RIVERHOG_HANDOFF_ENABLED:
        state["cancel_skipped_at"] = utc_timestamp_now()
        state["cancel_skipped_reason"] = "riverhog upload disabled"
        state_store.save_job(job)
        return
    api = ApiClient()
    try:
        payload = api.cancel_collection_upload_session(collection_id)
        update_remote_state_from_payload(job, payload)
        state = riverhog_session_state(job)
        state["canceled_at"] = utc_timestamp_now()
        state["cancel_reason"] = reason
        log.info(
            "canceled riverhog upload session job=%s collection=%s reason=%s",
            job.get("job_id"),
            collection_id,
            reason,
        )
    except NotFound:
        state["state"] = "canceled"
        state["remote_state"] = "absent"
        state["canceled_at"] = utc_timestamp_now()
        state["cancel_reason"] = reason
        state["cancel_not_found"] = True
        log.info(
            "riverhog upload session already absent job=%s collection=%s reason=%s",
            job.get("job_id"),
            collection_id,
            reason,
        )
    except Exception as exc:
        state["cancel_failed_at"] = utc_timestamp_now()
        state["cancel_error"] = str(exc)
        log.warning(
            "failed to cancel riverhog upload session job=%s collection=%s: %s",
            job.get("job_id"),
            collection_id,
            exc,
        )
    finally:
        api.close()
        state_store.save_job(job)


class RiverhogHandoffAdapter:
    name = "riverhog"

    def __init__(self) -> None:
        self.enabled = RIVERHOG_HANDOFF_ENABLED
        self.supports_eager = self.enabled
        self.eager_interval_seconds = RIVERHOG_EAGER_HANDOFF_INTERVAL_SECONDS
        self.worker_count = RIVERHOG_HANDOFF_WORKERS

    @property
    def background_running(self) -> bool:
        return bool(upstream_event_thread is not None and upstream_event_thread.is_alive())

    def start(self) -> None:
        global upstream_event_thread
        if not self.enabled:
            return
        upstream_event_stop.clear()
        upstream_event_thread = threading.Thread(
            target=riverhog_event_loop,
            name="riverhog-event-loop",
            daemon=True,
        )
        upstream_event_thread.start()

    def stop(self) -> None:
        upstream_event_stop.set()
        if upstream_event_thread is not None:
            upstream_event_thread.join(timeout=5)

    def advance(
        self,
        job: dict[str, Any],
        source_dir: Path,
        *,
        final: bool,
        source_label: str,
        context: Mapping[str, str] | None = None,
    ) -> dict[str, Any] | None:
        del source_label, context
        configured = handoff_service.handoff_config(job)
        configured["state"] = "transferring"
        if not final:
            maybe_upload_riverhog_artifacts(job, source_dir)
            return None
        receipt = upload_to_riverhog(job, source_dir)
        self.refresh(job)
        configured = handoff_service.handoff_config(job)
        configured["state"] = "complete"
        configured["safe_to_delete"] = self.safe_to_delete(job)
        state_store.save_job(job)
        return receipt

    def cancel(self, job: dict[str, Any], *, reason: str) -> None:
        cancel_riverhog_upload_session(job, reason=reason)

    def refresh(self, job: dict[str, Any]) -> None:
        refresh_riverhog_session_from_remote(job)

    def progress(self, job: dict[str, Any]) -> dict[str, Any] | None:
        progress = riverhog_handoff_progress(job)
        if progress is None:
            return None
        stages: list[dict[str, Any]] = [
            {
                "id": "transfer",
                "label": "Riverhog Handoff",
                "state": progress.get("state"),
                "items_done": progress.get("primary_files_uploaded"),
                "items_total": progress.get("primary_files_total"),
                "bytes_done": progress.get("uploaded_bytes"),
                "bytes_total": progress.get("bytes_total"),
                "rate_bytes_per_second": progress.get("rate_bytes_per_second"),
            }
        ]
        if (
            int(progress.get("archive_total_bytes") or 0) > 0
            or progress.get("archive_phase")
            or progress.get("collection_id")
        ):
            stages.append(
                {
                    "id": "archive",
                    "label": "Riverhog Archive",
                    "state": progress.get("archive_phase") or "waiting",
                    "bytes_done": progress.get("archive_uploaded_bytes"),
                    "bytes_total": progress.get("archive_total_bytes"),
                    "items_done": progress.get("archive_uploaded_parts"),
                    "items_total": progress.get("archive_total_parts"),
                    "item_label": "parts",
                }
            )
        return {
            "destination": self.name,
            "external_id": progress.get("collection_id"),
            "state": progress.get("state"),
            "completed": progress.get("completed"),
            "safe_to_delete": progress.get("safe_to_delete"),
            "stages": stages,
        }

    def safe_to_delete(self, job: dict[str, Any]) -> bool:
        progress = riverhog_handoff_progress(job)
        return isinstance(progress, dict) and bool(progress.get("safe_to_delete"))

    def eager_ready(self, job: dict[str, Any]) -> bool:
        return riverhog_eager_upload_allowed(job) and bool(eager_riverhog_artifact_paths(job))

    def wait_until_idle(self, job: dict[str, Any]) -> None:
        job_id = str(job.get("job_id") or "")
        if job_id:
            with riverhog_upload_call_lock(job_id):
                return

    def can_resume(self, job: dict[str, Any]) -> bool:
        return can_resume_preserving_riverhog_session(job) and riverhog_session_visible_for_resume(
            job
        )

    def merge_state(
        self,
        current: dict[str, Any],
        incoming: dict[str, Any],
    ) -> dict[str, Any]:
        return merge_riverhog_handoff_state(current, incoming)

    def expected_primary_files_total(
        self,
        input_upload: dict[str, Any],
        groups: dict[str, dict[str, Any]],
        routing: Mapping[str, Any] | None,
    ) -> int | None:
        if routing is not None:
            return expected_riverhog_primary_files_total_from_path_routing(
                input_upload,
                groups,
                routing,
            )
        return expected_riverhog_primary_files_total(input_upload, groups)

    def handed_off_paths(self, job: dict[str, Any]) -> set[str]:
        return uploaded_riverhog_paths(job)

    def artifact_record(self, job: dict[str, Any], path: str) -> dict[str, Any] | None:
        state = job.get("handoff_adapter_state")
        if not isinstance(state, dict):
            return None
        record = state_store.dict_or_empty(state.get("files")).get(path)
        return record if isinstance(record, dict) else None

    def artifact_complete(self, record: dict[str, Any]) -> bool:
        return riverhog_upload_file_complete(record)


def riverhog_handoff_progress(job: dict[str, Any]) -> dict[str, Any] | None:
    state = job.get("handoff_adapter_state")
    if not isinstance(state, dict):
        return None
    files = state.get("files")
    if not isinstance(files, dict):
        files = {}
    file_items = [item for item in files.values() if isinstance(item, dict)]
    if not file_items and not state.get("collection_id"):
        return None

    registered_files_total = len(file_items)
    local_artifacts_total = 0
    local_artifact_paths: set[str] = set()
    if riverhog_config_enabled(job):
        archive_dir = (
            runtime_config.GPU_RUNTIME_DIR / "jobs" / str(job.get("job_id") or "") / "archive"
        )
        local_artifact_paths = {
            rel_path
            for path in eager_riverhog_artifact_paths(job)
            if (rel_path := routing_service.path_relative_to_archive(path, archive_dir)) is not None
        }
        local_artifacts_total = len(local_artifact_paths)
        if not local_artifacts_total:
            local_artifact_paths = {
                rel_path
                for path in routing_service.archive_dir_artifact_paths(archive_dir)
                if (rel_path := routing_service.path_relative_to_archive(path, archive_dir))
                is not None
            }
            local_artifacts_total = len(local_artifact_paths)
    else:
        archive_dir = (
            runtime_config.GPU_RUNTIME_DIR / "jobs" / str(job.get("job_id") or "") / "archive"
        )
    registered_artifact_paths = {str(path) for path in files if isinstance(path, str)}
    known_artifact_paths = registered_artifact_paths | local_artifact_paths
    expected_primary_files_total = int(job.get("handoff_expected_primary_files_total") or 0)
    encode_progress = processing_service.encode_progress_for_job(job)
    encode_files_total = 0
    encode_files_encoded = 0
    if isinstance(encode_progress, dict):
        encode_files_total = int(encode_progress.get("files_total") or 0)
        encode_files_encoded = int(encode_progress.get("files_encoded") or 0)
    artifact_files_uploaded = 0
    artifact_files_deleted = 0
    bytes_total = 0
    uploaded_bytes = 0
    for item in file_items:
        item_bytes = int(item.get("bytes") or 0)
        item_uploaded = min(int(item.get("uploaded_bytes") or 0), item_bytes)
        bytes_total += item_bytes
        if riverhog_upload_file_complete(item):
            artifact_files_uploaded += 1
            item_uploaded = item_bytes
        if item.get("state") == "deleted":
            artifact_files_deleted += 1
        uploaded_bytes += item_uploaded

    uploaded_paths = uploaded_riverhog_paths(job)
    primary_paths = routing_service.primary_archive_output_paths(job, archive_dir)
    primary_files_total = max(expected_primary_files_total, encode_files_total, len(primary_paths))
    primary_files_encoded = max(encode_files_encoded, len(primary_paths))
    if primary_paths:
        primary_files_uploaded = sum(1 for path in primary_paths if path in uploaded_paths)
    elif primary_files_total:
        primary_files_uploaded = min(artifact_files_uploaded, primary_files_total)
    else:
        primary_files_uploaded = artifact_files_uploaded
        primary_files_total = max(
            registered_files_total, len(known_artifact_paths), primary_files_uploaded
        )
    primary_files_uploaded = min(primary_files_uploaded, primary_files_total)
    primary_files_encoded = min(
        max(primary_files_encoded, primary_files_uploaded), primary_files_total
    )
    artifact_files_known = max(
        len(known_artifact_paths), registered_files_total, local_artifacts_total
    )

    started_at = state_store.safe_parse_timestamp(state.get("opened_at"))
    if started_at is None:
        started_at = state_store.safe_parse_timestamp(state.get("started_at"))
    elapsed_seconds = (
        max(0.001, (datetime.now(UTC) - started_at).total_seconds()) if started_at else 0.0
    )
    average_rate = int(uploaded_bytes / elapsed_seconds) if elapsed_seconds else 0
    recent_rate = 0
    last_eager_upload_at = state_store.safe_parse_timestamp(state.get("last_eager_upload_at"))
    if last_eager_upload_at is not None:
        recent_age = (datetime.now(UTC) - last_eager_upload_at).total_seconds()
        recent_elapsed = float(state.get("last_eager_upload_elapsed_seconds") or 0.0)
        recent_bytes = int(state.get("last_eager_upload_bytes") or 0)
        if recent_age <= 120 and recent_elapsed > 0 and recent_bytes > 0:
            recent_rate = int(recent_bytes / recent_elapsed)
    rate = recent_rate or average_rate
    state_name = str(state.get("remote_state") or state.get("state") or "not_started")
    handoff_completed = state_name in {"archiving", "finalized"} or (
        primary_files_total > 0
        and primary_files_uploaded == primary_files_total
        and bool(state.get("completed_at"))
    )
    last_payload = state.get("last_payload")
    last_payload = last_payload if isinstance(last_payload, dict) else {}
    archive_uploaded_bytes = int(last_payload.get("archive_uploaded_bytes") or 0)
    archive_total_bytes = int(last_payload.get("archive_total_bytes") or 0)
    archive_uploaded_parts = last_payload.get("archive_uploaded_parts")
    archive_total_parts = last_payload.get("archive_total_parts")
    destination_files_total = int(last_payload.get("files_total") or primary_files_total)
    destination_bytes_total = int(last_payload.get("bytes_total") or bytes_total)
    finalized = state_name == "finalized" or str(last_payload.get("state") or "") == "finalized"
    return {
        "collection_id": int(state["collection_id"]),
        "state": state_name,
        "files_total": primary_files_total,
        "registered_files_total": registered_files_total,
        "local_artifacts_total": local_artifacts_total,
        "expected_primary_files_total": expected_primary_files_total,
        "files_uploaded": primary_files_uploaded,
        "files_deleted": artifact_files_deleted,
        "primary_files_total": primary_files_total,
        "primary_files_encoded": primary_files_encoded,
        "primary_files_uploaded": primary_files_uploaded,
        "artifact_files_known": artifact_files_known,
        "artifact_files_registered": registered_files_total,
        "artifact_files_uploaded": artifact_files_uploaded,
        "artifact_files_deleted": artifact_files_deleted,
        "artifact_files_pending_local": local_artifacts_total,
        "bytes_total": bytes_total,
        "uploaded_bytes": uploaded_bytes,
        "percent_bytes": round((uploaded_bytes / bytes_total * 100.0) if bytes_total else 0.0, 2),
        "percent_files": round(
            (primary_files_uploaded / primary_files_total * 100.0) if primary_files_total else 0.0,
            2,
        ),
        "percent_primary_files": round(
            (primary_files_uploaded / primary_files_total * 100.0) if primary_files_total else 0.0,
            2,
        ),
        "percent_artifact_files": round(
            (artifact_files_uploaded / artifact_files_known * 100.0)
            if artifact_files_known
            else 0.0,
            2,
        ),
        "rate_bytes_per_second": rate,
        "average_rate_bytes_per_second": average_rate,
        "recent_rate_bytes_per_second": recent_rate,
        "completed": handoff_completed,
        "handoff_completed": handoff_completed,
        "archive_phase": str(last_payload.get("archive_phase") or ""),
        "archive_uploaded_bytes": archive_uploaded_bytes,
        "archive_total_bytes": archive_total_bytes,
        "archive_uploaded_parts": archive_uploaded_parts,
        "archive_total_parts": archive_total_parts,
        "destination_files_total": destination_files_total,
        "destination_bytes_total": destination_bytes_total,
        "finalized": finalized,
        "safe_to_delete": finalized,
    }
