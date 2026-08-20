"""Restartable first-party target services over the released target boundary."""

from __future__ import annotations

import hashlib
import importlib.metadata
import json
import os
import secrets
import shutil
import subprocess
import threading
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Final

from jsonschema import Draft202012Validator
from pydantic import JsonValue
from riverhog_api_client import ProducerFile
from riverhog_protocol import ArtifactDisposition, canonical_json_bytes, canonical_json_sha256
from stove0_media_contracts import (
    AUDIO_ARCHIVE_OPERATION,
    AUDIO_ARCHIVE_OPERATION_ID,
    AUDIO_ARCHIVE_ROLE,
    PRESERVE_OPERATION,
    PRESERVE_OPERATION_ID,
    PRESERVED_ROLE,
    SOURCE_ARTIFACT_ROLE,
    VIDEO_ARCHIVE_OPERATION,
    VIDEO_ARCHIVE_OPERATION_ID,
    VIDEO_ARCHIVE_ROLE,
    AudioArchiveIntent,
    PreserveIntent,
    VideoArchiveIntent,
)
from stove0_protocol import JsonSchemaDocument
from stove0_review_contracts import (
    REVIEW_AUDIO_ROLE,
    REVIEW_INDEX_ROLE,
    REVIEW_SAMPLE_ENCODE_OPERATION,
    REVIEW_SAMPLE_ENCODE_OPERATION_ID,
    REVIEW_VIDEO_ROLE,
    ReviewSamplePlan,
)
from stove0_target_support import (
    AcceptedTargetJob,
    OperationContract,
    OutputArtifact,
    TargetCancelRequest,
    TargetContract,
    TargetContractPayload,
    TargetExecutionRuntime,
    TargetFailure,
    TargetInapplicable,
    TargetJobRequest,
    TargetJobStatus,
    TargetOperationSupport,
    TargetPreflightRequest,
    TargetPreflightResponse,
    TargetProgress,
    TargetServiceError,
    TransformPlan,
    TransformPlanPayload,
    validate_declaration_against_operation,
)

from stove0_extensions.media_source_artifacts import build_strict_source_artifacts

_ACTIVE_STATES: Final = frozenset({"queued", "running", "canceling"})
_TERMINAL_STATES: Final = frozenset({"inapplicable", "succeeded", "failed", "canceled"})


class TargetExecutionCanceled(RuntimeError):
    pass


class TargetExecutionInapplicable(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _distribution_version() -> str:
    try:
        return importlib.metadata.version("stove0-maintained-extensions")
    except importlib.metadata.PackageNotFoundError:
        return "development"


def _schema(identifier: str, properties: dict[str, Any]) -> JsonSchemaDocument:
    return JsonSchemaDocument.from_schema(
        identifier,
        {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "type": "object",
            "properties": dict(properties),
            "additionalProperties": False,
        },
    )


_EMPTY_OPTIONS = _schema("riverhog.stove0.empty-target-options/v1", {})
_FFMPEG_OPTIONS = _schema(
    "riverhog.stove0.ffmpeg-target-options/v1",
    {"ffmpeg_timeout_seconds": {"type": "integer", "minimum": 1, "maximum": 86400}},
)
_NVENC_OPTIONS = _schema(
    "riverhog.stove0.nvenc-target-options/v1",
    {
        "ffmpeg_timeout_seconds": {"type": "integer", "minimum": 1, "maximum": 86400},
        "preset": {"enum": ["p1", "p2", "p3", "p4", "p5", "p6", "p7"]},
    },
)


JobExecutor = Callable[[TargetJobRequest, int, threading.Event], TargetJobStatus]


class PersistentTargetService:
    """Durable non-secret target job identity with restart convergence.

    Accepted declarations and statuses are persisted atomically. Capability
    tokens live only in the active request closure and are never written.
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
            event = self._cancel.setdefault(job_id, threading.Event())
            self._operator_canceled.add(job_id)
            self._shutdown_interrupted.discard(job_id)
            event.set()
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
        event = self._cancel.setdefault(job_id, threading.Event())
        self._operator_canceled.discard(job_id)
        self._shutdown_interrupted.discard(job_id)
        event.clear()
        self._futures[job_id] = self._pool.submit(self._run, request, attempt, event)

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
        running = self._status(
            request,
            state="running",
            attempt=attempt,
            phase="preparing-inputs",
        )
        self._write_status(running)
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
        return self._status(
            request,
            state=state,
            attempt=attempt,
            phase=state,
        )

    def _status(
        self,
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
            if status.state not in _ACTIVE_STATES:
                continue
            interrupted = status.model_copy(
                update={
                    "state": "interrupted",
                    "progress": TargetProgress(phase="interrupted", completed=0),
                }
            )
            self._write_status(TargetJobStatus.model_validate(interrupted))

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
        payload = model.model_dump(mode="json", by_alias=True, exclude_none=True)
        encoded = canonical_json_bytes(payload)
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


@dataclass(frozen=True, slots=True)
class _PreparedInput:
    artifact: Any
    claimed: Any
    source: Path


class _MediaTargetService(PersistentTargetService):
    def __init__(
        self,
        *,
        implementation_id: str,
        supported: Sequence[tuple[OperationContract, JsonSchemaDocument]],
        state_root: Path,
        workspace_root: Path,
        maximum_workers: int = 1,
        ffmpeg: str = "ffmpeg",
        ffprobe: str = "ffprobe",
        nvenc: bool = False,
    ) -> None:
        self.workspace_root = workspace_root.resolve()
        self.workspace_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self.workspace_root, 0o700)
        self.ffmpeg = ffmpeg
        self.ffprobe = ffprobe
        self.nvenc = nvenc
        operations = {operation.id: operation for operation, _options in supported}
        contract = TargetContract.seal(
            TargetContractPayload(
                implementation_id=implementation_id,
                implementation_version=_distribution_version(),
                source_revision=os.getenv("STOVE0_EXTENSION_SOURCE_REVISION", "unknown"),
                operations=tuple(
                    TargetOperationSupport(
                        operation_id=operation.id,
                        operation_contract_sha256=operation.contract_sha256,
                        options_schema=options,
                    )
                    for operation, options in sorted(supported, key=lambda item: item[0].id)
                ),
            )
        )
        self._media_contract = contract
        super().__init__(
            contract=contract,
            operations=operations,
            state_root=state_root,
            execute=self._execute_media,
            maximum_workers=maximum_workers,
        )

    def _execute_media(
        self,
        request: TargetJobRequest,
        attempt: int,
        cancellation: threading.Event,
    ) -> TargetJobStatus:
        operation = self._operation(request.declaration.plan.operation_id)

        def check() -> None:
            if cancellation.is_set():
                raise TargetExecutionCanceled("target job was canceled")

        with TargetExecutionRuntime.from_request(
            request,
            cancellation_check=check,
            producer_version=_distribution_version(),
        ) as execution:
            workspace = execution.open_workspace(self.workspace_root)
            try:
                prepared = self._materialize(execution, workspace, check=check)
                outputs = self._transform(
                    operation.id,
                    request,
                    prepared,
                    workspace=workspace,
                    check=check,
                )
                output_artifacts = tuple(sorted((item[0] for item in outputs), key=lambda x: x.id))
                producer_files = {item.id: source for item, source in outputs}
                dispositions = _dispositions(request, output_artifacts)
                execution_sha256 = canonical_json_sha256(
                    {
                        "format": "stove0-maintained-target-execution/v1",
                        "target_contract_sha256": self._media_contract.contract_sha256,
                        "plan_sha256": request.declaration.plan.plan_sha256,
                        "attempt": attempt,
                        "outputs": [
                            item.model_dump(mode="json", exclude_none=True)
                            for item in output_artifacts
                        ],
                        "tools": {
                            "ffmpeg": _command_version(self.ffmpeg),
                            "ffprobe": _command_version(self.ffprobe),
                        },
                    }
                )
                return execution.publish_success(
                    producer_files,
                    artifacts=output_artifacts,
                    operation=operation,
                    execution_sha256=execution_sha256,
                    dispositions=dispositions,
                    attempt=attempt,
                    runtime_evidence={
                        "implementation": _distribution_version(),
                        "ffmpeg": _command_version(self.ffmpeg),
                        "ffprobe": _command_version(self.ffprobe),
                    },
                )
            finally:
                workspace.release()

    def _materialize(
        self,
        execution: TargetExecutionRuntime,
        workspace: Any,
        *,
        check: Callable[[], None],
    ) -> tuple[_PreparedInput, ...]:
        resolved = execution.inputs()
        prepared: list[_PreparedInput] = []
        with execution.prepare_inputs(tuple(item for item, _claimed in resolved)) as retrieval:
            for artifact, claimed in resolved:
                check()
                suffix = PurePosixPath(artifact.path).suffix[:32]
                output = workspace.resolve(f"input/{artifact.id}{suffix}")
                output.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                retrieval.download(claimed, output)
                prepared.append(_PreparedInput(artifact=artifact, claimed=claimed, source=output))
        return tuple(prepared)

    def _transform(
        self,
        operation_id: str,
        request: TargetJobRequest,
        prepared: Sequence[_PreparedInput],
        *,
        workspace: Any,
        check: Callable[[], None],
    ) -> tuple[tuple[OutputArtifact, ProducerFile], ...]:
        if operation_id == PRESERVE_OPERATION_ID:
            PreserveIntent.model_validate(request.declaration.plan.intent)
            return self._preserve(prepared, workspace=workspace, check=check)
        if operation_id == AUDIO_ARCHIVE_OPERATION_ID:
            audio_intent = AudioArchiveIntent.model_validate(request.declaration.plan.intent)
            return self._audio(
                prepared,
                intent=audio_intent,
                options=request.declaration.plan.target_options,
                workspace=workspace,
                check=check,
            )
        if operation_id == VIDEO_ARCHIVE_OPERATION_ID:
            video_intent = VideoArchiveIntent.model_validate(request.declaration.plan.intent)
            return self._video(
                prepared,
                intent=video_intent,
                options=request.declaration.plan.target_options,
                plan_sha256=request.declaration.plan.plan_sha256,
                workspace=workspace,
                check=check,
            )
        if operation_id == REVIEW_SAMPLE_ENCODE_OPERATION_ID:
            return self._review(
                prepared,
                intent=request.declaration.plan.intent,
                options=request.declaration.plan.target_options,
                workspace=workspace,
                check=check,
            )
        raise TargetExecutionInapplicable(
            "unsupported-operation",
            f"maintained target cannot execute {operation_id}",
        )

    def _preserve(
        self,
        prepared: Sequence[_PreparedInput],
        *,
        workspace: Any,
        check: Callable[[], None],
    ) -> tuple[tuple[OutputArtifact, ProducerFile], ...]:
        outputs: list[tuple[OutputArtifact, ProducerFile]] = []
        for item in prepared:
            check()
            suffix = PurePosixPath(item.artifact.path).suffix[:32]
            relative = f"preserved/{item.artifact.id}{suffix}"
            destination = workspace.resolve(f"output/{relative}")
            destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            shutil.copyfile(item.source, destination)
            outputs.append(
                _output(
                    source=destination,
                    artifact_id=_output_id("preserved", item.artifact.id),
                    role=PRESERVED_ROLE,
                    path=relative,
                    media_type=item.artifact.media_type,
                    derived_from=(item.artifact.id,),
                )
            )
        return tuple(outputs)

    def _audio(
        self,
        prepared: Sequence[_PreparedInput],
        *,
        intent: AudioArchiveIntent,
        options: Mapping[str, JsonValue],
        workspace: Any,
        check: Callable[[], None],
    ) -> tuple[tuple[OutputArtifact, ProducerFile], ...]:
        outputs: list[tuple[OutputArtifact, ProducerFile]] = []
        timeout = _int_option(options, "ffmpeg_timeout_seconds", 86400)
        for item in prepared:
            relative = f"audio/{item.artifact.id}.opus"
            destination = workspace.resolve(f"output/{relative}")
            destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            self._command(
                [
                    self.ffmpeg,
                    "-hide_banner",
                    "-nostdin",
                    "-y",
                    "-i",
                    str(item.source),
                    "-vn",
                    "-c:a",
                    "libopus",
                    "-b:a",
                    f"{intent.bitrate_kbps}k",
                    str(destination),
                ],
                timeout=timeout,
                check=check,
                invalid_code="audio-inapplicable",
            )
            outputs.append(
                _output(
                    source=destination,
                    artifact_id=_output_id("audio", item.artifact.id),
                    role=AUDIO_ARCHIVE_ROLE,
                    path=relative,
                    media_type="audio/ogg",
                    derived_from=(item.artifact.id,),
                )
            )
        return tuple(outputs)

    def _video(
        self,
        prepared: Sequence[_PreparedInput],
        *,
        intent: VideoArchiveIntent,
        options: Mapping[str, JsonValue],
        plan_sha256: str,
        workspace: Any,
        check: Callable[[], None],
    ) -> tuple[tuple[OutputArtifact, ProducerFile], ...]:
        if not self.nvenc:
            raise TargetExecutionInapplicable(
                "nvenc-unavailable",
                "Archive-video requires the maintained NVENC target.",
            )
        outputs: list[tuple[OutputArtifact, ProducerFile]] = []
        timeout = _int_option(options, "ffmpeg_timeout_seconds", 86400)
        preset = str(options.get("preset", "p7"))
        for item in prepared:
            relative = f"video/{item.artifact.id}.mkv"
            destination = workspace.resolve(f"output/{relative}")
            destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            command = self._video_command(
                item.source,
                destination,
                intent=intent,
                preset=preset,
            )
            effective_encode_command = command
            try:
                self._command(
                    command,
                    timeout=timeout,
                    check=check,
                    invalid_code="video-inapplicable",
                )
            except TargetExecutionInapplicable:
                if intent.salvage != "safe-remux":
                    raise
                remuxed = workspace.resolve(f"salvage/{item.artifact.id}.mkv")
                remuxed.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                self._command(
                    [
                        self.ffmpeg,
                        "-hide_banner",
                        "-nostdin",
                        "-y",
                        "-err_detect",
                        "ignore_err",
                        "-i",
                        str(item.source),
                        "-map",
                        "0",
                        "-c",
                        "copy",
                        str(remuxed),
                    ],
                    timeout=timeout,
                    check=check,
                    invalid_code="video-salvage-inapplicable",
                )
                effective_encode_command = self._video_command(
                    remuxed,
                    destination,
                    intent=intent,
                    preset=preset,
                )
                self._command(
                    effective_encode_command,
                    timeout=timeout,
                    check=check,
                    invalid_code="video-salvage-inapplicable",
                )
            outputs.append(
                _output(
                    source=destination,
                    artifact_id=_output_id("video", item.artifact.id),
                    role=VIDEO_ARCHIVE_ROLE,
                    path=relative,
                    media_type="video/x-matroska",
                    derived_from=(item.artifact.id,),
                )
            )
            bundle_relative = f"source-artifacts/{item.artifact.id}.tar.zst"
            bundle = workspace.resolve(f"output/{bundle_relative}")
            build_strict_source_artifacts(
                source=item.source,
                archive=destination,
                bundle=bundle,
                encode_command=effective_encode_command,
                intent=intent,
                target_options=options,
                target_contract_sha256=self._media_contract.contract_sha256,
                plan_sha256=plan_sha256,
            )
            outputs.append(
                _output(
                    source=bundle,
                    artifact_id=_output_id("source-artifacts", item.artifact.id),
                    role=SOURCE_ARTIFACT_ROLE,
                    path=bundle_relative,
                    media_type="application/zstd",
                    derived_from=(item.artifact.id,),
                )
            )
        return tuple(outputs)

    def _video_command(
        self,
        source: Path,
        destination: Path,
        *,
        intent: VideoArchiveIntent,
        preset: str,
    ) -> list[str]:
        filters: list[str] = []
        if intent.max_height is not None:
            filters.extend(["-vf", f"scale=-2:min(ih\\,{intent.max_height})"])
        return [
            self.ffmpeg,
            "-hide_banner",
            "-nostdin",
            "-y",
            "-i",
            str(source),
            *filters,
            "-c:v",
            "av1_nvenc",
            "-preset",
            preset,
            "-cq",
            str(intent.quality),
            "-c:a",
            "libopus",
            "-b:a",
            f"{intent.audio_bitrate_kbps}k",
            str(destination),
        ]

    def _review(
        self,
        prepared: Sequence[_PreparedInput],
        *,
        intent: Mapping[str, JsonValue],
        options: Mapping[str, JsonValue],
        workspace: Any,
        check: Callable[[], None],
    ) -> tuple[tuple[OutputArtifact, ProducerFile], ...]:
        raw_plan = intent.get("sample_plan")
        if not isinstance(raw_plan, dict):
            raise ValueError("review intent has no sample plan")
        plan = ReviewSamplePlan.model_validate(raw_plan)
        sources = {item.artifact.id: item for item in prepared}
        timeout = _int_option(options, "ffmpeg_timeout_seconds", 86400)
        outputs: list[tuple[OutputArtifact, ProducerFile]] = []
        index_rows: list[dict[str, JsonValue]] = []
        for number, window in enumerate(plan.windows, start=1):
            check()
            item = sources.get(window.artifact_id)
            if item is None:
                raise ValueError("review sample plan references an unknown input artifact")
            kind = _media_kind(self.ffprobe, item.source)
            if kind == "video":
                role = REVIEW_VIDEO_ROLE
                suffix = ".mkv"
                media_type = "video/x-matroska"
                codecs = ["-c:v", "libx264", "-preset", "ultrafast", "-c:a", "libopus"]
            elif kind == "audio":
                role = REVIEW_AUDIO_ROLE
                suffix = ".opus"
                media_type = "audio/ogg"
                codecs = ["-vn", "-c:a", "libopus"]
            else:
                raise TargetExecutionInapplicable(
                    "review-inapplicable",
                    f"Input artifact is not sampleable media: {item.artifact.id}",
                )
            relative = f"review/samples/{number:04d}-{item.artifact.id}{suffix}"
            destination = workspace.resolve(f"output/{relative}")
            destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            self._command(
                [
                    self.ffmpeg,
                    "-hide_banner",
                    "-nostdin",
                    "-y",
                    "-ss",
                    f"{window.start_ms / 1000:.3f}",
                    "-t",
                    f"{window.duration_ms / 1000:.3f}",
                    "-i",
                    str(item.source),
                    *codecs,
                    str(destination),
                ],
                timeout=timeout,
                check=check,
                invalid_code="review-inapplicable",
            )
            artifact_id = _output_id(f"review-{number}", item.artifact.id)
            outputs.append(
                _output(
                    source=destination,
                    artifact_id=artifact_id,
                    role=role,
                    path=relative,
                    media_type=media_type,
                    derived_from=(item.artifact.id,),
                )
            )
            index_rows.append(
                {
                    "artifact_id": artifact_id,
                    "source_artifact_id": item.artifact.id,
                    "path": relative,
                    "start_ms": window.start_ms,
                    "duration_ms": window.duration_ms,
                }
            )
        index_relative = "review/index.json"
        index_path = workspace.resolve(f"output/{index_relative}")
        index_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        index_path.write_bytes(
            canonical_json_bytes(
                {
                    "format": "stove0-review-index/v1",
                    "sample_plan": plan.model_dump(mode="json"),
                    "samples": index_rows,
                }
            )
        )
        outputs.append(
            _output(
                source=index_path,
                artifact_id="review-index",
                role=REVIEW_INDEX_ROLE,
                path=index_relative,
                media_type="application/json",
                derived_from=tuple(sorted(sources)),
            )
        )
        return tuple(outputs)

    def _command(
        self,
        command: Sequence[str],
        *,
        timeout: int,
        check: Callable[[], None],
        invalid_code: str,
    ) -> None:
        log = self.workspace_root / f".command-{threading.get_ident()}.log"
        started = __import__("time").monotonic()
        detail = ""
        try:
            with log.open("wb") as errors:
                process = subprocess.Popen(
                    list(command),
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=errors,
                )
                try:
                    while process.poll() is None:
                        check()
                        if __import__("time").monotonic() - started > timeout:
                            raise subprocess.TimeoutExpired(command, timeout)
                        threading.Event().wait(0.2)
                except BaseException:
                    process.terminate()
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait()
                    raise
            if process.returncode:
                detail = log.read_bytes()[-8192:].decode("utf-8", "replace").strip()
        finally:
            log.unlink(missing_ok=True)
        if process.returncode:
            raise TargetExecutionInapplicable(
                invalid_code,
                detail or f"transform tool exited with status {process.returncode}",
            )


class LocalMediaTargetService(_MediaTargetService):
    def __init__(
        self,
        *,
        state_root: Path,
        workspace_root: Path,
        ffmpeg: str = "ffmpeg",
        ffprobe: str = "ffprobe",
    ) -> None:
        super().__init__(
            implementation_id="riverhog.local-media-target/v1",
            supported=(
                (AUDIO_ARCHIVE_OPERATION, _FFMPEG_OPTIONS),
                (PRESERVE_OPERATION, _EMPTY_OPTIONS),
                (REVIEW_SAMPLE_ENCODE_OPERATION, _FFMPEG_OPTIONS),
            ),
            state_root=state_root,
            workspace_root=workspace_root,
            ffmpeg=ffmpeg,
            ffprobe=ffprobe,
        )


class NvencMediaTargetService(_MediaTargetService):
    def __init__(
        self,
        *,
        state_root: Path,
        workspace_root: Path,
        ffmpeg: str = "ffmpeg",
        ffprobe: str = "ffprobe",
    ) -> None:
        super().__init__(
            implementation_id="riverhog.av1-nvenc-target/v1",
            supported=(
                (REVIEW_SAMPLE_ENCODE_OPERATION, _FFMPEG_OPTIONS),
                (VIDEO_ARCHIVE_OPERATION, _NVENC_OPTIONS),
            ),
            state_root=state_root,
            workspace_root=workspace_root,
            ffmpeg=ffmpeg,
            ffprobe=ffprobe,
            nvenc=True,
        )


def _output(
    *,
    source: Path,
    artifact_id: str,
    role: str,
    path: str,
    media_type: str | None,
    derived_from: tuple[str, ...],
) -> tuple[OutputArtifact, ProducerFile]:
    digest = hashlib.sha256()
    byte_count = 0
    with source.open("rb") as stream:
        while chunk := stream.read(8 * 1024 * 1024):
            byte_count += len(chunk)
            digest.update(chunk)
    artifact = OutputArtifact(
        id=artifact_id,
        role=role,
        path=path,
        bytes=byte_count,
        sha256=digest.hexdigest(),
        media_type=media_type,
        derived_from=tuple(sorted(derived_from)),
    )
    return artifact, ProducerFile(source=source, path=path)


def _output_id(prefix: str, input_id: str) -> str:
    digest = hashlib.sha256(f"{prefix}\0{input_id}".encode()).hexdigest()[:24]
    return f"{prefix}-{digest}"


def _int_option(options: Mapping[str, JsonValue], key: str, default: int) -> int:
    value = options.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"target option must be an integer: {key}")
    return value


def _dispositions(
    request: TargetJobRequest,
    outputs: Sequence[OutputArtifact],
) -> tuple[ArtifactDisposition, ...]:
    by_source: dict[str, set[str]] = {item.id: set() for item in request.declaration.plan.inputs}
    for output in outputs:
        for source in output.derived_from:
            by_source[source].add(output.path)
    dispositions: list[ArtifactDisposition] = []
    for item in request.declaration.plan.inputs:
        paths = tuple(sorted(by_source[item.id]))
        if not paths:
            raise ValueError(f"successful transform did not account for input: {item.id}")
        dispositions.append(
            ArtifactDisposition(
                input_collection_id=item.collection.collection_id,
                input_manifest_sha256=item.collection.manifest_sha256,
                input_path=item.path,
                status="transformed",
                outputs=paths,
            )
        )
    return tuple(sorted(dispositions))


def _job_id(value: str) -> str:
    normalized = value.casefold()
    if len(normalized) != 64 or any(
        character not in "0123456789abcdef" for character in normalized
    ):
        raise ValueError("target job id must be a lowercase SHA-256")
    return normalized


def _command_version(command: str) -> str:
    try:
        result = subprocess.run(
            [command, "-version"],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return "unavailable"
    lines = (result.stdout or result.stderr).splitlines()
    return lines[0][:200] if lines else "unavailable"


def _media_kind(ffprobe: str, source: Path) -> str | None:
    result = subprocess.run(
        [
            ffprobe,
            "-v",
            "error",
            "-show_entries",
            "stream=codec_type",
            "-of",
            "json",
            str(source),
        ],
        check=False,
        capture_output=True,
    )
    if result.returncode:
        return None
    try:
        payload = json.loads(result.stdout)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    types = {
        str(item.get("codec_type")) for item in payload.get("streams", []) if isinstance(item, dict)
    }
    if "video" in types:
        return "video"
    if "audio" in types:
        return "audio"
    return None


__all__ = [
    "LocalMediaTargetService",
    "NvencMediaTargetService",
    "PersistentTargetService",
    "TargetExecutionCanceled",
    "TargetExecutionInapplicable",
]
