from __future__ import annotations

import copy
import hashlib
import json
import logging
import logging.config
import threading
import uuid
from contextlib import closing
from datetime import UTC, datetime, timedelta
from typing import Any

from lifecycle_events import (
    normalize_event_context,
)
from pydantic import ValidationError
from time_formats import (
    utc_timestamp_now,
)

import munchy_core.domain.errors as domain_errors
import munchy_core.domain.models as domain_models
import munchy_core.persistence.sqlite_state as state_store
import munchy_core.runtime.config as runtime_config
import munchy_core.runtime.execution as execution_runtime
import munchy_core.services.admission as admission_service
import munchy_core.services.cleanup as cleanup_service
import munchy_core.services.events as event_service
import munchy_core.services.handoffs as handoff_service
import munchy_core.services.media as media_service
import munchy_core.services.processing as processing_service
import munchy_core.services.routing as routing_service
import munchy_core.services.scheduling as scheduling_service
import munchy_core.services.templates as template_service
import munchy_core.services.uploads as upload_service
from munchy_core.domain.errors import ServiceError
from munchy_core.domain.job_templates import (
    JobTemplateError,
    render_job_template_inputs,
)
from munchy_core.persistence.application_keys import (
    MunchyPrincipal,
)

log = logging.getLogger("munchy.server")


def submission_request_digest(req: domain_models.SubmissionSpec) -> str:
    payload = req.model_dump(mode="json")
    payload.pop("submission_id", None)
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def resolved_submission(
    req: domain_models.SubmissionSpec,
    *,
    submission_id: str,
) -> tuple[dict[str, Any], domain_models.CreateJobRequest, domain_models.InputUploadStorageHint]:
    template = template_service.load_job_template(req.template_id, require_enabled=True)
    try:
        raw_job = render_job_template_inputs(
            dict(template["definition"]),
            dict(template["resolved_job"]),
            req.inputs,
        )
    except JobTemplateError as exc:
        raise ServiceError(status_code=422, detail=str(exc)) from exc
    handoff = state_store.dict_or_empty(raw_job.get("handoff"))
    handoff["on_failure"] = req.handoff_on_failure
    raw_job["handoff"] = handoff
    raw_job.update({"job_id": submission_id, "input_upload_id": submission_id})
    raw_job["event_context"] = req.event_context
    for key, value in (("run_id", req.run_id),):
        if value is not None:
            raw_job[key] = value
    try:
        job_request = domain_models.CreateJobRequest.model_validate(raw_job)
    except ValidationError as exc:
        raise ServiceError(status_code=422, detail=str(exc)) from exc
    storage_hint = admission_service.storage_hint_for_job_request(job_request)
    try:
        domain_models.CreateInputUploadRequest(files=req.files, storage_hint=storage_hint)
    except ValidationError as exc:
        raise ServiceError(status_code=422, detail=str(exc)) from exc
    return template, job_request, storage_hint


def submission_response(job: dict[str, Any]) -> dict[str, Any]:
    submission_id = str(job.get("submission_id") or job.get("job_id") or "")
    upload: dict[str, Any] | None
    try:
        upload = upload_service.load_input_upload(str(job["input_upload_id"]))
    except ServiceError as exc:
        if exc.status_code != 404:
            raise
        upload = None
    return {
        "submission_id": submission_id,
        "state": str(job.get("state") or "unknown"),
        "phase": str(job.get("phase") or ""),
        **template_service.submission_template_summary(job),
        "inputs": dict(job.get("submission_inputs") or {}),
        "upload": upload,
        "job": processing_service.compact_job_response(job),
    }


def create_submission_state(
    req: domain_models.CreateSubmissionRequest,
    *,
    initiator: MunchyPrincipal,
) -> tuple[dict[str, Any], bool]:
    submission_id = req.submission_id or uuid.uuid4().hex
    digest = submission_request_digest(req)
    existing = state_store.read_state("job", submission_id)
    if existing is not None:
        if existing.get("initiated_by_app") != initiator.app:
            raise ServiceError(status_code=404, detail=f"unknown submission: {submission_id}")
        if existing.get("submission_request_digest") != digest:
            raise ServiceError(
                status_code=409,
                detail={
                    "error": "submission_conflict",
                    "submission_id": submission_id,
                },
            )
        return existing, False
    template, job_request, storage_hint = resolved_submission(
        req,
        submission_id=submission_id,
    )
    admission_service.require_input_upload_capacity(req.files, storage_hint)
    upload_created = False
    try:
        upload = create_input_upload_state(
            input_upload_id=submission_id,
            files=req.files,
            storage_hint=storage_hint,
        )
        upload_created = True
        with execution_runtime.input_upload_state_lock(submission_id):
            upload = upload_service.load_input_upload_raw(submission_id)
            upload["submission_id"] = submission_id
            upload["submission_inputs"] = dict(req.inputs)
            upload["submission_request_digest"] = digest
            upload["template_id"] = template["template_id"]
            upload["template_revision"] = template["revision"]
            upload["template_digest"] = template["digest"]
            upload_service.save_input_upload_raw(upload)
        job = create_job_state_from_request(
            job_request,
            initiated_by_app=initiator.app,
            initiated_by_key_id=initiator.key_id,
        )
        job["submission_id"] = submission_id
        job["submission_inputs"] = dict(req.inputs)
        job["submission_request_digest"] = digest
        job["template_id"] = template["template_id"]
        job["template_revision"] = template["revision"]
        job["template_digest"] = template["digest"]
        job = state_store.save_job(job)
    except Exception:
        if upload_created:
            with execution_runtime.input_upload_state_lock(submission_id):
                cleanup_upload = state_store.read_state("input-upload", submission_id)
                if cleanup_upload is not None:
                    upload_service.remove_input_upload_data(cleanup_upload)
                state_store.delete_state("input-upload", submission_id)
        raise
    return job, True


def list_job_summaries_page(
    *,
    page: int,
    per_page: int,
    sort: str,
    order: str,
    query: str | None,
    terminal: str,
    state: str | None,
    workflow_mode: str | None,
    handoff_destination: str | None,
    cancel_requested: bool | None,
    storage_wait: bool | None,
    all_items: bool = False,
) -> dict[str, Any]:
    bounded_page = max(1, page)
    bounded_per_page = max(1, min(per_page, 100))
    normalized_sort = sort.casefold()
    if normalized_sort not in domain_models.JOB_LIST_SORT_COLUMNS:
        raise ServiceError(
            status_code=400,
            detail="sort must be one of: " + ", ".join(sorted(domain_models.JOB_LIST_SORT_COLUMNS)),
        )
    normalized_order = order.casefold()
    if normalized_order not in {"asc", "desc"}:
        raise ServiceError(status_code=400, detail="order must be asc or desc")
    normalized_terminal = terminal.casefold().replace("-", "_")
    if normalized_terminal not in domain_models.JOB_LIST_TERMINAL_FILTERS:
        raise ServiceError(
            status_code=400,
            detail="terminal must be active, terminal, or all",
        )

    where: list[str] = []
    params: list[Any] = []
    if normalized_terminal == "active":
        where.append("terminal = 0")
    elif normalized_terminal == "terminal":
        where.append("terminal = 1")
    if state:
        where.append("state = ?")
        params.append(state.strip().casefold())
    if workflow_mode:
        where.append("workflow_mode = ?")
        params.append(workflow_mode.strip().casefold().replace("-", "_"))
    if handoff_destination:
        where.append("handoff_destination = ?")
        params.append(handoff_destination.strip().casefold().replace("-", "_"))
    if cancel_requested is not None:
        where.append("cancel_requested = ?")
        params.append(state_store.bool_int(cancel_requested))
    if storage_wait is not None:
        where.append("storage_wait = ?")
        params.append(state_store.bool_int(storage_wait))
    search_query = state_store.job_search_match_query(query)
    if search_query:
        where.append(
            "job_id IN (SELECT job_id FROM job_summaries_fts WHERE job_summaries_fts MATCH ?)"
        )
        params.append(search_query)

    where_sql = " WHERE " + " AND ".join(where) if where else ""
    sort_column = domain_models.JOB_LIST_SORT_COLUMNS[normalized_sort]
    direction = normalized_order.upper()
    if sort_column == "job_id":
        order_sql = f"job_id {direction}"
    else:
        order_sql = (
            f"CASE WHEN {sort_column} = '' THEN 1 ELSE 0 END ASC, "
            f"{sort_column} {direction}, job_id ASC"
        )
    offset = (bounded_page - 1) * bounded_per_page
    limit_sql = "" if all_items else "LIMIT ? OFFSET ?"
    row_params = params if all_items else [*params, bounded_per_page, offset]
    with closing(state_store.state_db()) as conn:
        total = int(
            conn.execute(
                f"SELECT COUNT(*) AS total FROM job_summaries{where_sql}",
                params,
            ).fetchone()["total"]
        )
        rows = conn.execute(
            f"""
            SELECT job_id
            FROM job_summaries
            {where_sql}
            ORDER BY {order_sql}
            {limit_sql}
            """,
            row_params,
        ).fetchall()
    job_ids = [str(row["job_id"]) for row in rows]
    jobs = [
        processing_service.compact_job_response(job, include_queue=False)
        for job in state_store.load_jobs_by_ids(job_ids)
    ]
    return {
        "page": 1 if all_items else bounded_page,
        "pages": (1 if total else 0)
        if all_items
        else (total + bounded_per_page - 1) // bounded_per_page
        if total
        else 0,
        "per_page": total if all_items else bounded_per_page,
        "total": total,
        "sort": normalized_sort,
        "order": normalized_order,
        "query": query,
        "terminal": normalized_terminal,
        "filters": {
            "state": state,
            "workflow_mode": workflow_mode,
            "handoff_destination": handoff_destination,
            "cancel_requested": cancel_requested,
            "storage_wait": storage_wait,
        },
        "jobs": jobs,
    }


def wait_for_upload_groups(
    job: dict[str, Any],
    upload_id: str,
    group_names: set[str],
    groups: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    job_id = str(job["job_id"])
    requested_group_names = {str(group_name) for group_name in group_names}
    while True:
        state_store.raise_if_job_canceled(job_id)
        upload = upload_service.load_input_upload(upload_id)
        if groups is not None:
            upload = routing_service.route_completed_input_files(job, upload, groups)
        structured_pending = (
            isinstance(job.get("routing"), dict) and str(upload.get("state") or "") != "uploaded"
        )
        active_group_names = requested_group_names
        if not structured_pending:
            active_group_names = upload_service.upload_group_names_with_files(
                upload, requested_group_names
            )
            if not active_group_names:
                job["upload_progress"] = upload_service.upload_group_progress(
                    upload, active_group_names
                )
                state_store.save_job(job)
                return upload
        upload_service.sync_shared_input_tree(upload, active_group_names)
        progress = upload_service.upload_group_progress(upload, active_group_names)
        job["upload_progress"] = progress
        if not structured_pending and upload_service.upload_groups_complete(
            upload, active_group_names
        ):
            state_store.save_job(job)
            return upload
        job["phase"] = f"waiting_for_upload:{progress['files_uploaded']}/{progress['files_total']}"
        state_store.save_job(job)
        handoff_service.retry_sleep(runtime_config.EAGER_ARCHIVE_WAIT_SECONDS, job_id=job_id)


def run_job(job_id: str) -> None:
    with execution_runtime.state_lock:
        execution_runtime.scheduled_jobs.discard(job_id)
        if job_id in execution_runtime.active_jobs:
            return
        execution_runtime.active_jobs.add(job_id)
    try:
        job = state_store.load_job(job_id)
        state_store.raise_if_job_canceled(job_id)
        job["state"] = "running"
        job.setdefault("started_at", utc_timestamp_now())
        state_store.save_job(job)

        input_upload = upload_service.load_input_upload(str(job["input_upload_id"]))
        storage_hint = upload_service.input_upload_storage_hint(input_upload)
        gpu_job_root = runtime_config.GPU_RUNTIME_DIR / "jobs" / job_id
        input_dir = gpu_job_root / "input"
        archive_dir = gpu_job_root / "archive"
        review_dir = gpu_job_root / "review"

        groups = processing_service.ensure_job_groups(job, input_upload)

        group_results = job.setdefault("group_results", {})
        gpu_payloads = job.setdefault("gpu_payloads", {})
        gpu_results = job.setdefault("gpu_results", {})
        review_clip_plan = state_store.dict_or_empty(
            state_store.dict_or_empty(job.get("review")).get("clip_plan")
        )

        eager_groups = processing_service.eager_archive_group_names(groups)
        if eager_groups:
            input_upload = processing_service.run_eager_archive_groups(
                job,
                input_upload,
                groups,
                eager_groups,
                archive_dir,
            )
            state_store.raise_if_job_canceled(job_id)

        non_eager_groups = set(str(group_name) for group_name in groups) - eager_groups
        if non_eager_groups and (
            not isinstance(job.get("routing"), dict)
            or str(input_upload.get("state") or "") == "uploaded"
        ):
            input_upload = routing_service.route_completed_input_files(job, input_upload, groups)
            non_eager_groups = upload_service.upload_group_names_with_files(
                input_upload, non_eager_groups
            )
        input_dir = gpu_job_root / "input"
        if non_eager_groups:
            input_upload = wait_for_upload_groups(
                job,
                str(job["input_upload_id"]),
                non_eager_groups,
                groups,
            )
            non_eager_groups = upload_service.upload_group_names_with_files(
                input_upload, non_eager_groups
            )
        if non_eager_groups:
            non_eager_bytes = upload_service.upload_bytes_for_groups(input_upload, non_eager_groups)
            required_gpu_free = (
                admission_service.gpu_scratch_required_bytes(non_eager_bytes, storage_hint)
                + runtime_config.MIN_FREE_BYTES
            )
            admission_service.wait_for_free_space(
                job, runtime_config.GPU_RUNTIME_DIR, required_gpu_free, label="gpu scratch"
            )
            job["phase"] = "preparing_input"
            state_store.save_job(job)
            input_dir = upload_service.prepare_shared_input_tree(
                input_upload,
                non_eager_groups,
                job=job,
            )
            state_store.raise_if_job_canceled(job_id)

        if processing_service.is_review_sweep_job(job):
            processing_service.run_review_sweep_job(
                job,
                input_upload=input_upload,
                groups=groups,
                input_dir=input_dir,
                gpu_job_root=gpu_job_root,
                review_dir=review_dir,
            )
            state_store.raise_if_job_canceled(job_id)
            job["phase"] = "done"
            job["state"] = "succeeded"
            job["finished_at"] = utc_timestamp_now()
            state_store.save_job(job)
            if cleanup_service.should_cleanup_local_work_on_success(job):
                cleanup_service.cleanup_terminal_job(job)
            cleanup_service.compact_terminal_job_state(job)
            state_store.save_job(job)
            event_service.emit_job_event(job, "job.succeeded", "Munchy job completed successfully.")
            return

        gpu_work: list[tuple[str, dict[str, Any], list[domain_models.TaskName]]] = []
        for group_name, group_config in groups.items():
            if str(group_name) in eager_groups:
                continue
            if str(group_name) not in non_eager_groups:
                continue
            domain_models.validate_group_name(str(group_name))
            group_output_mode = domain_models.normalize_output_mode(
                str(group_config.get("output_mode") or "video")
            )
            if group_output_mode not in {"video", "audio", "preserve"}:
                raise RuntimeError(
                    f"unsupported output_mode for group {group_name}: {group_output_mode}"
                )
            if group_output_mode == "preserve" and not group_results.get(group_name, {}).get(
                "preserve_copied"
            ):
                job["phase"] = f"copying_preserve:{group_name}"
                state_store.save_job(job)
                upload_service.copy_preserve_group_files(
                    input_upload,
                    group_name=group_name,
                    source_root=input_dir / group_name,
                    dest_root=archive_dir / group_name,
                )
                input_upload_id = str(input_upload["input_upload_id"])
                with execution_runtime.input_upload_state_lock(input_upload_id):
                    input_upload = upload_service.load_input_upload(input_upload_id)
                    preserve_source_artifacts = (
                        upload_service.build_preserve_group_source_artifacts(
                            input_upload,
                            group_name=group_name,
                            source_root=input_dir / group_name,
                            output_root=archive_dir / group_name,
                        )
                    )
                    input_upload = upload_service.save_input_upload_raw(input_upload)
                group_results[group_name] = {
                    **group_results.get(group_name, {}),
                    "preserve_copied": True,
                    "preserve_source_artifacts": preserve_source_artifacts,
                    "copied_at": utc_timestamp_now(),
                }
                state_store.save_job(job)
                state_store.raise_if_job_canceled(job_id)

            tasks = list(group_config.get("tasks") or [])
            if group_output_mode == "preserve":
                tasks = [task for task in tasks if task not in {"archive_video", "archive_audio"}]
            if "archive_audio" in tasks and not group_results.get(group_name, {}).get(
                "archive_audio"
            ):
                job["phase"] = f"archive_audio:{group_name}"
                state_store.save_job(job)
                audio_file_states = upload_service.mutable_primary_upload_files_for_groups(
                    input_upload,
                    {group_name},
                )
                audio_rel_paths = {
                    upload_service.upload_file_group_rel_for_state(
                        file_state, group_name
                    ).as_posix()
                    for file_state in audio_file_states
                }
                group_results[group_name] = {
                    **group_results.get(group_name, {}),
                    "archive_audio": media_service.run_archive_audio_group(
                        input_root=input_dir / group_name,
                        output_root=archive_dir / group_name,
                        group_config=group_config,
                        source_rel_paths=audio_rel_paths,
                        source_artifacts_sidecars=upload_service.source_artifacts_sidecar_entries(
                            input_upload,
                            audio_file_states,
                            group_name=group_name,
                            materialized_group_root=input_dir / group_name,
                        ),
                    ),
                    "archive_audio_at": utc_timestamp_now(),
                }
                state_store.save_job(job)
                state_store.raise_if_job_canceled(job_id)
            gpu_target_tasks = [
                task for task in tasks if str(task) in domain_models.GPU_TARGET_TASKS
            ]
            if gpu_target_tasks and group_name not in gpu_results:
                gpu_work.append((str(group_name), group_config, gpu_target_tasks))

        if gpu_work:
            token = media_service.acquire_job_gpu(job)
            try:
                for group_name, group_config, tasks in gpu_work:
                    state_store.raise_if_job_canceled(job_id)
                    input_upload_id = str(input_upload["input_upload_id"])
                    with execution_runtime.input_upload_state_lock(input_upload_id):
                        input_upload = upload_service.load_input_upload(input_upload_id)
                        group_file_states = upload_service.mutable_primary_upload_files_for_groups(
                            input_upload,
                            {group_name},
                        )
                        container_metadata, container_metadata_changed = (
                            routing_service.container_metadata_for_gpu_payload(
                                job,
                                input_upload,
                                group_file_states,
                                group_name=group_name,
                                group_config=group_config,
                                tasks=tasks,
                            )
                        )
                        if container_metadata_changed:
                            input_upload = upload_service.save_input_upload_raw(input_upload)
                    gpu_job_id = routing_service.gpu_group_job_id(job_id, group_name)
                    job["phase"] = f"gpu:{group_name}"
                    state_store.save_job(job)
                    gpu_payload = {
                        "job_id": gpu_job_id,
                        "input_dir": upload_service.gpu_runtime_container_path(
                            input_dir / group_name
                        ),
                        "archive_dir": upload_service.gpu_runtime_container_path(
                            archive_dir / group_name
                        ),
                        "review_dir": upload_service.gpu_runtime_container_path(
                            review_dir / group_name
                        ),
                        "profile": group_config.get("profile", "av1-nvenc-high"),
                        "tasks": tasks,
                        "run_id": job.get("run_id"),
                        "container_metadata_required": (
                            routing_service.gpu_tasks_require_container_metadata(
                                tasks,
                                group_config,
                            )
                        ),
                    }
                    if group_config.get("encode_profile") is not None:
                        gpu_payload["encode_profile"] = group_config["encode_profile"]
                    if group_config.get("max_parallel_encodes") is not None:
                        gpu_payload["max_parallel_encodes"] = group_config["max_parallel_encodes"]
                    if review_clip_plan and any(
                        task in tasks for task in ("qcut_video", "audio_review")
                    ):
                        gpu_payload["review_clip_plan"] = copy.deepcopy(review_clip_plan)
                    if container_metadata:
                        gpu_payload["container_metadata"] = container_metadata
                    source_artifacts_sidecars = upload_service.source_artifacts_sidecar_entries(
                        input_upload,
                        group_file_states,
                        group_name=group_name,
                        materialized_group_root=input_dir / group_name,
                        container_group_root=upload_service.gpu_runtime_container_path(
                            input_dir / group_name
                        ),
                    )
                    if source_artifacts_sidecars:
                        gpu_payload["source_artifacts_sidecars"] = source_artifacts_sidecars
                    for task_name in ("qcut_video", "audio_review"):
                        if task_name not in tasks:
                            continue
                        review_plan = upload_service.load_shared_review_plan(
                            str(job["input_upload_id"]),
                            group_name,
                            task_name,
                        )
                        if review_plan is not None:
                            gpu_payload.setdefault("review_plans", {})[task_name] = review_plan
                    gpu_payloads[group_name] = gpu_payload
                    state_store.save_job(job)
                    media_service.start_gpu_job(gpu_payload)
                    gpu_results[group_name] = media_service.wait_gpu_job(
                        gpu_job_id,
                        gpu_payload=gpu_payload,
                        job=job,
                    )
                    upload_service.remember_review_plans_from_gpu_result(
                        job,
                        group_name,
                        gpu_results[group_name],
                    )
                    if len(groups) == 1:
                        job["gpu_result"] = gpu_results[group_name]
                    else:
                        job["gpu_result"] = {"state": "succeeded", "groups": gpu_results}
                    state_store.save_job(job)
            finally:
                media_service.release_job_gpu(job, token)
        state_store.raise_if_job_canceled(job_id)
        input_upload = upload_service.load_input_upload(str(job["input_upload_id"]))
        job["phase"] = "metadata_projection"
        state_store.save_job(job)
        handoff_service.handoff_adapter(job).wait_until_idle(job)
        input_upload = routing_service.write_metadata_projection_sidecars(
            job, input_upload, groups, archive_dir
        )
        state_store.raise_if_job_canceled(job_id)
        if isinstance(job.get("routing"), dict):
            routing_service.write_routing_manifest(job, input_upload, groups, archive_dir)

        workflow_mode = str(job.get("workflow_mode") or "collection_archive")
        source_dir = review_dir if workflow_mode == "review" else archive_dir
        source_label = "review" if workflow_mode == "review" else "collection archive"
        job["phase"] = "handoff"
        state_store.save_job(job)
        job["handoff_receipt"] = handoff_service.advance_handoff(
            job,
            source_dir,
            final=True,
            source_label=source_label,
        )
        state_store.save_job(job)
        state_store.raise_if_job_canceled(job_id)

        job["phase"] = "done"
        job["state"] = "succeeded"
        job["finished_at"] = utc_timestamp_now()
        state_store.save_job(job)
        if cleanup_service.should_cleanup_local_work_on_success(job):
            cleanup_service.cleanup_terminal_job(job)
        cleanup_service.compact_terminal_job_state(job)
        state_store.save_job(job)
        event_service.emit_job_event(job, "job.succeeded", "Munchy job completed successfully.")
    except domain_errors.JobCanceled as exc:
        log.info("job %s canceled: %s", job_id, exc)
        try:
            job = state_store.load_job(job_id)
        except ServiceError:
            job = {"job_id": job_id}
        cleanup_service.finalize_canceled_job(job, reason="job_canceled")
    except Exception as exc:
        log.exception("job %s failed", job_id)
        try:
            job = state_store.load_job(job_id)
        except ServiceError:
            job = {"job_id": job_id}
        job["state"] = "failed"
        job["error"] = str(exc)
        job["finished_at"] = utc_timestamp_now()
        upload_service.write_job_debug_bundle(
            job,
            reason="encoding_failed"
            if isinstance(exc, domain_errors.EncodingFailed)
            else "job_failed",
            error=exc,
        )
        if handoff_service.should_cancel_handoff_on_failure(job, exc):
            handoff_service.cancel_handoff(
                job,
                reason="encoding_failed"
                if isinstance(exc, domain_errors.EncodingFailed)
                else "job_failed",
            )
        else:
            state = job.setdefault("handoff_adapter_state", {})
            if isinstance(state, dict):
                state["preserved_after_failure_at"] = utc_timestamp_now()
        state_store.save_job(job)
        if isinstance(exc, domain_errors.EncodingFailed):
            cleanup_service.cleanup_terminal_job(job)
            cleanup_service.compact_terminal_job_state(job)
            state_store.save_job(job)
        elif isinstance(exc, domain_errors.RoutingFailed):
            event_service.emit_job_issue(job, component="routing", error=exc, severity="error")
        else:
            event_service.emit_job_issue(job, component="job", error=exc, severity="error")
    finally:
        with execution_runtime.state_lock:
            execution_runtime.active_jobs.discard(job_id)
        schedule_pending_jobs()


def schedule_job(
    job_id: str,
    background_tasks: execution_runtime.TaskScheduler | None = None,
    *,
    ignore_capacity: bool = False,
) -> bool:
    if scheduling_service.scheduling_paused():
        log.info("scheduler is paused; leaving job queued: %s", job_id)
        return False
    with execution_runtime.state_lock:
        if job_id in execution_runtime.active_jobs or job_id in execution_runtime.scheduled_jobs:
            return False
        if not ignore_capacity and scheduling_service.running_job_slots_available() <= 0:
            return False
        execution_runtime.scheduled_jobs.add(job_id)
    if background_tasks is None:
        thread = threading.Thread(target=run_job, args=(job_id,), daemon=True)
        thread.start()
        return True
    background_tasks.add_task(run_job, job_id)
    return True


def schedule_pending_jobs(
    background_tasks: execution_runtime.TaskScheduler | None = None,
) -> list[str]:
    if scheduling_service.scheduling_paused():
        return []
    scheduled: list[str] = []
    for job in scheduling_service.runnable_jobs_in_order():
        if scheduling_service.running_job_slots_available() <= 0:
            break
        if not scheduling_service.runnable_job(job):
            continue
        job_id = str(job["job_id"])
        if job_id in execution_runtime.active_jobs or job_id in execution_runtime.scheduled_jobs:
            continue
        if schedule_job(job_id, background_tasks, ignore_capacity=True):
            scheduled.append(job_id)
    return scheduled


def load_submission(submission_id: str) -> dict[str, Any]:
    job = state_store.load_job(submission_id)
    if job.get("submission_id") != submission_id:
        raise ServiceError(status_code=404, detail=f"unknown submission: {submission_id}")
    return job


def create_input_upload_state(
    *,
    input_upload_id: str,
    files: list[domain_models.InputFileSpec],
    storage_hint: domain_models.InputUploadStorageHint,
) -> dict[str, Any]:
    admission_service.require_input_upload_capacity(files, storage_hint)
    with execution_runtime.input_upload_state_lock(input_upload_id):
        if state_store.state_exists("input-upload", input_upload_id):
            raise ServiceError(
                status_code=409,
                detail=f"input upload already exists: {input_upload_id}",
            )
        file_states = []
        for item in files:
            target_path = upload_service.target_path_for(input_upload_id, item.path)
            file_states.append(
                {
                    "path": item.path,
                    "bytes": item.bytes,
                    "sha256": item.sha256,
                    "filesystem_metadata": item.filesystem_metadata,
                    "target_path": target_path,
                    "input_upload_id": input_upload_id,
                    "file_upload_id": upload_service.tusd_upload_id_for_target_path(target_path),
                    "upload_url": None,
                    "structured_routing": storage_hint.structured_routing,
                }
            )
        upload = {
            "input_upload_id": input_upload_id,
            "state": "uploading",
            "created_at": utc_timestamp_now(),
            "files": file_states,
            "storage_hint": storage_hint.model_dump(exclude_none=True),
            "tusd_creation_url": runtime_config.TUSD_PUBLIC_BASE_URL,
        }
        return upload_service.save_input_upload(upload)


def input_file_upload_response(
    *,
    upload_url: object,
    offset: int,
    length: int,
    status: dict[str, Any],
) -> dict[str, Any]:
    return {
        "protocol": "tus",
        "upload_url": upload_service.public_tusd_upload_url(str(upload_url))
        if upload_url
        else upload_url,
        "offset": offset,
        "length": length,
        "checksum_algorithm": "sha256",
        "headers": {"Tus-Resumable": "1.0.0"},
        "file": status,
    }


def _create_or_resume_input_file_upload(
    input_upload_id: str,
    rel_path: str,
) -> dict[str, Any]:
    with execution_runtime.input_upload_state_lock(input_upload_id):
        upload = upload_service.load_input_upload_raw(input_upload_id)
        file_state = upload_service.find_upload_file(upload, rel_path)
        if file_state.get("consumed_at"):
            status = upload_service.upload_file_status(file_state)
            return input_file_upload_response(
                upload_url=file_state.get("upload_url"),
                offset=int(file_state["bytes"]),
                length=int(file_state["bytes"]),
                status=status,
            )
        upload_url = file_state.get("upload_url")
        target_path = str(file_state["target_path"])
        length = int(file_state["bytes"])

    offset = upload_service.head_tusd_upload(str(upload_url)) if upload_url else -1
    if offset < 0:
        created_upload_url = upload_service.create_tusd_upload(target_path, length)
        with execution_runtime.input_upload_state_lock(input_upload_id):
            upload = upload_service.load_input_upload_raw(input_upload_id)
            file_state = upload_service.find_upload_file(upload, rel_path)
            if file_state.get("consumed_at"):
                status = upload_service.upload_file_status(file_state)
                return input_file_upload_response(
                    upload_url=file_state.get("upload_url") or created_upload_url,
                    offset=int(file_state["bytes"]),
                    length=int(file_state["bytes"]),
                    status=status,
                )
            existing_upload_url = file_state.get("upload_url")
            if existing_upload_url:
                upload_url = str(existing_upload_url)
                should_head_existing = True
            else:
                upload_url = created_upload_url
                offset = 0
                should_head_existing = False
                file_state["upload_url"] = upload_url
                upload = upload_service.save_input_upload_raw(upload)
        if should_head_existing:
            offset = upload_service.head_tusd_upload(upload_url)
            if offset < 0:
                offset = 0

    with execution_runtime.input_upload_state_lock(input_upload_id):
        upload = upload_service.load_input_upload_raw(input_upload_id)
        file_state = upload_service.find_upload_file(upload, rel_path)
        if file_state.get("consumed_at"):
            status = upload_service.upload_file_status(file_state)
            return input_file_upload_response(
                upload_url=file_state.get("upload_url") or upload_url,
                offset=int(file_state["bytes"]),
                length=int(file_state["bytes"]),
                status=status,
            )
        if upload_url and not file_state.get("upload_url"):
            file_state["upload_url"] = upload_url
            upload = upload_service.save_input_upload_raw(upload)
        status = upload_service.upload_file_status(file_state)
        length = int(file_state["bytes"])
    if offset < 0 and upload_url:
        offset = upload_service.head_tusd_upload(upload_url)
    if offset < 0:
        offset = 0
    return input_file_upload_response(
        upload_url=upload_url,
        offset=offset,
        length=length,
        status=status,
    )


def create_job_state_from_request(
    req: domain_models.CreateJobRequest,
    *,
    initiated_by_app: str = "munchy",
    initiated_by_key_id: str | None = None,
) -> dict[str, Any]:
    if req.input_upload_id is None:
        raise ServiceError(status_code=400, detail="input_upload_id is required")
    input_upload = upload_service.load_input_upload(req.input_upload_id)
    admission_service.validate_job_storage_hint(input_upload, req)
    groups = routing_service.resolve_job_groups(input_upload, req)
    routing = req.routing.model_dump(exclude_none=True) if req.routing is not None else None
    job_id = req.job_id or uuid.uuid4().hex
    if state_store.state_exists("job", job_id):
        raise ServiceError(status_code=409, detail=f"job already exists: {job_id}")
    handoff = {
        **req.handoff.model_dump(exclude_none=True),
        "state": "pending",
        "safe_to_delete": False,
    }
    job = {
        "job_id": job_id,
        "state": "queued",
        "phase": "queued",
        "created_at": utc_timestamp_now(),
        "initiated_by_app": initiated_by_app,
        "initiated_by_key_id": initiated_by_key_id,
        "input_upload_id": req.input_upload_id,
        "run_id": req.run_id or "",
        "workflow_mode": req.workflow_mode,
        "output_mode": req.output_mode,
        "tasks": routing_service.grouped_task_union(groups) if req.groups else req.tasks,
        "profile": req.encode_profile.name
        if req.encode_profile and req.encode_profile.name
        else "av1-nvenc-high",
        "encode_profile": req.encode_profile.server_payload()
        if req.encode_profile is not None
        else None,
        "groups": groups,
        "routing": routing,
        "handoff_expected_primary_files_total": 0,
        "handoff": handoff,
        "review": req.review.model_dump(exclude_none=True) if req.review is not None else None,
        "event_context": normalize_event_context(req.event_context),
    }
    job["handoff_expected_primary_files_total"] = (
        handoff_service.handoff_adapter(job).expected_primary_files_total(
            input_upload, groups, routing
        )
        or 0
    )
    return state_store.save_job(job)


def cleanup_once() -> dict[str, Any]:
    removed: list[str] = []
    compacted: list[str] = []
    repaired_canceled: list[str] = []
    upload_cutoff = datetime.now(UTC) - timedelta(hours=runtime_config.INPUT_UPLOAD_TTL_HOURS)
    orphan_upload_cutoff = datetime.now(UTC) - timedelta(
        hours=runtime_config.ORPHAN_INPUT_UPLOAD_TTL_HOURS
    )
    stale_canceled_jobs: list[dict[str, Any]] = []
    with execution_runtime.state_lock:
        for job in scheduling_service.job_states():
            job_id = str(job.get("job_id") or "")
            if (
                job_id
                and job_id not in execution_runtime.active_jobs
                and job.get("cancel_requested")
                and job.get("state") not in domain_models.TERMINAL_JOB_STATES
            ):
                stale_canceled_jobs.append(job)
    for job in stale_canceled_jobs:
        cleanup_service.finalize_canceled_job(job, reason="stale_cancel_requested")
        repaired_canceled.append(str(job.get("job_id") or ""))

    with execution_runtime.state_lock:
        referenced_uploads = scheduling_service.referenced_input_upload_ids()
        for upload_state in scheduling_service.input_upload_states():
            upload_id = str(upload_state["input_upload_id"])
            with execution_runtime.input_upload_state_lock(upload_id):
                current = state_store.read_state("input-upload", upload_id)
                if current is None:
                    continue
                upload = upload_service.refresh_input_upload(current)
                last_activity = upload_service.input_upload_last_activity(upload)
                if upload.get("state") == "uploaded":
                    if upload_id in referenced_uploads or last_activity > orphan_upload_cutoff:
                        continue
                    upload_service.remove_input_upload_data(upload)
                    state_store.delete_state("input-upload", upload_id)
                    removed.append(f"orphan-input-upload:{upload_id}")
                    continue
                if last_activity > upload_cutoff:
                    continue
                upload_service.remove_input_upload_data(upload)
                state_store.delete_state("input-upload", upload_id)
                removed.append(f"input-upload:{upload_id}")

        job_cutoff = datetime.now(UTC) - timedelta(hours=runtime_config.LOCAL_CLEANUP_MIN_AGE_HOURS)
        for job in scheduling_service.job_states():
            job_id = str(job.get("job_id") or "")
            if not job_id or job_id in execution_runtime.active_jobs:
                continue
            cleanup_due = cleanup_service.should_cleanup_terminal_local_work(job, job_cutoff)
            removed_for_job: list[str] = []
            if cleanup_due:
                if job.get("state") == "failed":
                    upload_service.write_job_debug_bundle(job, reason="maintenance_failed_cleanup")
                if job.get("state") in {"failed", "canceled"}:
                    handoff_service.cancel_handoff(job, reason="terminal_cleanup")
                removed_for_job = cleanup_service.cleanup_terminal_job(job)
            compacted_for_job = (
                job.get("state") in domain_models.TERMINAL_JOB_STATES
                and (cleanup_due or bool(job.get("cleanup_completed_at")))
                and cleanup_service.compact_terminal_job_state(job)
            )
            if removed_for_job:
                removed.append(f"job-cleanup:{job_id}")
            if compacted_for_job:
                compacted.append(job_id)
            if removed_for_job or compacted_for_job:
                state_store.save_job(job)

    vacuumed = False
    if removed or compacted or repaired_canceled:
        with execution_runtime.state_lock:
            if not execution_runtime.active_jobs:
                state_store.vacuum_state_store()
                vacuumed = True
    return {
        "removed": removed,
        "compacted": compacted,
        "repaired_canceled": repaired_canceled,
        "vacuumed": vacuumed,
    }


def cleanup_loop() -> None:
    while not execution_runtime.cleanup_stop.wait(runtime_config.CLEANUP_INTERVAL_SECONDS):
        try:
            result = cleanup_once()
            if result["removed"] or result["compacted"] or result["repaired_canceled"]:
                log.info(
                    "maintenance cleanup removed=%s compacted=%s repaired_canceled=%s vacuumed=%s",
                    ", ".join(result["removed"]) or "-",
                    len(result["compacted"]),
                    ", ".join(result["repaired_canceled"]) or "-",
                    result["vacuumed"],
                )
        except Exception:
            log.exception("maintenance cleanup failed")
