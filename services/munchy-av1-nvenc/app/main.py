from __future__ import annotations

import bisect
import hashlib
import json
import logging
import logging.config
import os
import random
import re
import shlex
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, cast

import uvicorn
from fastapi import BackgroundTasks, FastAPI, HTTPException
from pydantic import BaseModel, Field, field_validator, model_validator

from munchy.filesystem_metadata import load_filesystem_metadata_map
from munchy.metadata_projection import ProjectionMetadata, ffmpeg_container_metadata_args
from munchy.profiles import (
    MUNCHY_PROFILE_TARGET,
    ArchiveAudioProfile,
    ArchiveContainer,
    ArchiveEncodeProfile,
    EncodeProfile,
)
from munchy.source_artifact_bridge import build_strict_source_artifacts
from munchy.uvicorn_logging import uvicorn_log_config_without_health_access_logs

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "std": {
            "format": "%(asctime)s %(levelname)s %(name)s %(message)s",
            "datefmt": "%Y-%m-%dT%H:%M:%S%z",
        }
    },
    "handlers": {
        "stdout": {
            "class": "logging.StreamHandler",
            "stream": "ext://sys.stdout",
            "formatter": "std",
        }
    },
    "root": {"level": os.getenv("MUNCHY_LOG_LEVEL", "INFO"), "handlers": ["stdout"]},
    "loggers": {
        "httpx": {
            "level": os.getenv("MUNCHY_HTTPX_LOG_LEVEL", "WARNING"),
            "handlers": ["stdout"],
            "propagate": False,
        },
    },
}
logging.config.dictConfig(LOGGING)
log = logging.getLogger("munchy_av1")


TaskName = Literal["archive_video", "qcut_video", "audio_review"]

DATA_DIR = Path(os.getenv("MUNCHY_DATA_DIR", "/data")).resolve()
SOURCE_ARTIFACTS_SUFFIX = ".source-artifacts.tar.zst"
MAX_PARALLEL_ENCODES = max(1, int(os.getenv("MUNCHY_MAX_PARALLEL_ENCODES", "4")))
FFMPEG_TIMEOUT_SECONDS = float(os.getenv("MUNCHY_FFMPEG_TIMEOUT_SECONDS", "0"))
VIDEO_DECODE_MODE = os.getenv("MUNCHY_VIDEO_DECODE_MODE", "cuvid").strip().lower()
VIDEO_SCALE_MODE = os.getenv("MUNCHY_VIDEO_SCALE_MODE", "software").strip().lower()
ARCHIVE_CQ = os.getenv("MUNCHY_AV1_CQ", "23")
ARCHIVE_PRESET = os.getenv("MUNCHY_AV1_PRESET", "p7")
ARCHIVE_TUNE = os.getenv("MUNCHY_AV1_TUNE", "uhq")
ARCHIVE_LOOKAHEAD_LEVEL = os.getenv("MUNCHY_AV1_LOOKAHEAD_LEVEL", "3")
ARCHIVE_SPLIT_ENCODE_MODE = os.getenv("MUNCHY_AV1_SPLIT_ENCODE_MODE", "disabled")
ARCHIVE_PIX_FMT = os.getenv("MUNCHY_AV1_PIX_FMT", "p010le")
ARCHIVE_AUDIO_BITRATE = os.getenv("MUNCHY_AUDIO_BITRATE", "128k")
QCUT_TARGET_SECONDS = int(os.getenv("MUNCHY_QCUT_TARGET_SECONDS", "180"))
QCUT_MIN_SECONDS = int(os.getenv("MUNCHY_QCUT_MIN_SECONDS", "6"))
QCUT_MAX_SECONDS = int(os.getenv("MUNCHY_QCUT_MAX_SECONDS", "9"))
QCUT_CQ = os.getenv("MUNCHY_QCUT_CQ", "30")
QCUT_TUNE = os.getenv("MUNCHY_QCUT_TUNE", ARCHIVE_TUNE)
QCUT_LOOKAHEAD_LEVEL = os.getenv("MUNCHY_QCUT_LOOKAHEAD_LEVEL", ARCHIVE_LOOKAHEAD_LEVEL)
QCUT_SPLIT_ENCODE_MODE = os.getenv("MUNCHY_QCUT_SPLIT_ENCODE_MODE", ARCHIVE_SPLIT_ENCODE_MODE)
QCUT_TRUE_PEAK = os.getenv("MUNCHY_QCUT_TRUE_PEAK", "").strip()
REVIEW_AUDIO_BITRATE = os.getenv("MUNCHY_REVIEW_AUDIO_BITRATE", "96k")
RIVERHOG_UPLOAD_ENABLED = os.getenv("MUNCHY_RIVERHOG_UPLOAD_ENABLED", "0").lower() in {
    "1",
    "true",
    "yes",
    "on",
}
RIVERHOG_COMMAND = os.getenv("MUNCHY_RIVERHOG_COMMAND", "riverhog")
REVIEW_UPLOAD_ENABLED = os.getenv("MUNCHY_REVIEW_UPLOAD_ENABLED", "0").lower() in {
    "1",
    "true",
    "yes",
    "on",
}
REVIEW_UPLOAD_COMMAND = os.getenv("MUNCHY_REVIEW_UPLOAD_COMMAND", "").strip()

VIDEO_EXTENSIONS = {
    ".3g2",
    ".3gp",
    ".avi",
    ".m2ts",
    ".m4v",
    ".mkv",
    ".mov",
    ".mp4",
    ".mts",
    ".webm",
}
AUDIO_EXTENSIONS = {
    ".aac",
    ".aif",
    ".aiff",
    ".flac",
    ".m4a",
    ".mp3",
    ".ogg",
    ".opus",
    ".wav",
    ".wma",
}
CUVID_DECODERS = {
    "av1": "av1_cuvid",
    "h264": "h264_cuvid",
    "hevc": "hevc_cuvid",
    "mjpeg": "mjpeg_cuvid",
    "mpeg1video": "mpeg1_cuvid",
    "mpeg2video": "mpeg2_cuvid",
    "mpeg4": "mpeg4_cuvid",
    "vc1": "vc1_cuvid",
    "vp8": "vp8_cuvid",
    "vp9": "vp9_cuvid",
}

app = FastAPI(title="munchy-av1-nvenc", version="0.1.0")
jobs_lock = threading.Lock()
jobs: dict[str, dict[str, Any]] = {}
encode_semaphore = threading.Semaphore(MAX_PARALLEL_ENCODES)
ffmpeg_filter_lock = threading.Lock()
ffmpeg_filter_cache: set[str] | None = None


class RiverhogConfig(BaseModel):
    enabled: bool = False
    wait: Literal["staged", "finalized"] = "staged"


class InputVanishedDuringJob(RuntimeError):
    pass


class ReviewUploadConfig(BaseModel):
    enabled: bool = False


class ReviewClipPlanConfig(BaseModel):
    target_seconds: int = Field(default=QCUT_TARGET_SECONDS, ge=1)
    min_seconds: int = Field(default=QCUT_MIN_SECONDS, ge=1)
    max_seconds: int = Field(default=QCUT_MAX_SECONDS, ge=1)

    @model_validator(mode="after")
    def validate_bounds(self) -> "ReviewClipPlanConfig":
        if self.min_seconds > self.max_seconds:
            raise ValueError("review_clip_plan.min_seconds must be <= max_seconds")
        return self


def default_tasks() -> list[TaskName]:
    return ["archive_video"]


class JobRequest(BaseModel):
    job_id: str | None = Field(default=None, min_length=1, max_length=180)
    input_dir: Path
    archive_dir: Path
    review_dir: Path | None = None
    profile: str = "av1-nvenc-high"
    encode_profile: EncodeProfile | None = None
    max_parallel_encodes: int | None = Field(default=None, ge=1, le=64)
    tasks: list[TaskName] = Field(default_factory=default_tasks)
    collection_slug: str | None = None
    collection_timestamp: str | None = None
    riverhog: RiverhogConfig = Field(default_factory=RiverhogConfig)
    review_upload: ReviewUploadConfig = Field(default_factory=ReviewUploadConfig)
    review_clip_plan: ReviewClipPlanConfig = Field(default_factory=ReviewClipPlanConfig)
    review_plans: dict[str, dict[str, Any]] = Field(default_factory=dict)
    container_metadata: dict[str, dict[str, Any]] = Field(default_factory=dict)
    container_metadata_required: bool = True
    source_artifacts_sidecars: dict[str, list[dict[str, Any]]] = Field(default_factory=dict)
    dry_run: bool = False

    @field_validator("tasks")
    @classmethod
    def require_tasks(cls, value: list[TaskName]) -> list[TaskName]:
        if not value:
            raise ValueError("at least one task is required")
        return value


def now_iso() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def ensure_under_data_dir(path: Path, *, name: str) -> Path:
    resolved = path.expanduser().resolve()
    if resolved != DATA_DIR and DATA_DIR not in resolved.parents:
        raise HTTPException(status_code=400, detail=f"{name} must be under {DATA_DIR}")
    return resolved


def status_path(job_id: str) -> Path:
    return DATA_DIR / "jobs" / job_id / "status.json"


def write_status(job_id: str, payload: dict[str, Any]) -> None:
    payload = dict(payload)
    payload["updated_at"] = now_iso()
    path = status_path(job_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    with jobs_lock:
        jobs[job_id] = payload


def load_status(job_id: str) -> dict[str, Any]:
    with jobs_lock:
        if job_id in jobs:
            return jobs[job_id]
    path = status_path(job_id)
    if not path.exists():
        raise HTTPException(status_code=404, detail=f"unknown job: {job_id}")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise RuntimeError(f"job status is not an object: {path}")
    return cast(dict[str, Any], payload)


def mark_interrupted_jobs_on_startup() -> None:
    jobs_dir = DATA_DIR / "jobs"
    if not jobs_dir.exists():
        return
    for path in jobs_dir.glob("*/status.json"):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            log.exception("failed to read job status during startup recovery: %s", path)
            continue
        if payload.get("state") not in {"queued", "running"}:
            continue
        job_id = str(payload.get("job_id") or path.parent.name)
        payload["job_id"] = job_id
        payload["state"] = "failed"
        payload["error_code"] = "target_restarted"
        payload["error"] = "gpu target restarted before job completed"
        payload["finished_at"] = now_iso()
        log.warning("marking interrupted gpu job as failed after startup: %s", job_id)
        write_status(job_id, payload)


@app.on_event("startup")
def recover_interrupted_jobs() -> None:
    mark_interrupted_jobs_on_startup()


def run_command(
    cmd: list[str],
    *,
    action: str,
    dry_run: bool = False,
    env: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    rendered = shlex.join(cmd)
    log.info("%s: %s", action, rendered)
    started = time.monotonic()
    if dry_run:
        return {
            "command": cmd,
            "returncode": 0,
            "duration_s": 0.0,
            "stdout": "",
            "stderr": "",
            "dry_run": True,
        }
    timeout = None if FFMPEG_TIMEOUT_SECONDS <= 0 else FFMPEG_TIMEOUT_SECONDS
    with tempfile.TemporaryFile() as stdout_file, tempfile.TemporaryFile() as stderr_file:
        try:
            proc = subprocess.run(
                cmd,
                check=False,
                stdout=stdout_file,
                stderr=stderr_file,
                timeout=timeout,
                env=dict(env) if env is not None else None,
            )
        except subprocess.TimeoutExpired as exc:
            stdout_tail = tail_binary_file(stdout_file, 4000)
            stderr_tail = tail_binary_file(stderr_file, 4000)
            detail = (stderr_tail or stdout_tail or rendered)[-2000:]
            raise RuntimeError(f"{action} timed out after {timeout}s: {detail}") from exc
        stdout_tail = tail_binary_file(stdout_file, 4000)
        stderr_tail = tail_binary_file(stderr_file, 4000)
    result = {
        "command": cmd,
        "returncode": proc.returncode,
        "duration_s": round(time.monotonic() - started, 3),
        "stdout": stdout_tail,
        "stderr": stderr_tail,
    }
    if proc.returncode != 0:
        detail = (stderr_tail or stdout_tail or rendered)[-2000:]
        raise RuntimeError(f"{action} failed with {proc.returncode}: {detail}")
    return result


def tail_binary_file(file_obj: Any, limit: int) -> str:
    file_obj.flush()
    file_obj.seek(0, os.SEEK_END)
    size = file_obj.tell()
    file_obj.seek(max(0, size - limit))
    return file_obj.read().decode("utf-8", errors="replace")


def resolve_max_parallel_encodes(value: int | None) -> int:
    if value is None:
        return MAX_PARALLEL_ENCODES
    return max(1, min(int(value), MAX_PARALLEL_ENCODES))


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def ffprobe_duration(path: Path, *, timeout_s: int = 30) -> float:
    proc = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "csv=p=0",
            str(path),
        ],
        text=True,
        capture_output=True,
        timeout=timeout_s,
        check=False,
    )
    try:
        return max(0.0, float(proc.stdout.strip()))
    except ValueError:
        return 0.0


def has_audio_stream(path: Path, *, timeout_s: int = 15) -> bool:
    proc = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "a:0",
            "-show_entries",
            "stream=index",
            "-of",
            "csv=p=0",
            str(path),
        ],
        text=True,
        capture_output=True,
        timeout=timeout_s,
        check=False,
    )
    return bool(proc.stdout.strip())


def ffprobe_json(cmd: list[str], *, timeout_s: int = 30) -> dict[str, Any]:
    proc = subprocess.run(
        cmd,
        text=True,
        capture_output=True,
        timeout=timeout_s,
        check=False,
    )
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "ffprobe failed").strip()
        raise RuntimeError(detail)
    try:
        payload = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError("ffprobe returned invalid JSON") from exc
    if not isinstance(payload, dict):
        raise RuntimeError("ffprobe did not return a JSON object")
    return payload


def ffprobe_video_stream(path: Path, *, timeout_s: int = 30) -> dict[str, Any] | None:
    proc = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_streams",
            "-of",
            "json",
            str(path),
        ],
        text=True,
        capture_output=True,
        timeout=timeout_s,
        check=False,
    )
    if proc.returncode != 0:
        return None
    try:
        payload = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return None
    streams = payload.get("streams") or []
    return streams[0] if streams else None


def ffmpeg_filter_names() -> set[str]:
    global ffmpeg_filter_cache
    with ffmpeg_filter_lock:
        if ffmpeg_filter_cache is not None:
            return ffmpeg_filter_cache
        proc = subprocess.run(
            ["ffmpeg", "-hide_banner", "-filters"],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            check=False,
        )
        names: set[str] = set()
        for line in proc.stdout.splitlines():
            parts = line.split()
            if len(parts) >= 3 and "->" in parts[2]:
                names.add(parts[1])
        ffmpeg_filter_cache = names
        return names


def ffmpeg_filter_available(name: str) -> bool:
    return name in ffmpeg_filter_names()


def frame_crop(stream: dict[str, Any]) -> tuple[int, int, int, int]:
    for side_data in stream.get("side_data_list") or []:
        if side_data.get("side_data_type") == "Frame Cropping":
            return (
                int(side_data.get("crop_top") or 0),
                int(side_data.get("crop_bottom") or 0),
                int(side_data.get("crop_left") or 0),
                int(side_data.get("crop_right") or 0),
            )
    return 0, 0, 0, 0


def display_rotation(stream: dict[str, Any]) -> int:
    for side_data in stream.get("side_data_list") or []:
        if side_data.get("side_data_type") == "Display Matrix":
            try:
                return int(round(float(side_data.get("rotation") or 0))) % 360
            except (TypeError, ValueError):
                return 0
    return 0


def source_geometry_details(source: Path) -> dict[str, int] | None:
    stream = ffprobe_video_stream(source)
    if not stream:
        return None
    try:
        width = int(stream.get("width") or 0)
        height = int(stream.get("height") or 0)
    except (TypeError, ValueError):
        return None
    if width <= 0 or height <= 0:
        return None
    crop_top, crop_bottom, crop_left, crop_right = frame_crop(stream)
    cropped_width = max(1, width - crop_left - crop_right)
    cropped_height = max(1, height - crop_top - crop_bottom)
    rotation = display_rotation(stream)
    display_width = cropped_width
    display_height = cropped_height
    if rotation in {90, 270}:
        display_width, display_height = display_height, display_width
    return {
        "width": width,
        "height": height,
        "crop_top": crop_top,
        "crop_bottom": crop_bottom,
        "crop_left": crop_left,
        "crop_right": crop_right,
        "rotation": rotation,
        "display_width": display_width,
        "display_height": display_height,
    }


def expected_archive_geometry(source: Path) -> tuple[int, int] | None:
    details = source_geometry_details(source)
    if details is None:
        return None
    return details["display_width"], details["display_height"]


def source_frame_rate(source: Path) -> float | None:
    stream = ffprobe_video_stream(source)
    if not stream:
        return None
    for key in ("avg_frame_rate", "r_frame_rate"):
        value = str(stream.get(key) or "")
        if "/" in value:
            numerator, denominator = value.split("/", 1)
            try:
                numerator_f = float(numerator)
                denominator_f = float(denominator)
            except ValueError:
                continue
            if denominator_f > 0 and numerator_f > 0:
                return numerator_f / denominator_f
        else:
            try:
                parsed = float(value)
            except ValueError:
                continue
            if parsed > 0:
                return parsed
    return None


def ffmpeg_number(value: float) -> str:
    if float(value).is_integer():
        return str(int(value))
    return f"{value:.6f}".rstrip("0").rstrip(".")


def scale_geometry(width: int, height: int, max_height: int | None) -> tuple[int, int]:
    if max_height is None or height <= max_height:
        return width, height
    scaled_width = max(2, int(round((width * max_height / height) / 2.0)) * 2)
    return scaled_width, max_height


def archive_scale_target(
    source: Path, archive: ArchiveEncodeProfile
) -> tuple[dict[str, int], tuple[int, int]] | None:
    if archive.max_height is None:
        return None
    details = source_geometry_details(source)
    if details is None or details["display_height"] <= archive.max_height:
        return None
    return details, scale_geometry(
        details["display_width"],
        details["display_height"],
        archive.max_height,
    )


def archive_frame_rate_filters(source: Path, archive: ArchiveEncodeProfile) -> list[str]:
    filters: list[str] = []
    if archive.fps_mode == "halve_60_to_30":
        fps = source_frame_rate(source)
        if fps is not None and fps >= 45.0:
            output_fps = archive.output_fps or 30.0
            filters.append(f"framestep=2,setpts=N/({ffmpeg_number(output_fps)}*TB)")
        else:
            log.info("not halving frame rate for %s because source fps is %s", source, fps)
    return filters


def archive_video_filters(source: Path, archive: ArchiveEncodeProfile) -> list[str]:
    filters = archive_frame_rate_filters(source, archive)
    if archive_scale_target(source, archive) is not None:
        filters.append(f"scale=-2:{archive.max_height}:flags={archive.scale_flags}")
    return filters


def hardware_scale_mode() -> tuple[str, bool]:
    mode = VIDEO_SCALE_MODE
    if mode in {"", "cpu", "none"}:
        mode = "software"
    required = mode.endswith("-required")
    if required:
        mode = mode.removesuffix("-required")
    if mode == "auto":
        mode = "cuda"
    if mode not in {"software", "cuda", "npp"}:
        raise RuntimeError(f"unsupported MUNCHY_VIDEO_SCALE_MODE={VIDEO_SCALE_MODE!r}")
    return mode, required


def hardware_scale_interp(mode: str, scale_flags: str) -> str | None:
    if mode == "cuda":
        return {
            "fast_bilinear": "bilinear",
            "bilinear": "bilinear",
            "bicubic": "bicubic",
            "lanczos": "lanczos",
        }.get(scale_flags)
    if mode == "npp":
        return {
            "fast_bilinear": "linear",
            "bilinear": "linear",
            "bicubic": "cubic",
            "lanczos": "lanczos",
            "spline": "lanczos",
            "super": "super",
        }.get(scale_flags)
    return None


def archive_hardware_scale_filter(
    source: Path,
    archive: ArchiveEncodeProfile,
    decoder_args: list[str],
    *,
    allow_format_only: bool = False,
) -> str | None:
    mode, required = hardware_scale_mode()
    if mode == "software":
        return None
    scale_target = archive_scale_target(source, archive)
    if scale_target is None and not allow_format_only:
        return None

    def fallback(reason: str) -> None:
        if required:
            raise RuntimeError(f"{mode} archive scaling requested but unavailable: {reason}")
        log.info("falling back to software archive scaling for %s: %s", source, reason)

    frame_rate_filters = archive_frame_rate_filters(source, archive)
    if frame_rate_filters:
        fallback("frame-rate filters require the software filter path")
        return None
    if not any(arg.endswith("_cuvid") for arg in decoder_args):
        fallback("source is not using a CUVID decoder")
        return None
    if scale_target is not None:
        details, target = scale_target
        if any(
            details[key] != 0
            for key in ("crop_top", "crop_bottom", "crop_left", "crop_right", "rotation")
        ):
            fallback("cropped or rotated display geometry requires the software filter path")
            return None
    filter_name = "scale_npp" if mode == "npp" else "scale_cuda"
    if not ffmpeg_filter_available(filter_name):
        fallback(f"ffmpeg filter {filter_name!r} is not available in this image")
        return None
    interp = hardware_scale_interp(mode, archive.scale_flags)
    if interp is None:
        fallback(f"{filter_name} does not support scale_flags={archive.scale_flags!r}")
        return None
    pix_fmt = archive.pix_fmt or ARCHIVE_PIX_FMT
    if scale_target is None:
        return f"{filter_name}=format={pix_fmt}"
    width, height = target
    return f"{filter_name}=w={width}:h={height}:format={pix_fmt}:interp_algo={interp}"


def archive_decoder_args(source: Path) -> list[str]:
    if VIDEO_DECODE_MODE in {"", "cpu", "software", "none"}:
        return []
    stream = ffprobe_video_stream(source)
    codec = str((stream or {}).get("codec_name") or "")
    decoder = CUVID_DECODERS.get(codec)
    if decoder:
        return ["-c:v", decoder]
    if VIDEO_DECODE_MODE in {"cuvid-required", "required"}:
        raise RuntimeError(
            f"no CUVID decoder configured for video codec {codec or 'unknown'} in {source}"
        )
    if VIDEO_DECODE_MODE not in {"auto", "cuvid"}:
        raise RuntimeError(f"unsupported MUNCHY_VIDEO_DECODE_MODE={VIDEO_DECODE_MODE!r}")
    log.info("falling back to software decode for %s codec=%s", source, codec or "unknown")
    return []


def validate_archive_geometry(source: Path, output: Path, archive: ArchiveEncodeProfile) -> None:
    expected = expected_archive_geometry(source)
    actual_stream = ffprobe_video_stream(output)
    if expected is None or not actual_stream:
        return
    expected = scale_geometry(expected[0], expected[1], archive.max_height)
    try:
        actual = (int(actual_stream.get("width") or 0), int(actual_stream.get("height") or 0))
    except (TypeError, ValueError):
        return
    if actual != expected:
        raise RuntimeError(
            f"archive geometry mismatch for {source}: expected {expected[0]}x{expected[1]}, "
            f"got {actual[0]}x{actual[1]} in {output}"
        )


def archive_container_suffix(archive: ArchiveEncodeProfile) -> str:
    return ".webm" if archive.container == "webm" else ".mkv"


def archive_container_muxer(archive: ArchiveEncodeProfile) -> str:
    return "webm" if archive.container == "webm" else "matroska"


def validate_archive_container_source(source: Path, archive: ArchiveEncodeProfile) -> None:
    if archive.container != "webm":
        return
    metadata = ffprobe_json(
        [
            "ffprobe",
            "-v",
            "error",
            "-print_format",
            "json",
            "-show_streams",
            "-show_chapters",
            "-show_format",
            "-show_programs",
            str(source),
        ]
    )
    source_format = metadata.get("format")
    format_name = (
        str(source_format.get("format_name") or "").lower()
        if isinstance(source_format, dict)
        else ""
    )
    is_iso_bmff = any(token in format_name for token in ("mov", "mp4", "m4a", "3gp", "3g2", "mj2"))
    is_matroska = "matroska" in format_name or "webm" in format_name
    unsupported_streams: list[str] = []
    for stream in metadata.get("streams") or []:
        if not isinstance(stream, dict):
            continue
        codec_type = str(stream.get("codec_type") or "unknown")
        try:
            stream_index = int(str(stream.get("index")))
        except (TypeError, ValueError):
            stream_index = -1
        disposition = stream.get("disposition")
        attached_pic = isinstance(disposition, dict) and str(
            disposition.get("attached_pic", "0")
        ) not in {"0", "false", "False"}
        stream_needs_mkv = codec_type in {"subtitle", "attachment"} or attached_pic
        stream_unknown_to_source_artifacts = codec_type not in {
            "video",
            "audio",
            "data",
            "subtitle",
            "attachment",
        }
        data_stream_better_in_mkv = codec_type == "data" and (is_matroska or not is_iso_bmff)
        if stream_needs_mkv or stream_unknown_to_source_artifacts or data_stream_better_in_mkv:
            label = f"stream:{stream_index}" if stream_index >= 0 else "unknown stream"
            unsupported_streams.append(f"{label} ({codec_type})")
    if unsupported_streams:
        raise RuntimeError(
            f'archive.container = "webm" is only supported when MKV would not preserve '
            f"additional source streams directly; {source} has streams that need MKV: "
            f"{', '.join(unsupported_streams)}. "
            'Use archive.container = "mkv" when the source has side streams that MKV can '
            "preserve directly; MP4/MOV data streams may use WebM only when the source-artifacts "
            "audit can account for them."
        )
    if metadata.get("chapters"):
        raise RuntimeError(
            f'archive.container = "webm" is not supported for sources with chapters: {source}. '
            'Use archive.container = "mkv" for sources with chapter structure.'
        )
    if metadata.get("programs"):
        raise RuntimeError(
            f'archive.container = "webm" is not supported for sources with programs: {source}. '
            'Use archive.container = "mkv" for sources with program structure.'
        )


def iter_files(root: Path, extensions: set[str]) -> list[Path]:
    return [
        path
        for path in sorted(root.rglob("*"))
        if path.is_file() and path.suffix.lower() in extensions
    ]


def output_for(source: Path, input_root: Path, output_root: Path, suffix: str) -> Path:
    rel = source.relative_to(input_root)
    return (output_root / rel).with_suffix(suffix)


TIMESTAMP_PATTERNS = [
    re.compile(
        r"(?<!\d)(\d{4})[._-]?([01]\d)[._-]?([0-3]\d)[ T_-]?"
        r"([0-2]\d)[.:_-]?([0-5]\d)[.:_-]?([0-5]\d)(?!\d)"
    ),
    re.compile(
        r"(?<!\d)(\d{2})[._-]?([01]\d)[._-]?([0-3]\d)[ T_-]?"
        r"([0-2]\d)[.:_-]?([0-5]\d)[.:_-]?([0-5]\d)(?!\d)"
    ),
    re.compile(r"(\d{4})[-_]?([01]\d)[-_]?([0-3]\d)T([0-2]\d)([0-5]\d)([0-5]\d)"),
    re.compile(
        r"(\d{4})-(\d{2})-(\d{2})\s+at\s+([0-2]\d)\.([0-5]\d)\.([0-5]\d)",
        re.I,
    ),
]


def epoch_from_filename(path: Path) -> int | None:
    name = path.name
    for pattern in TIMESTAMP_PATTERNS:
        match = pattern.search(name)
        if match is None:
            continue
        year, month, day, hour, minute, second = [int(value) for value in match.groups()]
        if len(match.group(1)) == 2:
            year = 2000 + year if year < 70 else 1900 + year
        try:
            return int(datetime(year, month, day, hour, minute, second).timestamp())
        except ValueError:
            continue
    return None


def base_epoch_for_file(path: Path) -> int:
    return epoch_from_filename(path) or int(path.stat().st_mtime)


def build_len_slots(
    target_sec: int,
    min_slot_sec: int,
    max_slot_sec: int,
    *,
    rng: random.Random | None = None,
) -> list[int]:
    chooser = rng or random
    remain = max(0, int(target_sec))
    slots: list[int] = []
    while remain >= min_slot_sec:
        length = chooser.randint(min_slot_sec, max_slot_sec)
        if length > remain:
            length = remain
        slots.append(length)
        remain -= length
    index = 0
    while remain > 0 and slots:
        slots[index] += 1
        index = (index + 1) % len(slots)
        remain -= 1
    if not slots and target_sec > 0:
        slots.append(target_sec)
    return slots


def quotas_by_duration(durations: list[float], slot_count: int, min_seconds: int) -> list[int]:
    if not durations or slot_count <= 0:
        return [0] * len(durations)
    quotas = [0] * len(durations)
    eligible = [
        index
        for index, duration in enumerate(durations)
        if duration > 0 and int(duration) >= min_seconds
    ]
    if not eligible:
        eligible = [index for index, duration in enumerate(durations) if duration > 0]
    if not eligible:
        return quotas

    total = sum(max(0.0, durations[index]) for index in eligible)
    if total <= 0:
        return quotas

    if len(eligible) <= slot_count:
        for index in eligible:
            quotas[index] = 1
        while sum(quotas) < slot_count:
            best_index = max(
                eligible,
                key=lambda index: (
                    (durations[index] / total * slot_count) - quotas[index],
                    durations[index],
                    -index,
                ),
            )
            quotas[best_index] += 1
        return quotas

    cumulative: list[float] = []
    cumulative_indexes: list[int] = []
    running = 0.0
    for index in eligible:
        running += max(0.0, durations[index])
        cumulative.append(running)
        cumulative_indexes.append(index)

    def source_at(position: float) -> int:
        selected = bisect.bisect_left(cumulative, min(position, cumulative[-1]))
        selected = min(selected, len(cumulative_indexes) - 1)
        return cumulative_indexes[selected]

    if slot_count == 1:
        quotas[source_at(total / 2.0)] = 1
        return quotas

    # Keep the chronological endpoints visible, then sample the interior by duration.
    quotas[eligible[0]] += 1
    quotas[eligible[-1]] += 1
    interior_slots = slot_count - 2
    for slot in range(interior_slots):
        position = (slot + 1) * total / (interior_slots + 1)
        quotas[source_at(position)] += 1
    return quotas


def stable_review_plan_seed(
    sources: list[Path],
    durations: list[float],
    *,
    target_sec: int,
    min_sec: int,
    max_sec: int,
) -> str:
    payload = {
        "version": 1,
        "target_sec": int(target_sec),
        "min_sec": int(min_sec),
        "max_sec": int(max_sec),
        "sources": [
            {
                "path": str(source),
                "duration": round(float(duration), 6),
            }
            for source, duration in zip(sources, durations, strict=False)
        ],
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()


def plan_review_clips(
    sources: list[Path],
    *,
    target_sec: int,
    min_sec: int,
    max_sec: int,
    seed: str | None = None,
) -> dict[str, Any]:
    durations = [ffprobe_duration(path) for path in sources]
    if not any(duration > 0 for duration in durations):
        raise RuntimeError("cannot read durations for review sources")
    combined = sum(durations)
    target = min(target_sec, int(combined)) if combined > 0 else 0
    plan_seed = seed or stable_review_plan_seed(
        sources,
        durations,
        target_sec=target_sec,
        min_sec=min_sec,
        max_sec=max_sec,
    )
    rng = random.Random(plan_seed)
    slots = build_len_slots(target, min_sec, max_sec, rng=rng)
    quotas = quotas_by_duration(durations, len(slots), min_sec)
    files: list[dict[str, Any]] = []
    clips: list[dict[str, Any]] = []
    clip_index = 1
    for source_index, source in enumerate(sources):
        duration = durations[source_index]
        quota = quotas[source_index]
        base_epoch = base_epoch_for_file(source)
        files.append(
            {
                "path": str(source),
                "duration": duration,
                "quota": quota,
                "base_epoch": base_epoch,
            }
        )
        if quota <= 0:
            continue
        part = duration / quota
        for slot in range(1, quota + 1):
            length = float(slots[clip_index - 1] if clip_index - 1 < len(slots) else min_sec)
            slot_start = (slot - 1) * part
            max_offset = max(0.0, part - length)
            start = slot_start + (rng.random() * max_offset if max_offset > 0 else 0.0)
            start = min(start, max(0.0, duration - length))
            clips.append(
                {
                    "index": clip_index,
                    "source": str(source),
                    "start": round(start, 6),
                    "length": round(length, 6),
                    "epoch": int(base_epoch + int(start)),
                }
            )
            clip_index += 1
    return {
        "kind": "munchy.qcut-plan",
        "version": 1,
        "seed": plan_seed,
        "target_sec": target,
        "min_sec": min_sec,
        "max_sec": max_sec,
        "slots": slots,
        "files": files,
        "clips": clips,
    }


def archive_video_encode_args(
    archive: ArchiveEncodeProfile,
    *,
    hardware_frames: bool = False,
) -> list[str]:
    args = [
        "-c:v",
        "av1_nvenc",
        "-preset",
        archive.preset or ARCHIVE_PRESET,
        "-tune",
        archive.tune or ARCHIVE_TUNE,
        "-rc",
        "vbr",
        "-cq",
        str(archive.quality if archive.quality is not None else ARCHIVE_CQ),
        "-b:v",
        "0",
        "-multipass",
        "fullres",
        "-spatial-aq",
        "1",
        "-temporal-aq",
        "1",
        "-rc-lookahead",
        "32",
        "-lookahead_level",
        ARCHIVE_LOOKAHEAD_LEVEL,
        "-b_ref_mode",
        "middle",
        "-split_encode_mode",
        ARCHIVE_SPLIT_ENCODE_MODE,
        "-fps_mode",
        "passthrough",
    ]
    if not hardware_frames:
        args[-2:-2] = ["-pix_fmt", archive.pix_fmt or ARCHIVE_PIX_FMT]
    return args


def archive_audio_encode_args(audio: ArchiveAudioProfile) -> list[str]:
    args: list[str] = []
    if audio.sample_rate is not None:
        args.extend(["-ar", str(audio.sample_rate)])
    else:
        args.extend(["-ar", "48000"])
    if audio.channels is not None:
        args.extend(["-ac", str(audio.channels)])
    args.extend(["-b:a", audio.bitrate or ARCHIVE_AUDIO_BITRATE])
    if audio.vbr is not None:
        vbr = "on" if audio.vbr is True else "off" if audio.vbr is False else str(audio.vbr)
        args.extend(["-vbr", vbr])
    if audio.compression_level is not None:
        args.extend(["-compression_level", str(audio.compression_level)])
    if audio.application is not None:
        args.extend(["-application", audio.application])
    if audio.frame_duration is not None:
        args.extend(["-frame_duration", ffmpeg_number(audio.frame_duration)])
    if audio.cutoff is not None:
        args.extend(["-cutoff", str(audio.cutoff)])
    return args


def av1_archive_command(
    source: Path,
    dest: Path,
    archive: ArchiveEncodeProfile,
    *,
    metadata: ProjectionMetadata | None = None,
) -> list[str]:
    dest.parent.mkdir(parents=True, exist_ok=True)
    validate_archive_container_source(source, archive)
    decoder_args = archive_decoder_args(source)
    hardware_scale_filter = archive_hardware_scale_filter(source, archive, decoder_args)
    filters = (
        [hardware_scale_filter] if hardware_scale_filter else archive_video_filters(source, archive)
    )
    hardware_frames = hardware_scale_filter is not None
    if hardware_frames:
        decoder_args = [
            "-hwaccel",
            "cuda",
            "-hwaccel_output_format",
            "cuda",
            *decoder_args,
        ]
    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-y",
        "-ignore_unknown",
        *decoder_args,
        "-i",
        str(source),
        "-map",
        "0:v?",
        "-map",
        "0:a?",
        "-map_metadata",
        "0",
        *archive_video_encode_args(archive, hardware_frames=hardware_frames),
        "-c:a",
        "libopus",
        *archive_audio_encode_args(archive.audio),
        *ffmpeg_container_metadata_args(metadata),
        "-f",
        archive_container_muxer(archive),
        str(dest),
    ]
    if archive.container == "mkv":
        metadata_index = cmd.index("-map_metadata")
        cmd[metadata_index:metadata_index] = ["-map", "0:s?", "-map", "0:t?"]
        muxer_index = cmd.index("-f")
        cmd[muxer_index:muxer_index] = ["-c:s", "copy", "-c:t", "copy"]
    if filters:
        filter_index = cmd.index("-map_metadata") + 2
        cmd[filter_index:filter_index] = ["-vf", ",".join(filters)]
    return cmd


def qcut_video_command(
    source: Path,
    dest: Path,
    *,
    start: float,
    length: float,
    archive: ArchiveEncodeProfile,
) -> list[str]:
    dest.parent.mkdir(parents=True, exist_ok=True)
    decoder_args = archive_decoder_args(source)
    hardware_scale_filter = archive_hardware_scale_filter(
        source,
        archive,
        decoder_args,
        allow_format_only=True,
    )
    filters = (
        [hardware_scale_filter] if hardware_scale_filter else archive_video_filters(source, archive)
    )
    hardware_frames = hardware_scale_filter is not None
    if hardware_frames:
        decoder_args = [
            "-hwaccel",
            "cuda",
            "-hwaccel_output_format",
            "cuda",
            *decoder_args,
        ]
    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "warning",
        "-y",
        "-ss",
        f"{start:.6f}",
        "-t",
        f"{length:.6f}",
        *decoder_args,
        "-i",
        str(source),
        "-map",
        "0:v?",
        "-map",
        "0:a?",
        "-map_metadata",
        "0",
        *archive_video_encode_args(archive, hardware_frames=hardware_frames),
        "-c:a",
        "libopus",
        *archive_audio_encode_args(archive.audio),
        "-f",
        archive_container_muxer(archive),
        str(dest),
    ]
    if filters:
        filter_index = cmd.index("-map_metadata") + 2
        cmd[filter_index:filter_index] = ["-vf", ",".join(filters)]
    return cmd


def audio_review_clip_command(
    source: Path,
    dest: Path,
    *,
    start: float,
    length: float,
    archive: ArchiveEncodeProfile,
) -> list[str]:
    dest.parent.mkdir(parents=True, exist_ok=True)
    return [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "warning",
        "-y",
        "-ss",
        f"{start:.6f}",
        "-t",
        f"{length:.6f}",
        "-i",
        str(source),
        "-vn",
        "-c:a",
        "libopus",
        *archive_audio_encode_args(archive.audio),
        "-f",
        "opus",
        str(dest),
    ]


def run_encode_item(
    cmd: list[str],
    *,
    output_path: Path,
    action: str,
    dry_run: bool,
    source_artifacts_source: Path | None = None,
    source_artifacts_profile: dict[str, Any] | None = None,
    source_filesystem_metadata: dict[str, Any] | None = None,
    source_artifacts_sidecars: list[dict[str, Any]] | None = None,
    on_start: Callable[[], None] | None = None,
) -> dict[str, Any]:
    with encode_semaphore:
        if on_start is not None:
            on_start()
        output_path.parent.mkdir(parents=True, exist_ok=True)
        if source_artifacts_source is not None and not source_artifacts_source.exists():
            raise InputVanishedDuringJob(
                f"source disappeared during job cleanup: {source_artifacts_source}"
            )
        try:
            result = run_command(cmd, action=action, dry_run=dry_run)
        except RuntimeError as exc:
            if source_artifacts_source is not None and not source_artifacts_source.exists():
                raise InputVanishedDuringJob(
                    f"source disappeared during job cleanup: {source_artifacts_source}"
                ) from exc
            raise
    payload: dict[str, Any] = {
        "output": str(output_path),
        "command": result["command"],
        "duration_s": result["duration_s"],
    }
    if result.get("dry_run"):
        payload["dry_run"] = True
    elif source_artifacts_source is not None:
        if not source_artifacts_source.exists():
            raise InputVanishedDuringJob(
                f"source disappeared during job cleanup: {source_artifacts_source}"
            )
        payload["source_artifacts"] = build_strict_source_artifacts(
            source=source_artifacts_source,
            archive_mkv=output_path,
            encode_command=result["command"],
            encode_profile=source_artifacts_profile,
            source_filesystem_metadata=source_filesystem_metadata,
            source_sidecars=source_artifacts_sidecars,
        )
    payload["bytes"] = output_path.stat().st_size if output_path.exists() else 0
    if output_path.exists():
        payload["sha256"] = file_sha256(output_path)
    return payload


def projection_metadata_for_source(
    rel_path: str,
    *,
    container_metadata: Mapping[str, dict[str, Any]] | None,
    required: bool,
) -> ProjectionMetadata | None:
    if not container_metadata:
        if required:
            raise RuntimeError(f"container metadata is required for {rel_path}")
        return None
    raw = container_metadata.get(rel_path)
    if raw is None:
        if required:
            raise RuntimeError(f"container metadata is required for {rel_path}")
        return None
    if not isinstance(raw, Mapping):
        raise RuntimeError(f"container metadata for {rel_path} must be an object")
    return ProjectionMetadata.from_dict(raw)


def run_batch(
    *,
    sources: list[Path],
    input_root: Path,
    output_root: Path,
    suffix: str,
    command_builder: Callable[[Path, Path, ProjectionMetadata | None], list[str]],
    label: str,
    dry_run: bool,
    validate_archive: ArchiveEncodeProfile | None = None,
    source_artifacts: bool = False,
    source_artifacts_profile: dict[str, Any] | None = None,
    source_artifacts_sidecars: Mapping[str, list[dict[str, Any]]] | None = None,
    container_metadata: Mapping[str, dict[str, Any]] | None = None,
    container_metadata_required: bool = True,
    max_parallel_encodes: int | None = None,
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    filesystem_metadata = load_filesystem_metadata_map(input_root)
    if source_artifacts:
        missing_metadata: list[str] = []
        for source in sources:
            rel_path = source.relative_to(input_root).as_posix()
            if rel_path not in filesystem_metadata:
                missing_metadata.append(rel_path)
        if missing_metadata:
            sample = ", ".join(missing_metadata[:5])
            if len(missing_metadata) > 5:
                sample += f", ... ({len(missing_metadata)} total)"
            raise RuntimeError(
                f"unresumable: source filesystem metadata sidecar is missing entries for {sample}"
            )
    worker_count = resolve_max_parallel_encodes(max_parallel_encodes)
    with ThreadPoolExecutor(max_workers=worker_count) as pool:
        futures = {}
        for source in sources:
            dest = output_for(source, input_root, output_root, suffix)
            rel_path = source.relative_to(input_root).as_posix()
            metadata = projection_metadata_for_source(
                rel_path,
                container_metadata=container_metadata,
                required=container_metadata_required,
            )
            cmd = command_builder(source, dest, metadata)
            futures[
                pool.submit(
                    run_encode_item,
                    cmd,
                    output_path=dest,
                    action=label,
                    dry_run=dry_run,
                    source_artifacts_source=source if source_artifacts else None,
                    source_artifacts_profile=source_artifacts_profile,
                    source_artifacts_sidecars=(
                        source_artifacts_sidecars.get(rel_path, [])
                        if source_artifacts_sidecars
                        else []
                    ),
                    source_filesystem_metadata=filesystem_metadata.get(rel_path),
                )
            ] = source
        for future in as_completed(futures):
            source = futures[future]
            item = future.result()
            if validate_archive and not dry_run:
                validate_archive_geometry(source, Path(str(item["output"])), validate_archive)
            item["source"] = str(source)
            results.append(item)
    return sorted(results, key=lambda item: item["source"])


def concat_with_mkvmerge(
    clips: list[Path],
    output_path: Path,
    *,
    container: ArchiveContainer,
    dry_run: bool,
) -> dict[str, Any]:
    if not clips:
        raise RuntimeError("no clips produced for concat")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        "mkvmerge",
        "--quiet",
    ]
    if container == "webm":
        cmd.append("--webm")
    cmd += [
        "--no-track-tags",
        "--no-global-tags",
        "--no-chapters",
        "--append-mode",
        "track",
        "-o",
        str(output_path),
        str(clips[0]),
    ]
    cmd.extend("+" + str(path) for path in clips[1:])
    return run_command(cmd, action="qcut concat", dry_run=dry_run)


def concat_opus(clips: list[Path], output_path: Path, *, dry_run: bool) -> dict[str, Any]:
    if not clips:
        raise RuntimeError("no audio clips produced for concat")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    list_path = output_path.with_suffix(output_path.suffix + ".concat.txt")
    list_text = "\n".join(f"file {shlex.quote(str(path))}" for path in clips) + "\n"
    list_path.write_text(list_text, encoding="utf-8")
    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "warning",
        "-y",
        "-f",
        "concat",
        "-safe",
        "0",
        "-i",
        str(list_path),
        "-c",
        "copy",
        str(output_path),
    ]
    result = run_command(cmd, action="audio review concat", dry_run=dry_run)
    if not dry_run:
        list_path.unlink(missing_ok=True)
    return result


def safe_file_size(path: Path) -> int:
    try:
        return path.stat().st_size if path.exists() else 0
    except OSError:
        return 0


def clip_progress_payload(
    *,
    task: str,
    phase: str,
    clips_total: int,
    clips_done: int,
    clips_running: int,
    clips_failed: int,
    output_bytes: int,
    active_output_bytes: int,
    started_at: str,
    finished_at: str | None = None,
) -> dict[str, Any]:
    started = datetime.fromisoformat(started_at.replace("Z", "+00:00"))
    now = datetime.now(UTC)
    elapsed_seconds = max(0.001, (now - started).total_seconds())
    payload: dict[str, Any] = {
        "mode": task,
        "task": task,
        "phase": phase,
        "clips_total": clips_total,
        "clips_done": clips_done,
        "clips_running": clips_running,
        "clips_failed": clips_failed,
        "percent_clips": round((clips_done / clips_total * 100.0) if clips_total else 100.0, 2),
        "output_bytes": output_bytes,
        "active_output_bytes": active_output_bytes,
        "output_rate_bytes_per_second": int(output_bytes / elapsed_seconds),
        "started_at": started_at,
        "elapsed_seconds": round(elapsed_seconds, 3),
        "completed": clips_done == clips_total and clips_total > 0,
    }
    if finished_at:
        payload["finished_at"] = finished_at
    return payload


def normalize_supplied_review_plan(plan: dict[str, Any], *, input_dir: Path) -> dict[str, Any]:
    input_root = input_dir.resolve()
    try:
        normalized = json.loads(json.dumps(plan))
    except (TypeError, ValueError) as exc:
        raise RuntimeError("review plan must be JSON serializable") from exc
    if not isinstance(normalized, dict):
        raise RuntimeError("review plan must be an object")
    clips = normalized.get("clips")
    files = normalized.get("files")
    if not isinstance(clips, list) or not clips:
        raise RuntimeError("review plan must include clips")
    if not isinstance(files, list) or not files:
        raise RuntimeError("review plan must include files")

    for file_info in files:
        if not isinstance(file_info, dict):
            raise RuntimeError("review plan files must be objects")
        source = Path(str(file_info.get("path") or "")).resolve()
        if source != input_root and input_root not in source.parents:
            raise RuntimeError(f"review plan source is outside input_dir: {source}")
        if not source.is_file():
            raise RuntimeError(f"review plan source is missing: {source}")
        float(file_info.get("duration") or 0)
        int(file_info.get("base_epoch") or 0)

    for clip in clips:
        if not isinstance(clip, dict):
            raise RuntimeError("review plan clips must be objects")
        source = Path(str(clip.get("source") or "")).resolve()
        if source != input_root and input_root not in source.parents:
            raise RuntimeError(f"review plan clip source is outside input_dir: {source}")
        if not source.is_file():
            raise RuntimeError(f"review plan clip source is missing: {source}")
        int(clip["index"])
        float(clip["start"])
        float(clip["length"])
        int(clip["epoch"])
    normalized["reused"] = True
    return normalized


def run_qcut_video(
    input_dir: Path,
    review_dir: Path,
    *,
    archive: ArchiveEncodeProfile,
    dry_run: bool,
    review_plan: dict[str, Any] | None = None,
    clip_plan: ReviewClipPlanConfig | None = None,
    max_parallel_encodes: int | None = None,
    progress_callback: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    sources = iter_files(input_dir, VIDEO_EXTENSIONS)
    if not sources and review_plan is None:
        return {"status": "skipped", "reason": "no video sources"}
    work_dir = review_dir / ".qcut_work" / "video"
    work_dir.mkdir(parents=True, exist_ok=True)
    resolved_clip_plan = clip_plan or ReviewClipPlanConfig()
    plan = (
        normalize_supplied_review_plan(review_plan, input_dir=input_dir)
        if review_plan is not None
        else plan_review_clips(
            sources,
            target_sec=resolved_clip_plan.target_seconds,
            min_sec=resolved_clip_plan.min_seconds,
            max_sec=resolved_clip_plan.max_seconds,
        )
    )
    clip_outputs: list[Path] = []
    clip_results: list[dict[str, Any]] = []
    clips_total = len(plan["clips"])
    progress_lock = threading.Lock()
    progress_started_at = now_iso()
    progress_state: dict[str, Any] = {
        "clips_done": 0,
        "clips_running": 0,
        "clips_failed": 0,
        "output_bytes": 0,
        "active_outputs": set(),
    }

    def emit_progress(phase: str, *, finished_at: str | None = None) -> None:
        if progress_callback is None:
            return
        active_outputs = progress_state["active_outputs"]
        active_output_bytes = sum(safe_file_size(path) for path in active_outputs)
        progress_callback(
            clip_progress_payload(
                task="qcut_video",
                phase=phase,
                clips_total=clips_total,
                clips_done=int(progress_state["clips_done"]),
                clips_running=int(progress_state["clips_running"]),
                clips_failed=int(progress_state["clips_failed"]),
                output_bytes=int(progress_state["output_bytes"]),
                active_output_bytes=active_output_bytes,
                started_at=progress_started_at,
                finished_at=finished_at,
            )
        )

    def mark_started(output: Path) -> None:
        with progress_lock:
            progress_state["clips_running"] = int(progress_state["clips_running"]) + 1
            progress_state["active_outputs"].add(output)
            emit_progress("encoding_clips")

    def mark_finished(output: Path, item: dict[str, Any] | None, *, failed: bool = False) -> None:
        with progress_lock:
            active_outputs = progress_state["active_outputs"]
            active_outputs.discard(output)
            progress_state["clips_running"] = max(0, int(progress_state["clips_running"]) - 1)
            if failed:
                progress_state["clips_failed"] = int(progress_state["clips_failed"]) + 1
            else:
                progress_state["clips_done"] = int(progress_state["clips_done"]) + 1
                progress_state["output_bytes"] = int(progress_state["output_bytes"]) + int(
                    (item or {}).get("bytes") or safe_file_size(output)
                )
            emit_progress("encoding_clips")

    emit_progress("planning")

    def encode_clip(clip: dict[str, Any]) -> dict[str, Any]:
        output = work_dir / f"clip{int(clip['index']):03d}{archive_container_suffix(archive)}"
        source = Path(str(clip["source"]))
        cmd = qcut_video_command(
            source,
            output,
            start=float(clip["start"]),
            length=float(clip["length"]),
            archive=archive,
        )
        mark_started(output)
        try:
            item = run_encode_item(
                cmd,
                output_path=output,
                action="qcut video clip",
                dry_run=dry_run,
            )
        except Exception:
            mark_finished(output, None, failed=True)
            raise
        item["clip"] = clip
        mark_finished(output, item)
        return item

    worker_count = resolve_max_parallel_encodes(max_parallel_encodes)
    with ThreadPoolExecutor(max_workers=worker_count) as pool:
        futures = {}
        for clip in plan["clips"]:
            output = work_dir / f"clip{int(clip['index']):03d}{archive_container_suffix(archive)}"
            clip_outputs.append(output)
            futures[pool.submit(encode_clip, clip)] = clip
        for future in as_completed(futures):
            try:
                item = future.result()
            except Exception:
                raise
            clip_results.append(item)

    files_span = plan["files"]
    start_epoch = min(int(file["base_epoch"]) for file in files_span)
    last_file = max(files_span, key=lambda file: int(file["base_epoch"]))
    end_epoch = int(int(last_file["base_epoch"]) + float(last_file["duration"]))
    start_dt = datetime.fromtimestamp(start_epoch)
    end_dt = datetime.fromtimestamp(end_epoch)
    output_path = review_dir / (
        f"{start_dt:%Y%m%dT%H%M%S}--{end_dt:%Y%m%dT%H%M%S} "
        f"auto-edit{archive_container_suffix(archive)}"
    )
    emit_progress("concat")
    concat_result = concat_with_mkvmerge(
        clip_outputs,
        output_path,
        container=archive.container,
        dry_run=dry_run,
    )
    if not dry_run:
        shutil.rmtree(work_dir.parent, ignore_errors=True)
    result: dict[str, Any] = {
        "status": "done",
        "output": str(output_path),
        "plan": plan,
        "clips": sorted(clip_results, key=lambda item: int(item["clip"]["index"])),
        "concat": concat_result,
    }
    if output_path.exists():
        result["bytes"] = output_path.stat().st_size
        result["sha256"] = file_sha256(output_path)
    finished_at = now_iso()
    with progress_lock:
        progress_state["output_bytes"] = int(result.get("bytes") or progress_state["output_bytes"])
        emit_progress("done", finished_at=finished_at)
    result["progress"] = clip_progress_payload(
        task="qcut_video",
        phase="done",
        clips_total=clips_total,
        clips_done=clips_total,
        clips_running=0,
        clips_failed=0,
        output_bytes=int(result.get("bytes") or 0),
        active_output_bytes=0,
        started_at=progress_started_at,
        finished_at=finished_at,
    )
    return result


def run_audio_review(
    input_dir: Path,
    review_dir: Path,
    *,
    archive: ArchiveEncodeProfile,
    dry_run: bool,
    review_plan: dict[str, Any] | None = None,
    clip_plan: ReviewClipPlanConfig | None = None,
    max_parallel_encodes: int | None = None,
) -> dict[str, Any]:
    sources = iter_files(input_dir, AUDIO_EXTENSIONS)
    if not sources:
        sources = [
            path for path in iter_files(input_dir, VIDEO_EXTENSIONS) if has_audio_stream(path)
        ]
    if not sources and review_plan is None:
        return {"status": "skipped", "reason": "no audio sources"}
    work_dir = review_dir / ".qcut_work" / "audio"
    work_dir.mkdir(parents=True, exist_ok=True)
    resolved_clip_plan = clip_plan or ReviewClipPlanConfig()
    plan = (
        normalize_supplied_review_plan(review_plan, input_dir=input_dir)
        if review_plan is not None
        else plan_review_clips(
            sources,
            target_sec=resolved_clip_plan.target_seconds,
            min_sec=resolved_clip_plan.min_seconds,
            max_sec=resolved_clip_plan.max_seconds,
        )
    )
    clip_outputs: list[Path] = []
    clip_results: list[dict[str, Any]] = []
    worker_count = resolve_max_parallel_encodes(max_parallel_encodes)
    with ThreadPoolExecutor(max_workers=worker_count) as pool:
        futures = {}
        for clip in plan["clips"]:
            output = work_dir / f"clip{int(clip['index']):03d}.opus"
            clip_outputs.append(output)
            source = Path(str(clip["source"]))
            cmd = audio_review_clip_command(
                source,
                output,
                start=float(clip["start"]),
                length=float(clip["length"]),
                archive=archive,
            )
            futures[
                pool.submit(
                    run_encode_item,
                    cmd,
                    output_path=output,
                    action="audio review clip",
                    dry_run=dry_run,
                )
            ] = clip
        for future in as_completed(futures):
            item = future.result()
            item["clip"] = futures[future]
            clip_results.append(item)

    files_span = plan["files"]
    start_epoch = min(int(file["base_epoch"]) for file in files_span)
    last_file = max(files_span, key=lambda file: int(file["base_epoch"]))
    end_epoch = int(int(last_file["base_epoch"]) + float(last_file["duration"]))
    start_dt = datetime.fromtimestamp(start_epoch)
    end_dt = datetime.fromtimestamp(end_epoch)
    output_path = review_dir / f"{start_dt:%Y%m%dT%H%M%S}--{end_dt:%Y%m%dT%H%M%S} audio-review.opus"
    concat_result = concat_opus(clip_outputs, output_path, dry_run=dry_run)
    if not dry_run:
        shutil.rmtree(work_dir.parent, ignore_errors=True)
    result: dict[str, Any] = {
        "status": "done",
        "output": str(output_path),
        "plan": plan,
        "clips": sorted(clip_results, key=lambda item: int(item["clip"]["index"])),
        "concat": concat_result,
    }
    if output_path.exists():
        result["bytes"] = output_path.stat().st_size
        result["sha256"] = file_sha256(output_path)
    return result


def maybe_upload_riverhog(req: JobRequest) -> dict[str, Any] | None:
    if not req.riverhog.enabled:
        return None
    if not RIVERHOG_UPLOAD_ENABLED:
        raise RuntimeError(
            "riverhog upload requested, but MUNCHY_RIVERHOG_UPLOAD_ENABLED is not enabled"
        )
    if not req.collection_slug or not req.collection_timestamp:
        raise RuntimeError("riverhog upload requires collection_slug and collection_timestamp")
    cmd = [
        RIVERHOG_COMMAND,
        "upload",
        req.collection_slug,
        str(req.archive_dir),
        "--timestamp",
        req.collection_timestamp,
        "--wait",
        req.riverhog.wait,
    ]
    return run_command(cmd, action="riverhog upload", dry_run=req.dry_run)


def maybe_upload_review(req: JobRequest) -> dict[str, Any] | None:
    if not req.review_upload.enabled:
        return None
    if not REVIEW_UPLOAD_ENABLED:
        raise RuntimeError(
            "review upload requested, but MUNCHY_REVIEW_UPLOAD_ENABLED is not enabled"
        )
    if not REVIEW_UPLOAD_COMMAND:
        raise RuntimeError("review upload requested, but MUNCHY_REVIEW_UPLOAD_COMMAND is empty")
    if req.review_dir is None:
        raise RuntimeError("review upload requires review_dir")
    env = os.environ.copy()
    env["MUNCHY_REVIEW_SOURCE"] = str(req.review_dir)
    env["MUNCHY_JOB_ID"] = req.job_id or ""
    env["MUNCHY_COLLECTION_SLUG"] = req.collection_slug or ""
    env["MUNCHY_COLLECTION_TIMESTAMP"] = req.collection_timestamp or ""
    cmd = ["/bin/sh", "-lc", REVIEW_UPLOAD_COMMAND]
    log.info("review upload: %s", REVIEW_UPLOAD_COMMAND)
    if req.dry_run:
        return {"command": REVIEW_UPLOAD_COMMAND, "dry_run": True}
    result = run_command(cmd, action="review upload", env=env)
    return {
        "command": REVIEW_UPLOAD_COMMAND,
        "stdout": result["stdout"],
        "stderr": result["stderr"],
    }


def run_job(job_id: str, req: JobRequest) -> None:
    archive_profile = (
        req.encode_profile.archive if req.encode_profile is not None else ArchiveEncodeProfile()
    )
    encode_profile_dump = (
        req.encode_profile.runner_payload() if req.encode_profile is not None else None
    )
    max_parallel_encodes = resolve_max_parallel_encodes(req.max_parallel_encodes)
    status: dict[str, Any] = {
        "job_id": job_id,
        "state": "running",
        "profile": req.profile,
        "encode_profile": encode_profile_dump,
        "max_parallel_encodes": max_parallel_encodes,
        "tasks": req.tasks,
        "started_at": now_iso(),
        "input_dir": str(req.input_dir),
        "archive_dir": str(req.archive_dir),
        "review_dir": str(req.review_dir) if req.review_dir is not None else None,
        "items": {},
    }
    write_status(job_id, status)
    status_lock = threading.Lock()

    def update_task_progress(task: str, progress: dict[str, Any]) -> None:
        with status_lock:
            existing = status.setdefault("items", {}).get(task)
            item = dict(existing) if isinstance(existing, dict) else {}
            item["status"] = "running"
            item["progress"] = progress
            status["items"][task] = item
            write_status(job_id, status)

    try:
        video_sources = iter_files(req.input_dir, VIDEO_EXTENSIONS)
        if "archive_video" in req.tasks:
            status["items"]["archive_video"] = run_batch(
                sources=video_sources,
                input_root=req.input_dir,
                output_root=req.archive_dir,
                suffix=archive_container_suffix(archive_profile),
                command_builder=lambda source, dest, metadata: av1_archive_command(
                    source,
                    dest,
                    archive_profile,
                    metadata=metadata,
                ),
                label="archive video encode",
                dry_run=req.dry_run,
                validate_archive=archive_profile,
                source_artifacts=True,
                source_artifacts_profile=encode_profile_dump,
                source_artifacts_sidecars=req.source_artifacts_sidecars,
                container_metadata=req.container_metadata,
                container_metadata_required=req.container_metadata_required,
                max_parallel_encodes=max_parallel_encodes,
            )
            write_status(job_id, status)
        if "qcut_video" in req.tasks:
            if req.review_dir is None:
                raise RuntimeError("qcut_video requires review_dir")
            status["items"]["qcut_video"] = run_qcut_video(
                req.input_dir,
                req.review_dir / "video",
                archive=archive_profile,
                dry_run=req.dry_run,
                review_plan=req.review_plans.get("qcut_video"),
                clip_plan=req.review_clip_plan,
                max_parallel_encodes=max_parallel_encodes,
                progress_callback=lambda progress: update_task_progress(
                    "qcut_video",
                    progress,
                ),
            )
            write_status(job_id, status)
        if "audio_review" in req.tasks:
            if req.review_dir is None:
                raise RuntimeError("audio_review requires review_dir")
            status["items"]["audio_review"] = run_audio_review(
                req.input_dir,
                req.review_dir / "audio",
                archive=archive_profile,
                dry_run=req.dry_run,
                review_plan=req.review_plans.get("audio_review"),
                clip_plan=req.review_clip_plan,
                max_parallel_encodes=max_parallel_encodes,
            )
            write_status(job_id, status)
        status["riverhog_upload"] = maybe_upload_riverhog(req)
        status["review_upload"] = maybe_upload_review(req)
        status["state"] = "succeeded"
        status["finished_at"] = now_iso()
        write_status(job_id, status)
    except InputVanishedDuringJob as exc:
        log.warning("job %s input vanished while finishing: %s", job_id, exc)
        status["state"] = "failed"
        status["error_code"] = "input_vanished"
        status["error"] = str(exc)
        status["finished_at"] = now_iso()
        write_status(job_id, status)
    except Exception as exc:
        log.exception("job %s failed", job_id)
        status["state"] = "failed"
        status["error"] = str(exc)
        status["finished_at"] = now_iso()
        write_status(job_id, status)


@app.get("/health/live")
def health_live() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/health/ready")
def health_ready() -> dict[str, Any]:
    return {
        "status": "ok",
        "data_dir": str(DATA_DIR),
        "max_parallel_encodes": MAX_PARALLEL_ENCODES,
        "video_decode_mode": VIDEO_DECODE_MODE,
        "video_scale_mode": VIDEO_SCALE_MODE,
        "scale_cuda": ffmpeg_filter_available("scale_cuda"),
        "scale_npp": ffmpeg_filter_available("scale_npp"),
        "riverhog_upload_enabled": RIVERHOG_UPLOAD_ENABLED,
        "review_upload_enabled": REVIEW_UPLOAD_ENABLED,
    }


@app.get("/v1/capabilities")
def capabilities() -> dict[str, Any]:
    encoders = subprocess.run(
        ["ffmpeg", "-hide_banner", "-encoders"],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    ).stdout
    filters = subprocess.run(
        ["ffmpeg", "-hide_banner", "-filters"],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    ).stdout
    av1_help = subprocess.run(
        ["ffmpeg", "-hide_banner", "-h", "encoder=av1_nvenc"],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    ).stdout
    return {
        "profiles": ["av1-nvenc-high"],
        "encode_profile": {
            "schema_versions": [1],
            "targets": [MUNCHY_PROFILE_TARGET],
            "archive_codecs": ["av1_nvenc"],
            "containers": ["mkv", "webm"],
            "source_artifact_drops": [
                "stream:N",
                "atom:TYPE",
                "top-level-atom:TYPE",
                "atom-offset:OFFSET",
            ],
            "fps_modes": ["passthrough", "halve_60_to_30"],
            "audio_codecs": ["opus"],
            "scale_flags": ["fast_bilinear", "bilinear", "bicubic", "lanczos", "spline"],
            "scale_modes": [
                "software",
                "cuda",
                "cuda-required",
                "npp",
                "npp-required",
                "auto",
            ],
        },
        "tasks": ["archive_video", "qcut_video", "audio_review"],
        "job_payload": {
            "max_parallel_encodes": {
                "max": MAX_PARALLEL_ENCODES,
            },
        },
        "ffmpeg": {
            "av1_nvenc": "av1_nvenc" in encoders,
            "libopus": "libopus" in encoders,
            "scale_cuda": "scale_cuda" in filters,
            "scale_npp": "scale_npp" in filters,
            "av1_nvenc_uhq": "uhq" in av1_help,
        },
        "archive": {
            "decode_mode": VIDEO_DECODE_MODE,
            "scale_mode": VIDEO_SCALE_MODE,
            "preset": ARCHIVE_PRESET,
            "tune": ARCHIVE_TUNE,
            "cq": ARCHIVE_CQ,
            "lookahead_level": ARCHIVE_LOOKAHEAD_LEVEL,
            "split_encode_mode": ARCHIVE_SPLIT_ENCODE_MODE,
            "pix_fmt": ARCHIVE_PIX_FMT,
            "max_parallel_encodes": MAX_PARALLEL_ENCODES,
            "cuvid_decoders": sorted(CUVID_DECODERS.values()),
            "source_artifacts": {
                "enabled": True,
                "bundle_suffix": SOURCE_ARTIFACTS_SUFFIX,
                "kind": "munchy.source-artifacts",
                "strict_accounting": True,
                "includes": [
                    "manifest.json",
                    "inventory/source-ffprobe.json",
                    "inventory/source-inventory.json",
                    "stream transforms",
                    "rebuild plan",
                    "source container atoms",
                    "source stream artifacts",
                ],
            },
        },
        "qcut": {
            "decode_mode": VIDEO_DECODE_MODE,
            "encode_profile_source": "archive",
            "target_seconds": QCUT_TARGET_SECONDS,
            "min_seconds": QCUT_MIN_SECONDS,
            "max_seconds": QCUT_MAX_SECONDS,
        },
    }


@app.post("/v1/jobs", status_code=202)
def create_job(req: JobRequest, background_tasks: BackgroundTasks) -> dict[str, Any]:
    req.input_dir = ensure_under_data_dir(req.input_dir, name="input_dir")
    req.archive_dir = ensure_under_data_dir(req.archive_dir, name="archive_dir")
    if req.review_dir is not None:
        req.review_dir = ensure_under_data_dir(req.review_dir, name="review_dir")
    if not req.input_dir.is_dir():
        raise HTTPException(status_code=400, detail=f"input_dir is missing: {req.input_dir}")
    job_id = req.job_id or uuid.uuid4().hex
    with jobs_lock:
        existing = jobs.get(job_id)
    if existing and existing.get("state") in {"queued", "running"}:
        raise HTTPException(status_code=409, detail=f"job already active: {job_id}")
    initial = {
        "job_id": job_id,
        "state": "queued",
        "queued_at": now_iso(),
        "profile": req.profile,
        "encode_profile": req.encode_profile.runner_payload()
        if req.encode_profile is not None
        else None,
        "tasks": req.tasks,
    }
    write_status(job_id, initial)
    req.job_id = job_id
    background_tasks.add_task(run_job, job_id, req)
    return initial


@app.get("/v1/jobs/{job_id}")
def get_job(job_id: str) -> dict[str, Any]:
    return load_status(job_id)


def main() -> None:
    uvicorn.run(
        "app.main:app",
        host=os.getenv("MUNCHY_HOST", "0.0.0.0"),
        port=int(os.getenv("MUNCHY_PORT", "8000")),
        log_level=os.getenv("MUNCHY_UVICORN_LOG_LEVEL", "info"),
        log_config=uvicorn_log_config_without_health_access_logs(),
    )


if __name__ == "__main__":
    main()
