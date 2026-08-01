from __future__ import annotations

import logging
import logging.config
from typing import Any

from time_formats import (
    utc_timestamp_now,
)

import munchy_core.persistence.sqlite_state as state_store
import munchy_core.runtime.config as runtime_config
import munchy_core.runtime.execution as execution_runtime
import munchy_core.services.uploads as upload_service

log = logging.getLogger("munchy.server")


def input_upload_states() -> list[dict[str, Any]]:
    return state_store.list_states("input-upload")


def unreferenced_input_upload_states(
    *,
    limit: int = runtime_config.RETENTION_BATCH_SIZE,
) -> list[dict[str, Any]]:
    return state_store.unreferenced_input_upload_states(limit=limit)


def job_states() -> list[dict[str, Any]]:
    return state_store.list_states("job")


def active_job_states(
    *,
    cancel_requested: bool | None = None,
    limit: int = runtime_config.RETENTION_BATCH_SIZE,
) -> list[dict[str, Any]]:
    return state_store.load_jobs_by_ids(
        state_store.job_ids_by_summary(
            terminal=False,
            cancel_requested=cancel_requested,
            limit=limit,
        )
    )


def terminal_job_states(
    *,
    limit: int = runtime_config.RETENTION_BATCH_SIZE,
) -> list[dict[str, Any]]:
    return state_store.load_jobs_by_ids(state_store.job_ids_by_summary(terminal=True, limit=limit))


def scheduler_control() -> dict[str, Any]:
    control = state_store.read_state("control", "scheduler")
    if control is None:
        return {"paused": False}
    return control


def scheduling_paused() -> bool:
    return bool(scheduler_control().get("paused"))


def set_scheduling_paused(paused: bool) -> dict[str, Any]:
    payload = scheduler_control()
    payload["paused"] = paused
    payload["changed_at"] = utc_timestamp_now()
    return state_store.write_state("control", "scheduler", payload)


def runnable_job(job: dict[str, Any]) -> bool:
    if job.get("state") not in {"queued", "running"}:
        return False
    if job.get("cancel_requested"):
        return False
    return True


def input_upload_has_active_job(
    upload_id: str,
    *,
    exclude_job_id: str | None = None,
) -> bool:
    return state_store.input_upload_has_active_job(
        upload_id,
        exclude_job_id=exclude_job_id,
    )


def active_input_uploads() -> list[dict[str, Any]]:
    uploads: list[dict[str, Any]] = []
    for upload_state in input_upload_states():
        try:
            upload = upload_service.refresh_input_upload(upload_state)
        except Exception:
            log.exception("failed to read input upload state")
            continue
        if upload.get("state") != "uploaded":
            uploads.append(upload)
    return uploads


def running_job_count() -> int:
    return len(execution_runtime.active_jobs | execution_runtime.scheduled_jobs)


def running_job_slots_available() -> int:
    if runtime_config.MAX_RUNNING_JOBS <= 0:
        return 1_000_000
    return max(0, runtime_config.MAX_RUNNING_JOBS - running_job_count())


def runnable_job_sort_key(job: dict[str, Any]) -> tuple[int, str, str]:
    state = str(job.get("state") or "")
    state_priority = 0 if state == "running" else 1
    queued_at = str(job.get("started_at") or job.get("created_at") or job.get("updated_at") or "")
    return (state_priority, queued_at, str(job.get("job_id") or ""))


def runnable_jobs_in_order(
    *,
    limit: int = runtime_config.RETENTION_BATCH_SIZE,
    exclude_claimed: bool = True,
) -> list[dict[str, Any]]:
    excluded = (
        execution_runtime.active_jobs | execution_runtime.scheduled_jobs
        if exclude_claimed
        else set()
    )
    job_ids = state_store.job_ids_by_summary(
        terminal=False,
        cancel_requested=False,
        states=("queued", "running"),
        exclude_job_ids=excluded,
        limit=limit,
    )
    return state_store.load_jobs_by_ids(job_ids)


def runnable_job_count() -> int:
    return state_store.job_count_by_summary(
        terminal=False,
        cancel_requested=False,
        states=("queued", "running"),
    )


def queue_info_for_job(job_id: str) -> dict[str, Any] | None:
    position = state_store.runnable_job_position(
        job_id,
        exclude_job_ids=execution_runtime.active_jobs,
    )
    if position is None:
        return None
    return {
        "position": position,
        "running_job_limit": runtime_config.MAX_RUNNING_JOBS,
        "running_jobs": len(execution_runtime.active_jobs),
        "execution_runtime.scheduled_jobs": len(execution_runtime.scheduled_jobs),
    }
