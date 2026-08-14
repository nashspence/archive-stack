from __future__ import annotations

import hashlib
import importlib.metadata
import json
import logging
import os
import threading
import time
from collections.abc import Iterator, Mapping
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from lifecycle_events import CloudEvent, caused_event, normalize_event_context
from munchy_api_client.routing import (
    RoutingFile,
    routing_file_facts,
    routing_plan,
    sidecar_rules,
)
from pydantic import BaseModel, ConfigDict, field_validator
from riverhog_api_client import Conflict, NotFound
from riverhog_api_client.client import ApiClient
from riverhog_api_client.uploads import (
    configured_upload_concurrency,
    configured_upload_window,
    upload_collection_units,
)
from riverhog_protocol.manifest import collection_content_etag
from riverhog_protocol.raw_ingress import hash_raw_source
from riverhog_provenance import (
    FileProvenanceBinding,
    ProvenanceArchive,
    append_observation,
    append_replacement_transformation,
    build_provenance_archive,
    create_derivative_journal,
    load_or_create_installation_id,
    provenance_journal_filename,
    validate_journal,
)
from time_formats import format_utc_timestamp, utc_timestamp_now

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

RIVERHOG_FINALIZE_POLL_SECONDS = max(
    1.0,
    float(os.getenv("MUNCHY_RIVERHOG_FINALIZE_POLL_SECONDS", "5")),
)

MUNCHY_PROVENANCE_AGENT_NAME = "munchy-server"


def _provenance_host_id() -> str:
    return load_or_create_installation_id(runtime_config.STATE_DIR / "provenance-installation-id")


UPSTREAM_EVENT_POLL_SECONDS = max(
    1.0,
    float(os.getenv("MUNCHY_UPSTREAM_EVENT_POLL_SECONDS", "5")),
)

upstream_event_stop = threading.Event()

upstream_event_thread: threading.Thread | None = None

riverhog_upload_locks: dict[str, threading.RLock] = {}

riverhog_upload_locks_guard = threading.Lock()


def riverhog_upload_lock(job_id: str) -> threading.RLock:
    with riverhog_upload_locks_guard:
        lock = riverhog_upload_locks.get(job_id)
        if lock is None:
            lock = threading.RLock()
            riverhog_upload_locks[job_id] = lock
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
    occurred_at = state_store.safe_parse_timestamp(event_time)
    cutoff = format_utc_timestamp(occurred_at) if occurred_at is not None else None
    with closing(state_store.state_db()) as conn:
        row = conn.execute(
            """
            SELECT payload
            FROM states
            WHERE kind = 'job'
              AND (
                    json_extract(payload, '$.handoff_adapter_state.collection_id') = ?
                 OR json_extract(payload, '$.handoff_receipt.external_id') = ?
                 OR json_extract(payload, '$.handoff_progress.external_id') = ?
              )
              AND (
                    ? IS NULL
                 OR COALESCE(json_extract(payload, '$.created_at'), '') = ''
                 OR json_extract(payload, '$.created_at') <= ?
              )
            ORDER BY COALESCE(
                         NULLIF(json_extract(payload, '$.created_at'), ''),
                         updated_at
                     ) DESC,
                     id DESC
            LIMIT 1
            """,
            (collection_id, collection_id, collection_id, cutoff, cutoff),
        ).fetchone()
    if row is None:
        return None
    payload = json.loads(str(row["payload"]))
    return payload if isinstance(payload, dict) else None


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
    page.require_progress_after(cursor)
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
        "layout",
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
        provenance, _bindings, _journals = munchy_output_provenance(job, archive_dir)
        if state.get("collection_id"):
            return int(state["collection_id"])

        provenance_mode = "captured" if provenance is not None else "omitted"
        payload = api.create_or_resume_collection_upload_session(
            str(job.get("submission_id") or job["job_id"]),
            [str(tag) for tag in riverhog_handoff_options(job).get("tags") or []],
            ingest_source=str(archive_dir),
            archive_store=cast(
                str | None,
                riverhog_handoff_options(job).get("archive_store"),
            ),
            event_context=normalize_event_context(job.get("event_context")),
            provenance_mode=provenance_mode,
            provenance_omission_reason=(
                None
                if provenance is not None
                else "Provenance was explicitly omitted for every Munchy input."
            ),
        )
        update_remote_state_from_payload(job, payload)
        state = riverhog_session_state(job)
        state["opened_at"] = state.get("opened_at") or utc_timestamp_now()
        state_store.save_job(job)
        collection_id = state.get("collection_id")
        if collection_id is None:
            raise RuntimeError("riverhog upload session did not return a collection_id")
        return int(collection_id)


def _provenance_binding_payload(binding: FileProvenanceBinding) -> dict[str, object]:
    payload: dict[str, object] = {
        "path": binding.path,
        "bytes": binding.bytes,
        "sha256": binding.sha256,
        "status": binding.status,
    }
    if binding.status == "captured":
        payload.update(
            {
                "journal_id": str(binding.journal_id),
                "current_state_id": str(binding.current_state_id),
            }
        )
    else:
        payload["omission_reason"] = str(binding.omission_reason)
    return payload


def _provenance_binding_from_payload(value: Mapping[str, object]) -> FileProvenanceBinding:
    status = str(value.get("status") or "")
    if status == "captured":
        return FileProvenanceBinding(
            path=str(value["path"]),
            bytes=int(str(value["bytes"])),
            sha256=str(value["sha256"]),
            status="captured",
            journal_id=str(value["journal_id"]),
            current_state_id=str(value["current_state_id"]),
        )
    return FileProvenanceBinding(
        path=str(value["path"]),
        bytes=int(str(value["bytes"])),
        sha256=str(value["sha256"]),
        status="omitted",
        omission_reason=str(value["omission_reason"]),
    )


def _munchy_output_provenance_root(job: Mapping[str, Any]) -> Path:
    return (
        upload_service.shared_input_upload_root(str(job["input_upload_id"]))
        / ".riverhog"
        / "riverhog-handoff-provenance"
    )


def _archive_binding(path: Path, archive_dir: Path, **identity: object) -> FileProvenanceBinding:
    rel_path = path.relative_to(archive_dir).as_posix()
    return FileProvenanceBinding(
        path=rel_path,
        bytes=path.stat().st_size,
        sha256=upload_service.file_sha256(path),
        **identity,  # type: ignore[arg-type]
    )


def _input_provenance_sources(
    upload: Mapping[str, Any],
    primary: Mapping[str, Any],
) -> list[Mapping[str, Any]]:
    primary_path = str(primary.get("path") or "")
    sources = [primary]
    sources.extend(
        item
        for item in upload.get("files", [])
        if isinstance(item, Mapping) and str(item.get("sidecar_for") or "") == primary_path
    )
    return sources


def _source_omission_reason(sources: list[Mapping[str, Any]]) -> str | None:
    reasons = sorted(
        {
            str(provenance.get("omission_reason") or "")
            for source in sources
            if isinstance((provenance := source.get("provenance")), Mapping)
            and provenance.get("status") == "omitted"
        }
    )
    if not reasons:
        return None
    return "Munchy output provenance omitted because source provenance was omitted: " + "; ".join(
        reasons
    )


def _source_journal_bytes(
    sources: list[Mapping[str, Any]], journals: Mapping[str, bytes]
) -> list[bytes]:
    result: list[bytes] = []
    seen: set[str] = set()
    for source in sources:
        provenance = source.get("provenance")
        if not isinstance(provenance, Mapping) or provenance.get("status") != "captured":
            continue
        journal_id = str(provenance.get("journal_id") or "")
        if journal_id not in seen:
            result.append(journals[journal_id])
            seen.add(journal_id)
    return result


def _reachable_output_journals(
    directly_bound: set[str], candidates: Mapping[str, bytes]
) -> dict[str, bytes]:
    reachable = set(directly_bound)
    pending = list(directly_bound)
    while pending:
        journal_id = pending.pop()
        content = candidates.get(journal_id)
        if content is None:
            raise RuntimeError(f"Munchy output provenance is missing journal {journal_id}")
        for reference in validate_journal(content).external_states:
            if reference.journal_id not in reachable:
                reachable.add(reference.journal_id)
                pending.append(reference.journal_id)
    return {journal_id: candidates[journal_id] for journal_id in sorted(reachable)}


def _load_munchy_output_provenance(
    job: dict[str, Any], archive_dir: Path, state: Mapping[str, Any]
) -> tuple[ProvenanceArchive | None, tuple[FileProvenanceBinding, ...], dict[str, bytes]]:
    bindings = tuple(
        _provenance_binding_from_payload(item)
        for item in state.get("files", [])
        if isinstance(item, Mapping)
    )
    for binding in bindings:
        path = archive_dir / binding.path
        if (
            not path.is_file()
            or path.stat().st_size != binding.bytes
            or upload_service.file_sha256(path) != binding.sha256
        ):
            raise RuntimeError(
                f"Munchy output changed after provenance preparation: {binding.path}"
            )
    if state.get("mode") == "omitted":
        if any(binding.status != "omitted" for binding in bindings):
            raise RuntimeError("Munchy omitted provenance state has captured file bindings")
        return None, bindings, {}
    root = _munchy_output_provenance_root(job)
    journals = {
        str(journal_id): (root / provenance_journal_filename(str(journal_id))).read_bytes()
        for journal_id in state.get("journals", {})
    }
    archive = build_provenance_archive(bindings=bindings, journals=journals)
    if archive.identity != state.get("etag"):
        raise RuntimeError("Munchy output provenance identity changed after preparation")
    return archive, bindings, journals


def munchy_output_provenance(
    job: dict[str, Any], archive_dir: Path
) -> tuple[ProvenanceArchive | None, tuple[FileProvenanceBinding, ...], dict[str, bytes]]:
    state = riverhog_session_state(job)
    existing = state.get("provenance")
    if isinstance(existing, Mapping):
        return _load_munchy_output_provenance(job, archive_dir, existing)

    upload = upload_service.load_input_upload_raw(str(job["input_upload_id"]))
    input_journals = upload_service.input_provenance_journals(upload)
    candidates = dict(input_journals)
    groups = job.get("groups")
    if not isinstance(groups, Mapping):
        raise RuntimeError("Munchy job has no resolved groups for provenance preparation")
    primary_sources: dict[str, Mapping[str, Any]] = {}
    for group_name, raw_group in groups.items():
        if not isinstance(
            raw_group, dict
        ) or not routing_service.group_produces_primary_archive_output(raw_group):
            continue
        for source in upload_service.primary_upload_files_for_groups(upload, {str(group_name)}):
            output = routing_service.archive_output_path_for_routed_file(
                source,
                group_name=str(group_name),
                group_config=raw_group,
                archive_dir=archive_dir,
            )
            if output.is_file():
                primary_sources[output.relative_to(archive_dir).as_posix()] = source

    version = importlib.metadata.version("munchy-server")
    started_at = str(job.get("started_at") or utc_timestamp_now())
    ended_at = utc_timestamp_now()
    bindings: list[FileProvenanceBinding] = []
    directly_bound: set[str] = set()
    artifacts = routing_service.archive_dir_artifact_paths(archive_dir)
    for artifact in artifacts:
        rel_path = artifact.relative_to(archive_dir).as_posix()
        primary = primary_sources.get(rel_path)
        if primary is not None:
            sources = [primary]
            omission_reason = _source_omission_reason(sources)
            if omission_reason is not None:
                bindings.append(
                    _archive_binding(
                        artifact,
                        archive_dir,
                        status="omitted",
                        omission_reason=omission_reason,
                    )
                )
                continue
            source_journal = _source_journal_bytes(sources, input_journals)[0]
            group_name = upload_service.upload_file_resolved_group(dict(primary))
            group_config = groups.get(str(group_name))
            if not isinstance(group_config, dict):
                raise RuntimeError(f"Munchy output has no group contract: {rel_path}")
            if (
                domain_models.normalize_output_mode(str(group_config.get("output_mode") or "video"))
                == "preserve"
            ):
                result = append_observation(
                    source_journal,
                    artifact,
                    relative_path=rel_path,
                    host_id=_provenance_host_id(),
                    agent_name=MUNCHY_PROVENANCE_AGENT_NAME,
                    agent_version=version,
                )
            else:
                result = append_replacement_transformation(
                    source_journal,
                    artifact,
                    relative_path=rel_path,
                    host_id=_provenance_host_id(),
                    agent_name=MUNCHY_PROVENANCE_AGENT_NAME,
                    agent_version=version,
                    event_label="Munchy canonical archive transformation",
                    started_at=started_at,
                    ended_at=ended_at,
                )
            summary = validate_journal(result)
            candidates[summary.journal_id] = result
            directly_bound.add(summary.journal_id)
            bindings.append(
                _archive_binding(
                    artifact,
                    archive_dir,
                    status="captured",
                    journal_id=summary.journal_id,
                    current_state_id=summary.current_state_id,
                )
            )
            continue

        matching_primary = next(
            (
                (path, source)
                for path, source in primary_sources.items()
                if rel_path.startswith(path + ".")
            ),
            None,
        )
        if matching_primary is None:
            sources = list(primary_sources.values())
        else:
            sources = _input_provenance_sources(upload, matching_primary[1])
        omission_reason = _source_omission_reason(sources)
        source_journals = _source_journal_bytes(sources, input_journals)
        if omission_reason is not None or not source_journals:
            bindings.append(
                _archive_binding(
                    artifact,
                    archive_dir,
                    status="omitted",
                    omission_reason=omission_reason
                    or (
                        "Munchy could not associate this generated artifact with captured "
                        "source provenance."
                    ),
                )
            )
            continue
        result = create_derivative_journal(
            artifact,
            relative_path=rel_path,
            source_journals=source_journals,
            host_id=_provenance_host_id(),
            agent_name=MUNCHY_PROVENANCE_AGENT_NAME,
            agent_version=version,
            event_label="Munchy generated archive artifact",
            started_at=started_at,
            ended_at=ended_at,
            derivation_kind="aggregation" if len(source_journals) > 1 else "extraction",
        )
        summary = validate_journal(result)
        candidates[summary.journal_id] = result
        directly_bound.add(summary.journal_id)
        bindings.append(
            _archive_binding(
                artifact,
                archive_dir,
                status="captured",
                journal_id=summary.journal_id,
                current_state_id=summary.current_state_id,
            )
        )

    if not bindings:
        raise RuntimeError("Munchy produced no Riverhog archive artifacts")
    if not directly_bound:
        provenance: ProvenanceArchive | None = None
        journals: dict[str, bytes] = {}
        mode = "omitted"
    else:
        journals = _reachable_output_journals(directly_bound, candidates)
        provenance = build_provenance_archive(bindings=bindings, journals=journals)
        mode = "captured"
    root = _munchy_output_provenance_root(job)
    root.mkdir(parents=True, exist_ok=True)
    journal_descriptors: dict[str, dict[str, object]] = {}
    for journal_id, content in journals.items():
        (root / provenance_journal_filename(journal_id)).write_bytes(content)
        journal_descriptors[journal_id] = {
            "bytes": len(content),
            "sha256": hashlib.sha256(content).hexdigest(),
        }
    state["provenance"] = {
        "mode": mode,
        "etag": provenance.identity if provenance is not None else None,
        "files": [_provenance_binding_payload(item) for item in bindings],
        "journals": journal_descriptors,
        "prepared_at": ended_at,
    }
    touch_riverhog_session_state(job)
    state_store.save_job(job)
    return provenance, tuple(bindings), journals


def _iter_source_chunks(
    path: Path,
    *,
    chunk_bytes: int = 8 * 1024 * 1024,
) -> Iterator[bytes]:
    with path.open("rb") as stream:
        while chunk := stream.read(chunk_bytes):
            yield chunk


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
        last_payload = state_store.dict_or_empty(riverhog_session_state(job).get("last_payload"))
        layout = state_store.dict_or_empty(last_payload.get("layout"))
        pack_member_bytes = int(layout.get("pack_member_bytes") or 8 * 1024 * 1024)
        raw_part_plaintext_bytes = int(layout.get("raw_part_plaintext_bytes") or 64 * 1024 * 1024)
        raw_parts: dict[str, object] | None = None
        if stat.st_size > pack_member_bytes:
            hashed = hash_raw_source(
                path=rel_path,
                chunks=_iter_source_chunks(source_path),
                expected_bytes=stat.st_size,
                part_plaintext_bytes=raw_part_plaintext_bytes,
            )
            digest = hashed.sha256
            raw_parts = {
                "part_plaintext_bytes": hashed.part_plaintext_bytes,
                "sha256s": list(hashed.part_sha256s),
            }
        else:
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
                if raw_parts is not None:
                    record["raw_parts"] = raw_parts
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


def completed_riverhog_paths(job: dict[str, Any]) -> set[str]:
    state = job.get("handoff_adapter_state")
    if not isinstance(state, dict):
        return set()
    files = state.get("files")
    if not isinstance(files, dict):
        return set()
    completed: set[str] = set()
    for rel_path, item in files.items():
        if isinstance(item, dict) and riverhog_upload_file_complete(item):
            completed.add(str(rel_path))
    return completed


def _riverhog_registration_payload(record: Mapping[str, object]) -> dict[str, object]:
    payload: dict[str, object] = {
        "path": str(record["path"]),
        "bytes": int(str(record["bytes"])),
        "sha256": str(record["sha256"]),
    }
    raw_parts = record.get("raw_parts")
    if isinstance(raw_parts, dict):
        payload["raw_parts"] = dict(raw_parts)
    provenance = record.get("provenance")
    if not isinstance(provenance, Mapping):
        raise RuntimeError(f"Riverhog file has no provenance accounting: {record['path']}")
    status = str(provenance.get("status") or "")
    if status == "captured":
        payload["provenance"] = {
            "status": "captured",
            "journal_id": str(provenance.get("journal_id") or ""),
            "current_state_id": str(provenance.get("current_state_id") or ""),
        }
    elif status == "omitted":
        payload["provenance"] = {
            "status": "omitted",
            "omission_reason": str(provenance.get("omission_reason") or ""),
        }
    else:
        raise RuntimeError(f"Riverhog file has invalid provenance accounting: {record['path']}")
    return payload


def register_riverhog_artifacts(
    job: dict[str, Any],
    api: ApiClient,
    archive_dir: Path,
) -> dict[str, int | float]:
    started = time.monotonic()
    collection_id = ensure_riverhog_session(job, api, archive_dir)
    _provenance, bindings, journals = munchy_output_provenance(job, archive_dir)
    bindings_by_path = {item.path: item for item in bindings}
    for journal_id, content in sorted(journals.items()):
        api.put_collection_upload_session_provenance_journal(
            collection_id,
            journal_id,
            content=content,
            sha256=hashlib.sha256(content).hexdigest(),
        )
    records = [
        riverhog_file_record(job, archive_dir, path)
        for path in sorted(routing_service.archive_dir_artifact_paths(archive_dir))
        if path.is_file()
    ]
    for record in records:
        binding = bindings_by_path.get(str(record["path"]))
        if binding is None:
            raise RuntimeError(f"Munchy provenance did not account for {record['path']}")
        if int(record["bytes"]) != binding.bytes or str(record["sha256"]) != binding.sha256:
            raise RuntimeError(f"Munchy payload and provenance identity disagree: {record['path']}")
        record["provenance"] = _provenance_binding_payload(binding)
    pending = [
        record
        for record in records
        if record.get("state") not in {"registered", "uploaded", "deleted"}
    ]
    registered_files = 0
    registered_bytes = 0
    for offset in range(0, len(pending), 100):
        batch = pending[offset : offset + 100]
        payload = api.register_collection_upload_session_files(
            collection_id,
            tuple(_riverhog_registration_payload(record) for record in batch),
        )
        update_remote_state_from_payload(job, payload)
        with riverhog_upload_lock(str(job["job_id"])):
            files = state_store.dict_or_empty(riverhog_session_state(job).get("files"))
            for current in batch:
                current_record = files.get(str(current["path"]))
                if not isinstance(current_record, dict):
                    raise RuntimeError("Riverhog registration state lost a source file")
                current_record["registered_at"] = (
                    current_record.get("registered_at") or utc_timestamp_now()
                )
                current_record["state"] = "registered"
                registered_files += 1
                registered_bytes += int(current_record["bytes"])
            touch_riverhog_session_state(job)
        state_store.save_job(job)
    return {
        "processed_files": len(records),
        "registered_files": registered_files,
        "registered_bytes": registered_bytes,
        "elapsed_seconds": round(max(0.0, time.monotonic() - started), 6),
    }


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


def all_riverhog_session_files_registered(job: dict[str, Any]) -> bool:
    state = job.get("handoff_adapter_state")
    if not isinstance(state, dict):
        return False
    files = state.get("files")
    if not isinstance(files, dict) or not files:
        return False
    return all(
        isinstance(item, dict) and item.get("state") in {"registered", "uploaded", "deleted"}
        for item in files.values()
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
    provenance, _bindings, _journals = munchy_output_provenance(job, archive_dir)
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
            provenance_etag=provenance.identity if provenance is not None else None,
        )
    except Conflict:
        payload = api.get_collection_upload_session(collection_id)
        update_remote_state_from_payload(job, payload)
        if str(payload.get("state") or "") in {
            "uploading",
            "finalizing",
            "finalized",
        }:
            return _upload_riverhog_units(job, api, archive_dir, payload)
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
            if isinstance(record, dict)
            and record.get("state") not in {"registered", "uploaded", "deleted"}
        ]
        unavailable = sum(1 for path in missing_paths if not (archive_dir / path).is_file())
        state_store.save_job(job)
        if unavailable:
            raise domain_errors.HandoffFailed(
                f"riverhog reports missing registered files ({len(missing_paths)}); "
                f"local handoff artifacts unavailable ({unavailable})"
            ) from None
        raise
    update_remote_state_from_payload(job, payload)
    state = riverhog_session_state(job)
    state["completed_at"] = state.get("completed_at") or utc_timestamp_now()
    state_store.save_job(job)
    return _upload_riverhog_units(job, api, archive_dir, payload)


def validate_riverhog_resume_sources(
    job: dict[str, Any],
    api: ApiClient,
    archive_dir: Path,
    session_payload: Mapping[str, Any] | None = None,
) -> dict[str, int]:
    try:
        provenance, bindings, _journals = munchy_output_provenance(job, archive_dir)
    except Exception as exc:
        raise domain_errors.HandoffFailed(
            "riverhog handoff sources changed after provenance preparation"
        ) from exc
    if session_payload is not None:
        expected_provenance_etag = provenance.identity if provenance is not None else None
        if session_payload.get("provenance_etag") != expected_provenance_etag:
            raise domain_errors.HandoffFailed(
                "riverhog handoff provenance differs from the remote archive plan"
            )
    records = state_store.dict_or_empty(riverhog_session_state(job).get("files"))
    bindings_by_path = {binding.path: binding for binding in bindings}
    expected_paths = set(bindings_by_path)
    if expected_paths != set(records):
        raise domain_errors.HandoffFailed(
            "riverhog handoff registration differs from prepared provenance"
        )
    local_paths = {
        path.relative_to(archive_dir).as_posix()
        for path in routing_service.archive_dir_artifact_paths(archive_dir)
    }
    if local_paths != expected_paths:
        raise domain_errors.HandoffFailed(
            "riverhog handoff sources differ from prepared provenance"
        )
    for path, binding in bindings_by_path.items():
        record = records.get(path)
        if (
            not isinstance(record, Mapping)
            or record.get("bytes") != binding.bytes
            or record.get("sha256") != binding.sha256
        ):
            raise domain_errors.HandoffFailed(
                "riverhog handoff registration differs from prepared provenance"
            )
    collection_id = riverhog_collection_id_for_job(job)
    if collection_id is None:
        raise domain_errors.HandoffFailed("riverhog handoff has no remote session")
    payload = api.list_collection_upload_session_volumes(collection_id)
    volumes = payload.get("volumes")
    if not isinstance(volumes, list):
        raise domain_errors.HandoffFailed("riverhog handoff returned an invalid volume plan")
    coverage: dict[str, list[tuple[int, int]]] = {path: [] for path in expected_paths}
    units_total = 0
    for volume in volumes:
        if not isinstance(volume, Mapping) or not isinstance(volume.get("units"), list):
            raise domain_errors.HandoffFailed("riverhog handoff returned an invalid volume plan")
        for unit in volume["units"]:
            if not isinstance(unit, Mapping) or not isinstance(unit.get("sources"), list):
                raise domain_errors.HandoffFailed(
                    "riverhog handoff returned an invalid upload unit"
                )
            units_total += 1
            for source in unit["sources"]:
                if not isinstance(source, Mapping):
                    raise domain_errors.HandoffFailed(
                        "riverhog handoff returned an invalid upload unit source"
                    )
                source_path = source.get("path")
                offset = source.get("offset")
                byte_count = source.get("bytes")
                sha256 = source.get("sha256")
                record = records.get(source_path) if isinstance(source_path, str) else None
                record_bytes = record.get("bytes") if isinstance(record, Mapping) else None
                if (
                    not isinstance(record, Mapping)
                    or not isinstance(offset, int)
                    or offset < 0
                    or not isinstance(byte_count, int)
                    or byte_count < 0
                    or isinstance(record_bytes, bool)
                    or not isinstance(record_bytes, int)
                    or offset + byte_count > record_bytes
                    or not isinstance(sha256, str)
                    or sha256 != str(record.get("sha256") or "")
                ):
                    raise domain_errors.HandoffFailed(
                        "riverhog archive plan differs from registered source identities"
                    )
                assert isinstance(source_path, str)
                coverage[source_path].append((offset, offset + byte_count))
    for path, ranges in coverage.items():
        cursor = 0
        for start, end in sorted(ranges):
            if start != cursor:
                raise domain_errors.HandoffFailed(
                    "riverhog archive plan does not cover every registered source byte"
                )
            cursor = end
        if cursor != bindings_by_path[path].bytes:
            raise domain_errors.HandoffFailed(
                "riverhog archive plan does not cover every registered source byte"
            )
    return {
        "validated_files": len(coverage),
        "validated_units": units_total,
        "validated_volumes": len(volumes),
    }


def _upload_riverhog_units(
    job: dict[str, Any],
    api: ApiClient,
    archive_dir: Path,
    payload: dict[str, Any],
) -> dict[str, Any]:
    collection_id = int(payload["collection_id"])
    if str(payload.get("state") or "") not in {"finalizing", "finalized"}:
        concurrency = configured_upload_concurrency()
        upload_collection_units(
            api,
            collection_id,
            content_for_unit=lambda unit: _riverhog_unit_content(archive_dir, unit),
            concurrency=concurrency,
            window=configured_upload_window(concurrency=concurrency),
            cancel_check=lambda: state_store.raise_if_job_canceled(str(job["job_id"])),
            retry_notice=log.warning,
        )
        payload = api.get_collection_upload_session(collection_id)
        update_remote_state_from_payload(job, payload)
    with riverhog_upload_lock(str(job.get("job_id") or "")):
        for record in state_store.dict_or_empty(riverhog_session_state(job).get("files")).values():
            if not isinstance(record, dict):
                continue
            record["uploaded_bytes"] = int(record.get("bytes") or 0)
            record["state"] = "uploaded"
            record["uploaded_at"] = record.get("uploaded_at") or utc_timestamp_now()
        touch_riverhog_session_state(job)
    state_store.save_job(job)
    return payload


def _riverhog_unit_content(archive_dir: Path, unit: Mapping[str, object]) -> bytes:
    expected = unit.get("payload_bytes")
    sources = unit.get("sources")
    if not isinstance(expected, int) or expected < 0 or not isinstance(sources, list):
        raise RuntimeError("riverhog returned an invalid upload unit")
    content = bytearray()
    for source in sources:
        if not isinstance(source, Mapping):
            raise RuntimeError("riverhog returned an invalid upload unit source")
        path = source.get("path")
        offset = source.get("offset")
        byte_count = source.get("bytes")
        if (
            not isinstance(path, str)
            or not isinstance(offset, int)
            or offset < 0
            or not isinstance(byte_count, int)
            or byte_count < 0
        ):
            raise RuntimeError("riverhog returned an invalid upload unit source")
        with (archive_dir / path).open("rb") as stream:
            stream.seek(offset)
            current = stream.read(byte_count)
        if len(current) != byte_count:
            raise RuntimeError(f"riverhog upload source changed before handoff: {path}")
        content.extend(current)
    if len(content) != expected:
        raise RuntimeError("local sources did not match the Riverhog upload unit")
    return bytes(content)


def _remove_finalized_riverhog_artifacts(job: dict[str, Any], archive_dir: Path) -> None:
    files = state_store.dict_or_empty(riverhog_session_state(job).get("files"))
    for record in files.values():
        if not isinstance(record, dict) or not record.get("path"):
            continue
        source_path = archive_dir / str(record["path"])
        remove_uploaded_riverhog_artifact(
            job,
            archive_dir,
            source_path,
            record,
            persist=False,
        )
    state_store.save_job(job)


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


def resume_riverhog_post_registration(
    job: dict[str, Any],
    api: ApiClient,
    archive_dir: Path,
    payload: dict[str, Any],
    metrics: dict[str, Any],
) -> dict[str, Any]:
    remote_state = str(payload.get("state") or "")
    if remote_state not in {"uploading", "finalizing", "finalized"}:
        raise domain_errors.HandoffFailed(
            f"riverhog upload session cannot resume archive units from state {remote_state}"
        )
    metrics["post_registration_resume_started_at"] = utc_timestamp_now()
    metrics["post_registration_resume_state"] = remote_state
    if remote_state == "uploading":
        validation_started = time.monotonic()
        metrics["source_validation_started_at"] = utc_timestamp_now()
        validation = validate_riverhog_resume_sources(job, api, archive_dir, payload)
        metrics.update(validation)
        metrics["source_validation_finished_at"] = utc_timestamp_now()
        metrics["source_validation_elapsed_seconds"] = round(
            max(0.0, time.monotonic() - validation_started),
            6,
        )
    return _upload_riverhog_units(job, api, archive_dir, payload)


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
            payload = sync_riverhog_session_from_remote(job, api)
            remote_state = str(payload.get("state") or "") if payload is not None else ""
            metrics["remote_state_before"] = remote_state or "not_started"
            if remote_state in {"uploading", "finalizing", "finalized"}:
                assert payload is not None
                payload = resume_riverhog_post_registration(
                    job,
                    api,
                    archive_dir,
                    payload,
                    metrics,
                )
            elif remote_state == "failed":
                metrics["post_registration_resume_started_at"] = utc_timestamp_now()
                metrics["post_registration_resume_state"] = remote_state
                validation_started = time.monotonic()
                metrics["source_validation_started_at"] = utc_timestamp_now()
                validation = validate_riverhog_resume_sources(job, api, archive_dir)
                metrics.update(validation)
                metrics["source_validation_finished_at"] = utc_timestamp_now()
                metrics["source_validation_elapsed_seconds"] = round(
                    max(0.0, time.monotonic() - validation_started),
                    6,
                )
                payload = complete_riverhog_session(job, api, archive_dir)
            elif remote_state not in {"", "open"}:
                raise domain_errors.HandoffFailed(
                    f"riverhog upload session cannot resume from state {remote_state}"
                )
            else:
                metrics["registration_started_at"] = utc_timestamp_now()
                metrics["registration_before"] = compact_riverhog_progress_metrics(
                    riverhog_handoff_progress(job)
                )
                try:
                    registration = register_riverhog_artifacts(job, api, archive_dir)
                except Conflict:
                    payload = sync_riverhog_session_from_remote(job, api)
                    raced_state = str(payload.get("state") or "") if payload is not None else ""
                    if payload is None or raced_state not in {
                        "uploading",
                        "finalizing",
                        "finalized",
                    }:
                        raise
                    metrics["registration_phase_race_state"] = raced_state
                    payload = resume_riverhog_post_registration(
                        job,
                        api,
                        archive_dir,
                        payload,
                        metrics,
                    )
                else:
                    metrics["registration_finished_at"] = utc_timestamp_now()
                    metrics["registration_elapsed_seconds"] = registration["elapsed_seconds"]
                    metrics["registration_processed_files"] = registration["processed_files"]
                    metrics["registered_files"] = registration["registered_files"]
                    metrics["registered_bytes"] = registration["registered_bytes"]
                    sync_riverhog_session_from_remote(job, api)
                    metrics["registration_after"] = compact_riverhog_progress_metrics(
                        riverhog_handoff_progress(job)
                    )
                    state_store.save_job(job)
                    if not all_riverhog_session_files_registered(job):
                        raise RuntimeError("riverhog handoff did not register every source file")
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
                _remove_finalized_riverhog_artifacts(job, archive_dir)
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
        self.supports_eager = False
        self.eager_interval_seconds = 1.0

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
        del job
        return False

    def wait_until_idle(self, job: dict[str, Any]) -> None:
        del job

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
        return completed_riverhog_paths(job)

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

    registered_files_total = sum(
        1 for item in file_items if item.get("state") in {"registered", "uploaded", "deleted"}
    )
    local_artifacts_total = 0
    local_artifact_paths: set[str] = set()
    if riverhog_config_enabled(job):
        archive_dir = (
            runtime_config.GPU_RUNTIME_DIR / "jobs" / str(job.get("job_id") or "") / "archive"
        )
        local_artifact_paths = {
            rel_path
            for path in routing_service.archive_dir_artifact_paths(archive_dir)
            if (rel_path := routing_service.path_relative_to_archive(path, archive_dir)) is not None
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

    uploaded_paths = completed_riverhog_paths(job)
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
    rate = average_rate
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
        "recent_rate_bytes_per_second": 0,
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
