from __future__ import annotations

import logging
import logging.config
from typing import Any

from time_formats import (
    utc_timestamp_now,
)

import munchy_core.domain.models as domain_models
import munchy_core.persistence.sqlite_state as state_store
import munchy_core.runtime.config as runtime_config
import munchy_core.runtime.execution as execution_runtime
import munchy_core.services.uploads as upload_service

log = logging.getLogger("munchy.server")


def input_upload_states() -> list[dict[str, Any]]:
    return state_store.list_states("input-upload")


def job_states() -> list[dict[str, Any]]:
    return state_store.list_states("job")


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


def referenced_input_upload_ids() -> set[str]:
    upload_ids: set[str] = set()
    for job in job_states():
        try:
            if job.get("state") in domain_models.TERMINAL_JOB_STATES:
                continue
            upload_id = job.get("input_upload_id")
        except Exception:
            log.exception("failed to read job state")
            continue
        if upload_id:
            upload_ids.add(str(upload_id))
    return upload_ids


def jobs_referencing_input_upload(
    upload_id: str,
    *,
    exclude_job_id: str | None = None,
) -> list[str]:
    return [
        str(job["job_id"])
        for job in job_states()
        if str(job.get("input_upload_id") or "") == upload_id
        and str(job.get("job_id") or "") != exclude_job_id
        and job.get("state") not in domain_models.TERMINAL_JOB_STATES
    ]


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


def runnable_jobs_in_order() -> list[dict[str, Any]]:
    jobs = [job for job in job_states() if runnable_job(job)]
    jobs.sort(key=runnable_job_sort_key)
    return jobs


def queue_info_for_job(job_id: str) -> dict[str, Any] | None:
    ordered = [
        job
        for job in runnable_jobs_in_order()
        if str(job.get("job_id") or "") not in execution_runtime.active_jobs
    ]
    for index, job in enumerate(ordered, start=1):
        if str(job.get("job_id") or "") != job_id:
            continue
        return {
            "position": index,
            "running_job_limit": runtime_config.MAX_RUNNING_JOBS,
            "running_jobs": len(execution_runtime.active_jobs),
            "execution_runtime.scheduled_jobs": len(execution_runtime.scheduled_jobs),
        }
    return None
