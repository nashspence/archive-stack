from __future__ import annotations

import hashlib
import logging
import logging.config
from pathlib import Path
from typing import Any

from pydantic import ValidationError
from time_formats import (
    utc_timestamp_now,
)

import munchy_core.domain.errors as domain_errors
import munchy_core.domain.models as domain_models
import munchy_core.persistence.sqlite_state as state_store
import munchy_core.runtime.config as runtime_config
import munchy_core.services.events as event_service
import munchy_core.services.handoffs as handoff_service
import munchy_core.services.scheduling as scheduling_service
import munchy_core.services.uploads as upload_service
from munchy_core.domain.errors import ServiceError

log = logging.getLogger("munchy.server")


def insufficient_storage(
    *,
    label: str,
    required_bytes: int,
    free: int,
    reserved_bytes: int = 0,
) -> domain_errors.InsufficientStorage:
    return domain_errors.InsufficientStorage(
        f"insufficient disk space for {label}: need {required_bytes} free bytes, have {free}",
        label=label,
        required_bytes=required_bytes,
        free_bytes=free,
        reserved_bytes=reserved_bytes,
    )


def require_free_space(path: Path, required_bytes: int, *, label: str) -> None:
    free = upload_service.free_bytes(path)
    if free < required_bytes:
        raise insufficient_storage(label=label, required_bytes=required_bytes, free=free)


def emit_storage_waiting(
    job: dict[str, Any], exc: domain_errors.InsufficientStorage
) -> dict[str, Any] | None:
    fingerprint = hashlib.sha256(f"storage:{exc.label}:{exc.required_bytes}".encode()).hexdigest()
    return event_service.emit_job_event(
        job,
        "job.issue",
        f"Waiting for storage: {exc.label}",
        severity="warning",
        extra={
            "component": "storage",
            "error": str(exc),
            "label": exc.label,
            "required_bytes": exc.required_bytes,
            "upload_service.free_bytes": exc.free_bytes,
            "reserved_bytes": exc.reserved_bytes,
        },
        dedupe_key=f"job.issue:storage:{exc.label}",
        fingerprint=fingerprint,
    )


def wait_for_free_space(
    job: dict[str, Any],
    path: Path,
    required_bytes: int,
    *,
    label: str,
) -> None:
    job_id = str(job["job_id"])
    while True:
        state_store.raise_if_job_canceled(job_id)
        try:
            require_free_space(path, required_bytes, label=label)
            job.pop("storage_wait", None)
            return
        except domain_errors.InsufficientStorage as exc:
            job["phase"] = f"waiting_for_space:{label.replace(' ', '_')}"
            job["storage_wait"] = {
                "label": exc.label,
                "required_bytes": exc.required_bytes,
                "upload_service.free_bytes": exc.free_bytes,
                "reserved_bytes": exc.reserved_bytes,
                "last_checked_at": utc_timestamp_now(),
                "retry_after_seconds": runtime_config.STORAGE_WAIT_SECONDS,
            }
            state_store.save_job(job)
            emit_storage_waiting(job, exc)
            log.warning("job %s waiting for storage: %s", job_id, exc)
            handoff_service.retry_sleep(runtime_config.STORAGE_WAIT_SECONDS, job_id=job_id)


def input_upload_remaining_bytes(upload: dict[str, Any]) -> int:
    return max(0, int(upload.get("bytes_total", 0)) - int(upload.get("uploaded_bytes", 0)))


def storage_hint_group_configs(
    hint: domain_models.InputUploadStorageHint,
) -> list[domain_models.StorageGroupHint]:
    if hint.groups:
        return list(hint.groups.values())
    return [
        domain_models.StorageGroupHint(
            output_mode=hint.output_mode,
            tasks=hint.tasks,
        )
    ]


def storage_hint_has_gpu_work(hint: domain_models.InputUploadStorageHint) -> bool:
    return any(
        domain_models.tasks_require_gpu(group.tasks) for group in storage_hint_group_configs(hint)
    )


def storage_hint_scratch_extra_multiplier(hint: domain_models.InputUploadStorageHint) -> float:
    if not storage_hint_has_gpu_work(hint):
        return 0.0
    if hint.workflow_mode == "review":
        return runtime_config.REVIEW_SCRATCH_EXTRA_MULTIPLIER
    if hint.workflow_mode == "collection_archive" and hint.handoff_destination != "riverhog":
        return runtime_config.BUFFERED_HANDOFF_SCRATCH_EXTRA_MULTIPLIER
    return runtime_config.GPU_SCRATCH_MULTIPLIER


def gpu_input_copy_multiplier() -> float:
    return (
        0.0
        if upload_service.path_device(runtime_config.TUSD_DIR)
        == upload_service.path_device(runtime_config.GPU_RUNTIME_DIR)
        else 1.0
    )


def gpu_scratch_required_bytes(total_bytes: int, hint: domain_models.InputUploadStorageHint) -> int:
    multiplier = storage_hint_scratch_extra_multiplier(hint)
    if multiplier <= 0:
        return 0
    return int(total_bytes * (gpu_input_copy_multiplier() + multiplier))


def storage_group_hint_for_path(
    path: str,
    hint: domain_models.InputUploadStorageHint,
) -> domain_models.StorageGroupHint:
    if hint.structured_routing:
        return domain_models.StorageGroupHint(
            output_mode=hint.output_mode,
            tasks=hint.tasks,
        )
    group_name = domain_models.input_path_group(path)
    if hint.groups:
        group = hint.groups.get(group_name)
        if group is not None:
            return group
    return domain_models.StorageGroupHint(
        output_mode=hint.output_mode,
        tasks=hint.tasks,
    )


def storage_group_hint_is_eager_archive_only(group: domain_models.StorageGroupHint) -> bool:
    if domain_models.normalize_output_mode(str(group.output_mode or "video")) != "video":
        return False
    return set(str(task) for task in group.tasks) == {"archive_video"}


def eager_archive_admission_bytes(files: list[domain_models.InputFileSpec]) -> int:
    if not files:
        return 0
    concurrent_files = (
        runtime_config.EAGER_ARCHIVE_BATCH_FILES * runtime_config.EAGER_ARCHIVE_PIPELINE_BATCHES
    )
    if concurrent_files <= 0:
        return 0
    largest = sorted((int(item.bytes) for item in files), reverse=True)[:concurrent_files]
    return sum(largest)


def gpu_scratch_admission_required_bytes(
    files: list[domain_models.InputFileSpec],
    hint: domain_models.InputUploadStorageHint,
) -> int:
    multiplier = storage_hint_scratch_extra_multiplier(hint)
    if multiplier <= 0:
        return 0
    if (
        hint.workflow_mode != "collection_archive"
        or hint.handoff_destination != "riverhog"
        or hint.structured_routing
    ):
        return gpu_scratch_required_bytes(sum(item.bytes for item in files), hint)

    eager_files: list[domain_models.InputFileSpec] = []
    non_eager_gpu_bytes = 0
    for item in files:
        group = storage_group_hint_for_path(item.path, hint)
        if not domain_models.tasks_require_gpu(group.tasks):
            continue
        if storage_group_hint_is_eager_archive_only(group):
            eager_files.append(item)
        else:
            non_eager_gpu_bytes += int(item.bytes)

    eager_required = int(
        eager_archive_admission_bytes(eager_files)
        * (gpu_input_copy_multiplier() + runtime_config.EAGER_ARCHIVE_SCRATCH_MULTIPLIER)
    )
    non_eager_required = int(non_eager_gpu_bytes * (gpu_input_copy_multiplier() + multiplier))
    return eager_required + non_eager_required


def require_input_upload_capacity(
    files: list[domain_models.InputFileSpec],
    storage_hint: domain_models.InputUploadStorageHint,
) -> None:
    active_uploads = scheduling_service.active_input_uploads()
    if (
        runtime_config.MAX_ACTIVE_INPUT_UPLOADS > 0
        and len(active_uploads) >= runtime_config.MAX_ACTIVE_INPUT_UPLOADS
    ):
        raise ServiceError(
            status_code=429,
            detail={
                "error": "too_many_active_input_uploads",
                "active": len(active_uploads),
                "limit": runtime_config.MAX_ACTIVE_INPUT_UPLOADS,
            },
        )

    total_bytes = sum(item.bytes for item in files)
    reserved_spool_bytes = sum(input_upload_remaining_bytes(upload) for upload in active_uploads)
    spool_required = reserved_spool_bytes + total_bytes
    gpu_required = gpu_scratch_admission_required_bytes(files, storage_hint)

    requirements = [
        ("source upload spool", runtime_config.TUSD_DIR, spool_required, reserved_spool_bytes),
        ("future gpu scratch", runtime_config.GPU_RUNTIME_DIR, gpu_required, 0),
    ]
    by_device: dict[int, dict[str, Any]] = {}
    for label, path, required, reserved in requirements:
        device = upload_service.path_device(path)
        entry = by_device.setdefault(
            device,
            {
                "path": path,
                "data_required": 0,
                "reserved": 0,
                "labels": [],
            },
        )
        entry["data_required"] += required
        entry["reserved"] += reserved
        entry["labels"].append(label)

    for entry in by_device.values():
        free = upload_service.free_bytes(Path(entry["path"]))
        required = int(entry["data_required"]) + runtime_config.MIN_FREE_BYTES
        if free < required:
            raise insufficient_storage(
                label=", ".join(entry["labels"]),
                required_bytes=required,
                free=free,
                reserved_bytes=int(entry["reserved"]),
            )


def storage_hint_for_job_request(
    req: domain_models.CreateJobRequest,
) -> domain_models.InputUploadStorageHint:
    groups = {
        name: domain_models.StorageGroupHint(
            output_mode=group.output_mode,
            tasks=group.tasks,
            eager_pipeline_batches=group.eager_pipeline_batches,
        )
        for name, group in req.groups.items()
    }
    return domain_models.InputUploadStorageHint(
        workflow_mode=req.workflow_mode,
        handoff_destination=req.handoff.destination,
        output_mode=req.output_mode,
        tasks=req.tasks,
        groups=groups,
        structured_routing=req.routing is not None,
    )


def validate_job_storage_hint(
    input_upload: dict[str, Any], req: domain_models.CreateJobRequest
) -> None:
    try:
        upload_hint = upload_service.input_upload_storage_hint(input_upload).model_dump(
            exclude_none=True
        )
    except (RuntimeError, ValidationError) as exc:
        raise ServiceError(
            status_code=409,
            detail={
                "error": "input_upload_storage_hint_invalid",
                "message": "input upload storage_hint is missing or invalid",
                "input_upload_id": input_upload.get("input_upload_id"),
            },
        ) from exc
    job_hint = storage_hint_for_job_request(req).model_dump(exclude_none=True)
    if upload_hint != job_hint:
        raise ServiceError(
            status_code=409,
            detail={
                "error": "storage_hint_mismatch",
                "message": "input upload storage_hint does not match requested job",
                "upload_service.input_upload_storage_hint": upload_hint,
                "job_storage_hint": job_hint,
            },
        )
