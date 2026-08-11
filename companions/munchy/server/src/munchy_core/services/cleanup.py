from __future__ import annotations

import logging
import logging.config
import shutil
from datetime import datetime
from typing import Any

from time_formats import (
    utc_timestamp_now,
)

import munchy_core.domain.models as domain_models
import munchy_core.persistence.sqlite_state as state_store
import munchy_core.runtime.execution as execution_runtime
import munchy_core.services.handoffs as handoff_service
import munchy_core.services.processing as processing_service
import munchy_core.services.routing as routing_service
import munchy_core.services.scheduling as scheduling_service
import munchy_core.services.uploads as upload_service

log = logging.getLogger("munchy.server")


def remove_job_local_work(job: dict[str, Any]) -> list[str]:
    removed: list[str] = []
    for root in routing_service.gpu_job_work_roots(job):
        if not root.exists():
            continue
        shutil.rmtree(root, ignore_errors=True)
        if root.exists():
            log.warning("failed to remove local job work: %s", root)
            continue
        removed.append(str(root))
    if removed:
        job["local_work_cleaned_at"] = utc_timestamp_now()
        job["local_work_removed"] = removed
    return removed


def compact_command_result(result: Any) -> Any:
    if not isinstance(result, dict):
        return result
    keep_keys = [
        "artifact_count",
        "attempt",
        "collection_id",
        "destination",
        "duration_s",
        "method",
        "mode",
        "returncode",
        "source_label",
        "succeeded_at",
        "wait",
    ]
    compact = {key: result[key] for key in keep_keys if key in result}
    for key in ("stdout", "stderr"):
        value = str(result.get(key) or "")
        if value:
            compact[f"{key}_tail"] = value[-4000:]
    return compact


def compact_list_field(
    job: dict[str, Any],
    key: str,
    *,
    sample: int = 8,
) -> bool:
    value = job.get(key)
    if not isinstance(value, list):
        return False
    count_key = f"{key}_count"
    sample_key = f"{key}_sample"
    changed = False
    if job.get(count_key) != len(value):
        job[count_key] = len(value)
        changed = True
    if len(value) > sample:
        new_sample = value[:sample]
        if job.get(sample_key) != new_sample:
            job[sample_key] = new_sample
            changed = True
        job.pop(key, None)
        changed = True
    return changed


def append_cleanup_removed(job: dict[str, Any], removed: list[str]) -> None:
    if not removed:
        if not job.get("cleanup_completed_at") and "cleanup_removed_count" not in job:
            job["cleanup_removed"] = []
        return

    current = job.get("cleanup_removed")
    if isinstance(current, list):
        job["cleanup_removed"] = current + removed
        return

    count_key = "cleanup_removed_count"
    sample_key = "cleanup_removed_sample"
    if count_key not in job and sample_key not in job:
        job["cleanup_removed"] = removed
        return

    previous_count = int(job.get(count_key) or 0)
    sample = list(job.get(sample_key) or [])
    if len(sample) < 8:
        sample.extend(removed[: 8 - len(sample)])
    job[count_key] = previous_count + len(removed)
    job[sample_key] = sample


def compact_terminal_job_state(job: dict[str, Any]) -> bool:
    if job.get("state") not in domain_models.TERMINAL_JOB_STATES:
        return False
    changed = snapshot_terminal_progress(job)

    for result_key in ("handoff_receipt",):
        compact = compact_command_result(job.get(result_key))
        if compact != job.get(result_key):
            job[result_key] = compact
            changed = True

    for key in (
        "eager_archive",
        "gpu_payloads",
        "gpu_result",
        "gpu_results",
        "gpu_statuses",
        "group_results",
        "handoff_checkpoint",
        "handoff_adapter_state",
    ):
        if key in job:
            job.pop(key, None)
            changed = True
    if job.get("state") == "canceled" and "cancel_requested" in job:
        job.pop("cancel_requested", None)
        changed = True

    changed = compact_list_field(job, "cleanup_removed") or changed
    changed = compact_list_field(job, "local_work_removed") or changed
    if changed and not job.get("terminal_state_compacted_at"):
        job["terminal_state_compacted_at"] = utc_timestamp_now()
    return changed


RESUMABLE_RUNTIME_JOB_KEYS = (
    *state_store.TERMINAL_CLEANUP_JOB_KEYS,
    "eager_archive",
    "gpu_payloads",
    "gpu_result",
    "gpu_results",
    "gpu_statuses",
    "group_results",
    "upload_progress",
    "routing_result",
    "review_sweep_result",
    "handoff_metrics",
    "handoff_adapter_state",
    "handoff_receipt",
    "terminal_progress",
    "terminal_state_compacted_at",
)


def reset_resumable_job_runtime_state(job: dict[str, Any]) -> None:
    for key in RESUMABLE_RUNTIME_JOB_KEYS:
        job.pop(key, None)


def cleanup_terminal_job(job: dict[str, Any]) -> list[str]:
    snapshot_terminal_progress(job)
    removed = remove_job_local_work(job)
    job_id = str(job.get("job_id") or "")
    upload_id = str(job.get("input_upload_id") or "")
    if upload_id:
        with execution_runtime.input_upload_state_lock(upload_id):
            upload = state_store.read_state("input-upload", upload_id)
            if upload is not None and not scheduling_service.input_upload_has_active_job(
                upload_id,
                exclude_job_id=job_id,
            ):
                upload_service.remove_input_upload_data(upload)
                state_store.delete_state("input-upload", upload_id)
                removed.append(f"input-upload:{upload_id}")
                job["input_upload_deleted_at"] = utc_timestamp_now()
    append_cleanup_removed(job, removed)
    if removed and int(job.get("cleanup_removed_count") or 0) < len(removed):
        job["cleanup_removed"] = removed
        job["cleanup_removed_count"] = len(removed)
        job["cleanup_removed_sample"] = removed[:8]
    job["cleanup_completed_at"] = utc_timestamp_now()
    return removed


def cleanup_canceled_job(job: dict[str, Any]) -> list[str]:
    return cleanup_terminal_job(job)


def snapshot_terminal_progress(job: dict[str, Any]) -> bool:
    changed = False
    if "encode_progress" not in job:
        progress = processing_service.encode_progress_for_job(job)
        if progress is not None:
            job["encode_progress"] = progress
            changed = True
    if "upload_progress" not in job:
        progress = processing_service.upload_progress_for_job(job)
        if progress is not None:
            job["upload_progress"] = progress
            changed = True
    if "handoff_progress" not in job:
        progress = handoff_service.current_handoff_progress(job)
        if progress is not None:
            job["handoff_progress"] = progress
            changed = True
    return changed


def mark_job_canceled(job: dict[str, Any], *, reason: str) -> dict[str, Any]:
    snapshot_terminal_progress(job)
    canceled_at = job.get("canceled_at") or utc_timestamp_now()
    job["state"] = "canceled"
    job["phase"] = "canceled"
    job["canceled_at"] = canceled_at
    job["finished_at"] = job.get("finished_at") or canceled_at
    job["cleanup_requested"] = True
    job["cancel_reason"] = reason
    job.pop("error", None)
    job.pop("cancel_requested", None)
    return state_store.save_job(job)


def finalize_canceled_job(job: dict[str, Any], *, reason: str) -> dict[str, Any]:
    job = mark_job_canceled(job, reason=reason)
    try:
        handoff_service.cancel_handoff(job, reason=reason)
    except Exception:
        job["handoff_cancel_failed_at"] = utc_timestamp_now()
        job["handoff_cancel_error"] = "handoff cancellation failed"
        log.exception("unexpected failure while cancelling handoff for %s", job.get("job_id"))
        state_store.save_job(job)

    try:
        cleanup_canceled_job(job)
        compact_terminal_job_state(job)
    except Exception:
        job["cleanup_failed_at"] = utc_timestamp_now()
        job["cleanup_error"] = "local cleanup failed"
        log.exception("failed to clean canceled job %s", job.get("job_id"))
    return state_store.save_job(job)


def should_cleanup_local_work_on_success(job: dict[str, Any]) -> bool:
    return bool(state_store.dict_or_empty(job.get("handoff")).get("safe_to_delete"))


def should_cleanup_terminal_local_work(job: dict[str, Any], cutoff: datetime) -> bool:
    state = str(job.get("state") or "")
    if state == "succeeded":
        return should_cleanup_local_work_on_success(job)
    if state == "canceled":
        return True
    if state not in {"failed", "canceled"}:
        return False
    finished_at = state_store.safe_parse_timestamp(job.get("finished_at"))
    return finished_at is not None and finished_at <= cutoff
