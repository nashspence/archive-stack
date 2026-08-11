from __future__ import annotations

import copy
import hashlib
import logging
import logging.config
import shutil
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from lifecycle_events.repeats import (
    next_event_repeat_at,
)
from munchy_workflows.review_sweep import (
    default_encode_profile_for_output_mode,
    review_sweep_variants,
)
from time_formats import (
    format_utc_timestamp,
    utc_now,
    utc_timestamp_now,
)

import munchy_core.domain.errors as domain_errors
import munchy_core.domain.models as domain_models
import munchy_core.persistence.sqlite_state as state_store
import munchy_core.runtime.config as runtime_config
import munchy_core.runtime.execution as execution_runtime
import munchy_core.services.admission as admission_service
import munchy_core.services.events as event_service
import munchy_core.services.handoffs as handoff_service
import munchy_core.services.media as media_service
import munchy_core.services.routing as routing_service
import munchy_core.services.scheduling as scheduling_service
import munchy_core.services.uploads as upload_service

log = logging.getLogger("munchy.server")


def eager_archive_group_names(groups: dict[str, dict[str, Any]]) -> set[str]:
    return {
        str(group_name)
        for group_name, group_config in groups.items()
        if routing_service.group_is_eager_archive_only(group_config)
    }


def eager_archive_batch_limit(group_config: dict[str, Any]) -> int:
    executor = routing_service.eager_archive_executor(group_config)
    if executor == "local_audio":
        return runtime_config.AUDIO_ARCHIVE_MAX_PARALLEL
    return runtime_config.EAGER_ARCHIVE_BATCH_FILES


def eager_archive_pipeline_limit(group_config: dict[str, Any]) -> int:
    configured = group_config.get("eager_pipeline_batches")
    if configured is None:
        return runtime_config.EAGER_ARCHIVE_PIPELINE_BATCHES
    try:
        value = int(configured)
    except (TypeError, ValueError):
        return runtime_config.EAGER_ARCHIVE_PIPELINE_BATCHES
    return max(1, min(value, runtime_config.EAGER_ARCHIVE_PIPELINE_BATCHES))


def emit_upload_stalled(
    job: dict[str, Any],
    upload: dict[str, Any],
    progress: dict[str, Any],
) -> dict[str, Any] | None:
    interval = max(0, runtime_config.EVENT_REPEAT_INTERVAL_SECONDS)
    if interval <= 0:
        return None
    if int(progress.get("files_uploaded") or 0) >= int(progress.get("files_total") or 0):
        return None

    now = datetime.now(UTC)
    last_activity = upload_service.input_upload_data_last_activity(upload)
    stalled_seconds = max(0.0, (now - last_activity).total_seconds())
    if stalled_seconds < interval:
        return None
    next_due = next_event_repeat_at(
        last_activity,
        interval=interval,
        repeat_time=runtime_config.EVENT_REPEAT_TIME,
        repeat_timezone=runtime_config.EVENT_REPEAT_TIMEZONE,
    )
    if next_due is not None and now < next_due:
        return None

    files_uploaded = int(progress.get("files_uploaded") or 0)
    files_total = int(progress.get("files_total") or 0)
    message = f"Upload paused: {files_uploaded}/{files_total} files. Resume or cancel."
    extra: dict[str, Any] = {
        "input_upload_id": str(upload.get("input_upload_id") or ""),
        "upload_progress": progress,
        "last_upload_activity_at": format_utc_timestamp(last_activity),
        "stalled_seconds": int(stalled_seconds),
    }
    encode_progress = encode_progress_for_job(job)
    if encode_progress is not None:
        extra["encode_progress"] = encode_progress
    return event_service.emit_job_event(
        job,
        "job.upload_stalled",
        message,
        severity="warning",
        extra=extra,
    )


def review_sweep_config(job: Mapping[str, Any]) -> dict[str, Any] | None:
    if str(job.get("workflow_mode") or "") != "review":
        return None
    review = job.get("review")
    if not isinstance(review, Mapping):
        return None
    sweep = review.get("sweep")
    return dict(sweep) if isinstance(sweep, Mapping) else None


def is_review_sweep_job(job: Mapping[str, Any]) -> bool:
    return review_sweep_config(job) is not None


def review_tasks_for_group(group_config: Mapping[str, Any]) -> list[domain_models.TaskName]:
    return [
        cast(domain_models.TaskName, str(task))
        for task in group_config.get("tasks") or []
        if str(task) in {"qcut_video", "audio_review"}
    ]


def group_base_encode_profile(group_config: Mapping[str, Any]) -> dict[str, Any]:
    profile = group_config.get("encode_profile")
    if isinstance(profile, Mapping):
        return copy.deepcopy(dict(profile))
    output_mode = domain_models.normalize_output_mode(
        str(group_config.get("output_mode") or "video")
    )
    return default_encode_profile_for_output_mode(output_mode)


def review_sweep_route_file_states(
    upload: dict[str, Any],
    groups: dict[str, dict[str, Any]],
    *,
    requested_route_ids: set[str],
) -> dict[str, dict[str, Any]]:
    routes: dict[str, dict[str, Any]] = {}
    selected_groups = set(groups)
    for file_state in upload_service.mutable_primary_upload_files_for_groups(
        upload, selected_groups
    ):
        group_name = upload_service.upload_file_resolved_group(file_state)
        if not group_name or group_name not in groups:
            continue
        group_config = groups[group_name]
        tasks = review_tasks_for_group(group_config)
        if not tasks:
            continue
        route_id = str(file_state.get("route_id") or group_name).strip()
        if not route_id:
            continue
        if requested_route_ids and route_id not in requested_route_ids:
            continue
        route = routes.setdefault(
            route_id,
            {
                "route_id": route_id,
                "group_name": group_name,
                "tasks": tasks,
                "file_states": [],
            },
        )
        if route["group_name"] != group_name:
            raise RuntimeError(
                f"review sweep route {route_id!r} resolved to multiple groups: "
                f"{route['group_name']}, {group_name}"
            )
        route["file_states"].append(file_state)
    if requested_route_ids:
        missing = sorted(requested_route_ids - set(routes))
        if missing:
            raise RuntimeError(
                "review sweep route(s) had no reviewable files: " + ", ".join(missing)
            )
    if not routes:
        raise RuntimeError("review sweep found no reviewable routes")
    return routes


def prepare_review_sweep_route_input(
    *,
    upload: dict[str, Any],
    input_dir: Path,
    route_input_root: Path,
    group_name: str,
    file_states: list[dict[str, Any]],
) -> None:
    if route_input_root.exists():
        shutil.rmtree(route_input_root)
    route_input_root.mkdir(parents=True, exist_ok=True)
    all_file_states = [
        *file_states,
        *upload_service.sidecar_evidence_files_for_primaries(upload, file_states),
    ]
    for file_state in all_file_states:
        rel_path = upload_service.upload_file_group_rel_for_state(file_state, group_name)
        source = input_dir / group_name / rel_path
        if not source.is_file():
            raise RuntimeError(f"review sweep source file is missing: {source}")
        upload_service.link_or_copy(source, route_input_root / group_name / rel_path)


def review_sweep_result_state(job: dict[str, Any]) -> dict[str, Any]:
    result = job.setdefault(
        "review_sweep_result",
        {
            "kind": "munchy.review-sweep",
            "schema_version": 1,
            "started_at": utc_timestamp_now(),
            "routes": {},
            "variants": [],
        },
    )
    if not isinstance(result, dict):
        result = {
            "kind": "munchy.review-sweep",
            "schema_version": 1,
            "started_at": utc_timestamp_now(),
            "routes": {},
            "variants": [],
        }
        job["review_sweep_result"] = result
    return result


def clear_handoff_attempt_state(job: dict[str, Any], result_key: str) -> None:
    job.pop(result_key, None)
    attempts = job.get("handoff_attempts")
    if not isinstance(attempts, dict):
        return
    for key in (
        result_key,
        f"{result_key}_last_attempt_at",
        f"{result_key}_succeeded_at",
        f"{result_key}_next_retry_at",
        f"{result_key}_last_error",
    ):
        attempts.pop(key, None)
    if not attempts:
        job.pop("handoff_attempts", None)


def run_review_sweep_job(
    job: dict[str, Any],
    *,
    input_upload: dict[str, Any],
    groups: dict[str, dict[str, Any]],
    input_dir: Path,
    gpu_job_root: Path,
    review_dir: Path,
) -> None:
    sweep = review_sweep_config(job)
    if sweep is None:
        raise RuntimeError("job is not a review sweep")
    job_id = str(job["job_id"])
    requested_route_ids = {
        str(route_id).strip() for route_id in sweep.get("route_ids") or [] if str(route_id).strip()
    }
    routes = review_sweep_route_file_states(
        input_upload,
        groups,
        requested_route_ids=requested_route_ids,
    )
    route_variants: dict[str, list[dict[str, Any]]] = {}
    total_variants = 0
    for route_id, route in sorted(routes.items()):
        group_name = str(route["group_name"])
        group_config = groups[group_name]
        variants = review_sweep_variants(
            sweep,
            base_profile=group_base_encode_profile(group_config),
            route_id=route_id,
        )
        if not variants:
            raise RuntimeError(f"review sweep route {route_id!r} produced no variants")
        route_variants[route_id] = variants
        total_variants += len(variants)

    result = review_sweep_result_state(job)
    result["routes"] = {
        route_id: {
            "route_id": route_id,
            "group": route["group_name"],
            "tasks": route["tasks"],
            "files": len(route["file_states"]),
            "variants": [variant["profile_id"] for variant in route_variants[route_id]],
        }
        for route_id, route in sorted(routes.items())
    }
    result["variants_total"] = total_variants
    result["variants_completed"] = len(result.get("variants") or [])
    job["phase"] = "review_sweep"
    state_store.save_job(job)

    token = media_service.acquire_job_gpu(job)
    notified_handoff = False
    completed = len(result.get("variants") or [])
    try:
        for route_id, route in sorted(routes.items()):
            group_name = str(route["group_name"])
            group_config = groups[group_name]
            tasks = cast(list[domain_models.TaskName], list(route["tasks"]))
            route_input_root = (
                gpu_job_root / "review-sweep-input" / upload_service.opaque_local_id(route_id)
            )
            prepare_review_sweep_route_input(
                upload=input_upload,
                input_dir=input_dir,
                route_input_root=route_input_root,
                group_name=group_name,
                file_states=cast(list[dict[str, Any]], route["file_states"]),
            )
            for variant in route_variants[route_id]:
                state_store.raise_if_job_canceled(job_id)
                profile_id = domain_models.validate_group_name(str(variant["profile_id"]))
                variant_key = f"{route_id}/{profile_id}"
                if any(
                    isinstance(item, dict) and item.get("variant") == variant_key
                    for item in result.get("variants") or []
                ):
                    continue
                variant_archive_dir = (
                    gpu_job_root
                    / "review-sweep-archive"
                    / upload_service.opaque_local_id(route_id)
                    / upload_service.opaque_local_id(profile_id)
                )
                variant_review_dir = (
                    review_dir
                    / upload_service.opaque_local_id(route_id)
                    / upload_service.opaque_local_id(profile_id)
                )
                gpu_job_id = routing_service.gpu_group_job_id(job_id, f"{route_id}-{profile_id}")
                gpu_payload = {
                    "job_id": gpu_job_id,
                    "input_dir": upload_service.gpu_runtime_container_path(
                        route_input_root / group_name
                    ),
                    "archive_dir": upload_service.gpu_runtime_container_path(variant_archive_dir),
                    "review_dir": upload_service.gpu_runtime_container_path(variant_review_dir),
                    "profile": profile_id,
                    "tasks": tasks,
                    "run_id": job.get("run_id"),
                    "container_metadata_required": (
                        routing_service.gpu_tasks_require_container_metadata(
                            tasks,
                            group_config,
                        )
                    ),
                    "encode_profile": variant["encode_profile"],
                }
                if group_config.get("max_parallel_encodes") is not None:
                    gpu_payload["max_parallel_encodes"] = group_config["max_parallel_encodes"]
                review_clip_plan = state_store.dict_or_empty(
                    state_store.dict_or_empty(job.get("review")).get("clip_plan")
                )
                if review_clip_plan:
                    gpu_payload["review_clip_plan"] = copy.deepcopy(review_clip_plan)
                for task_name in ("qcut_video", "audio_review"):
                    if task_name not in tasks:
                        continue
                    review_plan = upload_service.load_shared_review_plan(
                        str(job["input_upload_id"]),
                        route_id,
                        task_name,
                    )
                    if review_plan is not None:
                        gpu_payload.setdefault("review_plans", {})[task_name] = review_plan
                job["phase"] = f"review_sweep:{route_id}:{profile_id}"
                job.setdefault("gpu_payloads", {})[variant_key] = gpu_payload
                state_store.save_job(job)
                media_service.start_gpu_job(gpu_payload)
                gpu_result = media_service.wait_gpu_job(
                    gpu_job_id, gpu_payload=gpu_payload, job=job
                )
                job.setdefault("gpu_results", {})[variant_key] = gpu_result
                upload_service.remember_review_plans_from_gpu_result(job, route_id, gpu_result)
                state_store.save_job(job)

                if not notified_handoff:
                    event_service.emit_job_event(
                        job,
                        "review.handoff",
                        "Review sweep artifacts are complete; handing off for upload.",
                        extra={
                            "component": "review_sweep",
                            "routes_total": len(routes),
                            "variants_total": total_variants,
                        },
                    )
                    notified_handoff = True
                upload_result = handoff_service.advance_handoff(
                    job,
                    variant_review_dir,
                    final=True,
                    source_label="review sweep",
                    context={
                        "route_id": route_id,
                        "profile_id": profile_id,
                    },
                )
                latest = state_store.read_state("job", job_id)
                if isinstance(latest, dict):
                    job.clear()
                    job.update(latest)
                clear_handoff_attempt_state(job, "handoff_receipt")
                result = review_sweep_result_state(job)
                result.setdefault("variants", []).append(
                    {
                        "variant": variant_key,
                        "route_id": route_id,
                        "profile_id": profile_id,
                        "tasks": tasks,
                        "encode_settings": variant.get("encode_settings") or {},
                        "axis_values": variant.get("axis_values") or {},
                        "handoff_receipt": upload_result,
                        "completed_at": utc_timestamp_now(),
                    }
                )
                completed += 1
                result["variants_completed"] = completed
                job["phase"] = f"review_sweep:{completed}/{total_variants}"
                state_store.save_job(job)
    finally:
        media_service.release_job_gpu(job, token)

    result = review_sweep_result_state(job)
    result["finished_at"] = utc_timestamp_now()
    result["variants_completed"] = len(result.get("variants") or [])
    state_store.save_job(job)


def ensure_job_groups(job: dict[str, Any], input_upload: dict[str, Any]) -> dict[str, Any]:
    groups = job.get("groups")
    if isinstance(groups, dict) and groups:
        return groups
    groups = {
        name: {
            "output_mode": job.get("output_mode", "video"),
            "tasks": list(job.get("tasks", [])),
            "profile": job.get("profile", "av1-nvenc-high"),
            "encode_profile": job.get("encode_profile"),
        }
        for name in upload_service.input_upload_groups(input_upload)
    }
    job["groups"] = groups
    state_store.save_job(job)
    return groups


def eager_archive_state(job: dict[str, Any]) -> dict[str, Any]:
    state = job.setdefault("eager_archive", {"files": {}, "batches": {}, "next_batch_number": 1})
    if not isinstance(state, dict):
        state = {"files": {}, "batches": {}, "next_batch_number": 1}
        job["eager_archive"] = state
    return cast(dict[str, Any], state)


def eager_file_encoded(job: dict[str, Any], rel_path: str) -> bool:
    files = eager_archive_state(job).setdefault("files", {})
    item = files.get(rel_path)
    return isinstance(item, dict) and item.get("state") == "encoded"


def eager_file_claimed(job: dict[str, Any], rel_path: str) -> bool:
    files = eager_archive_state(job).setdefault("files", {})
    item = files.get(rel_path)
    return isinstance(item, dict) and item.get("state") in {"encoding", "encoded", "failed"}


def format_log_bytes(value: int | str | None) -> str:
    try:
        num = int(value or 0)
    except (TypeError, ValueError):
        num = 0
    units = ("B", "KiB", "MiB", "GiB", "TiB")
    amount = float(num)
    unit = units[0]
    for unit in units:
        if amount < 1024 or unit == units[-1]:
            break
        amount /= 1024
    if unit == "B":
        return f"{num} B"
    return f"{amount:.2f} {unit}"


def mark_eager_file_encoding(
    job: dict[str, Any],
    file_state: dict[str, Any],
    *,
    group_name: str,
    group_config: dict[str, Any],
    archive_dir: Path,
    batch_id: str,
) -> None:
    rel_path = str(file_state["path"])
    started_at = utc_timestamp_now()
    output = routing_service.archive_output_for_upload_file(
        file_state,
        group_name=group_name,
        group_config=group_config,
        archive_dir=archive_dir,
    )
    files = eager_archive_state(job).setdefault("files", {})
    files[rel_path] = {
        "state": "encoding",
        "started_at": started_at,
        "batch_id": batch_id,
        "group": group_name,
        "input_bytes": int(file_state.get("bytes") or 0),
        "output": str(output),
    }
    log.info(
        "encoding started job=%s group=%s batch=%s path=%s input=%s output=%s",
        job.get("job_id"),
        group_name,
        batch_id,
        rel_path,
        format_log_bytes(file_state.get("bytes")),
        output,
    )


def mark_eager_file_encoded(
    job: dict[str, Any],
    file_state: dict[str, Any],
    *,
    group_name: str,
    group_config: dict[str, Any],
    archive_dir: Path,
    batch_id: str | None,
    detected_existing: bool = False,
) -> None:
    rel_path = str(file_state["path"])
    encoded_at = utc_timestamp_now()
    output = routing_service.archive_output_for_upload_file(
        file_state,
        group_name=group_name,
        group_config=group_config,
        archive_dir=archive_dir,
    )
    files = eager_archive_state(job).setdefault("files", {})
    previous = files.get(rel_path) if isinstance(files.get(rel_path), dict) else {}
    started = (
        state_store.safe_parse_timestamp(previous.get("started_at"))
        if isinstance(previous, dict)
        else None
    )
    elapsed = ""
    if started is not None:
        elapsed = f" elapsed={max(0.0, (datetime.now(UTC) - started).total_seconds()):.1f}s"
    output_bytes = output.stat().st_size if output.exists() else 0
    files[rel_path] = {
        "state": "encoded",
        "encoded_at": encoded_at,
        "batch_id": batch_id,
        "group": group_name,
        "input_bytes": int(file_state.get("bytes") or 0),
        "output_bytes": output_bytes,
        "output": str(output),
        "detected_existing": detected_existing,
    }
    log.info(
        "encoding finished job=%s group=%s batch=%s path=%s output=%s output_bytes=%s%s%s",
        job.get("job_id"),
        group_name,
        batch_id or "",
        rel_path,
        output,
        format_log_bytes(output_bytes),
        elapsed,
        " detected_existing=true" if detected_existing else "",
    )


def mark_eager_file_failed(
    job: dict[str, Any],
    file_state: dict[str, Any],
    *,
    group_name: str,
    group_config: dict[str, Any],
    archive_dir: Path,
    batch_id: str | None,
    error: str,
) -> None:
    rel_path = str(file_state["path"])
    output = routing_service.archive_output_for_upload_file(
        file_state,
        group_name=group_name,
        group_config=group_config,
        archive_dir=archive_dir,
    )
    files = eager_archive_state(job).setdefault("files", {})
    previous = files.get(rel_path) if isinstance(files.get(rel_path), dict) else {}
    started = previous.get("started_at") if isinstance(previous, dict) else None
    files[rel_path] = {
        "state": "failed",
        "started_at": started or utc_timestamp_now(),
        "failed_at": utc_timestamp_now(),
        "batch_id": batch_id,
        "group": group_name,
        "input_bytes": int(file_state.get("bytes") or 0),
        "output": str(output),
        "error": error,
    }


def consume_input_upload_file(upload: dict[str, Any], file_state: dict[str, Any]) -> bool:
    if file_state.get("consumed_at"):
        return False
    file_state["consumed_at"] = utc_timestamp_now()
    file_state["consumed_bytes"] = int(file_state["bytes"])
    if file_state.get("sha256"):
        file_state["consumed_sha256"] = file_state["sha256"]
    upload_service.remove_input_file_data(file_state)
    return True


def consume_input_upload_files(upload_id: str, rel_paths: set[str]) -> dict[str, Any]:
    with execution_runtime.input_upload_state_lock(upload_id):
        upload = upload_service.load_input_upload(upload_id)
        changed = False
        by_path = {str(file_state["path"]): file_state for file_state in upload.get("files", [])}
        for rel_path in sorted(rel_paths):
            file_state = by_path.get(rel_path)
            if file_state is None:
                raise RuntimeError(f"unknown input file while consuming source: {rel_path}")
            changed = consume_input_upload_file(upload, file_state) or changed
        if changed:
            return upload_service.save_input_upload(upload)
        return upload


def mark_existing_eager_outputs(
    job: dict[str, Any],
    upload: dict[str, Any],
    groups: dict[str, dict[str, Any]],
    eager_groups: set[str],
    archive_dir: Path,
) -> tuple[dict[str, Any], bool]:
    upload_id = str(upload["input_upload_id"])
    with execution_runtime.input_upload_state_lock(upload_id):
        snapshot = upload_service.load_input_upload_raw(upload_id)
    return mark_existing_eager_outputs_from_snapshot(
        job,
        snapshot,
        groups,
        eager_groups,
        archive_dir,
    )


def mark_existing_eager_outputs_from_snapshot(
    job: dict[str, Any],
    upload: dict[str, Any],
    groups: dict[str, dict[str, Any]],
    eager_groups: set[str],
    archive_dir: Path,
) -> tuple[dict[str, Any], bool]:
    changed = False
    upload_changed = False
    consume_paths: set[str] = set()
    for group_name in sorted(eager_groups):
        group_config = groups[group_name]
        for file_state in upload_service.mutable_primary_upload_files_for_groups(
            upload, {group_name}
        ):
            rel_path = str(file_state["path"])
            if eager_file_claimed(job, rel_path):
                continue
            output = routing_service.archive_output_for_upload_file(
                file_state,
                group_name=group_name,
                group_config=group_config,
                archive_dir=archive_dir,
            )
            if not output.exists():
                continue
            source_artifacts_sidecar = routing_service.source_artifact_sidecar_for_archive_output(
                output
            )
            if (
                upload_service.sidecar_evidence_files_for_primary(
                    upload,
                    file_state,
                )
                and not source_artifacts_sidecar.exists()
            ):
                continue
            if routing_service.ensure_file_projection_metadata(
                upload,
                file_state,
                job=job,
                group_config=group_config,
            ):
                upload_changed = True
            mark_eager_file_encoded(
                job,
                file_state,
                group_name=group_name,
                group_config=group_config,
                archive_dir=archive_dir,
                batch_id=None,
                detected_existing=True,
            )
            changed = True
            status = upload_service.upload_file_status(file_state)
            if status["upload_state"] == "uploaded":
                consume_paths.add(rel_path)
    if changed:
        state_store.save_job(job)
    upload = upload_service.merge_input_upload_projection_metadata(
        str(upload["input_upload_id"]),
        upload.get("files", []) if upload_changed else (),
    )
    if consume_paths:
        upload = consume_input_upload_files(str(upload["input_upload_id"]), consume_paths)
    return upload, changed or upload_changed or bool(consume_paths)


def claim_running_eager_batch_files(
    job: dict[str, Any],
    upload: dict[str, Any],
    groups: dict[str, dict[str, Any]],
    archive_dir: Path,
) -> bool:
    by_path = {str(file_state["path"]): file_state for file_state in upload.get("files", [])}
    changed = False
    for batch in running_eager_batches(job):
        group_name = str(batch.get("group") or "")
        group_config = groups.get(group_name)
        if group_config is None:
            continue
        batch_id = str(batch.get("batch_id") or "")
        if not batch_id:
            continue
        for rel_path in [str(path) for path in batch.get("paths") or []]:
            if eager_file_claimed(job, rel_path):
                continue
            file_state = by_path.get(rel_path)
            if file_state is None:
                continue
            mark_eager_file_encoding(
                job,
                file_state,
                group_name=group_name,
                group_config=group_config,
                archive_dir=archive_dir,
                batch_id=batch_id,
            )
            changed = True
    if changed:
        state_store.save_job(job)
    return changed


def eager_groups_complete(
    job: dict[str, Any],
    upload: dict[str, Any],
    eager_groups: set[str],
) -> bool:
    if isinstance(job.get("routing"), dict) and str(upload.get("state") or "") != "uploaded":
        return False
    files = [
        file_state
        for group_name in eager_groups
        for file_state in upload_service.primary_upload_files_for_groups(upload, {group_name})
    ]
    return bool(files) and all(
        eager_file_encoded(job, str(file_state["path"])) for file_state in files
    )


def safe_file_size(path: str | Path | None) -> int:
    if not path:
        return 0
    try:
        file_path = Path(path)
        return file_path.stat().st_size if file_path.exists() else 0
    except OSError:
        return 0


def review_encode_progress_for_job(job: dict[str, Any]) -> dict[str, Any] | None:
    statuses = job.get("gpu_statuses")
    if not isinstance(statuses, dict):
        return None
    progress_items: list[dict[str, Any]] = []
    for status in statuses.values():
        if not isinstance(status, dict):
            continue
        items = status.get("items")
        if not isinstance(items, dict):
            continue
        for task_name in ("qcut_video", "audio_review"):
            item = items.get(task_name)
            if not isinstance(item, dict):
                continue
            progress = item.get("progress")
            if isinstance(progress, dict):
                progress_items.append(progress)
    if not progress_items:
        return None

    # Prefer active work; otherwise show the most recently updated completed review task.
    progress_items.sort(
        key=lambda item: (
            0 if str(item.get("phase") or "") != "done" else 1,
            str(item.get("started_at") or ""),
        )
    )
    progress = dict(progress_items[0])
    clips_total = int(progress.get("clips_total") or 0)
    clips_done = int(progress.get("clips_done") or 0)
    clips_running = int(progress.get("clips_running") or 0)
    clips_failed = int(progress.get("clips_failed") or 0)
    pct = float(
        progress.get("percent_clips")
        or ((clips_done / clips_total * 100.0) if clips_total else 100.0)
    )
    return {
        **progress,
        "mode": str(progress.get("mode") or progress.get("task") or "review"),
        "clips_total": clips_total,
        "clips_done": clips_done,
        "clips_running": clips_running,
        "clips_failed": clips_failed,
        "files_total": clips_total,
        "files_encoded": clips_done,
        "files_encoding": clips_running,
        "files_failed": clips_failed,
        "percent_clips": round(pct, 2),
        "percent_files": round(pct, 2),
        "percent_input_bytes": round(pct, 2),
        "output_bytes": int(progress.get("output_bytes") or 0),
        "active_output_bytes": int(progress.get("active_output_bytes") or 0),
        "output_rate_bytes_per_second": int(progress.get("output_rate_bytes_per_second") or 0),
    }


def encode_progress_for_job(job: dict[str, Any]) -> dict[str, Any] | None:
    eager_value = job.get("eager_archive")
    if not isinstance(eager_value, dict):
        return review_encode_progress_for_job(job)
    eager = cast(dict[str, Any], eager_value)
    groups = job.get("groups")
    eager_groups = eager_archive_group_names(groups) if isinstance(groups, dict) else set()
    files_state = state_store.dict_or_empty(eager.get("files"))
    if not eager_groups:
        eager_groups = {
            str(item.get("group") or upload_service.upload_file_group(str(rel_path)))
            for rel_path, item in files_state.items()
            if isinstance(item, dict)
        }
    if not eager_groups:
        return None

    upload: dict[str, Any] | None = None
    upload_id = str(job.get("input_upload_id") or "")
    if upload_id:
        try:
            stored_upload = state_store.read_state("input-upload", upload_id)
            upload = (
                upload_service.refresh_input_upload(stored_upload)
                if stored_upload is not None
                else None
            )
        except Exception:
            upload = None

    upload_files: list[dict[str, Any]] = []
    if upload is not None:
        upload_files = upload_service.primary_upload_files_for_groups(upload, eager_groups)
    by_path = {str(item.get("path")): item for item in upload_files}
    known_paths = set(by_path) | {
        str(path)
        for path, item in files_state.items()
        if isinstance(item, dict)
        and str(item.get("group") or upload_service.upload_file_group(str(path))) in eager_groups
    }
    if not known_paths:
        return None

    now = utc_now()
    started_values: list[datetime] = []
    finished_values: list[datetime] = []
    files_total = len(known_paths)
    files_encoded = 0
    files_encoding = 0
    files_failed = 0
    input_bytes_total = 0
    input_bytes_encoded = 0
    input_bytes_encoding = 0
    output_bytes = 0
    active_output_bytes = 0

    for rel_path in sorted(known_paths):
        upload_file = by_path.get(rel_path, {})
        state_item = files_state.get(rel_path)
        state = state_item if isinstance(state_item, dict) else {}
        input_bytes = int(upload_file.get("bytes") or state.get("input_bytes") or 0)
        input_bytes_total += input_bytes
        started = state_store.safe_parse_timestamp(state.get("started_at"))
        finished = state_store.safe_parse_timestamp(state.get("encoded_at"))
        if started is not None:
            started_values.append(started)
        if finished is not None:
            finished_values.append(finished)
        current_output = int(state.get("output_bytes") or safe_file_size(state.get("output")))
        status = str(state.get("state") or "")
        if status == "encoded":
            files_encoded += 1
            input_bytes_encoded += input_bytes
            output_bytes += current_output
        elif status == "encoding":
            files_encoding += 1
            input_bytes_encoding += input_bytes
            active_output_bytes += current_output
        elif status == "failed":
            files_failed += 1

    batches = state_store.dict_or_empty(eager.get("batches"))
    for batch in batches.values():
        if not isinstance(batch, dict):
            continue
        batch_started = state_store.safe_parse_timestamp(batch.get("started_at"))
        batch_finished = state_store.safe_parse_timestamp(batch.get("finished_at"))
        if batch_started is not None:
            started_values.append(batch_started)
        if batch_finished is not None:
            finished_values.append(batch_finished)
    started_at = min(started_values) if started_values else None
    finished_at = max(finished_values) if finished_values else None
    elapsed_seconds = max(0.001, (now - started_at).total_seconds()) if started_at else 0.0
    input_rate = input_bytes_encoded / elapsed_seconds if elapsed_seconds else 0.0
    output_rate = output_bytes / elapsed_seconds if elapsed_seconds else 0.0
    running_batches = sum(
        1
        for batch in batches.values()
        if isinstance(batch, dict) and batch.get("state") == "running"
    )
    pipeline_batches = runtime_config.EAGER_ARCHIVE_PIPELINE_BATCHES
    if isinstance(groups, dict) and len(eager_groups) == 1:
        group_name = next(iter(eager_groups))
        group_config = groups.get(group_name)
        if isinstance(group_config, dict):
            pipeline_batches = eager_archive_pipeline_limit(group_config)
    return {
        "mode": "eager_archive",
        "groups": sorted(eager_groups),
        "files_total": files_total,
        "files_encoded": files_encoded,
        "files_encoding": files_encoding,
        "files_failed": files_failed,
        "input_bytes_total": input_bytes_total,
        "input_bytes_encoded": input_bytes_encoded,
        "input_bytes_encoding": input_bytes_encoding,
        "output_bytes": output_bytes,
        "active_output_bytes": active_output_bytes,
        "percent_files": round((files_encoded / files_total * 100.0) if files_total else 100.0, 2),
        "percent_input_bytes": round(
            (input_bytes_encoded / input_bytes_total * 100.0) if input_bytes_total else 100.0,
            2,
        ),
        "elapsed_seconds": round(elapsed_seconds, 3) if started_at else 0.0,
        "input_rate_bytes_per_second": int(input_rate),
        "output_rate_bytes_per_second": int(output_rate),
        "running_batches": running_batches,
        "pipeline_batches": pipeline_batches,
        "started_at": format_utc_timestamp(started_at) if started_at else None,
        "finished_at": format_utc_timestamp(finished_at) if finished_at else None,
        "completed": files_encoded == files_total and files_total > 0,
    }


def upload_progress_for_job(job: dict[str, Any]) -> dict[str, Any] | None:
    upload_id = str(job.get("input_upload_id") or "")
    if not upload_id:
        return None
    try:
        stored_upload = state_store.read_state("input-upload", upload_id)
        upload = (
            upload_service.refresh_input_upload(stored_upload)
            if stored_upload is not None
            else None
        )
    except Exception:
        upload = None
    if upload is None:
        return None
    files_total = int(upload.get("files_total") or 0)
    files_uploaded = int(upload.get("files_uploaded") or 0)
    bytes_total = int(upload.get("bytes_total") or 0)
    uploaded_bytes = int(upload.get("uploaded_bytes") or 0)
    tree_progress: dict[str, int] = {}
    group_names = upload_service.input_upload_routed_groups(upload)
    groups = job.get("groups")
    if isinstance(groups, dict):
        eager_groups = eager_archive_group_names(groups)
        shared_tree_groups = group_names - eager_groups
    else:
        shared_tree_groups = group_names
    if shared_tree_groups:
        tree_progress = upload_service.shared_input_tree_progress(upload, shared_tree_groups)
        shared_tree_files = upload_service.upload_files_for_groups(upload, shared_tree_groups)
        tree_progress["input_tree_files_total"] = len(shared_tree_files)
        tree_progress["input_tree_bytes_total"] = sum(
            int(file_state["bytes"]) for file_state in shared_tree_files
        )
    return {
        "files_total": files_total,
        "files_uploaded": files_uploaded,
        "bytes_total": bytes_total,
        "uploaded_bytes": uploaded_bytes,
        "percent_bytes": round((uploaded_bytes / bytes_total * 100.0) if bytes_total else 100.0, 2),
        "completed": files_uploaded == files_total and files_total > 0,
        **tree_progress,
    }


def job_response(
    job: dict[str, Any],
    *,
    include_queue: bool = True,
    refresh_progress: bool = True,
) -> dict[str, Any]:
    response = dict(job)
    if include_queue and (
        queue := scheduling_service.queue_info_for_job(str(job.get("job_id") or ""))
    ):
        response["queue"] = queue
    if refresh_progress:
        if progress := upload_progress_for_job(job):
            response["upload_progress"] = progress
        if progress := encode_progress_for_job(job):
            response["encode_progress"] = progress
        if progress := handoff_service.current_handoff_progress(job):
            response["handoff_progress"] = progress
    return response


def compact_job_response(
    job: dict[str, Any],
    *,
    include_queue: bool = True,
    refresh_progress: bool = True,
) -> dict[str, Any]:
    response = job_response(
        job,
        include_queue=include_queue,
        refresh_progress=refresh_progress,
    )
    keys = [
        "job_id",
        "state",
        "phase",
        "created_at",
        "updated_at",
        "started_at",
        "finished_at",
        "input_upload_id",
        "template_id",
        "template_revision",
        "template_digest",
        "run_id",
        "workflow_mode",
        "handoff",
        "review",
        "output_mode",
        "profile",
        "upload_progress",
        "encode_progress",
        "handoff_progress",
        "handoff_metrics",
        "review_sweep_result",
        "handoff_receipt",
        "queue",
        "storage_wait",
        "cancel_requested",
        "cleanup_removed",
        "cleanup_removed_count",
        "cleanup_removed_sample",
        "cleanup_completed_at",
        "input_upload_deleted_at",
        "local_work_cleaned_at",
        "local_work_removed",
        "local_work_removed_count",
        "local_work_removed_sample",
        "terminal_state_compacted_at",
        "error",
    ]
    return {key: response[key] for key in keys if key in response}


def ready_eager_files(
    job: dict[str, Any],
    upload: dict[str, Any],
    groups: dict[str, dict[str, Any]],
    eager_groups: set[str],
    archive_dir: Path,
    *,
    limit: int | None = None,
) -> tuple[str, list[dict[str, Any]]] | None:
    for group_name in sorted(eager_groups):
        group_config = groups[group_name]
        group_limit = limit or eager_archive_batch_limit(group_config)
        ready: list[dict[str, Any]] = []
        for file_state in upload_service.mutable_primary_upload_files_for_groups(
            upload, {group_name}
        ):
            rel_path = str(file_state["path"])
            if eager_file_claimed(job, rel_path):
                continue
            output = routing_service.archive_output_for_upload_file(
                file_state,
                group_name=group_name,
                group_config=group_config,
                archive_dir=archive_dir,
            )
            if output.exists():
                source_artifacts_sidecar = (
                    routing_service.source_artifact_sidecar_for_archive_output(output)
                )
                if (
                    upload_service.sidecar_evidence_files_for_primary(
                        upload,
                        file_state,
                    )
                    and not source_artifacts_sidecar.exists()
                ):
                    continue
                mark_eager_file_encoded(
                    job,
                    file_state,
                    group_name=group_name,
                    group_config=group_config,
                    archive_dir=archive_dir,
                    batch_id=None,
                    detected_existing=True,
                )
                continue
            status = upload_service.upload_file_status(file_state)
            if status["upload_state"] == "consumed":
                raise RuntimeError(f"eager source was consumed before output existed: {rel_path}")
            if status["upload_state"] != "uploaded":
                continue
            evidence = upload_service.sidecar_evidence_files_for_primary(upload, file_state)
            if not all(upload_service.upload_file_status(item)["complete"] for item in evidence):
                continue
            ready.append(file_state)
            if len(ready) >= group_limit:
                break
        if ready:
            state_store.save_job(job)
            return group_name, ready
    return None


def eager_batch_executor(batch: dict[str, Any]) -> str:
    executor = str(batch.get("executor") or "")
    if executor:
        return executor
    if batch.get("gpu_job_id"):
        return "gpu"
    return "gpu"


def running_eager_batch(
    job: dict[str, Any],
    *,
    executor: str | None = None,
) -> dict[str, Any] | None:
    batches = running_eager_batches(job, executor=executor)
    return batches[0] if batches else None


def running_eager_batches(
    job: dict[str, Any],
    *,
    executor: str | None = None,
    group_name: str | None = None,
) -> list[dict[str, Any]]:
    batches = eager_archive_state(job).setdefault("batches", {})
    running = [
        batch
        for batch in batches.values()
        if isinstance(batch, dict)
        and batch.get("state") == "running"
        and (executor is None or eager_batch_executor(batch) == executor)
        and (group_name is None or str(batch.get("group") or "") == group_name)
    ]
    return sorted(running, key=lambda batch: str(batch.get("started_at") or ""))


def eager_group_has_pipeline_capacity(
    job: dict[str, Any],
    group_name: str,
    group_config: dict[str, Any],
) -> bool:
    global_running = running_eager_batches(job)
    if len(global_running) >= runtime_config.EAGER_ARCHIVE_PIPELINE_BATCHES:
        return False
    group_running = running_eager_batches(job, group_name=group_name)
    return len(group_running) < eager_archive_pipeline_limit(group_config)


def eager_archive_pipeline_phase(
    job: dict[str, Any],
    groups: dict[str, dict[str, Any]],
) -> str:
    running = running_eager_batches(job)
    running_count = len(running)
    if running_count == 0:
        return "eager_archive:pipeline=0/0"
    running_groups = {str(batch.get("group") or "") for batch in running}
    if len(running_groups) == 1:
        group_name = next(iter(running_groups))
        group_config = groups.get(group_name)
        if isinstance(group_config, dict):
            return (
                f"eager_archive:{group_name}:pipeline="
                f"{running_count}/{eager_archive_pipeline_limit(group_config)}"
            )
    return f"eager_archive:pipeline={running_count}/{runtime_config.EAGER_ARCHIVE_PIPELINE_BATCHES}"


def next_eager_batch_id(job: dict[str, Any], group_name: str, paths: list[str]) -> str:
    eager = eager_archive_state(job)
    batch_number = int(eager.get("next_batch_number") or 1)
    eager["next_batch_number"] = batch_number + 1
    digest = hashlib.sha256("\n".join(paths).encode()).hexdigest()[:10]
    safe_group = group_name[:36]
    return f"{safe_group}-{batch_number:06d}-{digest}"


def eager_batch_input_root(job_id: str, batch_id: str) -> Path:
    return runtime_config.GPU_RUNTIME_DIR / "jobs" / job_id / "eager-input" / batch_id


def build_eager_gpu_payload(
    job: dict[str, Any],
    *,
    batch_id: str,
    group_name: str,
    group_config: dict[str, Any],
    tasks: list[domain_models.TaskName],
    container_metadata: dict[str, dict[str, Any]] | None = None,
    source_artifacts_sidecars: dict[str, list[dict[str, Any]]] | None = None,
) -> dict[str, Any]:
    job_id = str(job["job_id"])
    gpu_job_id = routing_service.gpu_eager_batch_job_id(job_id, batch_id)
    payload = {
        "job_id": gpu_job_id,
        "input_dir": f"/data/jobs/{job_id}/eager-input/{batch_id}/{group_name}",
        "archive_dir": f"/data/jobs/{job_id}/archive/{group_name}",
        "review_dir": f"/data/jobs/{job_id}/review/{group_name}",
        "profile": group_config.get("profile", "av1-nvenc-high"),
        "tasks": tasks,
        "run_id": job.get("run_id"),
        "container_metadata_required": routing_service.gpu_tasks_require_container_metadata(
            tasks, group_config
        ),
    }
    if group_config.get("encode_profile") is not None:
        payload["encode_profile"] = group_config["encode_profile"]
    if group_config.get("max_parallel_encodes") is not None:
        payload["max_parallel_encodes"] = group_config["max_parallel_encodes"]
    if container_metadata:
        payload["container_metadata"] = container_metadata
    if source_artifacts_sidecars:
        payload["source_artifacts_sidecars"] = source_artifacts_sidecars
    return payload


def finish_eager_gpu_batch(
    job: dict[str, Any],
    upload: dict[str, Any],
    batch: dict[str, Any],
    groups: dict[str, dict[str, Any]],
    archive_dir: Path,
    gpu_result: dict[str, Any],
) -> dict[str, Any]:
    group_name = str(batch["group"])
    group_config = groups[group_name]
    paths = set(str(path) for path in batch.get("paths") or [])
    evidence_paths = set(str(path) for path in batch.get("evidence_paths") or [])
    for file_state in upload_service.primary_upload_files_for_groups(upload, {group_name}):
        rel_path = str(file_state["path"])
        if rel_path not in paths:
            continue
        mark_eager_file_encoded(
            job,
            file_state,
            group_name=group_name,
            group_config=group_config,
            archive_dir=archive_dir,
            batch_id=str(batch["batch_id"]),
        )
    batch["state"] = "succeeded"
    batch["finished_at"] = utc_timestamp_now()
    batch["gpu_result"] = gpu_result
    eager_archive_state(job).setdefault("gpu_results", {})[str(batch["batch_id"])] = gpu_result
    shutil.rmtree(
        eager_batch_input_root(str(job["job_id"]), str(batch["batch_id"])),
        ignore_errors=True,
    )
    upload = consume_input_upload_files(str(job["input_upload_id"]), paths | evidence_paths)
    state_store.save_job(job)
    return upload


def submit_eager_gpu_job(
    job: dict[str, Any],
    batch: dict[str, Any],
    *,
    force: bool = False,
) -> bool:
    payload = batch.get("payload")
    if not isinstance(payload, dict):
        raise RuntimeError(f"eager batch is missing payload: {batch.get('batch_id')}")
    last_submitted = state_store.safe_parse_timestamp(batch.get("last_submitted_at"))
    if (
        not force
        and last_submitted is not None
        and (datetime.now(UTC) - last_submitted).total_seconds()
        < max(30.0, runtime_config.GPU_REPOST_SECONDS)
    ):
        return False
    media_service.start_gpu_job(payload)
    batch["last_submitted_at"] = utc_timestamp_now()
    batch["submit_count"] = int(batch.get("submit_count") or 0) + 1
    state_store.save_job(job)
    return True


def prepare_eager_gpu_batch_input(
    job: dict[str, Any],
    upload_id: str,
    *,
    paths: list[str],
    batch_id: str,
    group_name: str,
    group_config: dict[str, Any],
    tasks: list[domain_models.TaskName],
) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    job_id = str(job["job_id"])
    with execution_runtime.input_upload_state_lock(upload_id):
        upload = upload_service.load_input_upload(upload_id)
        files_by_path = {
            str(file_state["path"]): file_state for file_state in upload.get("files", [])
        }
        try:
            file_states = [files_by_path[path] for path in paths]
        except KeyError as exc:
            raise RuntimeError(f"unknown eager input file: {exc.args[0]}") from exc
        evidence_file_states = upload_service.sidecar_evidence_files_for_primaries(
            upload, file_states
        )
        batch_root = eager_batch_input_root(job_id, batch_id)
        if batch_root.exists():
            shutil.rmtree(batch_root, ignore_errors=True)
        batch_root.mkdir(parents=True, exist_ok=True)
        for file_state in [*file_states, *evidence_file_states]:
            upload_service.materialize_upload_file(file_state, batch_root)
        source_paths_by_path = {
            str(file_state["path"]): batch_root
            / upload_service.materialized_input_rel_path(file_state)
            for file_state in [*file_states, *evidence_file_states]
        }
    container_metadata, container_metadata_changed = (
        routing_service.container_metadata_for_gpu_payload(
            job,
            upload,
            file_states,
            group_name=group_name,
            group_config=group_config,
            tasks=tasks,
            source_paths_by_path=source_paths_by_path,
        )
    )
    upload = upload_service.merge_input_upload_projection_metadata(
        upload_id,
        file_states if container_metadata_changed else (),
    )
    current_by_path = {
        str(file_state["path"]): file_state for file_state in upload.get("files", [])
    }
    file_states = [current_by_path[path] for path in paths]
    evidence_file_states = upload_service.sidecar_evidence_files_for_primaries(upload, file_states)
    source_artifacts_sidecars = upload_service.source_artifacts_sidecar_entries(
        upload,
        file_states,
        group_name=group_name,
        materialized_group_root=batch_root / group_name,
        container_group_root=Path(f"/data/jobs/{job_id}/eager-input/{batch_id}/{group_name}"),
    )
    payload = build_eager_gpu_payload(
        job,
        batch_id=batch_id,
        group_name=group_name,
        group_config=group_config,
        tasks=tasks,
        container_metadata=container_metadata,
        source_artifacts_sidecars=source_artifacts_sidecars,
    )
    return upload, file_states, evidence_file_states, payload


def start_eager_gpu_batch(
    job: dict[str, Any],
    upload: dict[str, Any],
    *,
    group_name: str,
    group_config: dict[str, Any],
    file_states: list[dict[str, Any]],
    archive_dir: Path,
    space_checked: bool = False,
) -> dict[str, Any]:
    paths = [str(file_state["path"]) for file_state in file_states]
    evidence_file_states = upload_service.sidecar_evidence_files_for_primaries(upload, file_states)
    evidence_paths = [str(file_state["path"]) for file_state in evidence_file_states]
    batch_id = next_eager_batch_id(job, group_name, paths)
    batch_bytes = sum(
        int(file_state["bytes"]) for file_state in [*file_states, *evidence_file_states]
    )
    storage_hint = upload_service.input_upload_storage_hint(upload)
    required_gpu_free = (
        admission_service.gpu_scratch_required_bytes(batch_bytes, storage_hint)
        + runtime_config.MIN_FREE_BYTES
    )
    if not space_checked:
        admission_service.wait_for_free_space(
            job, runtime_config.GPU_RUNTIME_DIR, required_gpu_free, label="gpu eager scratch"
        )

    tasks: list[domain_models.TaskName] = ["archive_video"]
    upload, file_states, evidence_file_states, payload = prepare_eager_gpu_batch_input(
        job,
        str(upload["input_upload_id"]),
        paths=paths,
        batch_id=batch_id,
        group_name=group_name,
        group_config=group_config,
        tasks=tasks,
    )
    evidence_paths = [str(file_state["path"]) for file_state in evidence_file_states]
    batch = {
        "batch_id": batch_id,
        "state": "running",
        "executor": "gpu",
        "group": group_name,
        "paths": paths,
        "evidence_paths": evidence_paths,
        "gpu_job_id": payload["job_id"],
        "payload": payload,
        "started_at": utc_timestamp_now(),
    }
    eager_archive_state(job).setdefault("batches", {})[batch_id] = batch
    for file_state in file_states:
        mark_eager_file_encoding(
            job,
            file_state,
            group_name=group_name,
            group_config=group_config,
            archive_dir=archive_dir,
            batch_id=batch_id,
        )
    job["phase"] = (
        f"eager_archive:{group_name}:pipeline="
        f"{len(running_eager_batches(job, group_name=group_name))}/"
        f"{eager_archive_pipeline_limit(group_config)}"
    )
    state_store.save_job(job)

    try:
        submit_eager_gpu_job(job, batch, force=True)
    except Exception as exc:
        log.warning("gpu target eager submit failed; retrying: %s", exc)
    return upload


def start_eager_audio_batch(
    job: dict[str, Any],
    upload: dict[str, Any],
    *,
    group_name: str,
    group_config: dict[str, Any],
    file_states: list[dict[str, Any]],
    archive_dir: Path,
) -> dict[str, Any]:
    job_id = str(job["job_id"])
    paths = [str(file_state["path"]) for file_state in file_states]
    batch_id = next_eager_batch_id(job, group_name, paths)
    batch_root = eager_batch_input_root(job_id, batch_id)
    upload_id = str(upload["input_upload_id"])
    with execution_runtime.input_upload_state_lock(upload_id):
        upload = upload_service.load_input_upload(upload_id)
        files_by_path = {
            str(file_state["path"]): file_state for file_state in upload.get("files", [])
        }
        try:
            file_states = [files_by_path[path] for path in paths]
        except KeyError as exc:
            raise RuntimeError(f"unknown eager input file: {exc.args[0]}") from exc
        evidence_file_states = upload_service.sidecar_evidence_files_for_primaries(
            upload, file_states
        )
        if batch_root.exists():
            shutil.rmtree(batch_root, ignore_errors=True)
        batch_root.mkdir(parents=True, exist_ok=True)
        for file_state in [*file_states, *evidence_file_states]:
            upload_service.materialize_upload_file(file_state, batch_root)
        source_paths_by_path = {
            str(file_state["path"]): batch_root
            / upload_service.materialized_input_rel_path(file_state)
            for file_state in [*file_states, *evidence_file_states]
        }
    upload_changed = False
    for file_state in file_states:
        if routing_service.ensure_file_projection_metadata(
            upload,
            file_state,
            job=job,
            group_config=group_config,
            source_path=source_paths_by_path[str(file_state["path"])],
            sidecar_source_paths_by_path=source_paths_by_path,
        ):
            upload_changed = True
    upload = upload_service.merge_input_upload_projection_metadata(
        upload_id,
        file_states if upload_changed else (),
    )
    current_by_path = {
        str(file_state["path"]): file_state for file_state in upload.get("files", [])
    }
    file_states = [current_by_path[path] for path in paths]

    batch: dict[str, Any] = {
        "batch_id": batch_id,
        "state": "running",
        "executor": "local_audio",
        "group": group_name,
        "paths": paths,
        "started_at": utc_timestamp_now(),
    }
    eager_archive_state(job).setdefault("batches", {})[batch_id] = batch
    for file_state in file_states:
        mark_eager_file_encoding(
            job,
            file_state,
            group_name=group_name,
            group_config=group_config,
            archive_dir=archive_dir,
            batch_id=batch_id,
        )
    job["phase"] = f"eager_archive:{group_name}"
    state_store.save_job(job)

    try:
        result = media_service.run_archive_audio_group(
            input_root=batch_root / group_name,
            output_root=archive_dir / group_name,
            group_config=group_config,
        )
    except Exception as exc:
        error = str(exc)
        batch["state"] = "failed"
        batch["failed_at"] = utc_timestamp_now()
        batch["error"] = error
        for file_state in file_states:
            mark_eager_file_failed(
                job,
                file_state,
                group_name=group_name,
                group_config=group_config,
                archive_dir=archive_dir,
                batch_id=batch_id,
                error=error,
            )
        state_store.save_job(job)
        event_service.emit_job_issue(job, component="encoding", error=error, severity="error")
        raise domain_errors.EncodingFailed(error) from exc

    batch["state"] = "succeeded"
    batch["finished_at"] = utc_timestamp_now()
    batch["archive_audio_result"] = {
        "status": result.get("status"),
        "count": result.get("count"),
    }
    for file_state in file_states:
        mark_eager_file_encoded(
            job,
            file_state,
            group_name=group_name,
            group_config=group_config,
            archive_dir=archive_dir,
            batch_id=batch_id,
        )
    shutil.rmtree(batch_root, ignore_errors=True)
    upload = consume_input_upload_files(str(job["input_upload_id"]), set(paths))
    state_store.save_job(job)
    return upload


def poll_eager_gpu_batch(
    job: dict[str, Any],
    upload: dict[str, Any],
    batch: dict[str, Any],
    groups: dict[str, dict[str, Any]],
    archive_dir: Path,
) -> dict[str, Any]:
    payload = batch.get("payload")
    if not isinstance(payload, dict):
        raise RuntimeError(f"eager batch is missing payload: {batch.get('batch_id')}")
    gpu_job_id = str(batch.get("gpu_job_id") or payload.get("job_id"))
    try:
        status = media_service.gpu_target_request("GET", f"/v1/jobs/{gpu_job_id}")
    except Exception as exc:
        log.warning(
            "gpu target status check failed for eager batch %s; retrying: %s",
            gpu_job_id,
            exc,
        )
        try:
            submit_eager_gpu_job(job, batch, force=True)
        except Exception as start_exc:
            log.warning("gpu target eager re-submit failed; retrying: %s", start_exc)
        return upload

    state = str(status.get("state") or "")
    batch["gpu_state"] = state
    batch["last_polled_at"] = utc_timestamp_now()
    if state == "succeeded":
        return finish_eager_gpu_batch(job, upload, batch, groups, archive_dir, status)
    if state == "failed":
        if status.get("error_code") == "target_restarted":
            log.warning("gpu target restarted during eager batch %s; re-submitting job", gpu_job_id)
            submit_eager_gpu_job(job, batch, force=True)
            return upload
        error = f"gpu eager batch failed: {status.get('error')}"
        event_service.emit_job_issue(job, component="encoding", error=error, severity="error")
        raise domain_errors.EncodingFailed(error)
    state_store.save_job(job)
    return upload


def run_eager_archive_groups(
    job: dict[str, Any],
    upload: dict[str, Any],
    groups: dict[str, dict[str, Any]],
    eager_groups: set[str],
    archive_dir: Path,
) -> dict[str, Any]:
    job_id = str(job["job_id"])
    token = ""
    try:
        while True:
            state_store.raise_if_job_canceled(job_id)
            upload = upload_service.load_input_upload(str(job["input_upload_id"]))
            upload = routing_service.route_completed_input_files(job, upload, groups)
            upload_service.cleanup_consumed_shared_input_files(upload, eager_groups)
            upload, _ = mark_existing_eager_outputs(job, upload, groups, eager_groups, archive_dir)
            claim_running_eager_batch_files(job, upload, groups, archive_dir)
            running_gpu = running_eager_batches(job, executor="gpu")
            if running_gpu and not token:
                token = media_service.acquire_job_gpu(job)
            for batch in running_gpu:
                upload = poll_eager_gpu_batch(job, upload, batch, groups, archive_dir)
            if eager_groups_complete(job, upload, eager_groups):
                eager = eager_archive_state(job)
                eager["completed_at"] = eager.get("completed_at") or utc_timestamp_now()
                state_store.save_job(job)
                return upload

            while len(running_eager_batches(job)) < runtime_config.EAGER_ARCHIVE_PIPELINE_BATCHES:
                eligible_eager_groups = {
                    group_name
                    for group_name in eager_groups
                    if eager_group_has_pipeline_capacity(
                        job,
                        group_name,
                        groups[group_name],
                    )
                }
                if not eligible_eager_groups:
                    break
                ready = ready_eager_files(
                    job,
                    upload,
                    groups,
                    eligible_eager_groups,
                    archive_dir,
                )
                if ready is None:
                    break
                group_name, file_states = ready
                group_config = groups[group_name]
                executor = routing_service.eager_archive_executor(group_config)
                if executor is None:
                    raise RuntimeError(f"group {group_name} is not eager-archive eligible")
                batch_bytes = sum(int(file_state["bytes"]) for file_state in file_states)
                if executor == "gpu":
                    storage_hint = upload_service.input_upload_storage_hint(upload)
                    required_gpu_free = (
                        admission_service.gpu_scratch_required_bytes(batch_bytes, storage_hint)
                        + runtime_config.MIN_FREE_BYTES
                    )
                    admission_service.wait_for_free_space(
                        job,
                        runtime_config.GPU_RUNTIME_DIR,
                        required_gpu_free,
                        label="gpu eager scratch",
                    )
                    if not token:
                        token = media_service.acquire_job_gpu(job)
                    upload = start_eager_gpu_batch(
                        job,
                        upload,
                        group_name=group_name,
                        group_config=group_config,
                        file_states=file_states,
                        archive_dir=archive_dir,
                        space_checked=True,
                    )
                    continue
                if executor == "local_audio":
                    admission_service.wait_for_free_space(
                        job,
                        runtime_config.GPU_RUNTIME_DIR,
                        batch_bytes + runtime_config.MIN_FREE_BYTES,
                        label="eager archive scratch",
                    )
                    upload = start_eager_audio_batch(
                        job,
                        upload,
                        group_name=group_name,
                        group_config=group_config,
                        file_states=file_states,
                        archive_dir=archive_dir,
                    )
                    continue
                raise RuntimeError(f"unsupported eager archive executor: {executor}")

            running = running_eager_batches(job)
            if running:
                job["phase"] = eager_archive_pipeline_phase(job, groups)
                state_store.save_job(job)
                handoff_service.retry_sleep(
                    runtime_config.EAGER_ARCHIVE_WAIT_SECONDS, job_id=job_id
                )
                continue

            if token:
                media_service.release_job_gpu(job, token)
                token = ""
            progress = upload_service.upload_group_progress(upload, eager_groups)
            job["upload_progress"] = progress
            job["phase"] = (
                f"waiting_for_eager_files:{progress['files_uploaded']}/{progress['files_total']}"
            )
            state_store.save_job(job)
            emit_upload_stalled(job, upload, progress)
            handoff_service.retry_sleep(runtime_config.EAGER_ARCHIVE_WAIT_SECONDS, job_id=job_id)
    finally:
        if token:
            media_service.release_job_gpu(job, token)
