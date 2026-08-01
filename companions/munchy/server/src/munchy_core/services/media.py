from __future__ import annotations

import logging
import logging.config
import time
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, cast

from munchy_api_client.filesystem_metadata import (
    SOURCE_FILESYSTEM_METADATA_FILENAME,
    load_filesystem_metadata_map,
)
from munchy_target_support.metadata_projection import (
    ProjectionMetadata,
    ffmpeg_container_metadata_args,
)
from munchy_target_support.source_artifact_bridge import (
    build_strict_source_artifacts,
)
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
from munchy_core.ports.gpu import GpuPlatform

log = logging.getLogger("munchy.server")

gpu_platform: GpuPlatform | None = None


def register_gpu_platform(platform: GpuPlatform) -> None:
    global gpu_platform
    gpu_platform = platform


def configured_gpu_platform() -> GpuPlatform:
    if gpu_platform is None:
        raise RuntimeError("gpu platform adapter is not configured")
    return gpu_platform


def manager_request(
    method: str, path: str, payload: dict[str, Any] | None = None
) -> dict[str, Any]:
    return configured_gpu_platform().manager_request(method, path, payload)


def acquire_gpu(job_id: str, lease_token: str = "") -> str:
    token = lease_token
    deadline = time.monotonic() + runtime_config.GPU_LEASE_TTL_S
    while time.monotonic() < deadline:
        state_store.raise_if_job_canceled(job_id)
        payload = {
            "target": runtime_config.GPU_TARGET,
            "owner": f"munchy-server:{job_id}",
            "lease_token": token,
            "lease_ttl_s": runtime_config.GPU_LEASE_TTL_S,
            "wait_s": runtime_config.GPU_WAIT_S,
            "wait_ready": True,
            "priority": 0,
        }
        try:
            result = manager_request("POST", "/acquire", payload)
        except RuntimeError as exc:
            if "gpu busy" not in str(exc) and "queued" not in str(exc):
                raise
            handoff_service.retry_sleep(5, job_id=job_id)
            continue
        token = str(result.get("lease_token") or token)
        if not result.get("queued"):
            return token
        handoff_service.retry_sleep(5, job_id=job_id)
    raise RuntimeError("timed out waiting for gpu lease")


def release_gpu(token: str) -> bool:
    if not token:
        return True
    try:
        manager_request("POST", "/release", {"lease_token": token, "stop": False})
        return True
    except Exception:
        log.exception("failed to release gpu lease")
        return False


def acquire_job_gpu(job: dict[str, Any]) -> str:
    token = acquire_gpu(str(job["job_id"]), str(job.get("gpu_lease_token") or ""))
    job["gpu_lease_token"] = token
    job["gpu_lease_acquired_at"] = utc_timestamp_now()
    state_store.save_job(job)
    return token


def release_job_gpu(job: dict[str, Any], token: str) -> None:
    if release_gpu(token):
        if job.get("gpu_lease_token") == token:
            job.pop("gpu_lease_token", None)
            job["gpu_lease_released_at"] = utc_timestamp_now()
            state_store.save_job(job)


def gpu_target_request(
    method: str, path: str, payload: dict[str, Any] | None = None
) -> dict[str, Any]:
    return configured_gpu_platform().target_request(method, path, payload)


def start_gpu_job(gpu_payload: dict[str, Any]) -> None:
    try:
        gpu_target_request("POST", "/v1/jobs", gpu_payload)
    except RuntimeError as exc:
        if "gpu target returned 409" in str(exc):
            return
        raise


def compact_gpu_status_for_progress(status: dict[str, Any]) -> dict[str, Any]:
    compact: dict[str, Any] = {
        key: status[key]
        for key in (
            "job_id",
            "state",
            "profile",
            "tasks",
            "started_at",
            "finished_at",
            "updated_at",
        )
        if key in status
    }
    items = status.get("items")
    if isinstance(items, dict):
        compact_items: dict[str, dict[str, Any]] = {}
        for name, item in items.items():
            if not isinstance(item, dict):
                continue
            compact_item = {key: item[key] for key in ("status", "reason", "bytes") if key in item}
            progress = item.get("progress")
            if isinstance(progress, dict):
                compact_item["progress"] = progress
            if compact_item:
                compact_items[str(name)] = compact_item
        if compact_items:
            compact["items"] = compact_items
    if "error" in status:
        compact["error"] = status["error"]
    if "error_code" in status:
        compact["error_code"] = status["error_code"]
    return compact


def record_gpu_status(job: dict[str, Any], gpu_job_id: str, status: dict[str, Any]) -> None:
    statuses = job.setdefault("gpu_statuses", {})
    if not isinstance(statuses, dict):
        statuses = {}
        job["gpu_statuses"] = statuses
    statuses[gpu_job_id] = compact_gpu_status_for_progress(status)
    state_store.save_job(job)


def wait_gpu_job(
    gpu_job_id: str,
    *,
    gpu_payload: dict[str, Any],
    job: dict[str, Any],
) -> dict[str, Any]:
    job_id = str(job["job_id"])
    next_repost = time.monotonic() + max(30.0, runtime_config.GPU_REPOST_SECONDS)
    while True:
        state_store.raise_if_job_canceled(job_id)
        try:
            status = gpu_target_request("GET", f"/v1/jobs/{gpu_job_id}")
        except Exception as exc:
            log.warning("gpu target status check failed; retrying: %s", exc)
            handoff_service.retry_sleep(15)
            try:
                start_gpu_job(gpu_payload)
            except Exception as start_exc:
                log.warning("gpu target restart attempt failed; retrying: %s", start_exc)
            continue
        record_gpu_status(job, gpu_job_id, status)
        state = status.get("state")
        if state == "succeeded":
            return status
        if state == "failed":
            if status.get("error_code") == "target_restarted":
                log.warning("gpu target restarted during %s; re-submitting job", gpu_job_id)
                start_gpu_job(gpu_payload)
                next_repost = time.monotonic() + max(30.0, runtime_config.GPU_REPOST_SECONDS)
                time.sleep(5)
                continue
            error = f"gpu job failed: {status.get('error')}"
            event_service.emit_job_issue(job, component="encoding", error=error, severity="error")
            raise domain_errors.EncodingFailed(error)
        if time.monotonic() >= next_repost:
            try:
                start_gpu_job(gpu_payload)
            except Exception as exc:
                log.warning("gpu target re-submit failed; retrying: %s", exc)
            next_repost = time.monotonic() + max(30.0, runtime_config.GPU_REPOST_SECONDS)
        time.sleep(5)


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
    filesystem_metadata: Mapping[str, Any] | None,
) -> ProjectionMetadata | None:
    if not routing_service.metadata_projection_enabled(group_config):
        return None
    return routing_service.projection_metadata_from_source(
        rel_path,
        source,
        group_config=group_config,
        filesystem_metadata=filesystem_metadata,
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
    return sorted(
        path
        for path in input_root.rglob("*")
        if path.is_file() and path.name != SOURCE_FILESYSTEM_METADATA_FILENAME
    )


def archive_audio_output_for_source(source: Path, input_root: Path, output_root: Path) -> Path:
    return (output_root / source.relative_to(input_root)).with_suffix(".opus")


def run_archive_audio_item(
    *,
    source: Path,
    dest: Path,
    input_root: Path,
    group_config: dict[str, Any],
    filesystem_metadata: Mapping[str, Any],
    source_sidecars: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    profile, _audio = audio_archive_profile(group_config)
    rel_path = source.relative_to(input_root).as_posix()
    metadata = filesystem_metadata.get(rel_path)
    allow_missing_filesystem_metadata = bool(
        group_config.get("allow_missing_filesystem_metadata", False)
    )
    if (not isinstance(metadata, Mapping) or not metadata) and not (
        allow_missing_filesystem_metadata
    ):
        raise RuntimeError(
            f"unresumable: source filesystem metadata sidecar is missing entries for {rel_path}"
        )
    audio_metadata = audio_archive_metadata_for_source(
        source,
        rel_path=rel_path,
        group_config=group_config,
        filesystem_metadata=metadata,
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
        source_filesystem_metadata=metadata,
        allow_missing_filesystem_metadata=allow_missing_filesystem_metadata,
        source_sidecars=source_sidecars,
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
) -> dict[str, Any]:
    sources = archive_audio_sources(input_root, rel_paths=source_rel_paths)
    if not sources:
        return {"status": "skipped", "reason": "no audio sources", "items": []}
    filesystem_metadata = load_filesystem_metadata_map(input_root)
    if not filesystem_metadata and not bool(
        group_config.get("allow_missing_filesystem_metadata", False)
    ):
        raise RuntimeError(
            "unresumable: source filesystem metadata sidecar is missing for audio archive group"
        )
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
                filesystem_metadata=filesystem_metadata,
                source_sidecars=(
                    source_artifacts_sidecars.get(source.relative_to(input_root).as_posix(), [])
                    if source_artifacts_sidecars
                    else []
                ),
            ): source
            for source in sources
        }
        for future in as_completed(futures):
            items.append(future.result())
    items.sort(key=lambda item: str(item.get("source") or ""))
    return {"status": "succeeded", "items": items, "count": len(items)}
