from __future__ import annotations

import argparse
import importlib.metadata
import json
import logging
import os
import secrets
import sys
import threading
import uuid
from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import asynccontextmanager
from datetime import timedelta
from typing import Annotated, Any

import munchy_core.domain.errors as domain_errors
import munchy_core.domain.models as domain_models
import munchy_core.persistence as persistence
import munchy_core.persistence.lifecycle_events as lifecycle_store
import munchy_core.persistence.sqlite_state as state_store
import munchy_core.ports.handoff as handoff_port
import munchy_core.runtime.config as runtime_config
import munchy_core.runtime.execution as execution_runtime
import munchy_core.services.admission as admission_service
import munchy_core.services.cleanup as cleanup_service
import munchy_core.services.diagnostics as diagnostic_service
import munchy_core.services.events as event_service
import munchy_core.services.handoffs as handoff_service
import munchy_core.services.jobs as job_service
import munchy_core.services.processing as processing_service
import munchy_core.services.retention as retention_service
import munchy_core.services.scheduling as scheduling_service
import munchy_core.services.templates as template_service
import munchy_core.services.uploads as upload_service
import uvicorn
from fastapi import BackgroundTasks, FastAPI, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.routing import APIRoute
from http_api_contracts import (
    ErrorResponse,
    HealthResponse,
    apply_openapi_error_contract,
    error_code_for_status,
    error_payload,
    error_responses,
    status_for_error_code,
)
from lifecycle_events import cloud_event, normalize_event_context
from munchy_core.domain.errors import ServiceError
from munchy_core.persistence.application_keys import (
    EVENTS_READ,
    EVENTS_READ_ALL,
    SUBMISSIONS_MANAGE,
    MunchyPrincipal,
)
from munchy_core.persistence.template_registry import (
    TemplateRegistryError,
    validate_template_registry,
)
from munchy_target_support.uvicorn_logging import uvicorn_log_config_without_health_access_logs
from starlette.exceptions import HTTPException as StarletteHTTPException
from state_schema import StateSchemaError
from time_formats import utc_timestamp_now

from munchy_api.composition import configure_adapters
from munchy_api.schemas import (
    AppKeyPageResponse,
    AppPageResponse,
    JobDiagnosticPageResponse,
    JobPageResponse,
    JobTemplatePageResponse,
)

log = logging.getLogger("munchy.server")
adapters = configure_adapters()


def _operation_id(route: APIRoute) -> str:
    return route.name


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    upload_service.ensure_dirs()
    persistence.validate_persistence()
    if runtime_config.RESUME_ON_START:
        job_service.schedule_pending_jobs()
    if runtime_config.CLEANUP_INTERVAL_SECONDS > 0:
        execution_runtime.cleanup_stop.clear()
        execution_runtime.cleanup_thread = threading.Thread(
            target=job_service.cleanup_loop, name="cleanup-loop", daemon=True
        )
        execution_runtime.cleanup_thread.start()
    if any(adapter.supports_eager for adapter in handoff_port.HANDOFF_ADAPTERS.values()):
        execution_runtime.handoff_stop.clear()
        execution_runtime.handoff_thread = threading.Thread(
            target=handoff_service.handoff_loop,
            name="handoff-loop",
            daemon=True,
        )
        execution_runtime.handoff_thread.start()
    for adapter in handoff_port.HANDOFF_ADAPTERS.values():
        adapter.start()
    try:
        yield
    finally:
        for adapter in reversed(tuple(handoff_port.HANDOFF_ADAPTERS.values())):
            adapter.stop()
        execution_runtime.handoff_stop.set()
        if execution_runtime.handoff_thread is not None:
            execution_runtime.handoff_thread.join(timeout=5)
        execution_runtime.cleanup_stop.set()
        if execution_runtime.cleanup_thread is not None:
            execution_runtime.cleanup_thread.join(timeout=5)


app = FastAPI(
    title="munchy-server",
    version=importlib.metadata.version("munchy-server"),
    lifespan=lifespan,
    generate_unique_id_function=_operation_id,
)


def api_error_response(
    status_code: int,
    *,
    message: str,
    code: str | None = None,
    details: Mapping[str, Any] | None = None,
    headers: Mapping[str, str] | None = None,
) -> JSONResponse:
    resolved_code = code or error_code_for_status(status_code)
    return JSONResponse(
        status_code=status_for_error_code(resolved_code, fallback=status_code),
        content=error_payload(code=resolved_code, message=message, details=details),
        headers=dict(headers or {}),
    )


@app.exception_handler(ServiceError)
async def service_error_handler(_request: Request, exc: ServiceError) -> JSONResponse:
    if isinstance(exc.detail, Mapping):
        details = dict(exc.detail)
        code = str(details.pop("error", "") or error_code_for_status(exc.status_code))
        message = str(details.pop("message", "") or code.replace("_", " "))
        return api_error_response(
            exc.status_code,
            code=code,
            message=message,
            details=details,
        )
    return api_error_response(exc.status_code, message=str(exc.detail))


@app.exception_handler(RequestValidationError)
async def validation_error_handler(_request: Request, exc: RequestValidationError) -> JSONResponse:
    first = exc.errors()[0]
    location = ".".join(str(item) for item in first["loc"] if item not in {"body", "query"})
    message = str(first["msg"])
    if location:
        message = f"{location} {message}"
    return api_error_response(400, code="bad_request", message=message)


@app.exception_handler(StarletteHTTPException)
async def http_error_handler(_request: Request, exc: StarletteHTTPException) -> JSONResponse:
    return api_error_response(
        exc.status_code,
        message=str(exc.detail),
        headers=exc.headers,
    )


@app.exception_handler(Exception)
async def unexpected_error_handler(_request: Request, exc: Exception) -> JSONResponse:
    log.error("unhandled Munchy API error", exc_info=exc)
    return api_error_response(500, code="internal_error", message="internal server error")


def request_bearer_token(request: Request) -> str:
    raw = request.headers.get("authorization", "")
    scheme, _, token = raw.partition(" ")
    return token if scheme.casefold() == "bearer" else ""


def authorized_admin_bearer(request: Request) -> bool:
    if not runtime_config.ADMIN_TOKEN:
        return not runtime_config.APPLICATION_AUTH_REQUIRED
    return secrets.compare_digest(request_bearer_token(request), runtime_config.ADMIN_TOKEN)


def request_principal(request: Request) -> MunchyPrincipal:
    principal = getattr(request.state, "principal", None)
    if not isinstance(principal, MunchyPrincipal):
        raise HTTPException(status_code=401, detail="invalid application token")
    return principal


@app.middleware("http")
async def require_api_auth(request: Request, call_next):  # type: ignore[no-untyped-def]
    if request.url.path.startswith("/v1/admin/"):
        if not authorized_admin_bearer(request):
            return api_error_response(
                401,
                message="invalid admin token",
                headers={"WWW-Authenticate": "Bearer"},
            )
    elif request.url.path.startswith("/v1/"):
        if runtime_config.APPLICATION_AUTH_REQUIRED:
            principal = state_store.application_keys().authenticate(request_bearer_token(request))
            if principal is None:
                return api_error_response(
                    401,
                    message="invalid application token",
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
            return api_error_response(
                403,
                message=f"application permission required: {required_permission}",
            )
        request.state.principal = principal
    return await call_next(request)


@app.exception_handler(domain_errors.InsufficientStorage)
async def insufficient_storage_handler(
    _request: Request, exc: domain_errors.InsufficientStorage
) -> JSONResponse:
    return api_error_response(
        507,
        code="insufficient_storage",
        message=str(exc),
        details={
            "label": exc.label,
            "required_bytes": exc.required_bytes,
            "free_bytes": exc.free_bytes,
            "reserved_bytes": exc.reserved_bytes,
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


@app.get("/health/live", response_model=HealthResponse, tags=["health"])
def health_live() -> dict[str, str]:
    return {"service": "munchy", "status": "ok"}


@app.get(
    "/health/ready",
    response_model=HealthResponse,
    responses={503: {"model": ErrorResponse}},
    tags=["health"],
)
def health_ready() -> dict[str, str]:
    try:
        validate_template_registry(runtime_config.STATE_DB_PATH)
    except TemplateRegistryError as exc:
        raise HTTPException(
            status_code=503,
            detail="job template registry does not satisfy the current contract",
        ) from exc
    return {"service": "munchy", "status": "ok"}


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
        page = lifecycle_store.lifecycle_event_log().page(
            after=after,
            limit=limit,
            owner=None if principal.allows(EVENTS_READ_ALL) else principal.app,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return page.model_dump(mode="json")


@app.post("/v1/submissions/preflight-failures", status_code=202)
def record_submission_preflight_failure(
    req: domain_models.SubmissionPreflightFailureCreate,
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
        "submission_id": req.submission_id,
        "template_id": req.template_id,
        "workflow_mode": req.workflow_mode,
        "group": req.group,
        "run_id": req.run_id or "",
        "route_id": req.route_id or "",
        "profile_id": req.profile_id or "",
        "files_total": req.files_total,
        "failed_files_total": req.failed_files_total,
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
    event = cloud_event(
        source=runtime_config.EVENT_SOURCE,
        type="io.riverhog.munchy.submission.preflight_failed",
        subject=req.submission_id,
        data=data,
    )
    cursor = lifecycle_store.lifecycle_event_log().append(
        event,
        owner=principal.app,
        context=normalize_event_context(req.event_context),
        context_expires_at=event_service.event_context_expiry()
        if req.event_context is not None
        else None,
    )
    return {"status": "recorded", "cursor": cursor, "event_id": event.id}


@app.post("/v1/admin/job-templates/validate")
def validate_job_template(req: domain_models.JobTemplateCreateRequest) -> dict[str, Any]:
    definition, resolved_job, digest = template_service.validated_job_template_definition(
        req.definition
    )
    return {
        "template_id": req.template_id,
        "valid": True,
        "digest": digest,
        "definition": definition,
        "resolved_job": resolved_job,
    }


@app.get("/v1/admin/apps", response_model=AppPageResponse)
def list_apps(
    page: int = Query(1, ge=1),
    per_page: int = Query(25, ge=1, le=100),
    sort: str = "name",
    order: str = "asc",
    q: str | None = None,
    active: bool | None = None,
    all_items: bool = Query(False, alias="all"),
) -> dict[str, object]:
    try:
        return state_store.application_keys().list_apps(
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
def create_app_key(
    app_name: str,
    req: domain_models.CreateApplicationKeyRequest,
) -> dict[str, object]:
    try:
        return state_store.application_keys().create(
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


@app.get("/v1/admin/apps/{app_name}/keys", response_model=AppKeyPageResponse)
def list_app_keys(
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
        return state_store.application_keys().list_keys(
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
def revoke_app_key(app_name: str, key_id: str) -> dict[str, object]:
    try:
        return state_store.application_keys().revoke(app=app_name, key_id=key_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=exc.args[0]) from exc


@app.get("/v1/admin/job-templates", response_model=JobTemplatePageResponse)
def list_job_templates(
    page: Annotated[int, Query(ge=1)] = 1,
    per_page: Annotated[int, Query(ge=1, le=100)] = 25,
    sort: str = "template_id",
    order: str = "asc",
    q: str | None = None,
    enabled: bool | None = None,
    all_items: bool = Query(False, alias="all"),
) -> dict[str, Any]:
    return template_service.list_job_templates_page(
        page=page,
        per_page=per_page,
        sort=sort,
        order=order,
        query=q,
        enabled=enabled,
        all_items=all_items is True,
    )


@app.post("/v1/admin/job-templates", status_code=201)
def create_job_template(req: domain_models.JobTemplateCreateRequest) -> dict[str, Any]:
    return template_service.create_job_template_record(req)


@app.get("/v1/admin/job-templates/{template_id}")
def get_job_template(template_id: str) -> dict[str, Any]:
    return template_service.load_job_template(template_id)


@app.put("/v1/admin/job-templates/{template_id}")
def replace_job_template(
    template_id: str,
    req: domain_models.JobTemplateReplaceRequest,
) -> dict[str, Any]:
    return template_service.replace_job_template_record(template_id, req)


@app.post("/v1/admin/job-templates/{template_id}/enable")
def enable_job_template(
    template_id: str, req: domain_models.JobTemplateEnabledRequest
) -> dict[str, Any]:
    return template_service.set_job_template_enabled_record(
        template_id,
        enabled=True,
        expected_revision=req.expected_revision,
    )


@app.post("/v1/admin/job-templates/{template_id}/disable")
def disable_job_template(
    template_id: str, req: domain_models.JobTemplateEnabledRequest
) -> dict[str, Any]:
    return template_service.set_job_template_enabled_record(
        template_id,
        enabled=False,
        expected_revision=req.expected_revision,
    )


@app.delete("/v1/admin/job-templates/{template_id}")
def delete_job_template(template_id: str, expected_revision: int) -> dict[str, Any]:
    if expected_revision < 1:
        raise HTTPException(status_code=400, detail="expected_revision must be >= 1")
    return template_service.delete_job_template_record(
        template_id, expected_revision=expected_revision
    )


@app.post(
    "/v1/submissions/preflight",
    responses=error_responses(429, 507),
)
def preflight_submission(req: domain_models.SubmissionPreflightRequest) -> dict[str, Any]:
    provisional_id = f"preflight-{uuid.uuid4().hex}"
    template, job_request, storage_hint = job_service.resolved_submission(
        req,
        submission_id=provisional_id,
    )
    admission_service.require_input_upload_capacity(req.files, storage_hint)
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


@app.post(
    "/v1/submissions",
    status_code=202,
    responses=error_responses(429, 507),
)
def create_submission(
    req: domain_models.CreateSubmissionRequest,
    background_tasks: BackgroundTasks,
    request: Request,
) -> dict[str, Any]:
    principal = request_principal(request)
    with execution_runtime.state_lock:
        job, created = job_service.create_submission_state(req, initiator=principal)
    if created:
        event_service.emit_job_event(job, "job.received", "Munchy submission received.")
    job_service.schedule_pending_jobs(background_tasks)
    return job_service.submission_response(job)


@app.get("/v1/submissions/{submission_id}")
def get_submission(submission_id: str) -> dict[str, Any]:
    return job_service.submission_response(job_service.load_submission(submission_id))


@app.post(
    "/v1/submissions/{submission_id}/files/{rel_path:path}/upload",
    status_code=201,
)
def create_or_resume_submission_file_upload(
    submission_id: str,
    rel_path: str,
) -> dict[str, Any]:
    job_service.load_submission(submission_id)
    with execution_runtime.input_file_upload_setup_lock(submission_id, rel_path):
        return job_service._create_or_resume_input_file_upload(submission_id, rel_path)


@app.put("/v1/submissions/{submission_id}/provenance/journals/{journal_id}")
async def put_submission_provenance_journal(
    submission_id: str,
    journal_id: str,
    request: Request,
) -> dict[str, object]:
    job = job_service.load_submission(submission_id)
    digest = request.headers.get("X-Riverhog-Provenance-SHA256", "").strip().lower()
    if len(digest) != 64 or any(ch not in "0123456789abcdef" for ch in digest):
        raise HTTPException(
            status_code=400,
            detail="X-Riverhog-Provenance-SHA256 must be a SHA-256 hex digest",
        )
    return upload_service.put_input_provenance_journal(
        str(job["input_upload_id"]),
        journal_id,
        content=await request.body(),
        sha256=digest,
    )


@app.get("/v1/admin/scheduler")
def get_scheduler_status() -> dict[str, Any]:
    control = scheduling_service.scheduler_control()
    return {
        **control,
        "active_jobs": sorted(execution_runtime.active_jobs),
        "scheduled_jobs": sorted(execution_runtime.scheduled_jobs),
        "running_job_limit": runtime_config.MAX_RUNNING_JOBS,
        "running_job_slots_available": scheduling_service.running_job_slots_available(),
        "runnable_job_count": scheduling_service.runnable_job_count(),
        "runnable_jobs": [
            str(job["job_id"])
            for job in scheduling_service.runnable_jobs_in_order(
                limit=100,
                exclude_claimed=False,
            )
        ],
    }


@app.post("/v1/admin/scheduler/pause")
def pause_scheduler() -> dict[str, Any]:
    return scheduling_service.set_scheduling_paused(True)


@app.post("/v1/admin/scheduler/resume")
def resume_scheduler(background_tasks: BackgroundTasks) -> dict[str, Any]:
    control = scheduling_service.set_scheduling_paused(False)
    scheduled = job_service.schedule_pending_jobs(background_tasks)
    return {**control, "scheduled_jobs": scheduled}


@app.post("/internal/tusd/hooks")
async def tusd_hooks(request: Request) -> JSONResponse:
    if (
        runtime_config.TUSD_HOOK_SECRET
        and request.headers.get("X-Munchy-Tusd-Hook-Secret") != runtime_config.TUSD_HOOK_SECRET
    ):
        return hook_error("invalid hook secret", status_code=403)
    payload = await request.json()
    event = payload.get("Event", {})
    upload = event.get("Upload", {}) if isinstance(event, dict) else {}
    metadata = upload.get("MetaData", {}) if isinstance(upload, dict) else {}
    if payload.get("Type") == "post-finish":
        target_path = str(metadata.get("target_path", "")).lstrip("/")
        upload_id = upload_service.upload_id_from_target_path(target_path)
        rel_path = upload_service.rel_path_from_target_path(target_path)
        if upload_id and rel_path:
            try:
                upload_service.sync_shared_input_file(upload_id, rel_path)
            except Exception:
                log.exception("failed to sync shared input file after tusd post-finish")
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
        {"ChangeFileInfo": {"ID": upload_service.tusd_upload_id_for_target_path(target_path)}}
    )


@app.get("/v1/jobs", response_model=JobPageResponse)
def list_jobs(
    page: Annotated[int, Query(ge=1)] = 1,
    per_page: Annotated[int, Query(ge=1, le=100)] = 25,
    sort: str = "updated_at",
    order: str = "desc",
    q: str | None = None,
    terminal: str = "active",
    state: str | None = None,
    workflow_mode: str | None = None,
    handoff_destination: domain_models.HandoffDestination | None = None,
    cancel_requested: bool | None = None,
    storage_wait: bool | None = None,
    all_items: bool = Query(False, alias="all"),
) -> dict[str, Any]:
    return job_service.list_job_summaries_page(
        page=page,
        per_page=per_page,
        sort=sort,
        order=order,
        query=q,
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
    job = state_store.load_job(job_id)
    if compact:
        return processing_service.compact_job_response(
            job,
            refresh_progress=False,
        )
    handoff_service.refresh_handoff(job)
    return processing_service.job_response(job)


@app.get("/v1/admin/job-diagnostics", response_model=JobDiagnosticPageResponse)
def list_job_diagnostics(
    page: Annotated[int, Query(ge=1)] = 1,
    per_page: Annotated[int, Query(ge=1, le=100)] = 25,
    sort: str = "created_at",
    order: str = "desc",
    q: str | None = None,
    all_items: bool = Query(False, alias="all"),
) -> dict[str, Any]:
    return diagnostic_service.list_job_diagnostics_page(
        page=page,
        per_page=per_page,
        sort=sort,
        order=order,
        query=q,
        all_items=all_items is True,
    )


@app.get("/v1/admin/jobs/{job_id}/diagnostic")
def get_job_diagnostic(job_id: str) -> dict[str, Any]:
    return diagnostic_service.get_job_diagnostic(job_id)


@app.get("/v1/admin/jobs/{job_id}/diagnostic/content", response_class=StreamingResponse)
def download_job_diagnostic(job_id: str) -> StreamingResponse:
    chunks, diagnostic = diagnostic_service.job_diagnostic_content(job_id)
    return StreamingResponse(
        chunks,
        media_type="application/gzip",
        headers={
            "Content-Length": str(diagnostic["bytes"]),
            "ETag": f'"{diagnostic["sha256"]}"',
        },
    )


@app.delete("/v1/admin/jobs/{job_id}/diagnostic")
def remove_job_diagnostic(job_id: str) -> dict[str, Any]:
    diagnostic = diagnostic_service.remove_job_diagnostic(job_id)
    assert diagnostic is not None
    return {**diagnostic, "removed": True}


@app.delete("/v1/admin/jobs/{job_id}")
def remove_terminal_job(job_id: str) -> dict[str, Any]:
    return diagnostic_service.remove_terminal_job(job_id)


@app.get("/v1/admin/maintenance/retention")
def get_retention_plan() -> dict[str, Any]:
    return retention_service.retention_plan()


@app.post("/v1/admin/maintenance/retention")
def apply_retention() -> dict[str, Any]:
    return retention_service.apply_retention()


@app.post("/v1/jobs/{job_id}/resume", status_code=202)
def resume_job(job_id: str, background_tasks: BackgroundTasks) -> dict[str, Any]:
    with execution_runtime.state_lock:
        job = state_store.load_job(job_id)
        if job.get("state") == "succeeded":
            return job
        checkpoint_ready = handoff_service.handoff_source_ready(job)
        retained_handoff = handoff_service.retained_handoff_sources_available(job)
        preserve_handoff = checkpoint_ready and handoff_service.handoff_adapter(job).can_resume(job)
        input_upload_id = str(job.get("input_upload_id") or "")
        retained_input = bool(
            input_upload_id and state_store.read_state("input-upload", input_upload_id) is not None
        )
        if not preserve_handoff and not retained_handoff and not retained_input:
            raise ServiceError(
                status_code=409,
                detail="job retained work is no longer available; submit the source again",
            )
        diagnostic_service.remove_job_diagnostic(job_id, missing_ok=True)
        if preserve_handoff:
            for key in (
                "terminal_progress",
                "terminal_state_compacted_at",
            ):
                job.pop(key, None)
            job["handoff_resume_preserved_at"] = utc_timestamp_now()
        else:
            handoff_service.cancel_handoff(job, reason="job_resume_reset")
            cleanup_service.reset_resumable_job_runtime_state(job)
            if not retained_handoff:
                job.pop("handoff_checkpoint", None)
        configured_handoff = handoff_service.handoff_config(job)
        configured_handoff["state"] = "pending"
        configured_handoff["safe_to_delete"] = False
        job["state"] = "queued"
        job["phase"] = "queued"
        job.pop("cancel_requested", None)
        job.pop("cancel_requested_at", None)
        job.pop("canceled_at", None)
        job.pop("error", None)
        job.pop("finished_at", None)
        job["_allow_clear_cancel"] = True
        job["_reset_runtime_state"] = not preserve_handoff
        state_store.save_job(job)
    job_service.schedule_pending_jobs(background_tasks)
    return job


@app.post("/v1/jobs/{job_id}/cancel", status_code=202)
def cancel_job(job_id: str, cleanup: bool = False) -> dict[str, Any]:
    finalize_now = False
    with execution_runtime.state_lock:
        job = state_store.load_job(job_id)
        if job.get("state") in domain_models.TERMINAL_JOB_STATES:
            if cleanup:
                if job.get("state") == "failed":
                    diagnostic_service.create_job_diagnostic(
                        job,
                        reason="terminal_failed_cleanup",
                    )
                handoff_service.cancel_handoff(job, reason="terminal_cleanup")
                cleanup_service.cleanup_terminal_job(job)
                cleanup_service.compact_terminal_job_state(job)
                return processing_service.compact_job_response(state_store.save_job(job))
            return processing_service.compact_job_response(job)
        now = utc_timestamp_now()
        job["cancel_requested"] = True
        job["cancel_requested_at"] = now
        job["cleanup_requested"] = True
        if job_id not in execution_runtime.active_jobs:
            execution_runtime.scheduled_jobs.discard(job_id)
            job = state_store.save_job(job)
            finalize_now = True
        else:
            job["phase"] = "cancel_requested"
            return state_store.save_job(job)
    if finalize_now:
        return cleanup_service.finalize_canceled_job(job, reason="job_canceled")
    return job


app.openapi_schema = apply_openapi_error_contract(app.openapi())


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="munchy-server",
        description="Run the Munchy workflow server.",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=importlib.metadata.version("munchy-server"),
    )
    subparsers = parser.add_subparsers(dest="command")
    state = subparsers.add_parser("state", help="inspect or upgrade Munchy state")
    state_subparsers = state.add_subparsers(dest="state_command", required=True)
    for command_name, help_text in (
        ("status", "show the current and required state revisions"),
        ("upgrade", "explicitly upgrade state to the current revision"),
        ("verify", "verify the current revision and exact state schema"),
    ):
        command_parser = state_subparsers.add_parser(command_name, help=help_text)
        command_parser.add_argument("--json", action="store_true", help="Emit JSON.")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "state":
        schema = persistence.state_schema()
        try:
            if args.state_command == "status":
                status = schema.status()
            elif args.state_command == "upgrade":
                status = schema.upgrade()
            else:
                status = schema.validate()
        except StateSchemaError as exc:
            print(str(exc), file=sys.stderr)
            return 1
        payload = status.as_dict()
        if args.json:
            print(json.dumps(payload, sort_keys=True))
        else:
            print(
                f"munchy state: {payload['condition']} "
                f"({payload['current_revision'] or 'none'} -> {payload['head_revision']})"
            )
        return 0
    uvicorn.run(
        "munchy_api.app:app",
        host=os.getenv("MUNCHY_HOST", "127.0.0.1"),
        port=int(os.getenv("MUNCHY_PORT", "8092")),
        log_level=os.getenv("MUNCHY_UVICORN_LOG_LEVEL", "info"),
        log_config=uvicorn_log_config_without_health_access_logs(),
    )
    return 0
