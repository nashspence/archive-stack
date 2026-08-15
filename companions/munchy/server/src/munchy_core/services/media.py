from __future__ import annotations

import hashlib
import importlib.metadata
import json
import logging
import logging.config
import mimetypes
import os
import shutil
import subprocess
import threading
import time
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path, PurePosixPath
from typing import Any, cast

import jsonschema
from munchy_target_support.metadata_projection import (
    ProjectionMetadata,
    ffmpeg_container_metadata_args,
)
from munchy_target_support.operations import (
    AUDIO_ARCHIVE_OPERATION,
    AUDIO_ARCHIVE_ROLE,
    AUDIO_REVIEW_OPERATION,
    SOURCE_ARTIFACTS_ROLE,
    SOURCE_ROLE,
    TASK_OPERATIONS,
    VIDEO_REVIEW_OPERATION,
    SourceArtifactSidecarIntent,
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
    TargetJobRequestPayload,
    TargetJobState,
    TargetJobStatus,
    TargetOperationSupport,
    TargetPreflightRequest,
    TargetPreflightResponse,
    TargetProgress,
    TransformPlan,
    TransformPlanPayload,
    validate_artifacts_against_operation,
    validate_status_against_request,
)
from munchy_target_support.source_artifact_bridge import (
    build_strict_source_artifacts,
)
from munchy_target_support.source_artifacts import SOURCE_ARTIFACTS_SUFFIX
from munchy_target_support.workspace import (
    publish_file_atomically,
    verify_artifacts,
    workspace_area_root,
    workspace_artifact_path,
)
from munchy_workflows.profiles import EncodeProfile
from time_formats import (
    utc_timestamp_now,
)

import munchy_core.domain.errors as domain_errors
import munchy_core.persistence.sqlite_state as state_store
import munchy_core.runtime.commands as command_runtime
import munchy_core.runtime.config as runtime_config
import munchy_core.services.events as event_service
import munchy_core.services.handoffs as handoff_service
import munchy_core.services.routing as routing_service
import munchy_core.services.uploads as upload_service
from munchy_core.ports.transform_targets import TransformTargetPlatform

log = logging.getLogger("munchy.server")

transform_target_platform: TransformTargetPlatform | None = None


def register_transform_target_platform(platform: TransformTargetPlatform) -> None:
    global transform_target_platform
    transform_target_platform = platform


def configured_transform_target_platform() -> TransformTargetPlatform:
    if transform_target_platform is None:
        raise RuntimeError("transform target platform adapter is not configured")
    return transform_target_platform


def preflight_transform_target(
    profile: EncodeProfile,
    tasks: list[str] | tuple[str, ...],
) -> TargetContract:
    contract = configured_transform_target_platform().contract(profile.target)
    for task in tasks:
        try:
            operation_id = TASK_OPERATIONS[task]
        except KeyError as exc:
            raise RuntimeError(f"task does not have a transform operation: {task}") from exc
        operation = operation_contract(operation_id)
        try:
            support = contract.support_for(operation_id)
        except ValueError as exc:
            raise RuntimeError(str(exc)) from exc
        if support.operation_contract_sha256 != operation.contract_sha256:
            raise RuntimeError(f"transform target operation contract mismatch: {operation_id}")
        try:
            jsonschema.validate(profile.target_options, support.options_schema.document)
        except jsonschema.ValidationError as exc:
            raise RuntimeError(f"transform target options are invalid: {exc.message}") from exc
    return contract


def target_profile_for_group(group_config: Mapping[str, Any]) -> EncodeProfile:
    profile = group_config.get("encode_profile")
    if isinstance(profile, Mapping):
        return EncodeProfile.model_validate(profile)
    if str(group_config.get("output_mode") or "video") == "audio":
        return EncodeProfile.model_validate(
            {
                "target": "munchy-audio",
                "archive": {"codec": "opus", "container": "opus"},
            }
        )
    return EncodeProfile()


def _operation_job_id(base_job_id: str, task: str) -> str:
    digest = hashlib.sha256(f"{base_job_id}/{task}".encode()).hexdigest()[:12]
    suffix = f"--{task.replace('_', '-')}--{digest}"
    return f"{base_job_id[: max(1, 160 - len(suffix))]}{suffix}"


def _coordinator_path(value: object) -> Path:
    path = Path(str(value)).expanduser()
    if path.is_absolute() and path.parts[:2] == ("/", "data"):
        return runtime_config.TRANSFORM_RUNTIME_DIR.joinpath(*path.parts[2:])
    return path


def _relative_payload_path(value: object, old_root: object, local_root: Path) -> str:
    path = _coordinator_path(value)
    old = _coordinator_path(old_root)
    try:
        return path.relative_to(old).as_posix()
    except ValueError:
        try:
            return path.relative_to(local_root).as_posix()
        except ValueError as exc:
            raise RuntimeError(f"target input path is outside its declared root: {value}") from exc


def _artifact_id(relative_path: str) -> str:
    return f"input-{hashlib.sha256(relative_path.encode()).hexdigest()[:24]}"


def _target_output_artifact_id(relative_path: str) -> str:
    return f"output-{hashlib.sha256(relative_path.encode()).hexdigest()[:24]}"


def _materialize_transform_inputs(
    registration_id: str,
    workspace_id: str,
    source_root: Path,
    *,
    sidecar_paths: set[str],
) -> tuple[tuple[Artifact, ...], dict[str, Artifact]]:
    workspace_root = configured_transform_target_platform().workspace_root(registration_id)
    input_root = workspace_area_root(workspace_root, "input", workspace_id)
    shutil.rmtree(input_root, ignore_errors=True)
    input_root.mkdir(parents=True)
    artifacts: list[Artifact] = []
    by_path: dict[str, Artifact] = {}
    for source in sorted(path for path in source_root.rglob("*") if path.is_file()):
        relative = source.relative_to(source_root).as_posix()
        before = source.stat(follow_symlinks=False)
        destination = workspace_artifact_path(
            workspace_root,
            "input",
            workspace_id,
            relative,
        )
        destination.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.link(source, destination)
        except OSError:
            shutil.copyfile(source, destination)
        digest = upload_service.file_sha256(source)
        after = source.stat(follow_symlinks=False)
        if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
        ):
            raise RuntimeError(f"transform input changed during workspace publication: {relative}")
        artifact = Artifact(
            id=_artifact_id(relative),
            role=SOURCE_ARTIFACTS_ROLE if relative in sidecar_paths else SOURCE_ROLE,
            path=relative,
            bytes=before.st_size,
            sha256=digest,
            media_type=mimetypes.guess_type(relative)[0],
        )
        artifacts.append(artifact)
        by_path[relative] = artifact
    ordered = tuple(sorted(artifacts, key=lambda item: item.id))
    verify_artifacts(workspace_root, "input", workspace_id, ordered)
    return ordered, by_path


def _portable_review_plan(
    value: object,
    *,
    old_input_root: object,
    local_input_root: Path,
    artifacts_by_path: Mapping[str, Artifact],
) -> dict[str, Any] | None:
    if value is None:
        return None
    plan = json.loads(json.dumps(value))
    if not isinstance(plan, dict):
        raise RuntimeError("review plan must be an object")
    artifacts_by_id = {item.id: item for item in artifacts_by_path.values()}
    for item in plan.get("files") or []:
        if item.get("artifact_id"):
            if str(item["artifact_id"]) not in artifacts_by_id:
                raise RuntimeError(
                    f"review plan references unknown input artifact: {item['artifact_id']}"
                )
            continue
        relative = _relative_payload_path(item.pop("path", ""), old_input_root, local_input_root)
        try:
            item["artifact_id"] = artifacts_by_path[relative].id
        except KeyError as exc:
            raise RuntimeError(f"review plan references unknown input: {relative}") from exc
    for item in plan.get("clips") or []:
        if item.get("source_artifact_id"):
            if str(item["source_artifact_id"]) not in artifacts_by_id:
                raise RuntimeError(
                    f"review clip references unknown input artifact: {item['source_artifact_id']}"
                )
            continue
        relative = _relative_payload_path(item.pop("source", ""), old_input_root, local_input_root)
        try:
            item["source_artifact_id"] = artifacts_by_path[relative].id
        except KeyError as exc:
            raise RuntimeError(f"review clip references unknown input: {relative}") from exc
    return cast(dict[str, Any], plan)


def prepare_target_operation_payload(
    payload: Mapping[str, Any],
    task: str,
) -> dict[str, Any]:
    profile = EncodeProfile.model_validate(payload.get("encode_profile") or {})
    operation_id = TASK_OPERATIONS[task]
    operation = operation_contract(operation_id)
    preflight_transform_target(profile, [task])
    source_root = _coordinator_path(payload["input_dir"]).resolve()
    if not source_root.is_dir():
        raise RuntimeError(f"transform input directory is missing: {source_root}")
    workspace_id = _operation_job_id(str(payload["job_id"]), task)
    raw_sidecars = payload.get("source_artifacts_sidecars")
    sidecar_paths: set[str] = set()
    if isinstance(raw_sidecars, Mapping):
        for entries in raw_sidecars.values():
            if not isinstance(entries, list):
                continue
            for item in entries:
                if isinstance(item, Mapping):
                    sidecar_paths.add(
                        _relative_payload_path(
                            item.get("path"),
                            payload["input_dir"],
                            source_root,
                        )
                    )
    inputs, by_path = _materialize_transform_inputs(
        profile.target,
        workspace_id,
        source_root,
        sidecar_paths=sidecar_paths,
    )
    metadata: dict[str, dict[str, Any]] = {}
    raw_metadata = payload.get("container_metadata")
    if isinstance(raw_metadata, Mapping):
        for relative, value in raw_metadata.items():
            try:
                artifact = by_path[str(relative)]
            except KeyError as exc:
                raise RuntimeError(
                    f"container metadata references unknown input: {relative}"
                ) from exc
            if not isinstance(value, Mapping):
                raise RuntimeError(f"container metadata must be an object: {relative}")
            metadata[artifact.id] = dict(value)
    sidecar_associations: dict[str, tuple[SourceArtifactSidecarIntent, ...]] = {}
    if isinstance(raw_sidecars, Mapping):
        for source_path, entries in raw_sidecars.items():
            source = by_path.get(str(source_path))
            if source is None:
                raise RuntimeError(
                    f"source sidecar association references unknown input: {source_path}"
                )
            associations: list[SourceArtifactSidecarIntent] = []
            for item in entries if isinstance(entries, list) else []:
                relative = _relative_payload_path(
                    item.get("path"),
                    payload["input_dir"],
                    source_root,
                )
                associations.append(
                    SourceArtifactSidecarIntent(
                        artifact_id=by_path[relative].id,
                        sidecar_id=str(item.get("id") or ""),
                        format=str(item.get("format") or "opaque"),
                        arcname=str(item.get("arcname") or f"sidecars/{relative}"),
                        source_rel_path=str(item.get("source_rel_path") or ""),
                    )
                )
            sidecar_associations[source.id] = tuple(associations)
    intent: dict[str, Any] = {
        "archive": profile.archive.model_dump(mode="json", exclude_none=True),
        "run_id": payload.get("run_id"),
        "container_metadata_required": bool(payload.get("container_metadata_required", True)),
        "container_metadata": metadata,
        "source_artifact_sidecars": {
            key: [item.model_dump(mode="json") for item in value]
            for key, value in sidecar_associations.items()
        },
    }
    if profile.source is not None:
        intent["source"] = profile.source.model_dump(mode="json", exclude_none=True)
    if operation_id in {VIDEO_REVIEW_OPERATION, AUDIO_REVIEW_OPERATION}:
        intent["review_clip"] = payload.get("review_clip_plan")
        review_plans = payload.get("review_plans")
        raw_plan = review_plans.get(task) if isinstance(review_plans, Mapping) else None
        intent["review_plan"] = _portable_review_plan(
            raw_plan,
            old_input_root=payload["input_dir"],
            local_input_root=source_root,
            artifacts_by_path=by_path,
        )
    normalized_intent = validate_operation_intent(operation_id, intent).model_dump(
        mode="json", exclude_none=True
    )
    target_options = dict(profile.target_options)
    if payload.get("max_parallel_encodes") is not None:
        target_options["max_parallel_encodes"] = int(payload["max_parallel_encodes"])
    declaration = TargetPreflightRequest(
        operation_id=operation_id,
        operation_contract_sha256=operation.contract_sha256,
        workspace_id=workspace_id,
        inputs=inputs,
        intent=normalized_intent,
        target_options=target_options,
    )
    accepted = configured_transform_target_platform().preflight(profile.target, declaration)
    request = TargetJobRequest.seal(
        TargetJobRequestPayload(job_id=workspace_id, plan=accepted.plan)
    )
    return {
        "registration_id": profile.target,
        "task": task,
        "target_contract": accepted.target.model_dump(
            mode="json", by_alias=True, exclude_none=True
        ),
        "request": request.model_dump(mode="json", by_alias=True, exclude_none=True),
    }


def resource_request(
    registration_id: str,
    method: str,
    path: str,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    return configured_transform_target_platform().resource_request(
        registration_id, method, path, payload
    )


def acquire_target_resource(job: dict[str, Any], registration_id: str) -> str:
    job_id = str(job["job_id"])
    leases = job.setdefault("resource_leases", {})
    if not isinstance(leases, dict):
        leases = {}
        job["resource_leases"] = leases
    lease = leases.get(registration_id)
    token = str(lease.get("token") or "") if isinstance(lease, dict) else ""
    deadline = time.monotonic() + runtime_config.RESOURCE_LEASE_TTL_SECONDS
    while time.monotonic() < deadline:
        state_store.raise_if_job_canceled(job_id)
        payload = {
            "owner": f"munchy-server:{job_id}",
            "lease_token": token,
            "lease_ttl_s": runtime_config.RESOURCE_LEASE_TTL_SECONDS,
            "wait_s": runtime_config.RESOURCE_WAIT_SECONDS,
            "wait_ready": True,
            "priority": 0,
        }
        try:
            result = resource_request(registration_id, "POST", "/acquire", payload)
        except RuntimeError as exc:
            if "busy" not in str(exc) and "queued" not in str(exc):
                raise
            handoff_service.retry_sleep(5, job_id=job_id)
            continue
        if result is None:
            return ""
        token = str(result.get("lease_token") or token)
        if not result.get("queued"):
            leases[registration_id] = {
                "token": token,
                "acquired_at": utc_timestamp_now(),
            }
            state_store.save_job(job)
            return token
        handoff_service.retry_sleep(5, job_id=job_id)
    raise RuntimeError(f"timed out waiting for resource lease for {registration_id}")


def release_target_resource(registration_id: str, token: str, *, stop: bool = False) -> bool:
    if not token:
        return not stop
    try:
        confirmation = resource_request(
            registration_id,
            "POST",
            "/release",
            {"lease_token": token, "stop": stop},
        )
        if stop and (confirmation is None or confirmation.get("stopped") is not True):
            raise RuntimeError("resource broker did not confirm the target hard stop")
        return True
    except Exception:
        log.exception("failed to release resource lease for %s", registration_id)
        return False


def release_job_target_resource(
    job: dict[str, Any],
    registration_id: str,
    token: str,
    *,
    stop: bool = False,
) -> None:
    if release_target_resource(registration_id, token, stop=stop):
        leases = job.get("resource_leases")
        if isinstance(leases, dict):
            leases.pop(registration_id, None)
            if not leases:
                job.pop("resource_leases", None)
            job.setdefault("resource_releases", {})[registration_id] = {
                "released_at": utc_timestamp_now(),
                "target_stopped": stop,
            }
            if stop:
                unconfirmed = job.get("target_cancellation_unconfirmed")
                if isinstance(unconfirmed, dict):
                    for job_id, value in tuple(unconfirmed.items()):
                        if (
                            isinstance(value, dict)
                            and value.get("registration_id") == registration_id
                        ):
                            unconfirmed.pop(job_id, None)
                    if not unconfirmed:
                        job.pop("target_cancellation_unconfirmed", None)
                        job["target_custody_reconciled_at"] = utc_timestamp_now()
            state_store.save_job(job)


def start_target_job(registration_id: str, target_payload: dict[str, Any]) -> dict[str, Any]:
    request = TargetJobRequest.model_validate(target_payload["request"])
    status = configured_transform_target_platform().put_job(registration_id, request)
    validate_status_against_request(
        status,
        request,
        operation_contract(request.plan.operation_id),
    )
    return status.model_dump(mode="json", exclude_none=True)


def target_job_status(registration_id: str, target_job_id: str) -> dict[str, Any]:
    return (
        configured_transform_target_platform()
        .status(registration_id, target_job_id)
        .model_dump(mode="json", exclude_none=True)
    )


def resume_target_job(registration_id: str, target_payload: dict[str, Any]) -> dict[str, Any]:
    request = TargetJobRequest.model_validate(target_payload["request"])
    resumed = TargetJobRequest.seal(
        TargetJobRequestPayload(
            job_id=request.job_id,
            attempt=request.attempt + 1,
            plan=request.plan,
        )
    )
    target_payload["request"] = resumed.model_dump(mode="json", by_alias=True, exclude_none=True)
    return start_target_job(registration_id, target_payload)


def compact_target_status_for_progress(status: dict[str, Any]) -> dict[str, Any]:
    compact: dict[str, Any] = {
        key: status[key]
        for key in (
            "job_id",
            "attempt",
            "state",
            "protocol",
            "request_sha256",
            "plan_sha256",
            "progress",
            "started_at",
            "finished_at",
            "updated_at",
        )
        if key in status
    }
    if "failure" in status:
        compact["failure"] = status["failure"]
    return compact


def record_target_status(job: dict[str, Any], target_job_id: str, status: dict[str, Any]) -> None:
    statuses = job.setdefault("target_statuses", {})
    if not isinstance(statuses, dict):
        statuses = {}
        job["target_statuses"] = statuses
    statuses[target_job_id] = compact_target_status_for_progress(status)
    state_store.save_job(job)


def cancel_and_reconcile_target_job(
    registration_id: str,
    target_job_id: str,
    *,
    job: dict[str, Any],
) -> bool:
    reason = f"munchy job canceled: {job.get('job_id')}"
    try:
        configured_transform_target_platform().cancel(
            registration_id,
            target_job_id,
            TargetCancelRequest(reason=reason),
        )
    except Exception as exc:
        log.warning("transform target cancellation request failed for %s: %s", target_job_id, exc)
    deadline = time.monotonic() + runtime_config.TARGET_CANCEL_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        try:
            status = target_job_status(registration_id, target_job_id)
        except Exception as exc:
            log.warning(
                "transform target cancellation status failed for %s: %s", target_job_id, exc
            )
            time.sleep(1)
            continue
        record_target_status(job, target_job_id, status)
        if status.get("state") in {"succeeded", "failed", "canceled"}:
            return True
        time.sleep(1)
    job.setdefault("target_cancellation_unconfirmed", {})[target_job_id] = {
        "registration_id": registration_id,
        "recorded_at": utc_timestamp_now(),
    }
    state_store.save_job(job)
    return False


def wait_target_job(
    registration_id: str,
    target_job_id: str,
    *,
    target_payload: dict[str, Any],
    job: dict[str, Any],
) -> dict[str, Any]:
    job_id = str(job["job_id"])
    next_repost = time.monotonic() + max(30.0, runtime_config.TARGET_REPOST_SECONDS)
    while True:
        try:
            state_store.raise_if_job_canceled(job_id)
        except domain_errors.JobCanceled as exc:
            if not cancel_and_reconcile_target_job(registration_id, target_job_id, job=job):
                raise RuntimeError(
                    f"transform target cancellation could not be confirmed: {target_job_id}"
                ) from exc
            raise
        try:
            status = target_job_status(registration_id, target_job_id)
        except Exception as exc:
            log.warning("transform target status check failed; retrying: %s", exc)
            handoff_service.retry_sleep(15)
            try:
                start_target_job(registration_id, target_payload)
            except Exception as start_exc:
                log.warning("transform target restart attempt failed; retrying: %s", start_exc)
            continue
        parsed = TargetJobStatus.model_validate(status)
        request = TargetJobRequest.model_validate(target_payload["request"])
        validate_status_against_request(
            parsed,
            request,
            operation_contract(request.plan.operation_id),
        )
        record_target_status(job, target_job_id, status)
        state = status.get("state")
        if state == "succeeded":
            verify_artifacts(
                configured_transform_target_platform().workspace_root(registration_id),
                "output",
                target_job_id,
                parsed.outputs,
            )
            return status
        if state in {"failed", "canceled"}:
            message = parsed.failure.message if parsed.failure is not None else state
            error = f"transform target job {state}: {message}"
            event_service.emit_job_issue(job, component="encoding", error=error, severity="error")
            raise domain_errors.EncodingFailed(error)
        if state == "interrupted":
            resume_target_job(registration_id, target_payload)
            next_repost = time.monotonic() + max(30.0, runtime_config.TARGET_REPOST_SECONDS)
            time.sleep(5)
            continue
        if time.monotonic() >= next_repost:
            try:
                start_target_job(registration_id, target_payload)
            except Exception as exc:
                log.warning("transform target re-submit failed; retrying: %s", exc)
            next_repost = time.monotonic() + max(30.0, runtime_config.TARGET_REPOST_SECONDS)
        time.sleep(5)


def accept_target_outputs(
    registration_id: str,
    target_result: Mapping[str, Any],
    destination_root: Path,
) -> tuple[Artifact, ...]:
    status = TargetJobStatus.model_validate(target_result)
    source_root = configured_transform_target_platform().workspace_root(registration_id)
    verified = verify_artifacts(source_root, "output", status.job_id, status.outputs)
    for artifact in status.outputs:
        destination = destination_root.joinpath(*Path(artifact.path).parts)
        publish_file_atomically(verified[artifact.id], destination)
        if destination.stat().st_size != artifact.bytes:
            raise RuntimeError(f"accepted target output byte count changed: {artifact.id}")
        if upload_service.file_sha256(destination) != artifact.sha256:
            raise RuntimeError(f"accepted target output sha256 changed: {artifact.id}")
    return status.outputs


def target_workspace_roots(job: Mapping[str, Any]) -> tuple[Path, ...]:
    roots: set[Path] = set()

    def add_payload(value: object) -> None:
        if not isinstance(value, Mapping):
            return
        registration_id = str(value.get("registration_id") or "")
        request = value.get("request")
        if not registration_id or not isinstance(request, Mapping):
            return
        workspace_id = str(request.get("job_id") or "")
        if not workspace_id:
            return
        workspace_root = configured_transform_target_platform().workspace_root(registration_id)
        roots.update(
            {
                workspace_area_root(workspace_root, "input", workspace_id),
                workspace_area_root(workspace_root, "output", workspace_id),
                workspace_area_root(workspace_root, "jobs", workspace_id),
            }
        )

    target_payloads = job.get("target_payloads")
    if isinstance(target_payloads, Mapping):
        for payload in target_payloads.values():
            add_payload(payload)
    eager = job.get("eager_archive")
    batches = eager.get("batches") if isinstance(eager, Mapping) else None
    if isinstance(batches, Mapping):
        for batch in batches.values():
            if isinstance(batch, Mapping):
                add_payload(batch.get("payload"))
    return tuple(sorted(roots))


def ffmpeg_number(value: float) -> str:
    number = float(value)
    return str(int(number)) if number.is_integer() else f"{number:g}"


def archive_audio_encode_args(audio: Mapping[str, Any]) -> list[str]:
    args: list[str] = []
    sample_rate = audio.get("sample_rate")
    args.extend(["-ar", str(sample_rate if sample_rate is not None else 48000)])
    channels = audio.get("channels")
    if channels is not None:
        args.extend(["-ac", str(channels)])
    args.extend(["-b:a", str(audio.get("bitrate") or runtime_config.ARCHIVE_AUDIO_BITRATE)])
    vbr = audio.get("vbr")
    if vbr is not None:
        if isinstance(vbr, bool):
            args.extend(["-vbr", "on" if vbr else "off"])
        else:
            args.extend(["-vbr", str(vbr)])
    compression_level = audio.get("compression_level")
    if compression_level is not None:
        args.extend(["-compression_level", str(compression_level)])
    application = audio.get("application")
    if application is not None:
        args.extend(["-application", str(application)])
    frame_duration = audio.get("frame_duration")
    if frame_duration is not None:
        args.extend(["-frame_duration", ffmpeg_number(float(frame_duration))])
    cutoff = audio.get("cutoff")
    if cutoff is not None:
        args.extend(["-cutoff", str(cutoff)])
    return args


def audio_archive_profile(group_config: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    profile = group_config.get("encode_profile")
    if not isinstance(profile, dict):
        profile = {
            "target": "munchy-audio",
            "archive": {"codec": "opus", "container": "opus", "audio": {}},
        }
    target = str(profile.get("target") or "")
    if target != "munchy-audio":
        raise RuntimeError("archive audio groups require encode_profile.target = 'munchy-audio'")
    archive = profile.get("archive")
    if not isinstance(archive, dict):
        raise RuntimeError("archive audio groups require encode_profile.archive")
    codec = str(archive.get("codec") or "opus")
    container = str(archive.get("container") or "opus")
    if codec != "opus" or container != "opus":
        raise RuntimeError("archive audio groups currently support only opus in opus container")
    audio = archive.get("audio")
    if audio is None:
        audio = {}
    if not isinstance(audio, dict):
        raise RuntimeError("archive audio profile archive.audio must be a table")
    return profile, audio


def audio_container_metadata_args(metadata: ProjectionMetadata | None) -> list[str]:
    return ffmpeg_container_metadata_args(metadata)


def audio_archive_metadata_for_source(
    source: Path,
    *,
    rel_path: str,
    group_config: dict[str, Any],
    provenance: Mapping[str, Any] | None,
) -> ProjectionMetadata | None:
    if not routing_service.metadata_projection_enabled(group_config):
        return None
    return routing_service.projection_metadata_from_source(
        rel_path,
        source,
        group_config=group_config,
        provenance=provenance,
    )


def archive_audio_command(
    source: Path,
    dest: Path,
    group_config: dict[str, Any],
    *,
    metadata: ProjectionMetadata | None = None,
) -> list[str]:
    _profile, audio = audio_archive_profile(group_config)
    dest.parent.mkdir(parents=True, exist_ok=True)
    return [
        "ffmpeg",
        "-hide_banner",
        "-y",
        "-ignore_unknown",
        "-i",
        str(source),
        "-map",
        "0:a?",
        "-map_metadata",
        "0",
        "-vn",
        "-c:a",
        "libopus",
        *archive_audio_encode_args(audio),
        *audio_container_metadata_args(metadata),
        "-f",
        "opus",
        str(dest),
    ]


def archive_audio_sources(
    input_root: Path,
    *,
    rel_paths: set[str] | None = None,
) -> list[Path]:
    if not input_root.is_dir():
        raise RuntimeError(f"input group is missing: {input_root}")
    if rel_paths is not None:
        return sorted(input_root / Path(rel_path) for rel_path in rel_paths)
    return sorted(path for path in input_root.rglob("*") if path.is_file())


def archive_audio_output_for_source(source: Path, input_root: Path, output_root: Path) -> Path:
    return (output_root / source.relative_to(input_root)).with_suffix(".opus")


def run_archive_audio_item(
    *,
    source: Path,
    dest: Path,
    input_root: Path,
    group_config: dict[str, Any],
    provenance: Mapping[str, Any] | None,
    source_sidecars: list[dict[str, Any]] | None = None,
    target_outputs: Mapping[str, Mapping[str, Any]] | None = None,
    projection_metadata: ProjectionMetadata | None = None,
) -> dict[str, Any]:
    profile, _audio = audio_archive_profile(group_config)
    rel_path = source.relative_to(input_root).as_posix()
    audio_metadata = projection_metadata or audio_archive_metadata_for_source(
        source, rel_path=rel_path, group_config=group_config, provenance=provenance
    )
    sidecar = routing_service.source_artifact_sidecar_for_archive_output(dest)
    if dest.is_file() and sidecar.is_file():
        return {
            "source": str(source),
            "output": str(dest),
            "bytes": dest.stat().st_size,
            "sha256": upload_service.file_sha256(dest),
            "source_artifacts": {"path": str(sidecar), "reused": True},
            "container_metadata": audio_metadata.as_dict() if audio_metadata else None,
            "reused": True,
        }
    result = command_runtime.run_command(
        archive_audio_command(source, dest, group_config, metadata=audio_metadata),
        action="archive audio",
    )
    artifacts = build_strict_source_artifacts(
        source=source,
        archive_mkv=dest,
        encode_command=cast(list[str], result["command"]),
        encode_profile=profile,
        source_sidecars=source_sidecars,
        target_output=(
            target_outputs.get(PurePosixPath(rel_path).with_suffix(".opus").as_posix())
            if target_outputs
            else None
        ),
    )
    return {
        "source": str(source),
        "output": str(dest),
        "command": result["command"],
        "duration_s": result["duration_s"],
        "bytes": dest.stat().st_size if dest.exists() else 0,
        "sha256": upload_service.file_sha256(dest) if dest.exists() else "",
        "source_artifacts": artifacts,
        "container_metadata": audio_metadata.as_dict() if audio_metadata else None,
    }


def run_archive_audio_group(
    *,
    input_root: Path,
    output_root: Path,
    group_config: dict[str, Any],
    source_rel_paths: set[str] | None = None,
    source_artifacts_sidecars: Mapping[str, list[dict[str, Any]]] | None = None,
    target_outputs: Mapping[str, Mapping[str, Any]] | None = None,
    provenance_by_rel_path: Mapping[str, Mapping[str, Any]] | None = None,
    projection_metadata_by_rel_path: Mapping[str, ProjectionMetadata] | None = None,
) -> dict[str, Any]:
    sources = archive_audio_sources(input_root, rel_paths=source_rel_paths)
    if not sources:
        return {"status": "skipped", "reason": "no audio sources", "items": []}
    output_root.mkdir(parents=True, exist_ok=True)
    items: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=runtime_config.AUDIO_ARCHIVE_MAX_PARALLEL) as pool:
        futures = {
            pool.submit(
                run_archive_audio_item,
                source=source,
                dest=archive_audio_output_for_source(source, input_root, output_root),
                input_root=input_root,
                group_config=group_config,
                provenance=(
                    provenance_by_rel_path.get(source.relative_to(input_root).as_posix())
                    if provenance_by_rel_path
                    else None
                ),
                source_sidecars=(
                    source_artifacts_sidecars.get(source.relative_to(input_root).as_posix(), [])
                    if source_artifacts_sidecars
                    else []
                ),
                target_outputs=target_outputs,
                projection_metadata=(
                    projection_metadata_by_rel_path.get(source.relative_to(input_root).as_posix())
                    if projection_metadata_by_rel_path
                    else None
                ),
            ): source
            for source in sources
        }
        for future in as_completed(futures):
            items.append(future.result())
    items.sort(key=lambda item: str(item.get("source") or ""))
    return {"status": "succeeded", "items": items, "count": len(items)}


class LocalAudioTransformCanceled(RuntimeError):
    pass


def _local_audio_output_derivation(
    request: TargetJobRequest,
    relative_path: str,
    role: str,
) -> tuple[str, ...]:
    primary_path = relative_path.removesuffix(SOURCE_ARTIFACTS_SUFFIX)
    matches = [
        artifact
        for artifact in request.plan.inputs
        if artifact.role == SOURCE_ROLE
        and PurePosixPath(artifact.path).with_suffix(".opus").as_posix() == primary_path
    ]
    if len(matches) != 1:
        raise RuntimeError(f"audio output does not identify exactly one source: {relative_path}")
    derived_from = [matches[0].id]
    if role == SOURCE_ARTIFACTS_ROLE:
        intent = validate_operation_intent(
            request.plan.operation_id,
            request.plan.effective_intent,
        )
        derived_from.extend(
            sidecar.artifact_id
            for sidecar in intent.source_artifact_sidecars.get(matches[0].id, ())
        )
    return tuple(sorted(derived_from))


def _local_audio_target_outputs(
    request: TargetJobRequest,
) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for source in request.plan.inputs:
        if source.role != SOURCE_ROLE:
            continue
        relative = PurePosixPath(source.path).with_suffix(".opus").as_posix()
        result[relative] = {
            "id": _target_output_artifact_id(relative),
            "role": AUDIO_ARCHIVE_ROLE,
            "path": relative,
            "derived_from": list(
                _local_audio_output_derivation(request, relative, AUDIO_ARCHIVE_ROLE)
            ),
        }
    return result


class LocalAudioTransformTarget:
    """The local audio implementation behind the same target interface as HTTP targets."""

    def __init__(self, workspace_root: Path) -> None:
        self.workspace_root = workspace_root.resolve()
        self._statuses: dict[str, TargetJobStatus] = {}
        self._cancel_events: dict[str, threading.Event] = {}
        self._threads: dict[str, threading.Thread] = {}
        self._lock = threading.RLock()
        self._recover_interrupted_jobs()

    def _job_root(self, job_id: str) -> Path:
        return workspace_area_root(self.workspace_root, "jobs", job_id)

    def _status_path(self, job_id: str) -> Path:
        return self._job_root(job_id) / "local-audio-status.json"

    def _request_path(self, job_id: str) -> Path:
        return self._job_root(job_id) / "local-audio-request.json"

    def _save_status(self, status: TargetJobStatus) -> None:
        path = self._status_path(status.job_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock:
            temporary = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.part")
            try:
                temporary.write_text(
                    status.model_dump_json(by_alias=True, exclude_none=True, indent=2),
                    encoding="utf-8",
                )
                os.replace(temporary, path)
                self._statuses[status.job_id] = status
            finally:
                temporary.unlink(missing_ok=True)

    def _save_request(self, request: TargetJobRequest) -> None:
        path = self._request_path(request.job_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.{os.getpid()}.part")
        try:
            temporary.write_text(
                request.model_dump_json(by_alias=True, exclude_none=True, indent=2),
                encoding="utf-8",
            )
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)

    def _load_request(self, job_id: str) -> TargetJobRequest:
        path = self._request_path(job_id)
        if not path.is_file():
            raise RuntimeError(f"local audio job request is unavailable: {job_id}")
        return TargetJobRequest.model_validate_json(path.read_text(encoding="utf-8"))

    def _recover_interrupted_jobs(self) -> None:
        jobs_root = self.workspace_root / "jobs"
        if not jobs_root.is_dir():
            return
        for path in jobs_root.glob("*/local-audio-status.json"):
            status = TargetJobStatus.model_validate_json(path.read_text(encoding="utf-8"))
            if status.state in {"queued", "running", "canceling"}:
                status = status.model_copy(
                    update={
                        "state": "interrupted",
                        "progress": status.progress.model_copy(update={"phase": "interrupted"}),
                        "updated_at": utc_timestamp_now(),
                    }
                )
                self._save_status(status)
            else:
                self._statuses[status.job_id] = status

    def contract(self) -> TargetContract:
        options_schema = JsonSchemaDocument.from_schema(
            "munchy.audio-local.options/v1",
            {"type": "object", "additionalProperties": False},
        )
        operation = operation_contract(AUDIO_ARCHIVE_OPERATION)
        return TargetContract.seal(
            TargetContractPayload(
                implementation_id="munchy.audio-local/v1",
                implementation_version=importlib.metadata.version("munchy-server"),
                source_revision=os.getenv("MUNCHY_SOURCE_REVISION", "unknown").strip() or "unknown",
                operations=(
                    TargetOperationSupport(
                        operation_id=operation.id,
                        operation_contract_sha256=operation.contract_sha256,
                        options_schema=options_schema,
                    ),
                ),
            )
        )

    def preflight(self, request: TargetPreflightRequest) -> TargetPreflightResponse:
        if request.operation_id != AUDIO_ARCHIVE_OPERATION:
            raise ValueError(f"unsupported local audio operation: {request.operation_id}")
        operation = operation_contract(AUDIO_ARCHIVE_OPERATION)
        if request.operation_contract_sha256 != operation.contract_sha256:
            raise ValueError("operation contract digest mismatch")
        if request.target_options:
            raise ValueError("local audio target does not accept target options")
        validate_artifacts_against_operation(operation, inputs=request.inputs)
        verify_artifacts(self.workspace_root, "input", request.workspace_id, request.inputs)
        intent = validate_operation_intent(request.operation_id, request.intent)
        if intent.archive.codec != "opus" or intent.archive.container != "opus":
            raise ValueError("local audio target requires opus in an opus container")
        projected = [
            PurePosixPath(artifact.path).with_suffix(".opus").as_posix()
            for artifact in request.inputs
            if artifact.role == SOURCE_ROLE
        ]
        if len(projected) != len(set(projected)):
            raise ValueError("archive inputs project to duplicate output paths")
        contract = self.contract()
        plan = TransformPlan.seal(
            TransformPlanPayload(
                operation_id=request.operation_id,
                operation_contract_sha256=request.operation_contract_sha256,
                workspace_id=request.workspace_id,
                inputs=request.inputs,
                intent=request.intent,
                target_options={},
                target_implementation_id=contract.implementation_id,
                target_contract_sha256=contract.contract_sha256,
                effective_intent=intent.model_dump(mode="json", exclude_none=True),
                effective_target_options={},
            )
        )
        return TargetPreflightResponse(target=contract, plan=plan)

    @staticmethod
    def _status(
        request: TargetJobRequest,
        state: TargetJobState,
        *,
        outputs: tuple[Artifact, ...] = (),
        evidence: TargetExecutionEvidence | None = None,
        failure: TargetFailure | None = None,
        started_at: str | None = None,
        finished_at: str | None = None,
    ) -> TargetJobStatus:
        total = sum(artifact.role == SOURCE_ROLE for artifact in request.plan.inputs)
        return TargetJobStatus(
            job_id=request.job_id,
            attempt=request.attempt,
            request_sha256=request.request_sha256,
            plan_sha256=request.plan.plan_sha256,
            state=state,
            progress=TargetProgress(
                phase=state,
                completed=total if state == "succeeded" else 0,
                total=total,
            ),
            outputs=outputs,
            execution_evidence=evidence,
            failure=failure,
            started_at=started_at,
            finished_at=finished_at,
            updated_at=utc_timestamp_now(),
        )

    @staticmethod
    def _ffmpeg_version() -> str:
        try:
            result = subprocess.run(
                ["ffmpeg", "-version"],
                capture_output=True,
                check=False,
                text=True,
                timeout=10,
            )
        except (OSError, subprocess.SubprocessError):
            return "unavailable"
        lines = (result.stdout or result.stderr).strip().splitlines()
        return lines[0] if lines else "unavailable"

    def _execution_evidence(self, request: TargetJobRequest) -> TargetExecutionEvidence:
        return TargetExecutionEvidence(
            target=self.contract(),
            operation=operation_contract(request.plan.operation_id),
            effective_intent=request.plan.effective_intent,
            effective_target_options=request.plan.effective_target_options,
            tools=(ExecutionToolEvidence(name="ffmpeg", version=self._ffmpeg_version()),),
        )

    def put_job(self, request: TargetJobRequest) -> TargetJobStatus:
        existing = self._statuses.get(request.job_id)
        if existing is not None:
            if existing.request_sha256 == request.request_sha256:
                return existing
            if not (
                existing.state == "interrupted"
                and request.attempt == existing.attempt + 1
                and request.plan.plan_sha256 == existing.plan_sha256
            ):
                raise RuntimeError("local audio job ID is bound to an immutable request")
        elif request.attempt != 1:
            raise RuntimeError("a new local audio job must begin with attempt 1")
        preflight = self.preflight(
            TargetPreflightRequest(
                operation_id=request.plan.operation_id,
                operation_contract_sha256=request.plan.operation_contract_sha256,
                workspace_id=request.plan.workspace_id,
                inputs=request.plan.inputs,
                intent=request.plan.intent,
                target_options=request.plan.target_options,
            )
        )
        if preflight.plan != request.plan:
            raise RuntimeError("local audio job plan does not match preflight")
        queued = self._status(request, "queued")
        self._save_request(request)
        self._save_status(queued)
        cancel_event = threading.Event()
        thread = threading.Thread(
            target=self._run_job,
            args=(request, cancel_event),
            name=f"munchy-audio-{request.job_id}",
            daemon=True,
        )
        with self._lock:
            self._cancel_events[request.job_id] = cancel_event
            self._threads[request.job_id] = thread
        thread.start()
        return queued

    def _run_job(self, request: TargetJobRequest, cancel_event: threading.Event) -> None:
        started_at = utc_timestamp_now()
        try:
            if cancel_event.is_set():
                raise LocalAudioTransformCanceled("local audio job canceled before execution")
            self._save_status(self._status(request, "running", started_at=started_at))
            if cancel_event.is_set():
                raise LocalAudioTransformCanceled("local audio job canceled before execution")
            intent = validate_operation_intent(
                AUDIO_ARCHIVE_OPERATION, request.plan.effective_intent
            )
            input_root = workspace_area_root(self.workspace_root, "input", request.job_id)
            staging_root = (
                self.workspace_root / "jobs" / request.job_id / f"attempt-{request.attempt}-output"
            )
            shutil.rmtree(staging_root, ignore_errors=True)
            staging_root.mkdir(parents=True)
            output_root = workspace_area_root(self.workspace_root, "output", request.job_id)
            shutil.rmtree(output_root, ignore_errors=True)
            by_id = {artifact.id: artifact for artifact in request.plan.inputs}
            sidecars: dict[str, list[dict[str, Any]]] = {}
            for source_id, associations in intent.source_artifact_sidecars.items():
                source = by_id[source_id]
                sidecars[source.path] = [
                    {
                        "id": item.sidecar_id,
                        "format": item.format,
                        "path": str(
                            workspace_artifact_path(
                                self.workspace_root,
                                "input",
                                request.job_id,
                                by_id[item.artifact_id].path,
                            )
                        ),
                        "arcname": item.arcname,
                        "source_rel_path": item.source_rel_path,
                    }
                    for item in associations
                ]
            projection_metadata = {
                by_id[artifact_id].path: ProjectionMetadata.from_dict(metadata)
                for artifact_id, metadata in intent.container_metadata.items()
            }
            profile: dict[str, Any] = {
                "target": "munchy-audio",
                "archive": intent.archive.model_dump(mode="json", exclude_none=True),
                "target_evidence": {
                    "protocol": request.protocol,
                    "job_id": request.job_id,
                    "attempt": request.attempt,
                    "request_sha256": request.request_sha256,
                    "plan_sha256": request.plan.plan_sha256,
                    "target": self.contract().model_dump(
                        mode="json", by_alias=True, exclude_none=True
                    ),
                    "operation": operation_contract(request.plan.operation_id).model_dump(
                        mode="json", by_alias=True, exclude_none=True
                    ),
                    "effective_intent": request.plan.effective_intent,
                    "effective_target_options": request.plan.effective_target_options,
                },
            }
            if intent.source is not None:
                profile["source"] = intent.source.model_dump(mode="json", exclude_none=True)
            run_archive_audio_group(
                input_root=input_root,
                output_root=staging_root,
                group_config={"encode_profile": profile},
                source_rel_paths={
                    artifact.path
                    for artifact in request.plan.inputs
                    if artifact.role == SOURCE_ROLE
                },
                source_artifacts_sidecars=sidecars,
                target_outputs=_local_audio_target_outputs(request),
                projection_metadata_by_rel_path=projection_metadata,
            )
            if cancel_event.is_set():
                raise LocalAudioTransformCanceled("local audio job canceled during execution")
            outputs: list[Artifact] = []
            for output_path in sorted(path for path in staging_root.rglob("*") if path.is_file()):
                relative = output_path.relative_to(staging_root).as_posix()
                role = (
                    SOURCE_ARTIFACTS_ROLE
                    if output_path.name.endswith(SOURCE_ARTIFACTS_SUFFIX)
                    else AUDIO_ARCHIVE_ROLE
                )
                artifact = Artifact(
                    id=_target_output_artifact_id(relative),
                    role=role,
                    path=relative,
                    bytes=output_path.stat().st_size,
                    sha256=upload_service.file_sha256(output_path),
                    derived_from=_local_audio_output_derivation(request, relative, role),
                )
                publish_file_atomically(
                    output_path,
                    workspace_artifact_path(
                        self.workspace_root,
                        "output",
                        request.job_id,
                        relative,
                    ),
                )
                outputs.append(artifact)
            output_tuple = tuple(outputs)
            validate_artifacts_against_operation(
                operation_contract(AUDIO_ARCHIVE_OPERATION),
                inputs=request.plan.inputs,
                outputs=output_tuple,
            )
            verify_artifacts(self.workspace_root, "output", request.job_id, output_tuple)
            status = self._status(
                request,
                "succeeded",
                outputs=output_tuple,
                evidence=self._execution_evidence(request),
                started_at=started_at,
                finished_at=utc_timestamp_now(),
            )
        except LocalAudioTransformCanceled as exc:
            status = self._status(
                request,
                "canceled",
                evidence=self._execution_evidence(request),
                failure=TargetFailure(
                    code="job_canceled",
                    message=str(exc),
                    retryable=False,
                ),
                started_at=started_at,
                finished_at=utc_timestamp_now(),
            )
        except Exception as exc:
            status = self._status(
                request,
                "failed",
                evidence=self._execution_evidence(request),
                failure=TargetFailure(
                    code="target_execution_failed",
                    message=str(exc),
                    retryable=False,
                ),
                started_at=started_at,
                finished_at=utc_timestamp_now(),
            )
        self._save_status(status)
        with self._lock:
            self._threads.pop(request.job_id, None)
            self._cancel_events.pop(request.job_id, None)

    def status(self, job_id: str) -> TargetJobStatus:
        with self._lock:
            status = self._statuses.get(job_id)
        if status is not None:
            return status
        path = self._status_path(job_id)
        if not path.is_file():
            raise RuntimeError(f"unknown local audio job: {job_id}")
        status = TargetJobStatus.model_validate_json(path.read_text(encoding="utf-8"))
        with self._lock:
            self._statuses[job_id] = status
        return status

    def cancel(self, job_id: str, request: TargetCancelRequest) -> TargetJobStatus:
        with self._lock:
            status = self.status(job_id)
            if status.state in {"succeeded", "failed", "canceled"}:
                return status
            cancel_event = self._cancel_events.get(job_id)
            if cancel_event is not None:
                cancel_event.set()
            if status.state == "interrupted":
                accepted = self._load_request(job_id)
                canceled = self._status(
                    accepted,
                    "canceled",
                    evidence=self._execution_evidence(accepted),
                    failure=TargetFailure(
                        code="job_canceled",
                        message=request.reason,
                        retryable=False,
                    ),
                    started_at=status.started_at,
                    finished_at=utc_timestamp_now(),
                )
                self._save_status(canceled)
                return canceled
            canceling = status.model_copy(
                update={
                    "state": "canceling",
                    "progress": status.progress.model_copy(update={"phase": "canceling"}),
                    "updated_at": utc_timestamp_now(),
                }
            )
            self._save_status(canceling)
            return canceling
