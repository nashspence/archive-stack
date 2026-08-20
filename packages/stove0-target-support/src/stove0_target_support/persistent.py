"""Reusable restart-safe execution kernel for independently owned targets."""

from __future__ import annotations

import os
import secrets
import subprocess
import threading
from collections.abc import Callable, Mapping
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
from typing import Any, Final

from jsonschema import Draft202012Validator
from stove0_target_protocol import (
    AcceptedTargetJob,
    OperationContract,
    TargetCancelRequest,
    TargetContract,
    TargetFailure,
    TargetInapplicable,
    TargetJobRequest,
    TargetJobStatus,
    TargetPreflightRequest,
    TargetPreflightResponse,
    TargetProgress,
    TransformPlan,
    TransformPlanPayload,
    validate_declaration_against_operation,
)

from stove0_target_support.http_binding import TargetServiceError
from stove0_target_support.jcs import canonical_json_bytes

_ACTIVE_STATES: Final = frozenset({"queued", "running", "canceling"})
_TERMINAL_STATES: Final = frozenset({"inapplicable", "succeeded", "failed", "canceled"})

JobExecutor = Callable[[TargetJobRequest, int, threading.Event], TargetJobStatus]


class TargetExecutionCanceled(RuntimeError):
    """The owning target observed cancellation before publishing output."""


class TargetExecutionInapplicable(RuntimeError):
    """The exact declared input cannot be handled by this target."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class PersistentTargetService:
    """Persist non-secret job identity and converge identical restart requests.

    Capability tokens remain only in the active request closure. Accepted
    declarations and statuses are atomically persisted beneath one target-owned
    state root; an active job found after process loss becomes ``interrupted``
    and only an identical request may resume it.
    """

    def __init__(
        self,
        *,
        contract: TargetContract,
        operations: Mapping[str, OperationContract],
        state_root: Path,
        execute: JobExecutor,
        maximum_workers: int = 1,
    ) -> None:
        self._contract = contract
        self._operations = dict(operations)
        self.state_root = state_root.resolve()
        self.state_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self.state_root, 0o700)
        if self.state_root.is_symlink():
            raise ValueError("target state root must not be a symlink")
        self._execute = execute
        self._pool = ThreadPoolExecutor(
            max_workers=maximum_workers,
            thread_name_prefix="stove0-target",
        )
        self._lock = threading.RLock()
        self._cancel: dict[str, threading.Event] = {}
        self._operator_canceled: set[str] = set()
        self._shutdown_interrupted: set[str] = set()
        self._futures: dict[str, Future[TargetJobStatus]] = {}
        self._recover_interrupted()

    def contract(self) -> TargetContract:
        return self._contract

    def preflight(self, request: TargetPreflightRequest) -> TargetPreflightResponse:
        operation = self._operation(request.operation_id)
        validate_declaration_against_operation(request, operation)
        support = self._contract.support_for(request.operation_id)
        if request.operation_contract_sha256 != support.operation_contract_sha256:
            raise TargetServiceError(409, "operation-contract-mismatch", "operation changed")
        Draft202012Validator(operation.intent_schema.document).validate(request.intent)
        Draft202012Validator(support.options_schema.document).validate(request.target_options)
        plan = TransformPlan.seal(
            TransformPlanPayload(
                operation_id=request.operation_id,
                operation_contract_sha256=request.operation_contract_sha256,
                inputs=request.inputs,
                intent=request.intent,
                target_options=request.target_options,
                target_implementation_id=self._contract.implementation_id,
                target_contract_sha256=self._contract.contract_sha256,
            )
        )
        return TargetPreflightResponse(target=self._contract, plan=plan)

    def put_job(self, request: TargetJobRequest) -> TargetJobStatus:
        job_id = request.declaration.job_id
        if request.declaration.plan.target_contract_sha256 != self._contract.contract_sha256:
            raise TargetServiceError(409, "target-contract-mismatch", "target contract changed")
        operation = self._operation(request.declaration.plan.operation_id)
        validate_declaration_against_operation(request.declaration.plan, operation)
        with self._lock:
            existing = self._load_accepted(job_id)
            if existing is not None and not secrets.compare_digest(
                existing.request_sha256, request.request_sha256
            ):
                raise TargetServiceError(
                    409,
                    "job-request-mismatch",
                    "target job identity is already bound to another declaration",
                )
            if existing is None:
                self._write_model(self._accepted_path(job_id), request.accepted())
            status = self._load_status(job_id)
            if status is None:
                status = self._status(request, state="queued", attempt=1, phase="queued")
                self._write_status(status)
                self._submit(request, status.attempt)
            elif status.state == "interrupted":
                status = self._status(
                    request,
                    state="queued",
                    attempt=status.attempt + 1,
                    phase="restarting",
                )
                self._write_status(status)
                self._submit(request, status.attempt)
            return status

    def get_job(self, job_id: str) -> TargetJobStatus:
        with self._lock:
            status = self._load_status(job_id)
            if status is None:
                raise TargetServiceError(404, "job-not-found", "target job was not found")
            return status

    def cancel_job(self, job_id: str, request: TargetCancelRequest) -> TargetJobStatus:
        del request
        with self._lock:
            status = self._load_status(job_id)
            if status is None:
                raise TargetServiceError(404, "job-not-found", "target job was not found")
            if status.state in _TERMINAL_STATES:
                return status
            self._operator_canceled.add(job_id)
            self._shutdown_interrupted.discard(job_id)
            self._cancel.setdefault(job_id, threading.Event()).set()
            accepted = self._load_accepted(job_id)
            if accepted is None:
                raise RuntimeError("target job status has no accepted declaration")
            canceling = TargetJobStatus(
                job_id=job_id,
                state="canceling",
                attempt=status.attempt,
                request_sha256=accepted.request_sha256,
                plan_sha256=accepted.declaration.plan.plan_sha256,
                progress=TargetProgress(phase="canceling", completed=0),
            )
            self._write_status(canceling)
            return canceling

    def close(self) -> None:
        with self._lock:
            for job_id, future in self._futures.items():
                if future.done() or job_id in self._operator_canceled:
                    continue
                self._shutdown_interrupted.add(job_id)
                self._cancel.setdefault(job_id, threading.Event()).set()
        self._pool.shutdown(wait=True, cancel_futures=False)

    def _operation(self, operation_id: str) -> OperationContract:
        try:
            return self._operations[operation_id]
        except KeyError as exc:
            raise TargetServiceError(
                400,
                "unsupported-operation",
                f"target does not support operation: {operation_id}",
            ) from exc

    def _submit(self, request: TargetJobRequest, attempt: int) -> None:
        job_id = request.declaration.job_id
        active = self._futures.get(job_id)
        if active is not None and not active.done():
            return
        cancellation = self._cancel.setdefault(job_id, threading.Event())
        self._operator_canceled.discard(job_id)
        self._shutdown_interrupted.discard(job_id)
        cancellation.clear()
        self._futures[job_id] = self._pool.submit(self._run, request, attempt, cancellation)

    def _run(
        self,
        request: TargetJobRequest,
        attempt: int,
        cancellation: threading.Event,
    ) -> TargetJobStatus:
        if cancellation.is_set():
            terminal = self._stop_status(request, attempt)
            self._write_status(terminal)
            return terminal
        self._write_status(
            self._status(request, state="running", attempt=attempt, phase="preparing-inputs")
        )
        try:
            terminal = self._execute(request, attempt, cancellation)
        except TargetExecutionCanceled:
            terminal = self._stop_status(request, attempt)
        except TargetExecutionInapplicable as exc:
            terminal = TargetJobStatus(
                job_id=request.declaration.job_id,
                state="inapplicable",
                attempt=attempt,
                request_sha256=request.request_sha256,
                plan_sha256=request.declaration.plan.plan_sha256,
                progress=TargetProgress(phase="inapplicable", completed=0),
                inapplicable=TargetInapplicable(code=exc.code, message=exc.message),
            )
        except Exception as exc:
            retryable = isinstance(exc, (OSError, TimeoutError, subprocess.TimeoutExpired))
            terminal = TargetJobStatus(
                job_id=request.declaration.job_id,
                state="failed",
                attempt=attempt,
                request_sha256=request.request_sha256,
                plan_sha256=request.declaration.plan.plan_sha256,
                progress=TargetProgress(phase="failed", completed=0),
                failure=TargetFailure(
                    code="target-infrastructure" if retryable else "target-content",
                    message=f"{type(exc).__name__}: {exc}"[:1000],
                    retryable=retryable,
                ),
            )
        if cancellation.is_set():
            terminal = self._stop_status(request, attempt)
        self._write_status(terminal)
        return terminal

    def _stop_status(self, request: TargetJobRequest, attempt: int) -> TargetJobStatus:
        job_id = request.declaration.job_id
        with self._lock:
            interrupted = (
                job_id in self._shutdown_interrupted and job_id not in self._operator_canceled
            )
        state = "interrupted" if interrupted else "canceled"
        return self._status(request, state=state, attempt=attempt, phase=state)

    @staticmethod
    def _status(
        request: TargetJobRequest,
        *,
        state: str,
        attempt: int,
        phase: str,
    ) -> TargetJobStatus:
        return TargetJobStatus(
            job_id=request.declaration.job_id,
            state=state,  # type: ignore[arg-type]
            attempt=attempt,
            request_sha256=request.request_sha256,
            plan_sha256=request.declaration.plan.plan_sha256,
            progress=TargetProgress(phase=phase, completed=0),
        )

    def _recover_interrupted(self) -> None:
        for path in sorted(self.state_root.glob("*.status.json")):
            if path.is_symlink():
                raise ValueError("target state paths must not be symlinks")
            status = TargetJobStatus.model_validate_json(path.read_text(encoding="utf-8"))
            if status.state in _ACTIVE_STATES:
                self._write_status(
                    status.model_copy(
                        update={
                            "state": "interrupted",
                            "progress": TargetProgress(phase="interrupted", completed=0),
                        }
                    )
                )

    def _accepted_path(self, job_id: str) -> Path:
        return self.state_root / f"{_job_id(job_id)}.accepted.json"

    def _status_path(self, job_id: str) -> Path:
        return self.state_root / f"{_job_id(job_id)}.status.json"

    def _load_accepted(self, job_id: str) -> AcceptedTargetJob | None:
        path = self._accepted_path(job_id)
        return (
            None
            if not path.exists()
            else AcceptedTargetJob.model_validate_json(path.read_text(encoding="utf-8"))
        )

    def _load_status(self, job_id: str) -> TargetJobStatus | None:
        path = self._status_path(job_id)
        return (
            None
            if not path.exists()
            else TargetJobStatus.model_validate_json(path.read_text(encoding="utf-8"))
        )

    def _write_status(self, status: TargetJobStatus) -> None:
        with self._lock:
            self._write_model(self._status_path(status.job_id), status)

    def _write_model(self, path: Path, model: Any) -> None:
        if path.is_symlink():
            raise ValueError("target state paths must not be symlinks")
        temporary = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.part")
        encoded = canonical_json_bytes(
            model.model_dump(mode="json", by_alias=True, exclude_none=True)
        )
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        try:
            os.write(descriptor, encoded)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        os.replace(temporary, path)
        directory = os.open(self.state_root, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory)
        finally:
            os.close(directory)


def _job_id(value: str) -> str:
    if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
        raise ValueError("target job ID must be a lowercase SHA-256")
    return value


__all__ = [
    "JobExecutor",
    "PersistentTargetService",
    "TargetExecutionCanceled",
    "TargetExecutionInapplicable",
]
