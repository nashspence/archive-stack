from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from datetime import datetime
from typing import Any, cast

from time_formats import (
    parse_utc_timestamp,
    utc_timestamp_now,
)

import munchy_core.domain.errors as domain_errors
import munchy_core.domain.models as domain_models
import munchy_core.ports.handoff as handoff_port
import munchy_core.runtime.config as runtime_config
import munchy_core.runtime.execution as execution_runtime
from munchy_core.domain.errors import ServiceError
from munchy_core.persistence.application_keys import (
    SQLiteApplicationKeyStore,
)
from munchy_core.persistence.template_registry import (
    ensure_template_registry_schema,
)


def safe_parse_timestamp(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        return parse_utc_timestamp(str(value))
    except ValueError:
        return None


def state_db() -> sqlite3.Connection:
    runtime_config.STATE_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(runtime_config.STATE_DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def application_keys() -> SQLiteApplicationKeyStore:
    return SQLiteApplicationKeyStore(state_db)


def init_state_store() -> None:
    application_keys().initialize()
    with closing(state_db()) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS states (
                kind TEXT NOT NULL,
                id TEXT NOT NULL,
                payload TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (kind, id)
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS states_kind_updated_at ON states(kind, updated_at)"
        )
        ensure_template_registry_schema(conn)
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS job_summaries (
                job_id TEXT PRIMARY KEY,
                state TEXT NOT NULL,
                phase TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                started_at TEXT NOT NULL,
                finished_at TEXT NOT NULL,
                input_upload_id TEXT NOT NULL,
                template_id TEXT NOT NULL,
                run_id TEXT NOT NULL,
                workflow_mode TEXT NOT NULL,
                handoff_destination TEXT NOT NULL,
                output_mode TEXT NOT NULL,
                profile TEXT NOT NULL,
                terminal INTEGER NOT NULL,
                cancel_requested INTEGER NOT NULL,
                storage_wait INTEGER NOT NULL
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS job_summaries_terminal_updated "
            "ON job_summaries(terminal, updated_at, job_id)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS job_summaries_state_updated "
            "ON job_summaries(state, updated_at, job_id)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS job_summaries_workflow_updated "
            "ON job_summaries(workflow_mode, updated_at, job_id)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS job_summaries_handoff_destination_updated "
            "ON job_summaries(handoff_destination, updated_at, job_id)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS job_summaries_run_updated "
            "ON job_summaries(run_id, updated_at, job_id)"
        )
        conn.execute(
            "CREATE VIRTUAL TABLE IF NOT EXISTS job_summaries_fts "
            "USING fts5(job_id UNINDEXED, search_text)"
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS job_diagnostics (
                job_id TEXT PRIMARY KEY,
                created_at TEXT NOT NULL,
                reason TEXT NOT NULL,
                path TEXT NOT NULL,
                bytes INTEGER NOT NULL CHECK(bytes >= 0),
                sha256 TEXT NOT NULL CHECK(length(sha256) = 64)
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS job_diagnostics_created "
            "ON job_diagnostics(created_at, job_id)"
        )
        conn.commit()


def write_state(kind: str, item_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    payload = dict(payload)
    payload["updated_at"] = utc_timestamp_now()
    encoded = json.dumps(payload, sort_keys=True)
    with closing(state_db()) as conn:
        conn.execute(
            """
            INSERT INTO states(kind, id, payload, updated_at)
            VALUES(?, ?, ?, ?)
            ON CONFLICT(kind, id) DO UPDATE SET
                payload = excluded.payload,
                updated_at = excluded.updated_at
            """,
            (kind, item_id, encoded, payload["updated_at"]),
        )
        if kind == "job":
            upsert_job_summary(conn, payload)
        conn.commit()
    return payload


def read_state(kind: str, item_id: str) -> dict[str, Any] | None:
    with closing(state_db()) as conn:
        row = conn.execute(
            "SELECT payload FROM states WHERE kind = ? AND id = ?",
            (kind, item_id),
        ).fetchone()
    if row is None:
        return None
    payload = json.loads(str(row["payload"]))
    if not isinstance(payload, dict):
        raise RuntimeError(f"{kind} state is not an object: {item_id}")
    return cast(dict[str, Any], payload)


def dict_or_empty(value: Any) -> dict[str, Any]:
    return cast(dict[str, Any], value) if isinstance(value, dict) else {}


def state_exists(kind: str, item_id: str) -> bool:
    with closing(state_db()) as conn:
        row = conn.execute(
            "SELECT 1 FROM states WHERE kind = ? AND id = ?",
            (kind, item_id),
        ).fetchone()
    return row is not None


def list_states(kind: str) -> list[dict[str, Any]]:
    with closing(state_db()) as conn:
        rows = conn.execute(
            "SELECT payload FROM states WHERE kind = ? ORDER BY id",
            (kind,),
        ).fetchall()
    return [json.loads(str(row["payload"])) for row in rows]


def bool_int(value: Any) -> int:
    return 1 if bool(value) else 0


def job_summary_search_text(job: dict[str, Any]) -> str:
    values = [
        job.get("job_id"),
        job.get("template_id"),
        job.get("run_id"),
        job.get("input_upload_id"),
        job.get("state"),
        job.get("phase"),
        job.get("workflow_mode"),
        dict_or_empty(job.get("handoff")).get("destination"),
        job.get("output_mode"),
        job.get("profile"),
    ]
    return " ".join(str(value) for value in values if value)


def upsert_job_summary(conn: sqlite3.Connection, job: dict[str, Any]) -> None:
    job_id = str(job.get("job_id") or "")
    if not job_id:
        return
    handoff_destination = str(dict_or_empty(job.get("handoff")).get("destination") or "")
    summary = {
        "job_id": job_id,
        "state": str(job.get("state") or ""),
        "phase": str(job.get("phase") or ""),
        "created_at": str(job.get("created_at") or ""),
        "updated_at": str(job.get("updated_at") or job.get("created_at") or ""),
        "started_at": str(job.get("started_at") or ""),
        "finished_at": str(job.get("finished_at") or ""),
        "input_upload_id": str(job.get("input_upload_id") or ""),
        "template_id": str(job.get("template_id") or ""),
        "run_id": str(job.get("run_id") or ""),
        "workflow_mode": str(job.get("workflow_mode") or ""),
        "handoff_destination": handoff_destination,
        "output_mode": str(job.get("output_mode") or ""),
        "profile": str(job.get("profile") or ""),
        "terminal": bool_int(job.get("state") in domain_models.TERMINAL_JOB_STATES),
        "cancel_requested": bool_int(job.get("cancel_requested")),
        "storage_wait": bool_int(isinstance(job.get("storage_wait"), dict)),
    }
    conn.execute(
        """
        INSERT INTO job_summaries(
            job_id,
            state,
            phase,
            created_at,
            updated_at,
            started_at,
            finished_at,
            input_upload_id,
            template_id,
            run_id,
            workflow_mode,
            handoff_destination,
            output_mode,
            profile,
            terminal,
            cancel_requested,
            storage_wait
        )
        VALUES(
            :job_id,
            :state,
            :phase,
            :created_at,
            :updated_at,
            :started_at,
            :finished_at,
            :input_upload_id,
            :template_id,
            :run_id,
            :workflow_mode,
            :handoff_destination,
            :output_mode,
            :profile,
            :terminal,
            :cancel_requested,
            :storage_wait
        )
        ON CONFLICT(job_id) DO UPDATE SET
            state = excluded.state,
            phase = excluded.phase,
            created_at = excluded.created_at,
            updated_at = excluded.updated_at,
            started_at = excluded.started_at,
            finished_at = excluded.finished_at,
            input_upload_id = excluded.input_upload_id,
            template_id = excluded.template_id,
            run_id = excluded.run_id,
            workflow_mode = excluded.workflow_mode,
            handoff_destination = excluded.handoff_destination,
            output_mode = excluded.output_mode,
            profile = excluded.profile,
            terminal = excluded.terminal,
            cancel_requested = excluded.cancel_requested,
            storage_wait = excluded.storage_wait
        """,
        summary,
    )
    conn.execute("DELETE FROM job_summaries_fts WHERE job_id = ?", (job_id,))
    conn.execute(
        "INSERT INTO job_summaries_fts(job_id, search_text) VALUES(?, ?)",
        (job_id, job_summary_search_text(job)),
    )


def delete_job_summary(conn: sqlite3.Connection, job_id: str) -> None:
    conn.execute("DELETE FROM job_diagnostics WHERE job_id = ?", (job_id,))
    conn.execute("DELETE FROM job_summaries WHERE job_id = ?", (job_id,))
    conn.execute("DELETE FROM job_summaries_fts WHERE job_id = ?", (job_id,))


def save_job_diagnostic(payload: dict[str, Any]) -> dict[str, Any]:
    diagnostic = {
        "job_id": str(payload["job_id"]),
        "created_at": str(payload["created_at"]),
        "reason": str(payload["reason"]),
        "path": str(payload["path"]),
        "bytes": int(payload["bytes"]),
        "sha256": str(payload["sha256"]),
    }
    with closing(state_db()) as conn:
        conn.execute(
            """
            INSERT INTO job_diagnostics(job_id, created_at, reason, path, bytes, sha256)
            VALUES(:job_id, :created_at, :reason, :path, :bytes, :sha256)
            ON CONFLICT(job_id) DO UPDATE SET
                created_at = excluded.created_at,
                reason = excluded.reason,
                path = excluded.path,
                bytes = excluded.bytes,
                sha256 = excluded.sha256
            """,
            diagnostic,
        )
        conn.commit()
    return diagnostic


def read_job_diagnostic(job_id: str) -> dict[str, Any] | None:
    with closing(state_db()) as conn:
        row = conn.execute(
            "SELECT job_id, created_at, reason, path, bytes, sha256 "
            "FROM job_diagnostics WHERE job_id = ?",
            (job_id,),
        ).fetchone()
    return dict(row) if row is not None else None


def delete_job_diagnostic(job_id: str) -> None:
    with closing(state_db()) as conn:
        conn.execute("DELETE FROM job_diagnostics WHERE job_id = ?", (job_id,))
        conn.commit()


def delete_jobs(job_ids: list[str]) -> None:
    if not job_ids:
        return
    if len(job_ids) > runtime_config.RETENTION_BATCH_SIZE:
        raise ValueError("job deletion batch exceeds the retention batch size")
    placeholders = ", ".join("?" for _ in job_ids)
    with closing(state_db()) as conn:
        conn.execute(
            f"DELETE FROM job_diagnostics WHERE job_id IN ({placeholders})",
            job_ids,
        )
        conn.execute(
            f"DELETE FROM states WHERE kind = 'job' AND id IN ({placeholders})",
            job_ids,
        )
        conn.execute(
            f"DELETE FROM job_summaries WHERE job_id IN ({placeholders})",
            job_ids,
        )
        conn.execute(
            f"DELETE FROM job_summaries_fts WHERE job_id IN ({placeholders})",
            job_ids,
        )
        conn.commit()


def job_search_match_query(value: str | None) -> str | None:
    if not value:
        return None
    tokens = domain_models.JOB_SEARCH_TOKEN_RE.findall(value.casefold())
    if not tokens:
        return None
    return " AND ".join(f"{token}*" for token in tokens[:8])


def load_jobs_by_ids(job_ids: list[str]) -> list[dict[str, Any]]:
    if not job_ids:
        return []
    placeholders = ", ".join("?" for _ in job_ids)
    with closing(state_db()) as conn:
        rows = conn.execute(
            f"SELECT id, payload FROM states WHERE kind = 'job' AND id IN ({placeholders})",
            job_ids,
        ).fetchall()
    by_id = {str(row["id"]): json.loads(str(row["payload"])) for row in rows}
    return [by_id[job_id] for job_id in job_ids if isinstance(by_id.get(job_id), dict)]


def delete_state(kind: str, item_id: str) -> None:
    with closing(state_db()) as conn:
        conn.execute("DELETE FROM states WHERE kind = ? AND id = ?", (kind, item_id))
        if kind == "job":
            delete_job_summary(conn, item_id)
        conn.commit()


def vacuum_state_store() -> None:
    runtime_config.STATE_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(runtime_config.STATE_DB_PATH, timeout=30)) as conn:
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute("VACUUM")
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")


def item_lifecycle_time(item: dict[str, Any]) -> datetime | None:
    values: list[datetime] = []
    for key in (
        "updated_at",
        "finished_at",
        "encoded_at",
        "failed_at",
        "last_polled_at",
        "last_submitted_at",
        "started_at",
    ):
        value = safe_parse_timestamp(item.get(key))
        if value is not None:
            values.append(value)
    return max(values) if values else None


def newer_lifecycle_item(
    current_item: dict[str, Any],
    payload_item: dict[str, Any],
    *,
    state_rank: dict[str, int],
) -> dict[str, Any]:
    current_rank = state_rank.get(str(current_item.get("state") or ""), 0)
    payload_rank = state_rank.get(str(payload_item.get("state") or ""), 0)
    if current_rank > payload_rank:
        return current_item
    if payload_rank > current_rank:
        return payload_item
    current_time = item_lifecycle_time(current_item)
    payload_time = item_lifecycle_time(payload_item)
    if current_time is not None and (payload_time is None or current_time > payload_time):
        return current_item
    return payload_item


def merge_eager_archive_state(
    current_eager: dict[str, Any],
    payload_eager: dict[str, Any],
) -> dict[str, Any]:
    merged = dict(payload_eager)

    current_files = dict_or_empty(current_eager.get("files"))
    payload_files = dict_or_empty(payload_eager.get("files"))
    file_state_rank = {"encoding": 1, "encoded": 2, "failed": 2}
    merged_files: dict[str, Any] = dict(payload_files)
    for rel_path, current_item in current_files.items():
        if not isinstance(current_item, dict):
            continue
        payload_item = merged_files.get(rel_path)
        if isinstance(payload_item, dict):
            merged_files[rel_path] = newer_lifecycle_item(
                current_item,
                payload_item,
                state_rank=file_state_rank,
            )
        else:
            merged_files[rel_path] = current_item
    merged["files"] = merged_files

    current_batches = dict_or_empty(current_eager.get("batches"))
    payload_batches = dict_or_empty(payload_eager.get("batches"))
    batch_state_rank = {"running": 1, "succeeded": 2, "failed": 2}
    merged_batches: dict[str, Any] = dict(payload_batches)
    for batch_id, current_item in current_batches.items():
        if not isinstance(current_item, dict):
            continue
        payload_item = merged_batches.get(batch_id)
        if isinstance(payload_item, dict):
            merged_batches[batch_id] = newer_lifecycle_item(
                current_item,
                payload_item,
                state_rank=batch_state_rank,
            )
        else:
            merged_batches[batch_id] = current_item
    merged["batches"] = merged_batches

    current_results = dict_or_empty(current_eager.get("gpu_results"))
    payload_results = dict_or_empty(payload_eager.get("gpu_results"))
    if current_results or payload_results:
        merged["gpu_results"] = {**current_results, **payload_results}

    merged["next_batch_number"] = max(
        int(current_eager.get("next_batch_number") or 1),
        int(payload_eager.get("next_batch_number") or 1),
    )
    return merged


def merge_handoff_adapter_state(
    job: dict[str, Any],
    current: dict[str, Any],
    incoming: dict[str, Any],
) -> dict[str, Any]:
    destination = str(dict_or_empty(job.get("handoff")).get("destination") or "")
    adapter = handoff_port.HANDOFF_ADAPTERS.get(destination)
    if adapter is None:
        return {**current, **incoming}
    return adapter.merge_state(current, incoming)


GPU_RESULT_STORAGE_KEYS = {
    "job_id",
    "state",
    "profile",
    "tasks",
    "archive_dir",
    "review_dir",
    "started_at",
    "finished_at",
    "updated_at",
    "error",
    "error_code",
}


def compact_gpu_result_for_storage(value: Any) -> Any:
    if not isinstance(value, dict):
        return value
    compact = {key: value[key] for key in GPU_RESULT_STORAGE_KEYS if key in value}
    items = value.get("items")
    if isinstance(items, dict):
        item_counts: dict[str, int] = {}
        for task_name, task_items in items.items():
            if isinstance(task_items, list):
                item_counts[str(task_name)] = len(task_items)
            elif task_items is not None:
                item_counts[str(task_name)] = 1
        if item_counts:
            compact["item_counts"] = item_counts
    return compact


def compact_eager_archive_for_storage(eager: Any) -> None:
    if not isinstance(eager, dict):
        return
    batches = eager.get("batches")
    if isinstance(batches, dict):
        for batch in batches.values():
            if isinstance(batch, dict) and isinstance(batch.get("gpu_result"), dict):
                batch["gpu_result"] = compact_gpu_result_for_storage(batch["gpu_result"])
    gpu_results = eager.get("gpu_results")
    if isinstance(gpu_results, dict):
        for batch_id, result in list(gpu_results.items()):
            gpu_results[batch_id] = compact_gpu_result_for_storage(result)


def compact_job_for_storage(payload: dict[str, Any]) -> dict[str, Any]:
    compact_eager_archive_for_storage(payload.get("eager_archive"))
    return payload


TERMINAL_CLEANUP_JOB_KEYS = (
    "cleanup_completed_at",
    "cleanup_error",
    "cleanup_failed_at",
    "cleanup_removed",
    "cleanup_removed_count",
    "cleanup_removed_sample",
    "input_upload_deleted_at",
    "local_work_cleaned_at",
    "local_work_removed",
    "local_work_removed_count",
    "local_work_removed_sample",
    "handoff_cancel_error",
    "handoff_cancel_failed_at",
)


def save_job(job: dict[str, Any]) -> dict[str, Any]:
    payload = dict(job)
    allow_clear_cancel = bool(payload.pop("_allow_clear_cancel", False))
    reset_runtime_state = bool(payload.pop("_reset_runtime_state", False))
    replace_handoff_adapter_state = bool(payload.pop("_replace_handoff_adapter_state", False))
    job_id = str(payload["job_id"])
    with execution_runtime.job_state_lock:
        current = read_state("job", job_id)
        if (
            not allow_clear_cancel
            and isinstance(current, dict)
            and current.get("state") in domain_models.TERMINAL_JOB_STATES
            and payload.get("state") not in domain_models.TERMINAL_JOB_STATES
        ):
            return current
        if (
            isinstance(current, dict)
            and payload.get("state") not in domain_models.TERMINAL_JOB_STATES
            and not reset_runtime_state
        ):
            current_adapter_state = current.get("handoff_adapter_state")
            incoming_adapter_state = payload.get("handoff_adapter_state")
            if (
                isinstance(current_adapter_state, dict)
                and isinstance(incoming_adapter_state, dict)
                and not replace_handoff_adapter_state
            ):
                payload["handoff_adapter_state"] = merge_handoff_adapter_state(
                    payload,
                    current_adapter_state,
                    incoming_adapter_state,
                )
            elif isinstance(current_adapter_state, dict) and "handoff_adapter_state" not in payload:
                payload["handoff_adapter_state"] = current_adapter_state
            current_eager = current.get("eager_archive")
            payload_eager = payload.get("eager_archive")
            if isinstance(current_eager, dict) and isinstance(payload_eager, dict):
                payload["eager_archive"] = merge_eager_archive_state(current_eager, payload_eager)
            elif isinstance(current_eager, dict) and "eager_archive" not in payload:
                payload["eager_archive"] = current_eager
        if (
            not allow_clear_cancel
            and isinstance(current, dict)
            and current.get("cancel_requested")
            and not payload.get("cancel_requested")
            and payload.get("state") not in domain_models.TERMINAL_JOB_STATES
        ):
            payload["cancel_requested"] = True
            payload["cancel_requested_at"] = (
                current.get("cancel_requested_at") or utc_timestamp_now()
            )
            if current.get("phase") == "cancel_requested":
                payload["phase"] = "cancel_requested"
        if (
            isinstance(current, dict)
            and current.get("state") in domain_models.TERMINAL_JOB_STATES
            and payload.get("state") in domain_models.TERMINAL_JOB_STATES
        ):
            for key in (
                "cleanup_completed_at",
                "cleanup_removed",
                "cleanup_removed_count",
                "cleanup_removed_sample",
                "cleanup_failed_at",
                "cleanup_error",
                "input_upload_deleted_at",
                "local_work_cleaned_at",
                "local_work_removed",
                "local_work_removed_count",
                "local_work_removed_sample",
                "handoff_cancel_failed_at",
                "handoff_cancel_error",
                "handoff_metrics",
                "terminal_state_compacted_at",
            ):
                if key in current and key not in payload:
                    payload[key] = current[key]
        if payload.get("state") not in domain_models.TERMINAL_JOB_STATES:
            for key in TERMINAL_CLEANUP_JOB_KEYS:
                payload.pop(key, None)
        payload = compact_job_for_storage(payload)
        return write_state("job", job_id, payload)


def load_job(job_id: str) -> dict[str, Any]:
    job = read_state("job", job_id)
    if job is None:
        raise ServiceError(status_code=404, detail=f"unknown job: {job_id}")
    return job


def raise_if_job_canceled(job_id: str) -> None:
    job = read_state("job", job_id)
    if job is None:
        raise RuntimeError(f"unknown job: {job_id}")
    if job.get("cancel_requested") or job.get("state") == "canceled":
        raise domain_errors.JobCanceled(f"job canceled: {job_id}")
