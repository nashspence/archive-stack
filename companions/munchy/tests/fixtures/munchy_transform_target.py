"""Independently launched, deterministic test fixture for the Munchy target protocol."""

from __future__ import annotations

import importlib.metadata
import os
import shutil
import threading
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from fastapi import BackgroundTasks, FastAPI, HTTPException
from munchy_target_support.operations import (
    SOURCE_ROLE,
    VIDEO_ARCHIVE_OPERATION,
    VIDEO_ARCHIVE_ROLE,
    operation_contract,
    validate_operation_intent,
)
from munchy_target_support.protocol import (
    Artifact,
    ExecutionToolEvidence,
    JsonSchemaDocument,
    TargetCancelRequest,
    TargetContract,
    TargetContractPayload,
    TargetExecutionEvidence,
    TargetFailure,
    TargetJobRequest,
    TargetJobState,
    TargetJobStatus,
    TargetOperationSupport,
    TargetPreflightRequest,
    TargetPreflightResponse,
    TargetProgress,
    TransformPlan,
    TransformPlanPayload,
    validate_artifacts_against_operation,
)
from munchy_target_support.workspace import (
    publish_file_atomically,
    verify_artifacts,
    workspace_area_root,
    workspace_artifact_path,
)
from time_formats import utc_timestamp_now

ROOT = Path(os.environ["MUNCHY_FIXTURE_ROOT"]).resolve()
OPTIONS_SCHEMA = JsonSchemaDocument.from_schema(
    "fixture.copy.options/v1",
    {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "delay_seconds": {"type": "number", "minimum": 0, "maximum": 30},
        },
    },
)
TARGET = TargetContract.seal(
    TargetContractPayload(
        implementation_id="fixture.copy-target/v1",
        implementation_version="1.0.0",
        source_revision="fixture-v1",
        operations=(
            TargetOperationSupport(
                operation_id=VIDEO_ARCHIVE_OPERATION,
                operation_contract_sha256=operation_contract(
                    VIDEO_ARCHIVE_OPERATION
                ).contract_sha256,
                options_schema=OPTIONS_SCHEMA,
            ),
        ),
    )
)

_lock = threading.RLock()
_statuses: dict[str, TargetJobStatus] = {}
_cancel_events: dict[str, threading.Event] = {}


def _job_root(job_id: str) -> Path:
    return workspace_area_root(ROOT, "jobs", job_id)


def _status_path(job_id: str) -> Path:
    return _job_root(job_id) / "status.json"


def _request_path(job_id: str) -> Path:
    return _job_root(job_id) / "request.json"


def _write_json(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.part")
    try:
        temporary.write_text(value, encoding="utf-8")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _write_status(status: TargetJobStatus) -> None:
    _write_json(
        _status_path(status.job_id),
        status.model_dump_json(by_alias=True, exclude_none=True, indent=2),
    )
    with _lock:
        _statuses[status.job_id] = status


def _load_status(job_id: str) -> TargetJobStatus:
    with _lock:
        status = _statuses.get(job_id)
    if status is not None:
        return status
    path = _status_path(job_id)
    if not path.is_file():
        raise HTTPException(status_code=404, detail=f"unknown job: {job_id}")
    status = TargetJobStatus.model_validate_json(path.read_text(encoding="utf-8"))
    with _lock:
        _statuses[job_id] = status
    return status


def _write_request(request: TargetJobRequest) -> None:
    _write_json(
        _request_path(request.job_id),
        request.model_dump_json(by_alias=True, exclude_none=True, indent=2),
    )


def _load_request(job_id: str) -> TargetJobRequest:
    path = _request_path(job_id)
    if not path.is_file():
        raise HTTPException(status_code=409, detail=f"job request unavailable: {job_id}")
    return TargetJobRequest.model_validate_json(path.read_text(encoding="utf-8"))


def _progress(request: TargetJobRequest, phase: str, completed: int = 0) -> TargetProgress:
    total = sum(artifact.role == SOURCE_ROLE for artifact in request.plan.inputs)
    return TargetProgress(phase=phase, completed=completed, total=total)


def _evidence(request: TargetJobRequest) -> TargetExecutionEvidence:
    return TargetExecutionEvidence(
        target=TARGET,
        operation=operation_contract(request.plan.operation_id),
        effective_intent=request.plan.effective_intent,
        effective_target_options=request.plan.effective_target_options,
        tools=(
            ExecutionToolEvidence(
                name="fixture",
                version=importlib.metadata.version("munchy-target-support"),
            ),
        ),
    )


def _status(
    request: TargetJobRequest,
    state: TargetJobState,
    *,
    completed: int = 0,
    outputs: tuple[Artifact, ...] = (),
    failure: TargetFailure | None = None,
    started_at: str | None = None,
    finished_at: str | None = None,
) -> TargetJobStatus:
    return TargetJobStatus(
        job_id=request.job_id,
        attempt=request.attempt,
        request_sha256=request.request_sha256,
        plan_sha256=request.plan.plan_sha256,
        state=state,
        progress=_progress(request, state, completed),
        outputs=outputs,
        execution_evidence=(
            _evidence(request) if state in {"succeeded", "failed", "canceled"} else None
        ),
        failure=failure,
        started_at=started_at,
        finished_at=finished_at,
        updated_at=utc_timestamp_now(),
    )


def _preflight(request: TargetPreflightRequest) -> TargetPreflightResponse:
    operation = operation_contract(VIDEO_ARCHIVE_OPERATION)
    if request.operation_id != operation.id:
        raise ValueError(f"unsupported operation: {request.operation_id}")
    if request.operation_contract_sha256 != operation.contract_sha256:
        raise ValueError("operation contract digest mismatch")
    if set(request.target_options) - {"delay_seconds"}:
        raise ValueError("unsupported target option")
    delay = request.target_options.get("delay_seconds", 0)
    if not isinstance(delay, int | float) or not 0 <= delay <= 30:
        raise ValueError("delay_seconds must be a number from 0 through 30")
    validate_artifacts_against_operation(operation, inputs=request.inputs)
    verify_artifacts(ROOT, "input", request.workspace_id, request.inputs)
    intent = validate_operation_intent(request.operation_id, request.intent)
    return TargetPreflightResponse(
        target=TARGET,
        plan=TransformPlan.seal(
            TransformPlanPayload(
                operation_id=request.operation_id,
                operation_contract_sha256=request.operation_contract_sha256,
                workspace_id=request.workspace_id,
                inputs=request.inputs,
                intent=request.intent,
                target_options=request.target_options,
                target_implementation_id=TARGET.implementation_id,
                target_contract_sha256=TARGET.contract_sha256,
                effective_intent=intent.model_dump(mode="json", exclude_none=True),
                effective_target_options={"delay_seconds": float(delay)},
            )
        ),
    )


def _run(request: TargetJobRequest, cancel_event: threading.Event) -> None:
    started_at = utc_timestamp_now()
    _write_status(_status(request, "running", started_at=started_at))
    try:
        delay = float(request.plan.effective_target_options.get("delay_seconds") or 0)
        deadline = time.monotonic() + delay
        while time.monotonic() < deadline:
            if cancel_event.wait(min(0.05, deadline - time.monotonic())):
                raise InterruptedError("fixture job canceled")
        inputs = verify_artifacts(ROOT, "input", request.job_id, request.plan.inputs)
        staging = _job_root(request.job_id) / f"attempt-{request.attempt}-output"
        shutil.rmtree(staging, ignore_errors=True)
        staging.mkdir(parents=True)
        shutil.rmtree(workspace_area_root(ROOT, "output", request.job_id), ignore_errors=True)
        outputs: list[Artifact] = []
        for artifact in request.plan.inputs:
            if artifact.role != SOURCE_ROLE:
                continue
            relative = f"fixture/{artifact.path}.fixture"
            staged = staging.joinpath(*Path(relative).parts)
            publish_file_atomically(inputs[artifact.id], staged)
            output = Artifact(
                id=f"fixture-{artifact.id}",
                role=VIDEO_ARCHIVE_ROLE,
                path=relative,
                bytes=artifact.bytes,
                sha256=artifact.sha256,
                media_type="application/octet-stream",
                derived_from=(artifact.id,),
            )
            publish_file_atomically(
                staged,
                workspace_artifact_path(ROOT, "output", request.job_id, relative),
            )
            outputs.append(output)
        result = tuple(outputs)
        validate_artifacts_against_operation(
            operation_contract(request.plan.operation_id),
            inputs=request.plan.inputs,
            outputs=result,
        )
        verify_artifacts(ROOT, "output", request.job_id, result)
        if cancel_event.is_set():
            raise InterruptedError("fixture job canceled")
        _write_status(
            _status(
                request,
                "succeeded",
                completed=len(result),
                outputs=result,
                started_at=started_at,
                finished_at=utc_timestamp_now(),
            )
        )
    except InterruptedError as exc:
        _write_status(
            _status(
                request,
                "canceled",
                failure=TargetFailure(
                    code="job_canceled",
                    message=str(exc),
                    retryable=False,
                ),
                started_at=started_at,
                finished_at=utc_timestamp_now(),
            )
        )
    except Exception as exc:
        _write_status(
            _status(
                request,
                "failed",
                failure=TargetFailure(
                    code="fixture_execution_failed",
                    message=str(exc),
                    retryable=False,
                ),
                started_at=started_at,
                finished_at=utc_timestamp_now(),
            )
        )
    finally:
        with _lock:
            _cancel_events.pop(request.job_id, None)


def _recover() -> None:
    jobs = ROOT / "jobs"
    if not jobs.is_dir():
        return
    for path in jobs.glob("*/status.json"):
        status = TargetJobStatus.model_validate_json(path.read_text(encoding="utf-8"))
        if status.state in {"queued", "running", "canceling"}:
            status = status.model_copy(
                update={
                    "state": "interrupted",
                    "progress": status.progress.model_copy(update={"phase": "interrupted"}),
                    "updated_at": utc_timestamp_now(),
                }
            )
            _write_status(status)


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    _recover()
    yield


app = FastAPI(lifespan=lifespan)


@app.get("/v1/target", response_model=TargetContract)
def target() -> TargetContract:
    return TARGET


@app.post("/v1/preflight", response_model=TargetPreflightResponse)
def preflight(request: TargetPreflightRequest) -> TargetPreflightResponse:
    try:
        return _preflight(request)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.put("/v1/jobs/{job_id}", response_model=TargetJobStatus, status_code=202)
def put_job(
    job_id: str,
    request: TargetJobRequest,
    background_tasks: BackgroundTasks,
) -> TargetJobStatus:
    if job_id != request.job_id:
        raise HTTPException(status_code=409, detail="job path ID does not match request job_id")
    try:
        accepted = _preflight(
            TargetPreflightRequest(
                operation_id=request.plan.operation_id,
                operation_contract_sha256=request.plan.operation_contract_sha256,
                workspace_id=request.plan.workspace_id,
                inputs=request.plan.inputs,
                intent=request.plan.intent,
                target_options=request.plan.target_options,
            )
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if accepted.plan != request.plan:
        raise HTTPException(status_code=409, detail="job plan does not match fixture preflight")
    try:
        existing = _load_status(job_id)
    except HTTPException as exc:
        if exc.status_code != 404:
            raise
        existing = None
    if existing is not None:
        if existing.request_sha256 == request.request_sha256:
            return existing
        if not (
            existing.state == "interrupted"
            and request.attempt == existing.attempt + 1
            and request.plan.plan_sha256 == existing.plan_sha256
        ):
            raise HTTPException(status_code=409, detail="job ID is bound to another request")
    elif request.attempt != 1:
        raise HTTPException(status_code=409, detail="a new job must begin with attempt 1")
    queued = _status(request, "queued")
    _write_request(request)
    _write_status(queued)
    cancel_event = threading.Event()
    with _lock:
        _cancel_events[job_id] = cancel_event
    background_tasks.add_task(_run, request, cancel_event)
    return queued


@app.get("/v1/jobs/{job_id}", response_model=TargetJobStatus)
def status(job_id: str) -> TargetJobStatus:
    return _load_status(job_id)


@app.post("/v1/jobs/{job_id}/cancel", response_model=TargetJobStatus, status_code=202)
def cancel(job_id: str, request: TargetCancelRequest) -> TargetJobStatus:
    with _lock:
        current = _load_status(job_id)
        if current.state in {"succeeded", "failed", "canceled"}:
            return current
        event = _cancel_events.get(job_id)
        if event is not None:
            event.set()
        if current.state == "interrupted":
            accepted = _load_request(job_id)
            canceled = _status(
                accepted,
                "canceled",
                failure=TargetFailure(
                    code="job_canceled",
                    message=request.reason,
                    retryable=False,
                ),
                started_at=current.started_at,
                finished_at=utc_timestamp_now(),
            )
            _write_status(canceled)
            return canceled
        canceling = current.model_copy(
            update={
                "state": "canceling",
                "progress": current.progress.model_copy(update={"phase": "canceling"}),
                "updated_at": utc_timestamp_now(),
            }
        )
        _write_status(canceling)
        return canceling


if __name__ == "__main__":
    uvicorn.run(
        app,
        host="127.0.0.1",
        port=int(os.environ["MUNCHY_FIXTURE_PORT"]),
        log_level="warning",
    )
