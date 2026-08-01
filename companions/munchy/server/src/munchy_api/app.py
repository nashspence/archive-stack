from __future__ import annotations

import os
import secrets
import threading
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import timedelta
from typing import Any

import uvicorn
from fastapi import BackgroundTasks, FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from lifecycle_events import cloud_event, normalize_event_context
from munchy_core import coordinator as core
from munchy_core.adapters import external, gpu, riverhog
from munchy_core.application_keys import (
    EVENTS_READ,
    EVENTS_READ_ALL,
    SUBMISSIONS_MANAGE,
    MunchyPrincipal,
)
from munchy_core.errors import ServiceError
from munchy_core.template_registry import TemplateRegistryError, validate_template_registry
from munchy_target_support.uvicorn_logging import uvicorn_log_config_without_health_access_logs
from munchy_workflows.profiles import MUNCHY_AUDIO_PROFILE_TARGET, MUNCHY_PROFILE_TARGET
from time_formats import utc_timestamp_now

command_adapter = external.ExternalHandoffAdapter("command")
rclone_adapter = external.ExternalHandoffAdapter("rclone")
core.register_handoff_adapter(command_adapter, option_model=external.CommandHandoffOptions)
core.register_handoff_adapter(rclone_adapter, option_model=external.RcloneHandoffOptions)
core.register_gpu_platform(gpu.HttpGpuPlatform())
riverhog_adapter = riverhog.RiverhogHandoffAdapter()
core.register_handoff_adapter(riverhog_adapter, option_model=riverhog.RiverhogHandoffOptions)


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    core.ensure_dirs()
    core.init_state_store()
    if core.RESUME_ON_START:
        core.schedule_pending_jobs()
    if core.CLEANUP_INTERVAL_SECONDS > 0:
        core.cleanup_stop.clear()
        core.cleanup_thread = threading.Thread(
            target=core.cleanup_loop, name="cleanup-loop", daemon=True
        )
        core.cleanup_thread.start()
    if any(adapter.supports_eager for adapter in core.HANDOFF_ADAPTERS.values()):
        core.handoff_stop.clear()
        core.handoff_thread = threading.Thread(
            target=core.handoff_loop,
            name="handoff-loop",
            daemon=True,
        )
        core.handoff_thread.start()
    for adapter in core.HANDOFF_ADAPTERS.values():
        adapter.start()
    try:
        yield
    finally:
        for adapter in reversed(tuple(core.HANDOFF_ADAPTERS.values())):
            adapter.stop()
        core.handoff_stop.set()
        if core.handoff_thread is not None:
            core.handoff_thread.join(timeout=5)
        core.cleanup_stop.set()
        if core.cleanup_thread is not None:
            core.cleanup_thread.join(timeout=5)


app = FastAPI(title="munchy-server", version="0.1.0", lifespan=lifespan)


@app.exception_handler(ServiceError)
async def service_error_handler(_request: Request, exc: ServiceError) -> JSONResponse:
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})


def request_bearer_token(request: Request) -> str:
    raw = request.headers.get("authorization", "")
    scheme, _, token = raw.partition(" ")
    return token if scheme.casefold() == "bearer" else ""


def authorized_admin_bearer(request: Request) -> bool:
    if not core.ADMIN_TOKEN:
        return not core.APPLICATION_AUTH_REQUIRED
    return secrets.compare_digest(request_bearer_token(request), core.ADMIN_TOKEN)


def request_principal(request: Request) -> MunchyPrincipal:
    principal = getattr(request.state, "principal", None)
    if not isinstance(principal, MunchyPrincipal):
        raise HTTPException(status_code=401, detail="invalid application token")
    return principal


@app.middleware("http")
async def require_api_auth(request: Request, call_next):  # type: ignore[no-untyped-def]
    if request.url.path.startswith("/v1/admin/"):
        if not authorized_admin_bearer(request):
            return JSONResponse(
                {"detail": "invalid admin token"},
                status_code=401,
                headers={"WWW-Authenticate": "Bearer"},
            )
    elif request.url.path.startswith("/v1/"):
        if core.APPLICATION_AUTH_REQUIRED:
            principal = core.application_keys().authenticate(request_bearer_token(request))
            if principal is None:
                return JSONResponse(
                    {"detail": "invalid application token"},
                    status_code=401,
                    headers={"WWW-Authenticate": "Bearer"},
                )
        else:
            principal = MunchyPrincipal(
                app="local",
                key_id="local",
                permissions=frozenset({"*"}),
            )
        required_permission = (
            EVENTS_READ if request.url.path == "/v1/events" else SUBMISSIONS_MANAGE
        )
        if not principal.allows(required_permission):
            return JSONResponse(
                {"detail": f"application permission required: {required_permission}"},
                status_code=403,
            )
        request.state.principal = principal
    return await call_next(request)


@app.exception_handler(core.InsufficientStorage)
async def insufficient_storage_handler(
    _request: Request, exc: core.InsufficientStorage
) -> JSONResponse:
    return JSONResponse(
        status_code=507,
        content={
            "detail": {
                "error": "insufficient_storage",
                "message": str(exc),
                "label": exc.label,
                "required_bytes": exc.required_bytes,
                "free_bytes": exc.free_bytes,
                "reserved_bytes": exc.reserved_bytes,
            }
        },
    )


def hook_error(message: str, status_code: int = 400) -> JSONResponse:
    return JSONResponse(
        {
            "RejectUpload": True,
            "HTTPResponse": {
                "StatusCode": status_code,
                "Body": message,
                "Header": {"Content-Type": "text/plain"},
            },
        }
    )


@app.get("/health/live")
def health_live() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/health/ready")
def health_ready() -> dict[str, Any]:
    try:
        template_count = validate_template_registry(core.STATE_DB_PATH)
    except TemplateRegistryError as exc:
        raise HTTPException(
            status_code=503,
            detail="job template registry does not satisfy the current contract",
        ) from exc
    return {
        "status": "ok",
        "state_dir": str(core.STATE_DIR),
        "work_dir": str(core.WORK_DIR),
        "tusd_public_base_url": core.TUSD_PUBLIC_BASE_URL,
        "gpu_target": core.GPU_TARGET,
        "handoff_adapters": {
            "command": {
                "enabled": external.EXTERNAL_HANDOFF_ENABLED
                and bool(external.COMMAND_HANDOFF_COMMAND),
            },
            "rclone": {
                "enabled": external.EXTERNAL_HANDOFF_ENABLED
                and bool(external.RCLONE_HANDOFF_COMMAND),
            },
            "riverhog": {
                "enabled": riverhog_adapter.enabled,
                "eager_workers": riverhog_adapter.worker_count,
                "eager_worker_running": bool(
                    core.handoff_thread is not None and core.handoff_thread.is_alive()
                ),
                "event_worker_running": riverhog_adapter.background_running,
            },
        },
        "event_source": core.EVENT_SOURCE,
        "scheduler_paused": core.scheduling_paused(),
        "running_job_limit": core.MAX_RUNNING_JOBS,
        "running_jobs": len(core.active_jobs),
        "scheduled_jobs": len(core.scheduled_jobs),
        "job_templates": template_count,
    }


@app.get("/v1/capabilities")
def capabilities() -> dict[str, Any]:
    return {
        "workflow_modes": ["collection_archive", "review"],
        "handoff": {
            "destinations": {
                "command": {"options": ["exclude"]},
                "rclone": {"options": ["location", "mode", "exclude"]},
                "riverhog": {"options": ["archive_store", "tags"]},
            },
            "failure_actions": ["preserve_for_resume", "cancel"],
            "template_fields": [
                "job_id",
                "template_id",
                "route_id",
                "profile_id",
                "run_id",
            ],
        },
        "output_modes": ["video", "audio", "preserve"],
        "tasks": ["archive_video", "archive_audio", "qcut_video", "audio_review"],
        "encode_profile": {
            "schema_versions": [1],
            "targets": [MUNCHY_PROFILE_TARGET, MUNCHY_AUDIO_PROFILE_TARGET],
            "archive_codecs": ["av1_nvenc", "opus"],
            "containers": ["mkv", "webm", "opus"],
            "source_artifact_drops": [
                "stream:N",
                "atom:TYPE",
                "top-level-atom:TYPE",
                "atom-offset:OFFSET",
            ],
            "fps_modes": ["passthrough", "halve_60_to_30"],
            "audio_codecs": ["opus"],
        },
        "groups": {
            "input_path_shape": "<group>/<file>",
            "structured_input_path_shape": "<source-or-device>/<original-relative-path>",
            "structured_routing": True,
            "routing_match_fields": [
                "when.all",
                "when.any",
                "when.not",
                "when.path",
                "when.fact",
                "when.gate",
                "when.pair",
                "pairings",
                "gates",
                "into",
                "action",
            ],
            "group_name_chars": "letters, digits, dots, underscores, dashes",
            "job_groups": True,
        },
        "review": {
            "sweep": {
                "axes": ["quality", "max_height", "audio_bitrate"],
                "custom_axes": "encode-profile dotted paths",
                "variants": True,
                "single_job": True,
            },
            "clip_plan": {
                "target_seconds": core.DEFAULT_REVIEW_CLIP_TARGET_SECONDS,
                "min_seconds": core.DEFAULT_REVIEW_CLIP_MIN_SECONDS,
                "max_seconds": core.DEFAULT_REVIEW_CLIP_MAX_SECONDS,
            },
        },
        "storage": {
            "same_filesystem_hardlink_discount": core.path_device(core.TUSD_DIR)
            == core.path_device(core.GPU_RUNTIME_DIR),
            "max_active_input_uploads": core.MAX_ACTIVE_INPUT_UPLOADS,
            "max_running_jobs": core.MAX_RUNNING_JOBS,
            "eager_archive_only_encoding": True,
            "eager_archive_batch_files": core.EAGER_ARCHIVE_BATCH_FILES,
            "eager_archive_pipeline_batches": core.EAGER_ARCHIVE_PIPELINE_BATCHES,
            "storage_wait_seconds": core.STORAGE_WAIT_SECONDS,
            "scratch_extra_multipliers": {
                "review": core.REVIEW_SCRATCH_EXTRA_MULTIPLIER,
                "buffered_handoff": core.BUFFERED_HANDOFF_SCRATCH_EXTRA_MULTIPLIER,
                "handoff.riverhog": core.GPU_SCRATCH_MULTIPLIER,
            },
        },
        "events": {
            "format": "CloudEvents 1.0",
            "cursor_log": True,
            "operation_context_max_bytes": 4096,
            "operation_context_retention_seconds": int(
                core.EVENT_CONTEXT_RETENTION.total_seconds()
            ),
        },
        "operations": {
            "submit": True,
            "preflight_submission": True,
            "cancel_submission": True,
            "cancel_job": True,
            "list_jobs": True,
            "compact_job_status": True,
            "record_preflight_failure": True,
            "pause_scheduler": True,
            "resume_scheduler": True,
        },
    }


def _tail_truncate(value: str, limit: int) -> str:
    normalized = " ".join(str(value).split())
    if len(normalized) <= limit:
        return normalized
    if limit <= 3:
        return normalized[-limit:]
    return "..." + normalized[-(limit - 3) :]


def _head_truncate(value: str, limit: int) -> str:
    normalized = " ".join(str(value).split())
    if len(normalized) <= limit:
        return normalized
    if limit <= 3:
        return normalized[:limit]
    return f"{normalized[: limit - 3].rstrip()}..."


def preflight_issue_event_error(
    *,
    path: str,
    issue_message: str,
    limit: int = 120,
    filename_limit: int = 48,
) -> str:
    filename = path.rstrip("/").rsplit("/", 1)[-1] or path
    suffix = f" ({_tail_truncate(filename, filename_limit)})"
    issue_limit = max(1, limit - len(suffix))
    return f"{_head_truncate(issue_message, issue_limit)}{suffix}"


@app.get("/v1/events")
def list_lifecycle_events(
    request: Request,
    after: str | None = None,
    limit: int = Query(default=100, ge=1, le=100),
) -> dict[str, Any]:
    try:
        principal = request_principal(request)
        page = core.lifecycle_event_log().page(
            after=after,
            limit=limit,
            owner=None if principal.allows(EVENTS_READ_ALL) else principal.app,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return page.model_dump(mode="json")


@app.post("/v1/preflight-failures", status_code=202)
def record_preflight_failure(
    req: core.ClientPreflightFailureRequest,
    request: Request,
) -> dict[str, Any]:
    principal = request_principal(request)
    first_issue = ""
    if req.failed_files and req.failed_files[0].issues:
        first = req.failed_files[0]
        first_issue = preflight_issue_event_error(
            path=first.path,
            issue_message=first.issues[0].message,
        )
    data: dict[str, Any] = {
        "component": "preflight",
        "error": first_issue or req.message,
        "client_source": req.source,
        "template_id": req.template_id,
        "workflow_mode": req.workflow_mode,
        "group": req.group,
        "run_id": req.run_id or "",
        "route_id": req.route_id or "",
        "profile_id": req.profile_id or "",
        "input_upload_id": req.input_upload_id or "",
        "files": req.files,
        "failed_file_count": req.failed_file_count,
        "failed_files": [
            {
                "path": item.path,
                "source": item.source,
                "issues": [issue.model_dump() for issue in item.issues],
            }
            for item in req.failed_files[:20]
        ],
        "actor": {"app": "munchy"},
        "initiator": {"app": principal.app, "key_id": principal.key_id},
    }
    if req.elapsed_seconds is not None:
        data["elapsed_seconds"] = req.elapsed_seconds
    subject = req.job_id or req.run_id
    event = cloud_event(
        source=core.EVENT_SOURCE,
        type="io.riverhog.munchy.submission.preflight_failed",
        subject=subject,
        data=data,
    )
    cursor = core.lifecycle_event_log().append(
        event,
        owner=principal.app,
        context=normalize_event_context(req.event_context),
        context_expires_at=core.event_context_expiry() if req.event_context is not None else None,
    )
    return {"status": "recorded", "cursor": cursor, "event_id": event.id}


@app.post("/v1/admin/job-templates/validate")
def validate_job_template(req: core.JobTemplateCreateRequest) -> dict[str, Any]:
    definition, resolved_job, digest = core.validated_job_template_definition(req.definition)
    return {
        "template_id": req.template_id,
        "valid": True,
        "digest": digest,
        "definition": definition,
        "resolved_job": resolved_job,
    }


@app.get("/v1/admin/apps")
def list_applications(
    page: int = Query(1, ge=1),
    per_page: int = Query(25, ge=1, le=100),
    sort: str = "name",
    order: str = "asc",
    q: str | None = None,
    active: bool | None = None,
    all_items: bool = Query(False, alias="all"),
) -> dict[str, object]:
    try:
        return core.application_keys().list_apps(
            page=page,
            per_page=per_page,
            q=q,
            sort=sort,
            order=order,
            active=active,
            all_items=all_items,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/v1/admin/apps/{app_name}/keys", status_code=201)
def create_application_key(
    app_name: str,
    req: core.CreateApplicationKeyRequest,
) -> dict[str, object]:
    try:
        return core.application_keys().create(
            app=app_name,
            permissions=req.permissions,
            expires_in=(
                timedelta(seconds=req.expires_in_seconds)
                if req.expires_in_seconds is not None
                else None
            ),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/v1/admin/apps/{app_name}/keys")
def list_application_keys(
    app_name: str,
    page: int = Query(1, ge=1),
    per_page: int = Query(25, ge=1, le=100),
    sort: str = "created_at",
    order: str = "desc",
    q: str | None = None,
    active: bool | None = None,
    all_items: bool = Query(False, alias="all"),
) -> dict[str, object]:
    try:
        return core.application_keys().list_keys(
            app=app_name,
            page=page,
            per_page=per_page,
            q=q,
            sort=sort,
            order=order,
            active=active,
            all_items=all_items,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/v1/admin/apps/{app_name}/keys/{key_id}/revoke")
def revoke_application_key(app_name: str, key_id: str) -> dict[str, object]:
    try:
        return core.application_keys().revoke(app=app_name, key_id=key_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=exc.args[0]) from exc


@app.get("/v1/admin/job-templates")
def list_job_templates(
    page: int = 1,
    per_page: int = 25,
    sort: str = "template_id",
    order: str = "asc",
    q: str | None = None,
    query: str | None = None,
    enabled: bool | None = None,
    all_items: bool = Query(False, alias="all"),
) -> dict[str, Any]:
    return core.list_job_templates_page(
        page=page,
        per_page=per_page,
        sort=sort,
        order=order,
        query=q if q is not None else query,
        enabled=enabled,
        all_items=all_items is True,
    )


@app.post("/v1/admin/job-templates", status_code=201)
def create_job_template(req: core.JobTemplateCreateRequest) -> dict[str, Any]:
    return core.create_job_template_record(req)


@app.get("/v1/admin/job-templates/{template_id}")
def get_job_template(template_id: str) -> dict[str, Any]:
    return core.load_job_template(template_id)


@app.put("/v1/admin/job-templates/{template_id}")
def replace_job_template(
    template_id: str,
    req: core.JobTemplateReplaceRequest,
) -> dict[str, Any]:
    return core.replace_job_template_record(template_id, req)


@app.post("/v1/admin/job-templates/{template_id}/enable")
def enable_job_template(template_id: str, req: core.JobTemplateEnabledRequest) -> dict[str, Any]:
    return core.set_job_template_enabled_record(
        template_id,
        enabled=True,
        expected_revision=req.expected_revision,
    )


@app.post("/v1/admin/job-templates/{template_id}/disable")
def disable_job_template(template_id: str, req: core.JobTemplateEnabledRequest) -> dict[str, Any]:
    return core.set_job_template_enabled_record(
        template_id,
        enabled=False,
        expected_revision=req.expected_revision,
    )


@app.delete("/v1/admin/job-templates/{template_id}")
def delete_job_template(template_id: str, expected_revision: int) -> dict[str, Any]:
    if expected_revision < 1:
        raise HTTPException(status_code=400, detail="expected_revision must be >= 1")
    return core.delete_job_template_record(template_id, expected_revision=expected_revision)


@app.post("/v1/submissions/preflight")
def preflight_submission(req: core.SubmissionSpec) -> dict[str, Any]:
    provisional_id = f"preflight-{uuid.uuid4().hex}"
    template, job_request, storage_hint = core.resolved_submission(
        req,
        submission_id=provisional_id,
    )
    core.require_input_upload_capacity(req.files, storage_hint)
    return {
        "accepted": True,
        "template_id": template["template_id"],
        "template_revision": template["revision"],
        "template_digest": template["digest"],
        "workflow_mode": job_request.workflow_mode,
        "files_total": len(req.files),
        "bytes_total": sum(item.bytes for item in req.files),
        "storage_hint": storage_hint.model_dump(exclude_none=True),
        "content_inspection": "after_upload",
    }


@app.post("/v1/submissions", status_code=202)
def create_submission(
    req: core.CreateSubmissionRequest,
    background_tasks: BackgroundTasks,
    request: Request,
) -> dict[str, Any]:
    principal = request_principal(request)
    with core.state_lock:
        job, created = core.create_submission_state(req, initiator=principal)
    if created:
        core.emit_job_event(job, "job.received", "Munchy submission received.")
    core.schedule_pending_jobs(background_tasks)
    return core.submission_response(job)


@app.get("/v1/submissions/{submission_id}")
def get_submission(submission_id: str) -> dict[str, Any]:
    return core.submission_response(core.load_submission(submission_id))


@app.post(
    "/v1/submissions/{submission_id}/files/{rel_path:path}/upload",
    status_code=201,
)
def create_or_resume_submission_file_upload(
    submission_id: str,
    rel_path: str,
) -> dict[str, Any]:
    core.load_submission(submission_id)
    with core.input_file_upload_setup_lock(submission_id, rel_path):
        return core._create_or_resume_input_file_upload(submission_id, rel_path)


@app.delete("/v1/submissions/{submission_id}", status_code=202)
def cancel_submission(submission_id: str) -> dict[str, Any]:
    core.load_submission(submission_id)
    cancel_job(submission_id, cleanup=True)
    return core.submission_response(core.load_submission(submission_id))


@app.get("/v1/admin/scheduler")
def scheduler_status() -> dict[str, Any]:
    control = core.scheduler_control()
    return {
        **control,
        "active_jobs": sorted(core.active_jobs),
        "scheduled_jobs": sorted(core.scheduled_jobs),
        "running_job_limit": core.MAX_RUNNING_JOBS,
        "running_job_slots_available": core.running_job_slots_available(),
        "runnable_jobs": [
            str(job["job_id"]) for job in core.job_states() if core.runnable_job(job)
        ],
    }


@app.post("/v1/admin/scheduler/pause")
def pause_scheduler() -> dict[str, Any]:
    return core.set_scheduling_paused(True)


@app.post("/v1/admin/scheduler/resume")
def resume_scheduler(background_tasks: BackgroundTasks) -> dict[str, Any]:
    control = core.set_scheduling_paused(False)
    scheduled = core.schedule_pending_jobs(background_tasks)
    return {**control, "scheduled_jobs": scheduled}


@app.post("/internal/tusd/hooks")
async def tusd_hooks(request: Request) -> JSONResponse:
    if (
        core.TUSD_HOOK_SECRET
        and request.headers.get("X-Munchy-Tusd-Hook-Secret") != core.TUSD_HOOK_SECRET
    ):
        return hook_error("invalid hook secret", status_code=403)
    payload = await request.json()
    event = payload.get("Event", {})
    upload = event.get("Upload", {}) if isinstance(event, dict) else {}
    metadata = upload.get("MetaData", {}) if isinstance(upload, dict) else {}
    if payload.get("Type") == "post-finish":
        target_path = str(metadata.get("target_path", "")).lstrip("/")
        upload_id = core.upload_id_from_target_path(target_path)
        rel_path = core.rel_path_from_target_path(target_path)
        if upload_id and rel_path:
            try:
                core.sync_shared_input_file(upload_id, rel_path)
            except Exception:
                core.log.exception("failed to sync shared input file after tusd post-finish")
        return JSONResponse({})
    if payload.get("Type") != "pre-create":
        return JSONResponse({})
    target_path = str(metadata.get("target_path", "")).lstrip("/")
    if not target_path:
        return hook_error("missing target_path metadata")
    prefix = ".munchy-server/uploads/"
    if not target_path.startswith(prefix):
        return hook_error("target_path must stay within .munchy-server/uploads/")
    if any(part in {"", ".", ".."} for part in target_path.split("/")):
        return hook_error("target_path must be normalized")
    return JSONResponse(
        {"ChangeFileInfo": {"ID": core.tusd_upload_id_for_target_path(target_path)}}
    )


@app.get("/v1/jobs")
def list_jobs(
    page: int = 1,
    per_page: int = 25,
    sort: str = "updated_at",
    order: str = "desc",
    q: str | None = None,
    query: str | None = None,
    terminal: str = "active",
    state: str | None = None,
    workflow_mode: str | None = None,
    handoff_destination: core.HandoffDestination | None = None,
    cancel_requested: bool | None = None,
    storage_wait: bool | None = None,
    all_items: bool = Query(False, alias="all"),
) -> dict[str, Any]:
    return core.list_job_summaries_page(
        page=page,
        per_page=per_page,
        sort=sort,
        order=order,
        query=q if q is not None else query,
        terminal=terminal,
        state=state,
        workflow_mode=workflow_mode,
        handoff_destination=handoff_destination,
        cancel_requested=cancel_requested,
        storage_wait=storage_wait,
        all_items=all_items is True,
    )


@app.get("/v1/jobs/{job_id}")
def get_job(job_id: str, compact: bool = False) -> dict[str, Any]:
    job = core.load_job(job_id)
    core.refresh_handoff(job)
    return core.compact_job_response(job) if compact else core.job_response(job)


@app.post("/v1/jobs/{job_id}/resume", status_code=202)
def resume_job(job_id: str, background_tasks: BackgroundTasks) -> dict[str, Any]:
    with core.state_lock:
        job = core.load_job(job_id)
        if job.get("state") == "succeeded":
            return job
        preserve_handoff = core.handoff_adapter(job).can_resume(job)
        if preserve_handoff:
            for key in (
                "debug_bundle_created_at",
                "debug_bundle_dir",
                "debug_bundle_reason",
                "terminal_progress",
                "terminal_state_compacted_at",
            ):
                job.pop(key, None)
            job["handoff_resume_preserved_at"] = utc_timestamp_now()
        else:
            core.cancel_handoff(job, reason="job_resume_reset")
            core.reset_resumable_job_runtime_state(job)
        job["state"] = "queued"
        job["phase"] = "queued"
        job.pop("cancel_requested", None)
        job.pop("cancel_requested_at", None)
        job.pop("canceled_at", None)
        job.pop("error", None)
        job.pop("finished_at", None)
        job["_allow_clear_cancel"] = True
        job["_reset_runtime_state"] = not preserve_handoff
        core.save_job(job)
    core.schedule_pending_jobs(background_tasks)
    return job


@app.post("/v1/jobs/{job_id}/cancel", status_code=202)
def cancel_job(job_id: str, cleanup: bool = False) -> dict[str, Any]:
    finalize_now = False
    with core.state_lock:
        job = core.load_job(job_id)
        if job.get("state") in core.TERMINAL_JOB_STATES:
            if cleanup:
                if job.get("state") == "failed":
                    core.write_job_debug_bundle(job, reason="terminal_failed_cleanup")
                core.cancel_handoff(job, reason="terminal_cleanup")
                core.cleanup_terminal_job(job)
                core.compact_terminal_job_state(job)
                return core.compact_job_response(core.save_job(job))
            return core.compact_job_response(job)
        now = utc_timestamp_now()
        job["cancel_requested"] = True
        job["cancel_requested_at"] = now
        job["cleanup_requested"] = True
        if job_id not in core.active_jobs:
            core.scheduled_jobs.discard(job_id)
            job = core.save_job(job)
            finalize_now = True
        else:
            job["phase"] = "cancel_requested"
            return core.save_job(job)
    if finalize_now:
        return core.finalize_canceled_job(job, reason="job_canceled")
    return job


@app.post("/v1/maintenance/cleanup")
def cleanup() -> dict[str, Any]:
    return core.cleanup_once()


def main() -> None:
    uvicorn.run(
        "munchy_api.app:app",
        host=os.getenv("MUNCHY_HOST", "127.0.0.1"),
        port=int(os.getenv("MUNCHY_PORT", "8092")),
        log_level=os.getenv("MUNCHY_UVICORN_LOG_LEVEL", "info"),
        log_config=uvicorn_log_config_without_health_access_logs(),
    )
