"""Reusable restart-safe execution kernel for independently owned targets."""

from __future__ import annotations

import hashlib
import os
import secrets
import subprocess
import threading
import time
from collections.abc import Callable, Mapping
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
from typing import Any, Final

from jsonschema import Draft202012Validator
from riverhog_protocol import RiverhogError, ServiceUnavailable
from riverhog_transform_sdk import ClaimedCollectionRuntimeRegistry
from stove0_target_protocol import (
    EFFECT_TARGET_PROTOCOL,
    AcceptedTargetJob,
    EffectPlan,
    EffectPlanPayload,
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

from stove0_target_support.execution import TargetExecutionSession
from stove0_target_support.http_binding import TargetServiceError
from stove0_target_support.jcs import canonical_json_bytes

_ACTIVE_STATES: Final = frozenset({"queued", "running", "canceling"})
_TERMINAL_STATES: Final = frozenset({"inapplicable", "succeeded", "failed", "canceled"})
DEFAULT_TERMINAL_STATE_RETENTION_SECONDS: Final = 30 * 24 * 60 * 60

JobExecutor = Callable[
    [TargetJobRequest, int, threading.Event, TargetExecutionSession],
    TargetJobStatus,
]


class TargetExecutionCanceled(RuntimeError):
    """The owning target observed cancellation before publishing output."""


class TargetExecutionInapplicable(RuntimeError):
    """The exact declared input cannot be handled by this target."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class TargetExecutionFailure(RuntimeError):
    """The target classified a non-content execution failure explicitly."""

    def __init__(self, code: str, message: str, *, retryable: bool) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable


class TargetEffectCommitUncertain(RuntimeError):
    """An external-effect attempt may have committed and must not be repeated."""


class PersistentTargetService:
    """Persist non-secret job identity and converge identical restart requests.

    Capability tokens remain only in process memory. Accepted declarations and
    statuses are atomically persisted beneath one target-owned state root; an
    active job found after process loss becomes ``interrupted``. An identical
    transform request may resume it; effect jobs remain interrupted because
    repeating an uncertain external commit could duplicate the semantic effect.
    """

    def __init__(
        self,
        *,
        contract: TargetContract,
        operations: Mapping[str, OperationContract],
        state_root: Path,
        execute: JobExecutor,
        maximum_workers: int = 1,
        terminal_state_retention_seconds: int = DEFAULT_TERMINAL_STATE_RETENTION_SECONDS,
    ) -> None:
        self._contract = contract
        self._operations = dict(operations)
        if set(self._operations) != {item.operation_id for item in contract.operations}:
            raise ValueError("target operation implementations differ from the advertised contract")
        for support in contract.operations:
            operation = self._operations[support.operation_id]
            if (
                operation.contract_sha256 != support.operation_contract_sha256
                or operation.result_kind != support.result_kind
            ):
                raise ValueError("target operation implementation differs from its support binding")
        self.state_root = state_root.resolve()
        self.state_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self.state_root, 0o700)
        if self.state_root.is_symlink():
            raise ValueError("target state root must not be a symlink")
        if (
            isinstance(terminal_state_retention_seconds, bool)
            or terminal_state_retention_seconds < 1
        ):
            raise ValueError("target terminal-state retention must be positive")
        self.terminal_state_retention_seconds = terminal_state_retention_seconds
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
        self._runtime_registry = ClaimedCollectionRuntimeRegistry()
        self._runtime_contexts: dict[str, dict[str, object]] = {}
        self._runtime_token_fingerprints: dict[str, bytes] = {}
        self._sessions: dict[str, TargetExecutionSession] = {}
        self._recover_interrupted()
        self.prune_terminal_state()

    def contract(self) -> TargetContract:
        return self._contract

    def preflight(self, request: TargetPreflightRequest) -> TargetPreflightResponse:
        if request.protocol != self._contract.protocol:
            raise TargetServiceError(409, "target-protocol-mismatch", "target protocol changed")
        operation = self._operation(request.operation_id)
        validate_declaration_against_operation(request, operation)
        support = self._contract.support_for(request.operation_id)
        if request.operation_contract_sha256 != support.operation_contract_sha256:
            raise TargetServiceError(409, "operation-contract-mismatch", "operation changed")
        Draft202012Validator(operation.intent_schema.document).validate(request.intent)
        Draft202012Validator(support.options_schema.document).validate(request.target_options)
        plan_fields: dict[str, object] = {
            "operation_id": request.operation_id,
            "operation_contract_sha256": request.operation_contract_sha256,
            "inputs": request.inputs,
            "intent": request.intent,
            "target_options": request.target_options,
            "target_implementation_id": self._contract.implementation_id,
            "target_contract_sha256": self._contract.contract_sha256,
            "observation_result_sha256s": tuple(
                sorted(item.result.result_sha256 for item in request.observations)
            ),
        }
        plan = (
            EffectPlan.seal(EffectPlanPayload.model_validate(plan_fields))
            if self._contract.protocol == EFFECT_TARGET_PROTOCOL
            else TransformPlan.seal(TransformPlanPayload.model_validate(plan_fields))
        )
        return TargetPreflightResponse(target=self._contract, plan=plan)

    def put_job(self, request: TargetJobRequest) -> TargetJobStatus:
        job_id = request.declaration.job_id
        plan = request.declaration.plan
        if (
            plan.target_contract_sha256 != self._contract.contract_sha256
            or plan.target_implementation_id != self._contract.implementation_id
            or plan.protocol != self._contract.protocol
        ):
            raise TargetServiceError(409, "target-contract-mismatch", "target contract changed")
        operation = self._operation(plan.operation_id)
        validate_declaration_against_operation(plan, operation)
        support = self._contract.support_for(plan.operation_id)
        Draft202012Validator(operation.intent_schema.document).validate(plan.intent)
        Draft202012Validator(support.options_schema.document).validate(plan.target_options)
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
            if status is None or status.state not in _TERMINAL_STATES:
                runtime_context = request.runtime.model_dump(
                    mode="json",
                    exclude={"capability_token"},
                )
                prior_context = self._runtime_contexts.get(job_id)
                if prior_context is not None and prior_context != runtime_context:
                    raise TargetServiceError(
                        409,
                        "target-runtime-mismatch",
                        "active target runtime endpoint changed",
                    )
                self._runtime_contexts[job_id] = runtime_context
                token_fingerprint = hashlib.sha256(
                    request.runtime.capability_token.encode()
                ).digest()
                if self._runtime_token_fingerprints.get(job_id) != token_fingerprint:
                    self._runtime_token_fingerprints[job_id] = token_fingerprint
                    self._runtime_registry.refresh(
                        job_id,
                        request.runtime.capability_token,
                    )
            if status is None:
                status = self._status(request, state="queued", attempt=1, phase="queued")
                status = self._commit_status(status)
                self._submit(request, status.attempt)
            elif status.state == "interrupted":
                if request.declaration.plan.protocol == EFFECT_TARGET_PROTOCOL:
                    return status
                status = self._status(
                    request,
                    state="queued",
                    attempt=status.attempt + 1,
                    phase="restarting",
                )
                status = self._commit_status(status)
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
            if status.state == "interrupted":
                return status
            if status.state in _TERMINAL_STATES:
                return status
            self._operator_canceled.add(job_id)
            self._shutdown_interrupted.discard(job_id)
            self._cancel.setdefault(job_id, threading.Event()).set()
            accepted = self._load_accepted(job_id)
            if accepted is None:
                raise RuntimeError("target job status has no accepted declaration")
            canceling = TargetJobStatus(
                protocol=accepted.declaration.plan.protocol,
                job_id=job_id,
                state="canceling",
                attempt=status.attempt,
                request_sha256=accepted.request_sha256,
                plan_sha256=accepted.declaration.plan.plan_sha256,
                progress=TargetProgress(phase="canceling", completed=0),
            )
            return self._commit_status(canceling)

    def close(self) -> None:
        with self._lock:
            for job_id, future in self._futures.items():
                if future.done() or job_id in self._operator_canceled:
                    continue
                self._shutdown_interrupted.add(job_id)
                self._cancel.setdefault(job_id, threading.Event()).set()
        self._pool.shutdown(wait=True, cancel_futures=False)

    def prune_terminal_state(self, *, now: float | None = None) -> dict[str, int]:
        """Remove expired terminal request/status pairs while preserving retryable work."""

        cutoff = (time.time() if now is None else now) - self.terminal_state_retention_seconds
        removed_jobs = 0
        removed_bytes = 0
        with self._lock:
            for status_path in sorted(self.state_root.glob("*.status.json")):
                if status_path.is_symlink():
                    raise ValueError("target state paths must not be symlinks")
                job_id = status_path.name.removesuffix(".status.json")
                future = self._futures.get(job_id)
                if future is not None and not future.done():
                    continue
                try:
                    stat = status_path.stat()
                except FileNotFoundError:
                    continue
                if stat.st_mtime > cutoff:
                    continue
                status = TargetJobStatus.model_validate_json(
                    status_path.read_text(encoding="utf-8")
                )
                if status.state not in _TERMINAL_STATES:
                    continue
                if status.protocol == EFFECT_TARGET_PROTOCOL and status.state == "succeeded":
                    continue
                accepted_path = self._accepted_path(job_id)
                removed_bytes += stat.st_size
                if accepted_path.exists():
                    if accepted_path.is_symlink():
                        raise ValueError("target state paths must not be symlinks")
                    removed_bytes += accepted_path.stat().st_size
                status_path.unlink(missing_ok=True)
                accepted_path.unlink(missing_ok=True)
                removed_jobs += 1
            if removed_jobs:
                directory = os.open(
                    self.state_root,
                    os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
                )
                try:
                    os.fsync(directory)
                finally:
                    os.close(directory)
        return {"jobs": removed_jobs, "bytes": removed_bytes}

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
        session = TargetExecutionSession(
            request,
            attempt,
            self._runtime_registry,
        )
        self._sessions[job_id] = session
        future = self._pool.submit(
            self._run,
            request,
            attempt,
            cancellation,
            session,
        )
        self._futures[job_id] = future

        def forget(completed: Future[TargetJobStatus]) -> None:
            self._forget_future(job_id, completed)

        future.add_done_callback(forget)

    def _forget_future(
        self,
        job_id: str,
        completed: Future[TargetJobStatus],
    ) -> None:
        with self._lock:
            if self._futures.get(job_id) is completed:
                self._futures.pop(job_id, None)
            self._cancel.pop(job_id, None)
            self._operator_canceled.discard(job_id)
            self._shutdown_interrupted.discard(job_id)
        self.prune_terminal_state()

    def _run(
        self,
        request: TargetJobRequest,
        attempt: int,
        cancellation: threading.Event,
        session: TargetExecutionSession,
    ) -> TargetJobStatus:
        try:
            if cancellation.is_set():
                return self._commit_status(self._stop_status(request, attempt))
            active = self._commit_status(
                self._status(
                    request,
                    state="running",
                    attempt=attempt,
                    phase="preparing-inputs",
                )
            )
            if active.state == "canceling" or cancellation.is_set():
                return self._commit_status(self._stop_status(request, attempt))
            try:
                terminal = self._execute(request, attempt, cancellation, session)
            except TargetExecutionCanceled:
                terminal = self._stop_status(request, attempt)
            except TargetExecutionInapplicable as exc:
                terminal = TargetJobStatus(
                    protocol=request.declaration.plan.protocol,
                    job_id=request.declaration.job_id,
                    state="inapplicable",
                    attempt=attempt,
                    request_sha256=request.request_sha256,
                    plan_sha256=request.declaration.plan.plan_sha256,
                    progress=TargetProgress(phase="inapplicable", completed=0),
                    inapplicable=TargetInapplicable(code=exc.code, message=exc.message),
                )
            except TargetEffectCommitUncertain:
                terminal = self._status(
                    request,
                    state="interrupted",
                    attempt=attempt,
                    phase="external-commit-uncertain",
                )
            except Exception as exc:
                terminal = _failure_status(request, attempt=attempt, failure=exc)
            completed = session.completed_status
            if completed is not None:
                terminal = completed
            elif cancellation.is_set() and terminal.state not in {"succeeded", "interrupted"}:
                terminal = self._stop_status(request, attempt)
            return self._commit_status(terminal)
        finally:
            with self._lock:
                if self._sessions.get(request.declaration.job_id) is session:
                    self._sessions.pop(request.declaration.job_id, None)
                self._runtime_contexts.pop(request.declaration.job_id, None)
                self._runtime_token_fingerprints.pop(request.declaration.job_id, None)
                self._runtime_registry.discard(request.declaration.job_id)

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
            protocol=request.declaration.plan.protocol,
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
                self._commit_status(
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

    def _commit_status(self, status: TargetJobStatus) -> TargetJobStatus:
        with self._lock:
            current = self._load_status(status.job_id)
            if current is not None:
                if current.state in _TERMINAL_STATES:
                    return current
                if current.attempt > status.attempt:
                    return current
                if current.state == "canceling" and status.state in {"queued", "running"}:
                    return current
            self._write_model(self._status_path(status.job_id), status)
            if status.state in _TERMINAL_STATES:
                self.prune_terminal_state()
            return status

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


def _failure_status(
    request: TargetJobRequest,
    *,
    attempt: int,
    failure: Exception,
) -> TargetJobStatus:
    if isinstance(failure, TargetExecutionFailure):
        code = failure.code
        message = failure.message
        retryable = failure.retryable
    elif isinstance(failure, RiverhogError):
        status = failure.observed_status
        if status in {401, 403} or failure.code in {"unauthorized", "forbidden"}:
            code = "target-authorization"
            retryable = True
        elif status == 409 or failure.code in {"conflict", "invalid_state"}:
            code = "target-conflict"
            retryable = True
        elif isinstance(failure, ServiceUnavailable) or (status is not None and status >= 500):
            code = "target-infrastructure"
            retryable = True
        else:
            code = "target-api"
            retryable = False
        message = f"{type(failure).__name__}: {failure}"
    elif isinstance(failure, (OSError, TimeoutError, subprocess.TimeoutExpired)):
        code = "target-infrastructure"
        message = f"{type(failure).__name__}: {failure}"
        retryable = True
    else:
        code = "target-software"
        message = f"{type(failure).__name__}: {failure}"
        retryable = True
    return TargetJobStatus(
        protocol=request.declaration.plan.protocol,
        job_id=request.declaration.job_id,
        state="failed",
        attempt=attempt,
        request_sha256=request.request_sha256,
        plan_sha256=request.declaration.plan.plan_sha256,
        progress=TargetProgress(phase="failed", completed=0),
        failure=TargetFailure(
            code=code,
            message=message[:1000],
            retryable=retryable,
        ),
    )


__all__ = [
    "DEFAULT_TERMINAL_STATE_RETENTION_SECONDS",
    "JobExecutor",
    "PersistentTargetService",
    "TargetEffectCommitUncertain",
    "TargetExecutionCanceled",
    "TargetExecutionFailure",
    "TargetExecutionInapplicable",
]
