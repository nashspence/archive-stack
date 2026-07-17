from __future__ import annotations

import base64
import copy
import errno
import faulthandler
import gzip
import hashlib
import json
import logging
import logging.config
import os
import re
import secrets
import shutil
import signal
import sqlite3
import subprocess
import threading
import time
import uuid
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import asynccontextmanager, closing
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath
from typing import Any, Literal, cast
from urllib.parse import parse_qsl, quote, unquote, urlencode, urljoin, urlsplit, urlunsplit

import httpx
import uvicorn
from fastapi import BackgroundTasks, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from munchy.filesystem_metadata import (
    SOURCE_FILESYSTEM_METADATA_FILENAME,
    load_filesystem_metadata_map,
    write_filesystem_metadata_map,
)
from munchy.job_templates import (
    JobTemplateError,
    job_template_digest,
    normalize_job_template,
    render_job_template_inputs,
)
from munchy.metadata_projection import (
    MetadataProjectionError,
    ProjectionMetadata,
    ffmpeg_container_metadata_args,
    immich_xmp_sidecar_path,
    merge_immich_xmp_sidecar,
    project_immich_metadata,
    render_immich_xmp_sidecar,
)
from munchy.platform_files import (
    DEFAULT_PLATFORM_CRUFT_EXCLUDES,
    normalize_exclude_patterns,
    path_matches_exclude_patterns,
)
from munchy.profiles import (
    MUNCHY_AUDIO_PROFILE_TARGET,
    MUNCHY_PROFILE_TARGET,
    ArchiveContainer,
    EncodeProfile,
)
from munchy.review_sweep import (
    default_encode_profile_for_output_mode,
    ensure_review_sweep_has_variants,
    review_sweep_variants,
)
from munchy.routing import (
    PATH_PREDICATE_KEYS,
    PREDICATE_KEYS,
    RoutingFile,
    apply_sidecar_rules,
    exiftool_routing_facts,
    match_route,
    matched_fact_values,
    normalize_exiftool_tag,
    routing_exiftool_summary,
    routing_exiftool_tags,
    routing_file_facts,
    routing_file_requires_exiftool,
    routing_file_requires_probe,
    routing_plan,
    routing_probe_summary,
    routing_requires_exiftool,
    routing_requires_probe,
    sidecar_exiftool_fact_requests,
    sidecar_rule_exiftool_tags,
    sidecar_rule_fact_extractors,
    sidecar_rules,
)
from munchy.source_artifact_bridge import (
    build_preserve_source_artifacts,
    build_strict_source_artifacts,
)
from munchy.template_registry import ensure_template_registry_schema
from munchy.uvicorn_logging import uvicorn_log_config_without_health_access_logs
from riverhog_cli.client import ApiClient
from riverhog_core.domain.errors import Conflict, HashMismatch, NotFound, ServiceUnavailable
from riverhog_core.operator_reminders import (
    next_operator_reminder_at,
    normalize_reminder_time,
    operator_reminder_due,
    parse_reminder_interval_seconds,
    reminder_zone,
)
from riverhog_core.timestamps import (
    format_utc_timestamp,
    parse_utc_timestamp,
    utc_now,
    utc_timestamp_now,
)
from riverhog_core.tus_upload import TusUploadLease, upload_path_to_tus
from riverhog_core.webhooks import build_munchy_job_payload

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
    "root": {"level": os.getenv("MUNCHY_RUNNER_LOG_LEVEL", "INFO"), "handlers": ["stdout"]},
    "loggers": {
        "httpx": {
            "level": os.getenv("MUNCHY_RUNNER_HTTPX_LOG_LEVEL", "WARNING"),
            "handlers": ["stdout"],
            "propagate": False,
        },
    },
}
logging.config.dictConfig(LOGGING)
log = logging.getLogger("munchy_runner")

try:
    faulthandler.register(signal.SIGUSR1, all_threads=True)
except (RuntimeError, ValueError):
    log.debug("SIGUSR1 thread dumps are not available in this runtime")


def env_flag(name: str, default: str = "0") -> bool:
    return os.getenv(name, default).lower() in {"1", "true", "yes", "on"}


def env_list(name: str) -> tuple[str, ...]:
    return tuple(item.strip() for item in os.getenv(name, "").split(",") if item.strip())


STATE_DIR = Path(os.getenv("MUNCHY_RUNNER_STATE_DIR", "/state")).resolve()
STATE_DB_PATH = Path(
    os.getenv("MUNCHY_RUNNER_STATE_DB", str(STATE_DIR / "runner.sqlite3"))
).resolve()
DEBUG_DIR = Path(os.getenv("MUNCHY_RUNNER_DEBUG_DIR", str(STATE_DIR / "debug"))).resolve()
WORK_DIR = Path(os.getenv("MUNCHY_RUNNER_WORK_DIR", "/work")).resolve()
TUSD_DIR = Path(os.getenv("MUNCHY_RUNNER_TUSD_DIR", "/tusd")).resolve()
TUSD_INTERNAL_BASE_URL = os.getenv(
    "MUNCHY_RUNNER_TUSD_INTERNAL_BASE_URL", "http://127.0.0.1:8093/files"
).rstrip("/")
TUSD_PUBLIC_BASE_URL = os.getenv(
    "MUNCHY_RUNNER_TUSD_PUBLIC_BASE_URL", TUSD_INTERNAL_BASE_URL
).rstrip("/")
TUSD_HOOK_SECRET = os.getenv("MUNCHY_RUNNER_TUSD_HOOK_SECRET", "").strip()
API_TOKEN = os.getenv("MUNCHY_RUNNER_API_TOKEN", "").strip()
ADMIN_TOKEN = os.getenv("MUNCHY_RUNNER_ADMIN_TOKEN", "").strip()
TUSD_PUBLIC_SIGNING_SECRET = os.getenv("MUNCHY_RUNNER_TUSD_PUBLIC_SIGNING_SECRET", "").strip()
GPU_RUNTIME_DIR = Path(
    os.getenv("MUNCHY_RUNNER_GPU_RUNTIME_DIR", "/gpu-runtime/munchy-av1-nvenc")
).resolve()
GPU_MANAGER_URL = os.getenv("MUNCHY_RUNNER_GPU_MANAGER_URL", "http://127.0.0.1:8080").rstrip("/")
GPU_TARGET_URL = os.getenv("MUNCHY_RUNNER_GPU_TARGET_URL", "http://127.0.0.1:8000").rstrip("/")
GPU_TARGET = os.getenv("MUNCHY_RUNNER_GPU_TARGET", "munchy-av1-nvenc")
GPU_LEASE_TTL_S = int(os.getenv("MUNCHY_RUNNER_GPU_LEASE_TTL_S", "28800"))
GPU_WAIT_S = int(os.getenv("MUNCHY_RUNNER_GPU_WAIT_S", "300"))
GPU_REPOST_SECONDS = float(os.getenv("MUNCHY_RUNNER_GPU_REPOST_SECONDS", "120"))
MIN_FREE_BYTES = int(os.getenv("MUNCHY_RUNNER_MIN_FREE_BYTES", str(10 * 1024 * 1024 * 1024)))
GPU_SCRATCH_MULTIPLIER = float(os.getenv("MUNCHY_RUNNER_GPU_SCRATCH_MULTIPLIER", "2.5"))
EAGER_ARCHIVE_SCRATCH_MULTIPLIER = float(
    os.getenv("MUNCHY_RUNNER_EAGER_ARCHIVE_SCRATCH_MULTIPLIER", "0.5")
)
REVIEW_SCRATCH_EXTRA_MULTIPLIER = float(
    os.getenv("MUNCHY_RUNNER_REVIEW_SCRATCH_EXTRA_MULTIPLIER", "0.35")
)
COLLECTION_ARCHIVE_TARGET_SCRATCH_EXTRA_MULTIPLIER = float(
    os.getenv("MUNCHY_RUNNER_COLLECTION_ARCHIVE_TARGET_SCRATCH_EXTRA_MULTIPLIER", "1.25")
)
MAX_ACTIVE_INPUT_UPLOADS = int(os.getenv("MUNCHY_RUNNER_MAX_ACTIVE_INPUT_UPLOADS", "8"))
MAX_RUNNING_JOBS = int(os.getenv("MUNCHY_RUNNER_MAX_RUNNING_JOBS", "1"))
RIVERHOG_UPLOAD_ENABLED = os.getenv("MUNCHY_RUNNER_RIVERHOG_UPLOAD_ENABLED", "0").lower() in {
    "1",
    "true",
    "yes",
    "on",
}
RIVERHOG_WAIT = os.getenv("MUNCHY_RUNNER_RIVERHOG_WAIT", "finalized").strip() or "finalized"
RIVERHOG_UPLOAD_CHUNK_BYTES = int(
    os.getenv("MUNCHY_RUNNER_RIVERHOG_UPLOAD_CHUNK_BYTES", str(8 * 1024 * 1024))
)
RIVERHOG_UPLOAD_WORKERS = max(
    1,
    int(os.getenv("MUNCHY_RUNNER_RIVERHOG_UPLOAD_WORKERS", "8")),
)
RIVERHOG_EAGER_UPLOAD_FILES_PER_TICK = max(
    1,
    int(os.getenv("MUNCHY_RUNNER_RIVERHOG_EAGER_UPLOAD_FILES_PER_TICK", "128")),
)
RIVERHOG_EAGER_UPLOAD_BYTES_PER_TICK = max(
    1,
    int(os.getenv("MUNCHY_RUNNER_RIVERHOG_EAGER_UPLOAD_BYTES_PER_TICK", str(512 * 1024 * 1024))),
)
RIVERHOG_EAGER_UPLOAD_SECONDS_PER_TICK = max(
    0.25,
    float(os.getenv("MUNCHY_RUNNER_RIVERHOG_EAGER_UPLOAD_SECONDS_PER_TICK", "8")),
)
RIVERHOG_EAGER_UPLOAD_INTERVAL_SECONDS = max(
    0.25,
    float(os.getenv("MUNCHY_RUNNER_RIVERHOG_EAGER_UPLOAD_INTERVAL_SECONDS", "1")),
)
RIVERHOG_UPLOAD_SAVE_EVERY_FILES = max(
    1,
    int(os.getenv("MUNCHY_RUNNER_RIVERHOG_UPLOAD_SAVE_EVERY_FILES", "32")),
)
RIVERHOG_UPLOAD_SAVE_EVERY_SECONDS = max(
    0.25,
    float(os.getenv("MUNCHY_RUNNER_RIVERHOG_UPLOAD_SAVE_EVERY_SECONDS", "5")),
)
RIVERHOG_FINALIZE_POLL_SECONDS = max(
    1.0,
    float(os.getenv("MUNCHY_RUNNER_RIVERHOG_FINALIZE_POLL_SECONDS", "5")),
)
UPLOAD_ATTEMPTS = int(os.getenv("MUNCHY_RUNNER_UPLOAD_ATTEMPTS", "3"))
TARGET_UPLOAD_ENABLED = os.getenv("MUNCHY_RUNNER_TARGET_UPLOAD_ENABLED", "0").lower() in {
    "1",
    "true",
    "yes",
    "on",
}
TARGET_UPLOAD_COMMAND = os.getenv("MUNCHY_RUNNER_TARGET_UPLOAD_COMMAND", "").strip()
TARGET_RCLONE_COMMAND = os.getenv("MUNCHY_RUNNER_TARGET_RCLONE_COMMAND", "rclone")
DEFAULT_TARGET_UPLOAD_EXCLUDES = DEFAULT_PLATFORM_CRUFT_EXCLUDES
NOTIFY_ENABLED = env_flag("MUNCHY_RUNNER_NOTIFY_ENABLED")
NOTIFY_REMINDER_INTERVAL_SECONDS = parse_reminder_interval_seconds(
    os.getenv("MUNCHY_RUNNER_NOTIFY_REMINDER_INTERVAL")
)
NOTIFY_REMINDER_TIME = normalize_reminder_time(os.getenv("MUNCHY_RUNNER_NOTIFY_REMINDER_TIME"))
NOTIFY_REMINDER_TIMEZONE = (
    os.getenv("MUNCHY_RUNNER_NOTIFY_REMINDER_TIMEZONE", "UTC").strip() or "UTC"
)
reminder_zone(NOTIFY_REMINDER_TIMEZONE)
NOTIFY_TIMEOUT_SECONDS = float(os.getenv("MUNCHY_RUNNER_NOTIFY_TIMEOUT_SECONDS", "5"))
DEFAULT_NOTIFY_RECIPIENTS = env_list("MUNCHY_RUNNER_NOTIFY_DEFAULT_RECIPIENTS")
DEFAULT_NOTIFY_ENABLED = env_flag(
    "MUNCHY_RUNNER_NOTIFY_DEFAULT_ENABLED",
    "1" if NOTIFY_ENABLED and DEFAULT_NOTIFY_RECIPIENTS else "0",
)
ROUTING_MANIFEST_FILENAME = ".munchy-routing-manifest.json"
HANDOFF_RETRY_INITIAL_SECONDS = float(
    os.getenv("MUNCHY_RUNNER_HANDOFF_RETRY_INITIAL_SECONDS", "30")
)
HANDOFF_RETRY_MAX_SECONDS = float(os.getenv("MUNCHY_RUNNER_HANDOFF_RETRY_MAX_SECONDS", "3600"))
RESUME_ON_START = os.getenv("MUNCHY_RUNNER_RESUME_ON_START", "1").lower() in {
    "1",
    "true",
    "yes",
    "on",
}
LOCAL_CLEANUP_MIN_AGE_HOURS = float(os.getenv("MUNCHY_RUNNER_LOCAL_CLEANUP_MIN_AGE_HOURS", "24"))
INPUT_UPLOAD_TTL_HOURS = float(os.getenv("MUNCHY_RUNNER_INPUT_UPLOAD_TTL_HOURS", "168"))
ORPHAN_INPUT_UPLOAD_TTL_HOURS = float(
    os.getenv("MUNCHY_RUNNER_ORPHAN_INPUT_UPLOAD_TTL_HOURS", str(INPUT_UPLOAD_TTL_HOURS))
)
CLEANUP_INTERVAL_SECONDS = int(os.getenv("MUNCHY_RUNNER_CLEANUP_INTERVAL_SECONDS", "3600"))
EAGER_ARCHIVE_BATCH_FILES = max(1, int(os.getenv("MUNCHY_RUNNER_EAGER_ARCHIVE_BATCH_FILES", "32")))
EAGER_ARCHIVE_PIPELINE_BATCHES = max(
    1,
    int(os.getenv("MUNCHY_RUNNER_EAGER_ARCHIVE_PIPELINE_BATCHES", "3")),
)
EAGER_ARCHIVE_WAIT_SECONDS = max(
    1.0,
    float(os.getenv("MUNCHY_RUNNER_EAGER_ARCHIVE_WAIT_SECONDS", "5")),
)
STORAGE_WAIT_SECONDS = max(
    1.0,
    float(os.getenv("MUNCHY_RUNNER_STORAGE_WAIT_SECONDS", "60")),
)

UploadState = Literal["pending", "partial", "uploaded", "consumed"]
OutputMode = Literal["video", "audio", "preserve"]
WorkflowMode = Literal["collection_archive", "review"]
CollectionArchiveDestination = Literal["target", "riverhog"]
TaskName = Literal["archive_video", "archive_audio", "qcut_video", "audio_review"]
RiverhogUploadSessionFailureAction = Literal["preserve_for_resume", "cancel"]
DEFAULT_TASKS: tuple[TaskName, ...] = ("archive_video", "qcut_video", "audio_review")
DEFAULT_AUDIO_TASKS: tuple[TaskName, ...] = ("archive_audio",)
DEFAULT_REVIEW_CLIP_TARGET_SECONDS = 180
DEFAULT_REVIEW_CLIP_MIN_SECONDS = 6
DEFAULT_REVIEW_CLIP_MAX_SECONDS = 9
GPU_TARGET_TASKS = frozenset({"archive_video", "qcut_video", "audio_review"})
AUDIO_ARCHIVE_MAX_PARALLEL = max(1, int(os.getenv("MUNCHY_RUNNER_AUDIO_ARCHIVE_WORKERS", "2")))
ARCHIVE_AUDIO_BITRATE = os.getenv("MUNCHY_RUNNER_AUDIO_BITRATE", "128k")
NotifyEvent = Literal[
    "job.received",
    "review.handoff",
    "collection_archive.handoff",
    "archive.handoff",
    "job.issue",
    "job.upload_waiting.reminder",
    "job.succeeded",
]
DEFAULT_NOTIFY_EVENTS: list[NotifyEvent] = [
    "job.received",
    "review.handoff",
    "collection_archive.handoff",
    "archive.handoff",
    "job.issue",
    "job.upload_waiting.reminder",
    "job.succeeded",
]


def default_tasks() -> list[TaskName]:
    return list(DEFAULT_TASKS)


def tasks_require_gpu(tasks: Sequence[Any]) -> bool:
    return any(str(task) in GPU_TARGET_TASKS for task in tasks)


SAFE_GROUP_NAME_CHARS = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-")
TERMINAL_JOB_STATES = {"succeeded", "failed", "canceled"}
JOB_LIST_SORT_COLUMNS = {
    "job_id": "job_id",
    "collection_slug": "collection_slug",
    "created_at": "created_at",
    "finished_at": "finished_at",
    "input_upload_id": "input_upload_id",
    "phase": "phase",
    "state": "state",
    "updated_at": "updated_at",
    "workflow_mode": "workflow_mode",
}
JOB_LIST_TERMINAL_FILTERS = {"active", "all", "terminal"}
JOB_SEARCH_TOKEN_RE = re.compile(r"[0-9A-Za-z]+")
JOB_TEMPLATE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,159}$")
JOB_TEMPLATE_LIST_SORT_COLUMNS = {
    "created_at": "created_at",
    "name": "name",
    "revision": "revision",
    "updated_at": "updated_at",
}
cleanup_stop = threading.Event()
cleanup_thread: threading.Thread | None = None
riverhog_upload_stop = threading.Event()
riverhog_upload_thread: threading.Thread | None = None


def validate_group_name(value: str) -> str:
    name = str(value).strip()
    if not name or name in {".", ".."}:
        raise ValueError("group name must not be blank, '.', or '..'")
    if "/" in name or "\\" in name:
        raise ValueError("group name must be a single path segment")
    if any(ch not in SAFE_GROUP_NAME_CHARS for ch in name):
        raise ValueError(
            "group name may contain only letters, digits, dots, underscores, and dashes"
        )
    return name


def input_path_group(path: str) -> str:
    parts = path.split("/")
    if len(parts) < 2:
        raise ValueError("input file paths must be '<group>/<file>'")
    return validate_group_name(parts[0])


def normalize_posix(value: str) -> str:
    path = str(value).strip().lstrip("/")
    if not path or any(part in {"", ".", ".."} for part in path.split("/")):
        raise ValueError("path must be normalized and relative")
    return path


def normalize_output_mode(value: str | None) -> str:
    return str(value or "video")


class InsufficientStorage(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        label: str,
        required_bytes: int,
        free_bytes: int,
        reserved_bytes: int = 0,
    ) -> None:
        super().__init__(message)
        self.label = label
        self.required_bytes = required_bytes
        self.free_bytes = free_bytes
        self.reserved_bytes = reserved_bytes


class EncodingFailed(RuntimeError):
    pass


class RoutingFailed(RuntimeError):
    pass


class JobCanceled(RuntimeError):
    pass


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    global cleanup_thread, riverhog_upload_thread
    ensure_dirs()
    init_state_store()
    if RESUME_ON_START:
        schedule_pending_jobs()
    if CLEANUP_INTERVAL_SECONDS > 0:
        cleanup_stop.clear()
        cleanup_thread = threading.Thread(target=cleanup_loop, name="cleanup-loop", daemon=True)
        cleanup_thread.start()
    if RIVERHOG_UPLOAD_ENABLED:
        riverhog_upload_stop.clear()
        riverhog_upload_thread = threading.Thread(
            target=riverhog_upload_loop,
            name="riverhog-upload-loop",
            daemon=True,
        )
        riverhog_upload_thread.start()
    try:
        yield
    finally:
        riverhog_upload_stop.set()
        if riverhog_upload_thread is not None:
            riverhog_upload_thread.join(timeout=5)
        cleanup_stop.set()
        if cleanup_thread is not None:
            cleanup_thread.join(timeout=5)


app = FastAPI(title="munchy-runner", version="0.1.0", lifespan=lifespan)
state_lock = threading.RLock()
job_state_lock = threading.RLock()
active_jobs: set[str] = set()
scheduled_jobs: set[str] = set()
shared_input_tree_locks: dict[str, threading.Lock] = {}
shared_input_tree_locks_guard = threading.Lock()
riverhog_upload_locks: dict[str, threading.RLock] = {}
riverhog_upload_locks_guard = threading.Lock()
riverhog_upload_call_locks: dict[str, threading.Lock] = {}
riverhog_upload_call_locks_guard = threading.Lock()
input_file_upload_setup_locks: dict[tuple[str, str], threading.Lock] = {}
input_file_upload_setup_locks_guard = threading.Lock()


def authorized_api_bearer(request: Request) -> bool:
    if not API_TOKEN:
        return True
    raw = request.headers.get("authorization", "")
    scheme, _, token = raw.partition(" ")
    return scheme.casefold() == "bearer" and secrets.compare_digest(token, API_TOKEN)


def authorized_admin_bearer(request: Request) -> bool:
    if not ADMIN_TOKEN:
        return not API_TOKEN
    raw = request.headers.get("authorization", "")
    scheme, _, token = raw.partition(" ")
    return scheme.casefold() == "bearer" and secrets.compare_digest(token, ADMIN_TOKEN)


@app.middleware("http")
async def require_api_auth(request: Request, call_next):  # type: ignore[no-untyped-def]
    if request.url.path.startswith("/v1/admin/"):
        if not authorized_admin_bearer(request):
            return JSONResponse(
                {"detail": "invalid admin token"},
                status_code=401,
                headers={"WWW-Authenticate": "Bearer"},
            )
    elif request.url.path.startswith("/v1/") and not authorized_api_bearer(request):
        return JSONResponse(
            {"detail": "invalid api token"},
            status_code=401,
            headers={"WWW-Authenticate": "Bearer"},
        )
    return await call_next(request)


def shared_input_tree_lock(upload_id: str) -> threading.Lock:
    with shared_input_tree_locks_guard:
        lock = shared_input_tree_locks.get(upload_id)
        if lock is None:
            lock = threading.Lock()
            shared_input_tree_locks[upload_id] = lock
        return lock


def riverhog_upload_lock(job_id: str) -> threading.RLock:
    with riverhog_upload_locks_guard:
        lock = riverhog_upload_locks.get(job_id)
        if lock is None:
            lock = threading.RLock()
            riverhog_upload_locks[job_id] = lock
        return lock


def riverhog_upload_call_lock(job_id: str) -> threading.Lock:
    with riverhog_upload_call_locks_guard:
        lock = riverhog_upload_call_locks.get(job_id)
        if lock is None:
            lock = threading.Lock()
            riverhog_upload_call_locks[job_id] = lock
        return lock


def input_file_upload_setup_lock(upload_id: str, rel_path: str) -> threading.Lock:
    key = (upload_id, rel_path)
    with input_file_upload_setup_locks_guard:
        lock = input_file_upload_setup_locks.get(key)
        if lock is None:
            lock = threading.Lock()
            input_file_upload_setup_locks[key] = lock
        return lock


@app.exception_handler(InsufficientStorage)
async def insufficient_storage_handler(_request: Request, exc: InsufficientStorage) -> JSONResponse:
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


class InputFileSpec(BaseModel):
    path: str = Field(min_length=1, max_length=4096)
    bytes: int = Field(ge=0)
    sha256: str | None = Field(default=None, min_length=64, max_length=64)
    filesystem_metadata: dict[str, Any] | None = None

    @field_validator("path")
    @classmethod
    def normalize_path(cls, value: str) -> str:
        normalized = value.strip().lstrip("/")
        if not normalized or any(part in {"", ".", ".."} for part in normalized.split("/")):
            raise ValueError("path must be relative and normalized")
        return normalized

    @field_validator("sha256")
    @classmethod
    def validate_sha256(cls, value: str | None) -> str | None:
        if value is None:
            return None
        lowered = value.lower()
        if len(lowered) != 64 or any(ch not in "0123456789abcdef" for ch in lowered):
            raise ValueError("sha256 must be a 64-character hex digest")
        return lowered


class RiverhogConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    wait: Literal["staged", "finalized"] = "finalized"
    archive_store: str | None = None
    retain_hot: bool = True


class TargetUploadConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    method: Literal["command", "rclone"] = "command"
    destination: str | None = Field(default=None, min_length=1, max_length=4096)
    mode: Literal["copy", "sync"] = "copy"
    exclude: list[str] = Field(default_factory=list)

    @field_validator("destination")
    @classmethod
    def normalize_destination(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if not normalized:
            raise ValueError("destination must not be blank")
        return normalized

    @field_validator("exclude")
    @classmethod
    def normalize_exclude(cls, value: list[str]) -> list[str]:
        return normalize_exclude_patterns(value, label="target upload exclude")

    @model_validator(mode="after")
    def require_rclone_destination(self) -> TargetUploadConfig:
        if self.enabled and self.method == "rclone" and not self.destination:
            raise ValueError("target upload destination is required for rclone uploads")
        return self


class CollectionArchiveConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    destination: CollectionArchiveDestination = "riverhog"
    target: TargetUploadConfig = Field(default_factory=TargetUploadConfig)
    riverhog: RiverhogConfig = Field(default_factory=RiverhogConfig)


class ReviewClipPlanConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    target_seconds: int = Field(default=DEFAULT_REVIEW_CLIP_TARGET_SECONDS, ge=1)
    min_seconds: int = Field(default=DEFAULT_REVIEW_CLIP_MIN_SECONDS, ge=1)
    max_seconds: int = Field(default=DEFAULT_REVIEW_CLIP_MAX_SECONDS, ge=1)

    @model_validator(mode="after")
    def validate_bounds(self) -> ReviewClipPlanConfig:
        if self.min_seconds > self.max_seconds:
            raise ValueError("clip_plan.min_seconds must be <= max_seconds")
        return self


class ReviewSweepConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    quality: Any = None
    max_height: Any = None
    audio_bitrate: Any = None
    axes: dict[str, Any] | list[dict[str, Any]] | None = None
    variants: list[dict[str, Any]] = Field(default_factory=list)
    profile_id_template: str | None = Field(default=None, min_length=1, max_length=180)
    route_ids: list[str] = Field(default_factory=list)

    @field_validator("profile_id_template")
    @classmethod
    def validate_profile_id_template(cls, value: str | None) -> str | None:
        if value is None:
            return None
        text = value.strip()
        if not text:
            raise ValueError("review.sweep.profile_id_template must not be blank")
        return text

    @field_validator("route_ids")
    @classmethod
    def normalize_route_ids(cls, value: list[str]) -> list[str]:
        route_ids: list[str] = []
        for item in value:
            route_id = str(item).strip()
            if not route_id:
                raise ValueError("review.sweep.route_ids must not contain blanks")
            if route_id not in route_ids:
                route_ids.append(route_id)
        return route_ids

    @model_validator(mode="after")
    def validate_sweep(self) -> ReviewSweepConfig:
        try:
            ensure_review_sweep_has_variants(self.model_dump(exclude_none=True))
        except ValueError as exc:
            raise ValueError(str(exc)) from exc
        return self


class ReviewConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    device_id: str = Field(min_length=1, max_length=180)
    route_id: str | None = Field(default=None, min_length=1, max_length=180)
    profile_id: str | None = Field(default=None, min_length=1, max_length=180)
    target: TargetUploadConfig = Field(default_factory=TargetUploadConfig)
    clip_plan: ReviewClipPlanConfig | None = None
    sweep: ReviewSweepConfig | None = None

    @model_validator(mode="after")
    def validate_review_shape(self) -> ReviewConfig:
        if self.sweep is None:
            if not self.route_id or not self.profile_id:
                raise ValueError("review jobs require route_id and profile_id unless sweep is set")
            return self
        if self.route_id or self.profile_id:
            raise ValueError("review sweep jobs must not set top-level route_id or profile_id")
        return self


class NotifyConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = DEFAULT_NOTIFY_ENABLED
    recipients: list[str] = Field(default_factory=lambda: list(DEFAULT_NOTIFY_RECIPIENTS))
    events: list[NotifyEvent] = Field(default_factory=lambda: list(DEFAULT_NOTIFY_EVENTS))

    @field_validator("recipients")
    @classmethod
    def normalize_recipients(cls, value: list[str]) -> list[str]:
        allowed = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.-")
        recipients: list[str] = []
        for item in value:
            recipient = str(item).strip()
            if not recipient:
                raise ValueError("notify recipients must not be blank")
            if any(ch not in allowed for ch in recipient):
                raise ValueError(
                    "notify recipients may contain only letters, digits, dots, "
                    "underscores, and dashes"
                )
            if recipient not in recipients:
                recipients.append(recipient)
        return recipients

    @field_validator("events")
    @classmethod
    def normalize_events(cls, value: list[NotifyEvent]) -> list[NotifyEvent]:
        if not value:
            return list(DEFAULT_NOTIFY_EVENTS)
        return list(dict.fromkeys(value))

    @model_validator(mode="after")
    def require_recipients_when_enabled(self) -> NotifyConfig:
        if self.enabled and not self.recipients:
            raise ValueError("notify.recipients is required when notifications are enabled")
        return self


class ClientPreflightIssue(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str = Field(min_length=1, max_length=120)
    message: str = Field(min_length=1, max_length=1000)


class ClientPreflightFailedFile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str = Field(min_length=1, max_length=4096)
    source: str = Field(min_length=1, max_length=4096)
    issues: list[ClientPreflightIssue] = Field(default_factory=list)


class ClientPreflightFailedNotificationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: str = Field(default="client", min_length=1, max_length=120)
    message: str = Field(min_length=1, max_length=1000)
    device_id: str = Field(min_length=1, max_length=180)
    workflow_mode: WorkflowMode
    group: str = Field(min_length=1, max_length=180)
    collection_slug: str | None = Field(default=None, min_length=1, max_length=180)
    collection_timestamp: str | None = Field(default=None, min_length=1, max_length=64)
    run_id: str | None = Field(default=None, min_length=1, max_length=64)
    route_id: str | None = Field(default=None, min_length=1, max_length=180)
    profile_id: str | None = Field(default=None, min_length=1, max_length=180)
    input_upload_id: str | None = Field(default=None, min_length=1, max_length=180)
    job_id: str | None = Field(default=None, min_length=1, max_length=180)
    files: int = Field(ge=0)
    failed_file_count: int = Field(ge=1)
    failed_files: list[ClientPreflightFailedFile] = Field(default_factory=list)
    elapsed_seconds: float | None = Field(default=None, ge=0)
    notify: NotifyConfig = Field(default_factory=NotifyConfig)

    @field_validator("group")
    @classmethod
    def validate_group(cls, value: str) -> str:
        return validate_group_name(value)


class MetadataProjectionDeviceConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    make: str | None = None
    model: str | None = None

    @field_validator("make", "model")
    @classmethod
    def normalize_optional_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        text = value.strip()
        return text or None


class MetadataProjectionGpsConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    latitude: float
    longitude: float
    altitude: float | None = None

    @model_validator(mode="after")
    def validate_position(self) -> MetadataProjectionGpsConfig:
        if not (-90.0 <= self.latitude <= 90.0):
            raise ValueError("metadata_projection.gps.latitude must be between -90 and 90")
        if not (-180.0 <= self.longitude <= 180.0):
            raise ValueError("metadata_projection.gps.longitude must be between -180 and 180")
        return self


class MetadataProjectionConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    target: Literal["immich_xmp"] = "immich_xmp"
    allow_missing_capture_date: bool = False
    allow_missing_gps: bool = False
    allow_missing_device_make: bool = False
    allow_missing_device_model: bool = False
    allow_missing_creators: bool = False
    capture_date_sources: list[dict[str, Any]] | None = None
    gps_sources: list[dict[str, Any]] | None = None
    device: MetadataProjectionDeviceConfig = Field(default_factory=MetadataProjectionDeviceConfig)
    gps: MetadataProjectionGpsConfig | None = None
    creators: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    include_context_tags: bool = True

    @field_validator("creators", "tags")
    @classmethod
    def normalize_text_list(cls, value: list[str]) -> list[str]:
        return list(dict.fromkeys(item.strip() for item in value if item.strip()))

    @field_validator("capture_date_sources")
    @classmethod
    def validate_capture_date_sources(
        cls,
        value: list[dict[str, Any]] | None,
    ) -> list[dict[str, Any]] | None:
        return validate_metadata_sources(
            value,
            label="metadata_projection.capture_date_sources",
            allowed={"embedded", "path_regex", "filesystem_birthtime", "sidecar"},
        )

    @field_validator("gps_sources")
    @classmethod
    def validate_gps_sources(
        cls,
        value: list[dict[str, Any]] | None,
    ) -> list[dict[str, Any]] | None:
        return validate_metadata_sources(
            value,
            label="metadata_projection.gps_sources",
            allowed={"embedded", "sidecar"},
        )


MetadataProjectionSetting = MetadataProjectionConfig | Literal[False]


def validate_metadata_sources(
    value: list[dict[str, Any]] | None,
    *,
    label: str,
    allowed: set[str],
) -> list[dict[str, Any]] | None:
    if value is None:
        return None
    normalized: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, dict):
            raise ValueError(f"{label} entries must be tables")
        source_type = str(item.get("type") or "embedded").strip()
        if source_type not in allowed:
            raise ValueError(f"{label} type must be one of: {', '.join(sorted(allowed))}")
        normalized.append(dict(item))
    return normalized


class GroupConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    output_mode: OutputMode = "video"
    tasks: list[TaskName] = Field(default_factory=default_tasks)
    encode_profile: EncodeProfile | None = None
    max_parallel_encodes: int | None = Field(default=None, ge=1, le=64)
    eager_pipeline_batches: int | None = Field(default=None, ge=1, le=64)
    metadata_projection: MetadataProjectionSetting = Field(default_factory=MetadataProjectionConfig)

    @field_validator("tasks")
    @classmethod
    def normalize_tasks(cls, value: list[TaskName]) -> list[TaskName]:
        return list(dict.fromkeys(value))

    @model_validator(mode="after")
    def normalize_preserve(self) -> GroupConfig:
        if self.output_mode == "preserve":
            self.tasks = []
        elif self.output_mode == "audio" and "tasks" not in self.model_fields_set:
            self.tasks = list(DEFAULT_AUDIO_TASKS)
        if self.output_mode == "video" and "archive_audio" in self.tasks:
            raise ValueError("video groups cannot run archive_audio")
        if self.output_mode == "audio" and any(
            task in self.tasks for task in ("archive_video", "qcut_video")
        ):
            raise ValueError("audio groups cannot run archive_video or qcut_video")
        return self


class StorageGroupHint(BaseModel):
    model_config = ConfigDict(extra="forbid")

    output_mode: OutputMode = "video"
    tasks: list[TaskName] = Field(default_factory=list)
    eager_pipeline_batches: int | None = Field(default=None, ge=1, le=64)

    @field_validator("tasks")
    @classmethod
    def normalize_tasks(cls, value: list[TaskName]) -> list[TaskName]:
        return list(dict.fromkeys(value))

    @model_validator(mode="after")
    def normalize_preserve(self) -> StorageGroupHint:
        if self.output_mode == "preserve":
            self.tasks = []
        elif self.output_mode == "audio" and "tasks" not in self.model_fields_set:
            self.tasks = list(DEFAULT_AUDIO_TASKS)
        if self.output_mode == "video" and "archive_audio" in self.tasks:
            raise ValueError("video storage groups cannot run archive_audio")
        if self.output_mode == "audio" and any(
            task in self.tasks for task in ("archive_video", "qcut_video")
        ):
            raise ValueError("audio storage groups cannot run archive_video or qcut_video")
        return self


class InputUploadStorageHint(BaseModel):
    model_config = ConfigDict(extra="forbid")

    workflow_mode: WorkflowMode
    collection_archive_destination: CollectionArchiveDestination = "riverhog"
    output_mode: OutputMode = "video"
    tasks: list[TaskName] = Field(default_factory=list)
    groups: dict[str, StorageGroupHint] = Field(default_factory=dict)
    structured_routing: bool = False

    @field_validator("tasks")
    @classmethod
    def normalize_tasks(cls, value: list[TaskName]) -> list[TaskName]:
        return list(dict.fromkeys(value))

    @field_validator("groups")
    @classmethod
    def normalize_groups(
        cls,
        value: dict[str, StorageGroupHint],
    ) -> dict[str, StorageGroupHint]:
        return {validate_group_name(name): group for name, group in value.items()}

    @model_validator(mode="after")
    def normalize_preserve(self) -> InputUploadStorageHint:
        if self.output_mode == "preserve":
            self.tasks = []
        elif self.output_mode == "audio" and "tasks" not in self.model_fields_set:
            self.tasks = list(DEFAULT_AUDIO_TASKS)
        if self.output_mode == "video" and "archive_audio" in self.tasks:
            raise ValueError("video input upload hints cannot run archive_audio")
        if self.output_mode == "audio" and any(
            task in self.tasks for task in ("archive_video", "qcut_video")
        ):
            raise ValueError("audio input upload hints cannot run archive_video or qcut_video")
        return self


def validate_routing_predicate(value: Mapping[str, Any], *, label: str) -> None:
    unknown = sorted(set(value) - PREDICATE_KEYS)
    if unknown:
        raise ValueError(f"{label} has unknown key(s): {', '.join(unknown)}")
    path_predicate = value.get("path")
    if isinstance(path_predicate, Mapping):
        path_unknown = sorted(set(path_predicate) - PATH_PREDICATE_KEYS)
        if path_unknown:
            raise ValueError(f"{label}.path has unknown key(s): {', '.join(path_unknown)}")
    for key in ("all", "any"):
        items = value.get(key)
        if items is None:
            continue
        if not isinstance(items, list):
            raise ValueError(f"{label}.{key} must be a list")
        for index, item in enumerate(items):
            if not isinstance(item, Mapping):
                raise ValueError(f"{label}.{key}[{index}] must be a predicate")
            validate_routing_predicate(item, label=f"{label}.{key}[{index}]")
    not_item = value.get("not")
    if not_item is not None:
        if not isinstance(not_item, Mapping):
            raise ValueError(f"{label}.not must be a predicate")
        validate_routing_predicate(not_item, label=f"{label}.not")


class RoutingRule(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=120)
    action: Literal["upload", "leave"] = "upload"
    group: str | None = Field(default=None, min_length=1, max_length=120)
    into: str | None = Field(default=None, min_length=1, max_length=512)
    when: dict[str, Any] = Field(default_factory=dict)

    @field_validator("group")
    @classmethod
    def validate_group(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return validate_group_name(value)

    @field_validator("into")
    @classmethod
    def normalize_output_dir(cls, value: str | None) -> str | None:
        if value is None:
            return None
        text = normalize_posix(value.strip().rstrip("/"))
        if not text or any(part in {"", ".", ".."} for part in text.split("/")):
            raise ValueError("output directory must be normalized and relative")
        return text

    @field_validator("when")
    @classmethod
    def validate_when(cls, value: dict[str, Any]) -> dict[str, Any]:
        validate_routing_predicate(value, label="route.when")
        return value

    @model_validator(mode="after")
    def validate_route_semantics(self) -> RoutingRule:
        if self.action == "upload" and not self.group:
            raise ValueError("upload routes require group")
        if self.action == "leave" and self.group:
            raise ValueError("leave routes must not set group")
        if self.action == "leave" and self.into:
            raise ValueError("leave routes must not set into")
        return self


class RoutingConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    extra_exiftool_tags: list[str] | None = None
    gates: dict[str, dict[str, Any]] = Field(default_factory=dict)
    pairings: list[dict[str, Any]] = Field(default_factory=list)
    sidecars: list[dict[str, Any]] = Field(default_factory=list)
    routes: list[RoutingRule] = Field(default_factory=list)

    @field_validator("extra_exiftool_tags")
    @classmethod
    def normalize_extra_exiftool_tags(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
        normalized: list[str] = []
        seen: set[str] = set()
        for item in value:
            tag = normalize_exiftool_tag(item)
            if tag is None:
                raise ValueError("routing.extra_exiftool_tags must be non-empty tags")
            if tag in seen:
                continue
            seen.add(tag)
            normalized.append(tag)
        return normalized

    @field_validator("routes")
    @classmethod
    def require_routes(cls, value: list[RoutingRule]) -> list[RoutingRule]:
        if not value:
            raise ValueError("routing.routes must contain at least one route")
        ids = [route.id for route in value]
        if len(ids) != len(set(ids)):
            raise ValueError("routing route ids must be unique")
        return value

    @model_validator(mode="after")
    def validate_predicates(self) -> RoutingConfig:
        for name, gate in self.gates.items():
            validate_routing_predicate(gate, label=f"routing.gates.{name}")
        allowed_pairing_keys = {"id", "key", "prefer_same_stem", "still", "movie"}
        for index, pairing in enumerate(self.pairings):
            unknown = sorted(set(pairing) - allowed_pairing_keys)
            if unknown:
                raise ValueError(
                    f"routing.pairings[{index}] has unknown key(s): " + ", ".join(unknown)
                )
            if not str(pairing.get("id") or "").strip():
                raise ValueError(f"routing.pairings[{index}].id is required")
            for key in ("still", "movie"):
                predicate = pairing.get(key)
                if not isinstance(predicate, Mapping):
                    raise ValueError(f"routing.pairings[{index}].{key} must be a predicate")
                validate_routing_predicate(
                    predicate,
                    label=f"routing.pairings[{index}].{key}",
                )
        allowed_sidecar_keys = {
            "id",
            "facts",
            "format",
            "path",
            "paths",
            "primary",
            "sidecar",
        }
        for index, sidecar in enumerate(self.sidecars):
            unknown = sorted(set(sidecar) - allowed_sidecar_keys)
            if unknown:
                raise ValueError(
                    f"routing.sidecars[{index}] has unknown key(s): " + ", ".join(unknown)
                )
            if not str(sidecar.get("id") or "").strip():
                raise ValueError(f"routing.sidecars[{index}].id is required")
            if "path" in sidecar and "paths" in sidecar:
                raise ValueError(f"routing.sidecars[{index}] must use path or paths, not both")
            paths = sidecar.get("paths")
            if paths is not None and (
                not isinstance(paths, list)
                or not all(isinstance(item, str) and item.strip() for item in paths)
            ):
                raise ValueError(f"routing.sidecars[{index}].paths must be strings")
            path = sidecar.get("path")
            if path is not None and not (isinstance(path, str) and path.strip()):
                raise ValueError(f"routing.sidecars[{index}].path must be a string")
            facts = sidecar.get("facts")
            if facts is not None:
                if not isinstance(facts, Mapping):
                    raise ValueError(f"routing.sidecars[{index}].facts must be a table")
                unknown_fact_keys = sorted(set(facts) - {"source", "tags", "extractors"})
                if unknown_fact_keys:
                    raise ValueError(
                        f"routing.sidecars[{index}].facts has unknown key(s): "
                        + ", ".join(unknown_fact_keys)
                    )
                source = str(facts.get("source") or "exiftool").strip().casefold()
                if source != "exiftool":
                    raise ValueError(f"routing.sidecars[{index}].facts.source must be exiftool")
                tags = facts.get("tags")
                if (
                    not isinstance(tags, list)
                    or not tags
                    or not all(isinstance(item, str) and item.strip() for item in tags)
                ):
                    raise ValueError(
                        f"routing.sidecars[{index}].facts.tags must be non-empty strings"
                    )
                for tag in tags:
                    normalize_exiftool_tag(tag)
                extractors = facts.get("extractors")
                if extractors is not None:
                    if not isinstance(extractors, list):
                        raise ValueError(
                            f"routing.sidecars[{index}].facts.extractors must be a list"
                        )
                    sidecar_rule_fact_extractors(sidecar)
            for key in ("primary", "sidecar"):
                predicate = sidecar.get(key)
                if predicate is None:
                    continue
                if not isinstance(predicate, Mapping):
                    raise ValueError(f"routing.sidecars[{index}].{key} must be a predicate")
                validate_routing_predicate(
                    predicate,
                    label=f"routing.sidecars[{index}].{key}",
                )
        return self


class CreateInputUploadRequest(BaseModel):
    input_upload_id: str | None = Field(default=None, min_length=1, max_length=160)
    files: list[InputFileSpec]
    storage_hint: InputUploadStorageHint

    @field_validator("files")
    @classmethod
    def require_files(cls, value: list[InputFileSpec]) -> list[InputFileSpec]:
        if not value:
            raise ValueError("at least one file is required")
        paths = [item.path for item in value]
        if len(paths) != len(set(paths)):
            raise ValueError("file paths must be unique")
        return value

    @model_validator(mode="after")
    def validate_path_shape(self) -> CreateInputUploadRequest:
        if self.storage_hint.structured_routing:
            return self
        for item in self.files:
            input_path_group(item.path)
        return self


class CreateJobRequest(BaseModel):
    job_id: str | None = Field(default=None, min_length=1, max_length=180)
    input_upload_id: str | None = Field(default=None, min_length=1, max_length=180)
    run_id: str | None = Field(default=None, min_length=1, max_length=64)
    collection_slug: str | None = Field(default=None, min_length=1, max_length=180)
    collection_timestamp: str | None = Field(default=None, min_length=16, max_length=32)
    workflow_mode: WorkflowMode = "collection_archive"
    output_mode: OutputMode = "video"
    tasks: list[TaskName] = Field(default_factory=default_tasks)
    encode_profile: EncodeProfile | None = None
    groups: dict[str, GroupConfig] = Field(default_factory=dict)
    routing: RoutingConfig | None = None
    collection_archive: CollectionArchiveConfig = Field(default_factory=CollectionArchiveConfig)
    review: ReviewConfig | None = None
    notify: NotifyConfig = Field(default_factory=NotifyConfig)
    cleanup_local_on_success: bool = False
    riverhog_upload_session_on_failure: RiverhogUploadSessionFailureAction = "preserve_for_resume"

    @field_validator("tasks")
    @classmethod
    def normalize_tasks(cls, value: list[TaskName]) -> list[TaskName]:
        return list(dict.fromkeys(value))

    @field_validator("groups")
    @classmethod
    def normalize_groups(
        cls,
        value: dict[str, GroupConfig],
    ) -> dict[str, GroupConfig]:
        return {validate_group_name(name): group for name, group in value.items()}

    @model_validator(mode="after")
    def validate_workflow_mode(self) -> CreateJobRequest:
        if self.output_mode == "preserve":
            self.tasks = []
        elif self.output_mode == "audio" and "tasks" not in self.model_fields_set:
            self.tasks = list(DEFAULT_AUDIO_TASKS)
        if self.routing is not None:
            if not self.groups:
                raise ValueError("routing requires explicit groups")
            group_names = set(self.groups)
            route_groups = {
                route.group
                for route in self.routing.routes
                if route.action == "upload" and route.group
            }
            missing = sorted(route_groups - group_names)
            if missing:
                raise ValueError("routing references unknown group(s): " + ", ".join(missing))
        task_lists = (
            [(name, group.output_mode, group.tasks) for name, group in self.groups.items()]
            if self.groups
            else [("default", self.output_mode, self.tasks)]
        )
        for name, output_mode, tasks in task_lists:
            if output_mode == "video" and "archive_audio" in tasks:
                raise ValueError(f"video group {name!r} cannot run archive_audio")
            if output_mode == "audio" and any(
                task in tasks for task in ("archive_video", "qcut_video")
            ):
                raise ValueError(f"audio group {name!r} cannot run archive_video or qcut_video")
        if self.workflow_mode == "review":
            if self.review is None:
                raise ValueError("review jobs require review config")
            if not self.review.target.enabled:
                raise ValueError("review jobs require review.target.enabled")
            reviewable_group_found = False
            for name, output_mode, tasks in task_lists:
                if any(task in tasks for task in ("archive_video", "archive_audio")):
                    raise ValueError(
                        f"review group {name!r} cannot run archive_video or archive_audio"
                    )
                has_review_task = any(task in tasks for task in ("qcut_video", "audio_review"))
                if has_review_task:
                    reviewable_group_found = True
                if output_mode == "preserve":
                    continue
                if not has_review_task:
                    raise ValueError(f"review group {name!r} requires qcut_video or audio_review")
            if not reviewable_group_found:
                raise ValueError("review jobs require at least one reviewable group")
            if self.cleanup_local_on_success:
                raise ValueError("review jobs cannot cleanup local work on success")
            return self

        if not self.collection_slug:
            raise ValueError("collection_archive jobs require collection_slug")
        for name, output_mode, tasks in task_lists:
            if output_mode in {"video", "audio"} and not any(
                task in tasks for task in ("archive_video", "archive_audio")
            ):
                raise ValueError(
                    f"collection_archive group {name!r} requires archive_video or archive_audio"
                )
        if (
            self.collection_archive.destination == "target"
            and not self.collection_archive.target.enabled
        ):
            raise ValueError("collection_archive destination target requires target.enabled")
        if self.collection_archive.destination == "target" and self.cleanup_local_on_success:
            raise ValueError(
                "collection_archive target jobs cannot cleanup local archive work on success"
            )
        return self


class JobTemplateCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=160)
    definition: dict[str, Any]
    enabled: bool = True

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        name = value.strip()
        if not JOB_TEMPLATE_NAME_RE.fullmatch(name):
            raise ValueError(
                "name must start with an alphanumeric character and contain only "
                "letters, digits, dots, underscores, and dashes"
            )
        return name


class JobTemplateReplaceRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    definition: dict[str, Any]
    enabled: bool = True
    expected_revision: int = Field(ge=1)


class JobTemplateEnabledRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_revision: int = Field(ge=1)


class SubmissionSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    template: str = Field(min_length=1, max_length=160)
    inputs: dict[str, str] = Field(default_factory=dict)
    files: list[InputFileSpec]
    collection_slug: str | None = Field(default=None, min_length=1, max_length=180)
    collection_timestamp: str | None = Field(default=None, min_length=16, max_length=32)
    run_id: str | None = Field(default=None, min_length=1, max_length=64)
    riverhog_upload_session_on_failure: RiverhogUploadSessionFailureAction = (
        "preserve_for_resume"
    )

    @field_validator("template")
    @classmethod
    def validate_template(cls, value: str) -> str:
        name = value.strip()
        if not JOB_TEMPLATE_NAME_RE.fullmatch(name):
            raise ValueError("template is not a valid job-template name")
        return name

    @field_validator("files")
    @classmethod
    def require_files(cls, value: list[InputFileSpec]) -> list[InputFileSpec]:
        if not value:
            raise ValueError("at least one file is required")
        paths = [item.path for item in value]
        if len(paths) != len(set(paths)):
            raise ValueError("file paths must be unique")
        return value

    @field_validator("inputs")
    @classmethod
    def normalize_inputs(cls, value: dict[str, str]) -> dict[str, str]:
        normalized: dict[str, str] = {}
        for raw_name, raw_value in value.items():
            name = str(raw_name).strip()
            if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_.-]*", name):
                raise ValueError(f"invalid submission input name: {raw_name}")
            normalized[name] = str(raw_value).strip()
        return normalized


class CreateSubmissionRequest(SubmissionSpec):
    submission_id: str | None = Field(default=None, min_length=1, max_length=160)


def safe_parse_timestamp(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        return parse_utc_timestamp(str(value))
    except ValueError:
        return None


def state_db() -> sqlite3.Connection:
    STATE_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(STATE_DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def init_state_store() -> None:
    with closing(state_db()) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS states (
                kind TEXT NOT NULL,
                id TEXT NOT NULL,
                payload TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (kind, id)
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS states_kind_updated_at ON states(kind, updated_at)"
        )
        ensure_template_registry_schema(conn)
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS job_summaries (
                job_id TEXT PRIMARY KEY,
                state TEXT NOT NULL,
                phase TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                started_at TEXT NOT NULL,
                finished_at TEXT NOT NULL,
                input_upload_id TEXT NOT NULL,
                collection_slug TEXT NOT NULL,
                collection_timestamp TEXT NOT NULL,
                workflow_mode TEXT NOT NULL,
                collection_archive_destination TEXT NOT NULL,
                output_mode TEXT NOT NULL,
                profile TEXT NOT NULL,
                terminal INTEGER NOT NULL,
                cancel_requested INTEGER NOT NULL,
                storage_wait INTEGER NOT NULL
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS job_summaries_terminal_updated "
            "ON job_summaries(terminal, updated_at, job_id)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS job_summaries_state_updated "
            "ON job_summaries(state, updated_at, job_id)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS job_summaries_workflow_updated "
            "ON job_summaries(workflow_mode, updated_at, job_id)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS job_summaries_collection_archive_destination_updated "
            "ON job_summaries(collection_archive_destination, updated_at, job_id)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS job_summaries_collection_updated "
            "ON job_summaries(collection_slug, updated_at, job_id)"
        )
        conn.execute(
            "CREATE VIRTUAL TABLE IF NOT EXISTS job_summaries_fts "
            "USING fts5(job_id UNINDEXED, search_text)"
        )
        conn.commit()


def validated_job_template_definition(
    definition: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, Any], str]:
    try:
        normalized, defaults = normalize_job_template(definition)
        input_values = {
            name: str(spec.get("enum", ["template-validation"])[0])
            for name, spec in dict(normalized.get("inputs") or {}).items()
        }
        validation_payload = render_job_template_inputs(
            normalized,
            defaults,
            input_values,
        )
        validation_payload["input_upload_id"] = "template-validation"
        if (
            str(validation_payload.get("workflow_mode") or "collection_archive")
            == "collection_archive"
        ):
            validation_payload["collection_slug"] = "template-validation"
            validation_payload["collection_timestamp"] = "20260101T000000Z"
        CreateJobRequest.model_validate(validation_payload)
    except (JobTemplateError, ValidationError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return normalized, defaults, job_template_digest(normalized)


def job_template_row_payload(
    row: sqlite3.Row,
    *,
    include_definition: bool,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "name": str(row["name"]),
        "enabled": bool(row["enabled"]),
        "revision": int(row["revision"]),
        "digest": str(row["digest"]),
        "created_at": str(row["created_at"]),
        "updated_at": str(row["updated_at"]),
    }
    if include_definition:
        payload["definition"] = json.loads(str(row["definition"]))
        payload["resolved_job"] = json.loads(str(row["resolved_job"]))
    return payload


def load_job_template(name: str, *, require_enabled: bool = False) -> dict[str, Any]:
    with closing(state_db()) as conn:
        row = conn.execute(
            "SELECT * FROM job_templates WHERE name = ?",
            (name,),
        ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail=f"unknown job template: {name}")
    payload = job_template_row_payload(row, include_definition=True)
    if require_enabled and not payload["enabled"]:
        raise HTTPException(status_code=409, detail=f"job template is disabled: {name}")
    return payload


def create_job_template_record(req: JobTemplateCreateRequest) -> dict[str, Any]:
    definition, resolved_job, digest = validated_job_template_definition(req.definition)
    now = utc_timestamp_now()
    try:
        with closing(state_db()) as conn:
            conn.execute(
                """
                INSERT INTO job_templates(
                    name, definition, resolved_job, digest, revision, enabled,
                    created_at, updated_at
                ) VALUES(?, ?, ?, ?, 1, ?, ?, ?)
                """,
                (
                    req.name,
                    json.dumps(definition, sort_keys=True),
                    json.dumps(resolved_job, sort_keys=True),
                    digest,
                    bool_int(req.enabled),
                    now,
                    now,
                ),
            )
            conn.commit()
    except sqlite3.IntegrityError as exc:
        raise HTTPException(
            status_code=409,
            detail=f"job template already exists: {req.name}",
        ) from exc
    return load_job_template(req.name)


def replace_job_template_record(
    name: str,
    req: JobTemplateReplaceRequest,
) -> dict[str, Any]:
    definition, resolved_job, digest = validated_job_template_definition(req.definition)
    now = utc_timestamp_now()
    with closing(state_db()) as conn:
        changed = conn.execute(
            """
            UPDATE job_templates
            SET definition = ?, resolved_job = ?, digest = ?, revision = revision + 1,
                enabled = ?, updated_at = ?
            WHERE name = ? AND revision = ?
            """,
            (
                json.dumps(definition, sort_keys=True),
                json.dumps(resolved_job, sort_keys=True),
                digest,
                bool_int(req.enabled),
                now,
                name,
                req.expected_revision,
            ),
        ).rowcount
        if not changed:
            exists = conn.execute(
                "SELECT revision FROM job_templates WHERE name = ?",
                (name,),
            ).fetchone()
            if exists is None:
                raise HTTPException(status_code=404, detail=f"unknown job template: {name}")
            raise HTTPException(
                status_code=409,
                detail={
                    "error": "job_template_revision_conflict",
                    "name": name,
                    "expected_revision": req.expected_revision,
                    "current_revision": int(exists["revision"]),
                },
            )
        conn.commit()
    return load_job_template(name)


def set_job_template_enabled_record(
    name: str,
    *,
    enabled: bool,
    expected_revision: int,
) -> dict[str, Any]:
    now = utc_timestamp_now()
    with closing(state_db()) as conn:
        changed = conn.execute(
            """
            UPDATE job_templates
            SET enabled = ?, revision = revision + 1, updated_at = ?
            WHERE name = ? AND revision = ?
            """,
            (bool_int(enabled), now, name, expected_revision),
        ).rowcount
        if not changed:
            exists = conn.execute(
                "SELECT revision FROM job_templates WHERE name = ?",
                (name,),
            ).fetchone()
            if exists is None:
                raise HTTPException(status_code=404, detail=f"unknown job template: {name}")
            raise HTTPException(
                status_code=409,
                detail={
                    "error": "job_template_revision_conflict",
                    "name": name,
                    "expected_revision": expected_revision,
                    "current_revision": int(exists["revision"]),
                },
            )
        conn.commit()
    return load_job_template(name)


def delete_job_template_record(name: str, *, expected_revision: int) -> dict[str, Any]:
    with closing(state_db()) as conn:
        changed = conn.execute(
            "DELETE FROM job_templates WHERE name = ? AND revision = ?",
            (name, expected_revision),
        ).rowcount
        if not changed:
            exists = conn.execute(
                "SELECT revision FROM job_templates WHERE name = ?",
                (name,),
            ).fetchone()
            if exists is None:
                raise HTTPException(status_code=404, detail=f"unknown job template: {name}")
            raise HTTPException(
                status_code=409,
                detail={
                    "error": "job_template_revision_conflict",
                    "name": name,
                    "expected_revision": expected_revision,
                    "current_revision": int(exists["revision"]),
                },
            )
        conn.commit()
    return {"name": name, "state": "removed"}


def list_job_templates_page(
    *,
    page: int,
    per_page: int,
    sort: str,
    order: str,
    query: str | None,
    enabled: bool | None,
) -> dict[str, Any]:
    bounded_page = max(1, page)
    bounded_per_page = max(1, min(per_page, 500))
    normalized_sort = sort.casefold()
    if normalized_sort not in JOB_TEMPLATE_LIST_SORT_COLUMNS:
        raise HTTPException(
            status_code=400,
            detail="sort must be one of: " + ", ".join(sorted(JOB_TEMPLATE_LIST_SORT_COLUMNS)),
        )
    normalized_order = order.casefold()
    if normalized_order not in {"asc", "desc"}:
        raise HTTPException(status_code=400, detail="order must be asc or desc")
    where: list[str] = []
    params: list[Any] = []
    if query:
        escaped = query.casefold().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        where.append("lower(name) LIKE ? ESCAPE '\\'")
        params.append(f"%{escaped}%")
    if enabled is not None:
        where.append("enabled = ?")
        params.append(bool_int(enabled))
    where_sql = " WHERE " + " AND ".join(where) if where else ""
    sort_column = JOB_TEMPLATE_LIST_SORT_COLUMNS[normalized_sort]
    direction = normalized_order.upper()
    offset = (bounded_page - 1) * bounded_per_page
    with closing(state_db()) as conn:
        total = int(
            conn.execute(
                f"SELECT COUNT(*) AS total FROM job_templates{where_sql}",
                params,
            ).fetchone()["total"]
        )
        rows = conn.execute(
            f"""
            SELECT * FROM job_templates
            {where_sql}
            ORDER BY {sort_column} {direction}, name ASC
            LIMIT ? OFFSET ?
            """,
            [*params, bounded_per_page, offset],
        ).fetchall()
    return {
        "page": bounded_page,
        "pages": (total + bounded_per_page - 1) // bounded_per_page if total else 0,
        "per_page": bounded_per_page,
        "total": total,
        "sort": normalized_sort,
        "order": normalized_order,
        "query": query,
        "filters": {"enabled": enabled},
        "templates": [
            job_template_row_payload(row, include_definition=False) for row in rows
        ],
    }


def submission_request_digest(req: SubmissionSpec) -> str:
    payload = req.model_dump(mode="json")
    payload.pop("submission_id", None)
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def resolved_submission(
    req: SubmissionSpec,
    *,
    submission_id: str,
) -> tuple[dict[str, Any], CreateJobRequest, InputUploadStorageHint]:
    template = load_job_template(req.template, require_enabled=True)
    try:
        raw_job = render_job_template_inputs(
            dict(template["definition"]),
            dict(template["resolved_job"]),
            req.inputs,
        )
    except JobTemplateError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    workflow_mode = str(raw_job.get("workflow_mode") or "collection_archive")
    if workflow_mode == "collection_archive" and not req.collection_timestamp:
        raise HTTPException(
            status_code=422,
            detail="collection_timestamp is required for collection_archive submissions",
        )
    raw_job.update(
        {
            "job_id": submission_id,
            "input_upload_id": submission_id,
            "riverhog_upload_session_on_failure": req.riverhog_upload_session_on_failure,
        }
    )
    for key, value in (
        ("collection_slug", req.collection_slug),
        ("collection_timestamp", req.collection_timestamp),
        ("run_id", req.run_id),
    ):
        if value is not None:
            raw_job[key] = value
    try:
        job_request = CreateJobRequest.model_validate(raw_job)
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    storage_hint = storage_hint_for_job_request(job_request)
    try:
        CreateInputUploadRequest(files=req.files, storage_hint=storage_hint)
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return template, job_request, storage_hint


def submission_template_summary(job: Mapping[str, Any]) -> dict[str, Any]:
    raw = job.get("job_template")
    if not isinstance(raw, Mapping):
        return {}
    return {
        key: raw[key]
        for key in ("name", "revision", "digest")
        if key in raw
    }


def submission_response(job: dict[str, Any]) -> dict[str, Any]:
    submission_id = str(job.get("submission_id") or job.get("job_id") or "")
    upload: dict[str, Any] | None
    try:
        upload = load_input_upload(str(job["input_upload_id"]))
    except HTTPException as exc:
        if exc.status_code != 404:
            raise
        upload = None
    return {
        "submission_id": submission_id,
        "state": str(job.get("state") or "unknown"),
        "phase": str(job.get("phase") or ""),
        "template": submission_template_summary(job),
        "inputs": dict(job.get("submission_inputs") or {}),
        "upload": upload,
        "job": compact_job_response(job),
    }


def create_submission_state(
    req: CreateSubmissionRequest,
) -> tuple[dict[str, Any], bool]:
    submission_id = req.submission_id or uuid.uuid4().hex
    digest = submission_request_digest(req)
    existing = read_state("job", submission_id)
    if existing is not None:
        if existing.get("submission_request_digest") != digest:
            raise HTTPException(
                status_code=409,
                detail={
                    "error": "submission_conflict",
                    "submission_id": submission_id,
                },
            )
        return existing, False
    template, job_request, storage_hint = resolved_submission(
        req,
        submission_id=submission_id,
    )
    require_input_upload_capacity(req.files, storage_hint)
    upload_created = False
    try:
        upload = create_input_upload_state(
            input_upload_id=submission_id,
            files=req.files,
            storage_hint=storage_hint,
        )
        upload_created = True
        upload["submission_id"] = submission_id
        upload["submission_inputs"] = dict(req.inputs)
        upload["submission_request_digest"] = digest
        upload["job_template"] = {
            "name": template["name"],
            "revision": template["revision"],
            "digest": template["digest"],
        }
        save_input_upload_raw(upload)
        job = create_job_state_from_request(job_request)
        job["submission_id"] = submission_id
        job["submission_inputs"] = dict(req.inputs)
        job["submission_request_digest"] = digest
        job["job_template"] = {
            "name": template["name"],
            "revision": template["revision"],
            "digest": template["digest"],
        }
        job = save_job(job)
    except Exception:
        if upload_created:
            cleanup_upload = read_state("input-upload", submission_id)
            if cleanup_upload is not None:
                remove_input_upload_data(cleanup_upload)
            delete_state("input-upload", submission_id)
        raise
    return job, True


def write_state(kind: str, item_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    payload = dict(payload)
    payload["updated_at"] = utc_timestamp_now()
    encoded = json.dumps(payload, sort_keys=True)
    with closing(state_db()) as conn:
        conn.execute(
            """
            INSERT INTO states(kind, id, payload, updated_at)
            VALUES(?, ?, ?, ?)
            ON CONFLICT(kind, id) DO UPDATE SET
                payload = excluded.payload,
                updated_at = excluded.updated_at
            """,
            (kind, item_id, encoded, payload["updated_at"]),
        )
        if kind == "job":
            upsert_job_summary(conn, payload)
        conn.commit()
    return payload


def read_state(kind: str, item_id: str) -> dict[str, Any] | None:
    with closing(state_db()) as conn:
        row = conn.execute(
            "SELECT payload FROM states WHERE kind = ? AND id = ?",
            (kind, item_id),
        ).fetchone()
    if row is None:
        return None
    payload = json.loads(str(row["payload"]))
    if not isinstance(payload, dict):
        raise RuntimeError(f"{kind} state is not an object: {item_id}")
    return cast(dict[str, Any], payload)


def dict_or_empty(value: Any) -> dict[str, Any]:
    return cast(dict[str, Any], value) if isinstance(value, dict) else {}


def state_exists(kind: str, item_id: str) -> bool:
    with closing(state_db()) as conn:
        row = conn.execute(
            "SELECT 1 FROM states WHERE kind = ? AND id = ?",
            (kind, item_id),
        ).fetchone()
    return row is not None


def list_states(kind: str) -> list[dict[str, Any]]:
    with closing(state_db()) as conn:
        rows = conn.execute(
            "SELECT payload FROM states WHERE kind = ? ORDER BY id",
            (kind,),
        ).fetchall()
    return [json.loads(str(row["payload"])) for row in rows]


def bool_int(value: Any) -> int:
    return 1 if bool(value) else 0


def job_summary_search_text(job: dict[str, Any]) -> str:
    values = [
        job.get("job_id"),
        job.get("collection_slug"),
        job.get("collection_timestamp"),
        job.get("input_upload_id"),
        job.get("state"),
        job.get("phase"),
        job.get("workflow_mode"),
        dict_or_empty(job.get("collection_archive")).get("destination"),
        job.get("output_mode"),
        job.get("profile"),
    ]
    return " ".join(str(value) for value in values if value)


def upsert_job_summary(conn: sqlite3.Connection, job: dict[str, Any]) -> None:
    job_id = str(job.get("job_id") or "")
    if not job_id:
        return
    riverhog = job.get("riverhog")
    collection_archive = dict_or_empty(job.get("collection_archive"))
    collection_archive_destination = str(collection_archive.get("destination") or "")
    if (
        not collection_archive_destination
        and str(job.get("workflow_mode") or "") == "collection_archive"
    ):
        collection_archive_destination = (
            "riverhog" if isinstance(riverhog, dict) and riverhog.get("enabled") else "target"
        )
    summary = {
        "job_id": job_id,
        "state": str(job.get("state") or ""),
        "phase": str(job.get("phase") or ""),
        "created_at": str(job.get("created_at") or ""),
        "updated_at": str(job.get("updated_at") or job.get("created_at") or ""),
        "started_at": str(job.get("started_at") or ""),
        "finished_at": str(job.get("finished_at") or ""),
        "input_upload_id": str(job.get("input_upload_id") or ""),
        "collection_slug": str(job.get("collection_slug") or ""),
        "collection_timestamp": str(job.get("collection_timestamp") or ""),
        "workflow_mode": str(job.get("workflow_mode") or ""),
        "collection_archive_destination": collection_archive_destination,
        "output_mode": str(job.get("output_mode") or ""),
        "profile": str(job.get("profile") or ""),
        "terminal": bool_int(job.get("state") in TERMINAL_JOB_STATES),
        "cancel_requested": bool_int(job.get("cancel_requested")),
        "storage_wait": bool_int(isinstance(job.get("storage_wait"), dict)),
    }
    conn.execute(
        """
        INSERT INTO job_summaries(
            job_id,
            state,
            phase,
            created_at,
            updated_at,
            started_at,
            finished_at,
            input_upload_id,
            collection_slug,
            collection_timestamp,
            workflow_mode,
            collection_archive_destination,
            output_mode,
            profile,
            terminal,
            cancel_requested,
            storage_wait
        )
        VALUES(
            :job_id,
            :state,
            :phase,
            :created_at,
            :updated_at,
            :started_at,
            :finished_at,
            :input_upload_id,
            :collection_slug,
            :collection_timestamp,
            :workflow_mode,
            :collection_archive_destination,
            :output_mode,
            :profile,
            :terminal,
            :cancel_requested,
            :storage_wait
        )
        ON CONFLICT(job_id) DO UPDATE SET
            state = excluded.state,
            phase = excluded.phase,
            created_at = excluded.created_at,
            updated_at = excluded.updated_at,
            started_at = excluded.started_at,
            finished_at = excluded.finished_at,
            input_upload_id = excluded.input_upload_id,
            collection_slug = excluded.collection_slug,
            collection_timestamp = excluded.collection_timestamp,
            workflow_mode = excluded.workflow_mode,
            collection_archive_destination = excluded.collection_archive_destination,
            output_mode = excluded.output_mode,
            profile = excluded.profile,
            terminal = excluded.terminal,
            cancel_requested = excluded.cancel_requested,
            storage_wait = excluded.storage_wait
        """,
        summary,
    )
    conn.execute("DELETE FROM job_summaries_fts WHERE job_id = ?", (job_id,))
    conn.execute(
        "INSERT INTO job_summaries_fts(job_id, search_text) VALUES(?, ?)",
        (job_id, job_summary_search_text(job)),
    )


def delete_job_summary(conn: sqlite3.Connection, job_id: str) -> None:
    conn.execute("DELETE FROM job_summaries WHERE job_id = ?", (job_id,))
    conn.execute("DELETE FROM job_summaries_fts WHERE job_id = ?", (job_id,))


def job_search_match_query(value: str | None) -> str | None:
    if not value:
        return None
    tokens = JOB_SEARCH_TOKEN_RE.findall(value.casefold())
    if not tokens:
        return None
    return " AND ".join(f"{token}*" for token in tokens[:8])


def load_jobs_by_ids(job_ids: list[str]) -> list[dict[str, Any]]:
    if not job_ids:
        return []
    placeholders = ", ".join("?" for _ in job_ids)
    with closing(state_db()) as conn:
        rows = conn.execute(
            f"SELECT id, payload FROM states WHERE kind = 'job' AND id IN ({placeholders})",
            job_ids,
        ).fetchall()
    by_id = {str(row["id"]): json.loads(str(row["payload"])) for row in rows}
    return [by_id[job_id] for job_id in job_ids if isinstance(by_id.get(job_id), dict)]


def list_job_summaries_page(
    *,
    page: int,
    per_page: int,
    sort: str,
    order: str,
    query: str | None,
    terminal: str,
    state: str | None,
    workflow_mode: str | None,
    collection_archive_destination: str | None,
    cancel_requested: bool | None,
    storage_wait: bool | None,
) -> dict[str, Any]:
    bounded_page = max(1, page)
    bounded_per_page = max(1, min(per_page, 500))
    normalized_sort = sort.casefold()
    if normalized_sort not in JOB_LIST_SORT_COLUMNS:
        raise HTTPException(
            status_code=400,
            detail="sort must be one of: " + ", ".join(sorted(JOB_LIST_SORT_COLUMNS)),
        )
    normalized_order = order.casefold()
    if normalized_order not in {"asc", "desc"}:
        raise HTTPException(status_code=400, detail="order must be asc or desc")
    normalized_terminal = terminal.casefold().replace("-", "_")
    if normalized_terminal not in JOB_LIST_TERMINAL_FILTERS:
        raise HTTPException(
            status_code=400,
            detail="terminal must be active, terminal, or all",
        )

    where: list[str] = []
    params: list[Any] = []
    if normalized_terminal == "active":
        where.append("terminal = 0")
    elif normalized_terminal == "terminal":
        where.append("terminal = 1")
    if state:
        where.append("state = ?")
        params.append(state.strip().casefold())
    if workflow_mode:
        where.append("workflow_mode = ?")
        params.append(workflow_mode.strip().casefold().replace("-", "_"))
    if collection_archive_destination:
        where.append("collection_archive_destination = ?")
        params.append(collection_archive_destination.strip().casefold().replace("-", "_"))
    if cancel_requested is not None:
        where.append("cancel_requested = ?")
        params.append(bool_int(cancel_requested))
    if storage_wait is not None:
        where.append("storage_wait = ?")
        params.append(bool_int(storage_wait))
    search_query = job_search_match_query(query)
    if search_query:
        where.append(
            "job_id IN (SELECT job_id FROM job_summaries_fts WHERE job_summaries_fts MATCH ?)"
        )
        params.append(search_query)

    where_sql = " WHERE " + " AND ".join(where) if where else ""
    sort_column = JOB_LIST_SORT_COLUMNS[normalized_sort]
    direction = normalized_order.upper()
    if sort_column == "job_id":
        order_sql = f"job_id {direction}"
    else:
        order_sql = (
            f"CASE WHEN {sort_column} = '' THEN 1 ELSE 0 END ASC, "
            f"{sort_column} {direction}, job_id ASC"
        )
    offset = (bounded_page - 1) * bounded_per_page
    with closing(state_db()) as conn:
        total = int(
            conn.execute(
                f"SELECT COUNT(*) AS total FROM job_summaries{where_sql}",
                params,
            ).fetchone()["total"]
        )
        rows = conn.execute(
            f"""
            SELECT job_id
            FROM job_summaries
            {where_sql}
            ORDER BY {order_sql}
            LIMIT ? OFFSET ?
            """,
            [*params, bounded_per_page, offset],
        ).fetchall()
    job_ids = [str(row["job_id"]) for row in rows]
    jobs = [compact_job_response(job, include_queue=False) for job in load_jobs_by_ids(job_ids)]
    return {
        "page": bounded_page,
        "pages": (total + bounded_per_page - 1) // bounded_per_page if total else 0,
        "per_page": bounded_per_page,
        "total": total,
        "sort": normalized_sort,
        "order": normalized_order,
        "query": query,
        "terminal": normalized_terminal,
        "filters": {
            "state": state,
            "workflow_mode": workflow_mode,
            "collection_archive_destination": collection_archive_destination,
            "cancel_requested": cancel_requested,
            "storage_wait": storage_wait,
        },
        "jobs": jobs,
    }


def delete_state(kind: str, item_id: str) -> None:
    with closing(state_db()) as conn:
        conn.execute("DELETE FROM states WHERE kind = ? AND id = ?", (kind, item_id))
        if kind == "job":
            delete_job_summary(conn, item_id)
        conn.commit()


def vacuum_state_store() -> None:
    STATE_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(STATE_DB_PATH, timeout=30)) as conn:
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute("VACUUM")
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")


def ensure_dirs() -> None:
    for path in (STATE_DIR, WORK_DIR, TUSD_DIR, GPU_RUNTIME_DIR):
        path.mkdir(parents=True, exist_ok=True)


def tusd_upload_id_for_target_path(target_path: str) -> str:
    normalized = target_path.lstrip("/")
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
    return f".munchy-runner/uploads/by-target/{digest}"


def tusd_data_path(upload_id: str) -> Path:
    return TUSD_DIR / upload_id


def safe_local_id(value: str) -> str:
    cleaned = "".join(ch if ch in SAFE_GROUP_NAME_CHARS else "-" for ch in value).strip(".-_")
    digest = hashlib.sha256(value.encode()).hexdigest()[:12]
    prefix = (cleaned or "upload")[:96].strip(".-_") or "upload"
    return f"{prefix}-{digest}"


def write_job_debug_bundle(
    job: dict[str, Any],
    *,
    reason: str,
    error: Any | None = None,
) -> bool:
    if job.get("debug_bundle_dir"):
        return False
    job_id = str(job.get("job_id") or "unknown-job")
    created_at = utc_timestamp_now()
    bundle_dir = DEBUG_DIR / "jobs" / safe_local_id(job_id) / created_at.replace(":", "")
    bundle_dir.mkdir(parents=True, exist_ok=True)
    error_text = str(error or job.get("error") or "")
    metadata = {
        "job_id": job_id,
        "state": job.get("state"),
        "phase": job.get("phase"),
        "reason": reason,
        "error": error_text,
        "created_at": created_at,
    }
    (bundle_dir / "metadata.json").write_text(
        json.dumps(metadata, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    if error_text:
        (bundle_dir / "error.txt").write_text(error_text + "\n", encoding="utf-8")
    with gzip.open(bundle_dir / "job-state-full.json.gz", "wt", encoding="utf-8") as handle:
        json.dump(job, handle, sort_keys=True)
    for key in ("gpu_statuses", "gpu_payloads", "gpu_result", "gpu_results", "eager_archive"):
        value = job.get(key)
        if value is None:
            continue
        with gzip.open(bundle_dir / f"{key}.json.gz", "wt", encoding="utf-8") as handle:
            json.dump(value, handle, sort_keys=True)
    job["debug_bundle_dir"] = str(bundle_dir)
    job["debug_bundle_created_at"] = created_at
    job["debug_bundle_reason"] = reason
    return True


def shared_input_upload_root(upload_id: str) -> Path:
    return GPU_RUNTIME_DIR / "input-uploads" / safe_local_id(upload_id)


def shared_review_plan_path(upload_id: str, group_name: str, task_name: str) -> Path:
    validate_group_name(group_name)
    return shared_input_upload_root(upload_id) / f".munchy-{task_name}-{group_name}-plan.json"


def gpu_runtime_container_path(path: Path) -> str:
    rel = path.resolve().relative_to(GPU_RUNTIME_DIR)
    return f"/data/{rel.as_posix()}"


def target_path_for(upload_id: str, rel_path: str) -> str:
    return f".munchy-runner/uploads/{upload_id}/{rel_path}"


def upload_id_from_target_path(target_path: str) -> str | None:
    normalized = target_path.lstrip("/")
    prefix = ".munchy-runner/uploads/"
    if not normalized.startswith(prefix):
        return None
    rest = normalized.removeprefix(prefix)
    upload_id, sep, _rel_path = rest.partition("/")
    if not sep or not upload_id:
        return None
    return upload_id


def rel_path_from_target_path(target_path: str) -> str | None:
    normalized = target_path.lstrip("/")
    prefix = ".munchy-runner/uploads/"
    if not normalized.startswith(prefix):
        return None
    rest = normalized.removeprefix(prefix)
    _upload_id, sep, rel_path = rest.partition("/")
    if not sep or not rel_path:
        return None
    return rel_path


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def path_device(path: Path) -> int:
    path.mkdir(parents=True, exist_ok=True)
    return path.stat().st_dev


def free_bytes(path: Path) -> int:
    path.mkdir(parents=True, exist_ok=True)
    return shutil.disk_usage(path).free


def insufficient_storage(
    *,
    label: str,
    required_bytes: int,
    free: int,
    reserved_bytes: int = 0,
) -> InsufficientStorage:
    return InsufficientStorage(
        f"insufficient disk space for {label}: need {required_bytes} free bytes, have {free}",
        label=label,
        required_bytes=required_bytes,
        free_bytes=free,
        reserved_bytes=reserved_bytes,
    )


def require_free_space(path: Path, required_bytes: int, *, label: str) -> None:
    free = free_bytes(path)
    if free < required_bytes:
        raise insufficient_storage(label=label, required_bytes=required_bytes, free=free)


def notify_storage_waiting(job: dict[str, Any], exc: InsufficientStorage) -> dict[str, Any] | None:
    fingerprint = hashlib.sha256(f"storage:{exc.label}:{exc.required_bytes}".encode()).hexdigest()
    return notify_job_event(
        job,
        "job.issue",
        f"Waiting for storage: {exc.label}",
        severity="warning",
        extra={
            "component": "storage",
            "error": str(exc),
            "label": exc.label,
            "required_bytes": exc.required_bytes,
            "free_bytes": exc.free_bytes,
            "reserved_bytes": exc.reserved_bytes,
        },
        dedupe_key=f"job.issue:storage:{exc.label}",
        fingerprint=fingerprint,
    )


def wait_for_free_space(
    job: dict[str, Any],
    path: Path,
    required_bytes: int,
    *,
    label: str,
) -> None:
    job_id = str(job["job_id"])
    while True:
        raise_if_job_canceled(job_id)
        try:
            require_free_space(path, required_bytes, label=label)
            job.pop("storage_wait", None)
            return
        except InsufficientStorage as exc:
            job["phase"] = f"waiting_for_space:{label.replace(' ', '_')}"
            job["storage_wait"] = {
                "label": exc.label,
                "required_bytes": exc.required_bytes,
                "free_bytes": exc.free_bytes,
                "reserved_bytes": exc.reserved_bytes,
                "last_checked_at": utc_timestamp_now(),
                "retry_after_seconds": STORAGE_WAIT_SECONDS,
            }
            save_job(job)
            notify_storage_waiting(job, exc)
            log.warning("job %s waiting for storage: %s", job_id, exc)
            retry_sleep(STORAGE_WAIT_SECONDS, job_id=job_id)


def input_upload_states() -> list[dict[str, Any]]:
    return list_states("input-upload")


def job_states() -> list[dict[str, Any]]:
    return list_states("job")


def scheduler_control() -> dict[str, Any]:
    control = read_state("control", "scheduler")
    if control is None:
        return {"paused": False}
    return control


def scheduling_paused() -> bool:
    return bool(scheduler_control().get("paused"))


def set_scheduling_paused(paused: bool) -> dict[str, Any]:
    payload = scheduler_control()
    payload["paused"] = paused
    payload["changed_at"] = utc_timestamp_now()
    return write_state("control", "scheduler", payload)


def runnable_job(job: dict[str, Any]) -> bool:
    if job.get("state") not in {"queued", "running"}:
        return False
    if job.get("cancel_requested"):
        return False
    return True


def referenced_input_upload_ids() -> set[str]:
    upload_ids: set[str] = set()
    for job in job_states():
        try:
            if job.get("state") in TERMINAL_JOB_STATES:
                continue
            upload_id = job.get("input_upload_id")
        except Exception:
            log.exception("failed to read job state")
            continue
        if upload_id:
            upload_ids.add(str(upload_id))
    return upload_ids


def jobs_referencing_input_upload(
    upload_id: str,
    *,
    exclude_job_id: str | None = None,
) -> list[str]:
    return [
        str(job["job_id"])
        for job in job_states()
        if str(job.get("input_upload_id") or "") == upload_id
        and str(job.get("job_id") or "") != exclude_job_id
        and job.get("state") not in TERMINAL_JOB_STATES
    ]


def active_input_uploads() -> list[dict[str, Any]]:
    uploads: list[dict[str, Any]] = []
    for upload_state in input_upload_states():
        try:
            upload = refresh_input_upload(upload_state)
        except Exception:
            log.exception("failed to read input upload state")
            continue
        if upload.get("state") != "uploaded":
            uploads.append(upload)
    return uploads


def running_job_count() -> int:
    return len(active_jobs | scheduled_jobs)


def running_job_slots_available() -> int:
    if MAX_RUNNING_JOBS <= 0:
        return 1_000_000
    return max(0, MAX_RUNNING_JOBS - running_job_count())


def runnable_job_sort_key(job: dict[str, Any]) -> tuple[int, str, str]:
    state = str(job.get("state") or "")
    state_priority = 0 if state == "running" else 1
    queued_at = str(job.get("started_at") or job.get("created_at") or job.get("updated_at") or "")
    return (state_priority, queued_at, str(job.get("job_id") or ""))


def runnable_jobs_in_order() -> list[dict[str, Any]]:
    jobs = [job for job in job_states() if runnable_job(job)]
    jobs.sort(key=runnable_job_sort_key)
    return jobs


def queue_info_for_job(job_id: str) -> dict[str, Any] | None:
    ordered = [
        job for job in runnable_jobs_in_order() if str(job.get("job_id") or "") not in active_jobs
    ]
    for index, job in enumerate(ordered, start=1):
        if str(job.get("job_id") or "") != job_id:
            continue
        return {
            "position": index,
            "running_job_limit": MAX_RUNNING_JOBS,
            "running_jobs": len(active_jobs),
            "scheduled_jobs": len(scheduled_jobs),
        }
    return None


def input_upload_remaining_bytes(upload: dict[str, Any]) -> int:
    return max(0, int(upload.get("bytes_total", 0)) - int(upload.get("uploaded_bytes", 0)))


def storage_hint_group_configs(hint: InputUploadStorageHint) -> list[StorageGroupHint]:
    if hint.groups:
        return list(hint.groups.values())
    return [
        StorageGroupHint(
            output_mode=hint.output_mode,
            tasks=hint.tasks,
        )
    ]


def storage_hint_has_gpu_work(hint: InputUploadStorageHint) -> bool:
    return any(tasks_require_gpu(group.tasks) for group in storage_hint_group_configs(hint))


def storage_hint_scratch_extra_multiplier(hint: InputUploadStorageHint) -> float:
    if not storage_hint_has_gpu_work(hint):
        return 0.0
    if hint.workflow_mode == "review":
        return REVIEW_SCRATCH_EXTRA_MULTIPLIER
    if (
        hint.workflow_mode == "collection_archive"
        and hint.collection_archive_destination == "target"
    ):
        return COLLECTION_ARCHIVE_TARGET_SCRATCH_EXTRA_MULTIPLIER
    return GPU_SCRATCH_MULTIPLIER


def gpu_input_copy_multiplier() -> float:
    return 0.0 if path_device(TUSD_DIR) == path_device(GPU_RUNTIME_DIR) else 1.0


def gpu_scratch_required_bytes(total_bytes: int, hint: InputUploadStorageHint) -> int:
    multiplier = storage_hint_scratch_extra_multiplier(hint)
    if multiplier <= 0:
        return 0
    return int(total_bytes * (gpu_input_copy_multiplier() + multiplier))


def storage_group_hint_for_path(
    path: str,
    hint: InputUploadStorageHint,
) -> StorageGroupHint:
    if hint.structured_routing:
        return StorageGroupHint(
            output_mode=hint.output_mode,
            tasks=hint.tasks,
        )
    group_name = input_path_group(path)
    if hint.groups:
        group = hint.groups.get(group_name)
        if group is not None:
            return group
    return StorageGroupHint(
        output_mode=hint.output_mode,
        tasks=hint.tasks,
    )


def storage_group_hint_is_eager_archive_only(group: StorageGroupHint) -> bool:
    if normalize_output_mode(str(group.output_mode or "video")) != "video":
        return False
    return set(str(task) for task in group.tasks) == {"archive_video"}


def eager_archive_admission_bytes(files: list[InputFileSpec]) -> int:
    if not files:
        return 0
    concurrent_files = EAGER_ARCHIVE_BATCH_FILES * EAGER_ARCHIVE_PIPELINE_BATCHES
    if concurrent_files <= 0:
        return 0
    largest = sorted((int(item.bytes) for item in files), reverse=True)[:concurrent_files]
    return sum(largest)


def gpu_scratch_admission_required_bytes(
    files: list[InputFileSpec],
    hint: InputUploadStorageHint,
) -> int:
    multiplier = storage_hint_scratch_extra_multiplier(hint)
    if multiplier <= 0:
        return 0
    if (
        hint.workflow_mode != "collection_archive"
        or hint.collection_archive_destination != "riverhog"
        or hint.structured_routing
    ):
        return gpu_scratch_required_bytes(sum(item.bytes for item in files), hint)

    eager_files: list[InputFileSpec] = []
    non_eager_gpu_bytes = 0
    for item in files:
        group = storage_group_hint_for_path(item.path, hint)
        if not tasks_require_gpu(group.tasks):
            continue
        if storage_group_hint_is_eager_archive_only(group):
            eager_files.append(item)
        else:
            non_eager_gpu_bytes += int(item.bytes)

    eager_required = int(
        eager_archive_admission_bytes(eager_files)
        * (gpu_input_copy_multiplier() + EAGER_ARCHIVE_SCRATCH_MULTIPLIER)
    )
    non_eager_required = int(non_eager_gpu_bytes * (gpu_input_copy_multiplier() + multiplier))
    return eager_required + non_eager_required


def input_upload_storage_hint(upload: dict[str, Any]) -> InputUploadStorageHint:
    raw = upload.get("storage_hint")
    if not isinstance(raw, dict):
        raise RuntimeError(f"input upload {upload.get('input_upload_id')} is missing storage_hint")
    return InputUploadStorageHint.model_validate(raw)


def require_input_upload_capacity(
    files: list[InputFileSpec],
    storage_hint: InputUploadStorageHint,
) -> None:
    active_uploads = active_input_uploads()
    if MAX_ACTIVE_INPUT_UPLOADS > 0 and len(active_uploads) >= MAX_ACTIVE_INPUT_UPLOADS:
        raise HTTPException(
            status_code=429,
            detail={
                "error": "too_many_active_input_uploads",
                "active": len(active_uploads),
                "limit": MAX_ACTIVE_INPUT_UPLOADS,
            },
        )

    total_bytes = sum(item.bytes for item in files)
    reserved_spool_bytes = sum(input_upload_remaining_bytes(upload) for upload in active_uploads)
    spool_required = reserved_spool_bytes + total_bytes
    gpu_required = gpu_scratch_admission_required_bytes(files, storage_hint)

    requirements = [
        ("source upload spool", TUSD_DIR, spool_required, reserved_spool_bytes),
        ("future gpu scratch", GPU_RUNTIME_DIR, gpu_required, 0),
    ]
    by_device: dict[int, dict[str, Any]] = {}
    for label, path, required, reserved in requirements:
        device = path_device(path)
        entry = by_device.setdefault(
            device,
            {
                "path": path,
                "data_required": 0,
                "reserved": 0,
                "labels": [],
            },
        )
        entry["data_required"] += required
        entry["reserved"] += reserved
        entry["labels"].append(label)

    for entry in by_device.values():
        free = free_bytes(Path(entry["path"]))
        required = int(entry["data_required"]) + MIN_FREE_BYTES
        if free < required:
            raise insufficient_storage(
                label=", ".join(entry["labels"]),
                required_bytes=required,
                free=free,
                reserved_bytes=int(entry["reserved"]),
            )


def upload_file_resolved_group(file_state: dict[str, Any]) -> str:
    group = file_state.get("resolved_group")
    if isinstance(group, str) and group.strip():
        return validate_group_name(group)
    if file_state.get("structured_routing"):
        return ""
    try:
        return input_path_group(str(file_state["path"]))
    except ValueError:
        return ""


def upload_file_group_rel_for_state(file_state: dict[str, Any], group_name: str) -> Path:
    resolved = file_state.get("resolved_group_rel")
    if isinstance(resolved, str) and resolved.strip():
        if upload_file_resolved_group(file_state) != group_name:
            raise RuntimeError(
                f"input file {file_state.get('path')!r} is not in group {group_name!r}"
            )
        return Path(normalize_posix(resolved))
    return upload_file_group_rel(str(file_state["path"]), group_name)


def materialized_input_rel_path(file_state: dict[str, Any]) -> Path:
    group_name = upload_file_resolved_group(file_state)
    if not group_name:
        raise RuntimeError(f"input file has not been routed yet: {file_state.get('path')!r}")
    return Path(group_name) / upload_file_group_rel_for_state(file_state, group_name)


def shared_input_file_path(file_state: dict[str, Any]) -> Path | None:
    input_upload_id = str(file_state.get("input_upload_id") or "")
    rel_path = str(file_state.get("path") or "")
    if not input_upload_id or not rel_path:
        return None
    root = shared_input_upload_root(input_upload_id)
    original_path = root / rel_path
    group_name = upload_file_resolved_group(file_state)
    routed_path: Path | None = None
    if group_name:
        resolved = file_state.get("resolved_group_rel")
        if isinstance(resolved, str) and resolved.strip():
            routed_path = root / group_name / Path(normalize_posix(resolved))
        elif rel_path.startswith(f"{group_name}/"):
            routed_path = original_path
    if routed_path is not None and routed_path.exists():
        return routed_path
    if original_path.exists():
        return original_path
    return routed_path or original_path


def file_matches_size(path: Path, expected_bytes: int) -> bool:
    try:
        return path.stat().st_size >= expected_bytes
    except FileNotFoundError:
        return False


def upload_file_status(file_state: dict[str, Any]) -> dict[str, Any]:
    file_state = dict(file_state)
    upload_id = str(file_state["file_upload_id"])
    data_path = tusd_data_path(upload_id)
    expected = int(file_state["bytes"])
    if file_state.get("consumed_at"):
        uploaded = expected
        state: UploadState = "consumed"
    else:
        uploaded = data_path.stat().st_size if data_path.exists() else 0
        if uploaded >= expected:
            state = "uploaded"
        elif uploaded > 0:
            state = "partial"
        elif (shared_path := shared_input_file_path(file_state)) is not None and file_matches_size(
            shared_path,
            expected,
        ):
            uploaded = expected
            state = "uploaded"
        else:
            state = "pending"
    out = dict(file_state)
    out["uploaded_bytes"] = min(uploaded, expected)
    out["upload_state"] = state
    out["complete"] = state in {"uploaded", "consumed"}
    return out


def normalized_input_upload(upload: dict[str, Any]) -> dict[str, Any]:
    out = dict(upload)
    input_upload_id = str(out.get("input_upload_id") or "")
    files: list[dict[str, Any]] = []
    for file_state in out.get("files", []):
        if not isinstance(file_state, dict):
            continue
        item = dict(file_state)
        if input_upload_id:
            item.setdefault("input_upload_id", input_upload_id)
        files.append(item)
    out["files"] = files
    return out


def refresh_input_upload(upload: dict[str, Any]) -> dict[str, Any]:
    upload = normalized_input_upload(upload)
    files = [upload_file_status(file_state) for file_state in upload.get("files", [])]
    out = dict(upload)
    out["files"] = files
    out["files_total"] = len(files)
    out["files_uploaded"] = sum(1 for item in files if item["complete"])
    out["bytes_total"] = sum(int(item["bytes"]) for item in files)
    out["uploaded_bytes"] = sum(int(item["uploaded_bytes"]) for item in files)
    out["state"] = "uploaded" if out["files_uploaded"] == out["files_total"] else "uploading"
    return out


def save_input_upload(upload: dict[str, Any]) -> dict[str, Any]:
    upload = refresh_input_upload(upload)
    return write_state("input-upload", str(upload["input_upload_id"]), upload)


def load_input_upload_raw(upload_id: str) -> dict[str, Any]:
    upload = read_state("input-upload", upload_id)
    if upload is None:
        raise HTTPException(status_code=404, detail=f"unknown input upload: {upload_id}")
    return normalized_input_upload(upload)


def save_input_upload_raw(upload: dict[str, Any]) -> dict[str, Any]:
    upload = normalized_input_upload(upload)
    return write_state("input-upload", str(upload["input_upload_id"]), upload)


def remove_input_upload_data(upload: dict[str, Any]) -> None:
    upload = normalized_input_upload(upload)
    for file_state in upload.get("files", []):
        remove_input_file_data(file_state)
    shutil.rmtree(shared_input_upload_root(str(upload["input_upload_id"])), ignore_errors=True)


def remove_tusd_file_data(file_state: dict[str, Any]) -> None:
    tus_path = tusd_data_path(str(file_state["file_upload_id"]))
    tus_path.unlink(missing_ok=True)
    tus_path.with_suffix(tus_path.suffix + ".info").unlink(missing_ok=True)
    tus_path.with_suffix(tus_path.suffix + ".lock").unlink(missing_ok=True)


def remove_empty_parents(path: Path, root: Path) -> None:
    parent = path.parent
    while parent != root:
        try:
            parent.rmdir()
        except OSError:
            return
        parent = parent.parent
    try:
        root.rmdir()
    except OSError:
        pass


def remove_shared_input_file_data(file_state: dict[str, Any]) -> None:
    shared_path = shared_input_file_path(file_state)
    if shared_path is None:
        return
    root = shared_input_upload_root(str(file_state["input_upload_id"]))
    shared_path.unlink(missing_ok=True)
    remove_empty_parents(shared_path, root)


def remove_input_file_data(file_state: dict[str, Any]) -> None:
    remove_tusd_file_data(file_state)
    remove_shared_input_file_data(file_state)


def input_upload_last_activity(upload: dict[str, Any]) -> datetime:
    timestamps = [
        parsed
        for value in (upload.get("updated_at"), upload.get("created_at"))
        if (parsed := safe_parse_timestamp(value)) is not None
    ]
    for file_state in upload.get("files", []):
        tus_path = tusd_data_path(str(file_state["file_upload_id"]))
        for path in (
            tus_path,
            tus_path.with_suffix(tus_path.suffix + ".info"),
            tus_path.with_suffix(tus_path.suffix + ".lock"),
        ):
            if path.exists():
                timestamps.append(datetime.fromtimestamp(path.stat().st_mtime, UTC))
    return max(timestamps) if timestamps else datetime.now(UTC)


def input_upload_data_last_activity(upload: dict[str, Any]) -> datetime:
    timestamps = [
        parsed
        for value in (upload.get("created_at"),)
        if (parsed := safe_parse_timestamp(value)) is not None
    ]
    for file_state in upload.get("files", []):
        tus_path = tusd_data_path(str(file_state["file_upload_id"]))
        for path in (
            tus_path,
            tus_path.with_suffix(tus_path.suffix + ".info"),
            tus_path.with_suffix(tus_path.suffix + ".lock"),
        ):
            if path.exists():
                timestamps.append(datetime.fromtimestamp(path.stat().st_mtime, UTC))
    return max(timestamps) if timestamps else datetime.now(UTC)


def load_input_upload(upload_id: str) -> dict[str, Any]:
    with state_lock:
        return refresh_input_upload(load_input_upload_raw(upload_id))


def item_lifecycle_time(item: dict[str, Any]) -> datetime | None:
    values: list[datetime] = []
    for key in (
        "updated_at",
        "finished_at",
        "encoded_at",
        "failed_at",
        "last_polled_at",
        "last_submitted_at",
        "started_at",
    ):
        value = safe_parse_timestamp(item.get(key))
        if value is not None:
            values.append(value)
    return max(values) if values else None


def newer_lifecycle_item(
    current_item: dict[str, Any],
    payload_item: dict[str, Any],
    *,
    state_rank: dict[str, int],
) -> dict[str, Any]:
    current_rank = state_rank.get(str(current_item.get("state") or ""), 0)
    payload_rank = state_rank.get(str(payload_item.get("state") or ""), 0)
    if current_rank > payload_rank:
        return current_item
    if payload_rank > current_rank:
        return payload_item
    current_time = item_lifecycle_time(current_item)
    payload_time = item_lifecycle_time(payload_item)
    if current_time is not None and (payload_time is None or current_time > payload_time):
        return current_item
    return payload_item


def merge_eager_archive_state(
    current_eager: dict[str, Any],
    payload_eager: dict[str, Any],
) -> dict[str, Any]:
    merged = dict(payload_eager)

    current_files = dict_or_empty(current_eager.get("files"))
    payload_files = dict_or_empty(payload_eager.get("files"))
    file_state_rank = {"encoding": 1, "encoded": 2, "failed": 2}
    merged_files: dict[str, Any] = dict(payload_files)
    for rel_path, current_item in current_files.items():
        if not isinstance(current_item, dict):
            continue
        payload_item = merged_files.get(rel_path)
        if isinstance(payload_item, dict):
            merged_files[rel_path] = newer_lifecycle_item(
                current_item,
                payload_item,
                state_rank=file_state_rank,
            )
        else:
            merged_files[rel_path] = current_item
    merged["files"] = merged_files

    current_batches = dict_or_empty(current_eager.get("batches"))
    payload_batches = dict_or_empty(payload_eager.get("batches"))
    batch_state_rank = {"running": 1, "succeeded": 2, "failed": 2}
    merged_batches: dict[str, Any] = dict(payload_batches)
    for batch_id, current_item in current_batches.items():
        if not isinstance(current_item, dict):
            continue
        payload_item = merged_batches.get(batch_id)
        if isinstance(payload_item, dict):
            merged_batches[batch_id] = newer_lifecycle_item(
                current_item,
                payload_item,
                state_rank=batch_state_rank,
            )
        else:
            merged_batches[batch_id] = current_item
    merged["batches"] = merged_batches

    current_results = dict_or_empty(current_eager.get("gpu_results"))
    payload_results = dict_or_empty(payload_eager.get("gpu_results"))
    if current_results or payload_results:
        merged["gpu_results"] = {**current_results, **payload_results}

    merged["next_batch_number"] = max(
        int(current_eager.get("next_batch_number") or 1),
        int(payload_eager.get("next_batch_number") or 1),
    )
    return merged


RIVERHOG_FILE_STATE_RANK = {
    "": 0,
    "pending": 1,
    "registered": 2,
    "uploading": 3,
    "uploaded": 4,
    "deleted": 5,
}


def merge_riverhog_file_record(
    current_record: dict[str, Any],
    payload_record: dict[str, Any],
) -> dict[str, Any]:
    merged = {**current_record, **payload_record}
    for key in ("bytes", "uploaded_bytes"):
        merged[key] = max(int(current_record.get(key) or 0), int(payload_record.get(key) or 0))
    current_state = str(current_record.get("state") or "")
    payload_state = str(payload_record.get("state") or "")
    if RIVERHOG_FILE_STATE_RANK.get(current_state, 0) > RIVERHOG_FILE_STATE_RANK.get(
        payload_state,
        0,
    ):
        merged["state"] = current_state
    return merged


def merge_riverhog_files(
    current_files: Any,
    payload_files: Any,
) -> dict[str, Any]:
    current = current_files if isinstance(current_files, dict) else {}
    payload = payload_files if isinstance(payload_files, dict) else {}
    merged: dict[str, Any] = {}
    for rel_path in sorted(set(current) | set(payload)):
        current_record = current.get(rel_path)
        payload_record = payload.get(rel_path)
        if isinstance(current_record, dict) and isinstance(payload_record, dict):
            merged[str(rel_path)] = merge_riverhog_file_record(current_record, payload_record)
        elif isinstance(payload_record, dict):
            merged[str(rel_path)] = dict(payload_record)
        elif isinstance(current_record, dict):
            merged[str(rel_path)] = dict(current_record)
    return merged


def merge_last_eager_upload_metrics(
    merged: dict[str, Any],
    current_riverhog: dict[str, Any],
    payload_riverhog: dict[str, Any],
) -> None:
    current_at = safe_parse_timestamp(current_riverhog.get("last_eager_upload_at"))
    payload_at = safe_parse_timestamp(payload_riverhog.get("last_eager_upload_at"))
    if current_at is None and payload_at is None:
        return
    source = (
        payload_riverhog
        if current_at is None or (payload_at is not None and payload_at >= current_at)
        else current_riverhog
    )
    for key in (
        "last_eager_upload_at",
        "last_eager_upload_files",
        "last_eager_upload_bytes",
        "last_eager_upload_elapsed_seconds",
    ):
        if key in source:
            merged[key] = source[key]


def merge_riverhog_session_upload_state(
    current_riverhog: dict[str, Any],
    payload_riverhog: dict[str, Any],
) -> dict[str, Any]:
    current_updated = safe_parse_timestamp(current_riverhog.get("updated_at"))
    payload_updated = safe_parse_timestamp(payload_riverhog.get("updated_at"))
    if current_updated and (payload_updated is None or current_updated > payload_updated):
        merged = {**payload_riverhog, **current_riverhog}
    else:
        merged = {**current_riverhog, **payload_riverhog}
    merge_last_eager_upload_metrics(merged, current_riverhog, payload_riverhog)
    merged["files"] = merge_riverhog_files(
        current_riverhog.get("files"),
        payload_riverhog.get("files"),
    )
    return merged


GPU_RESULT_STORAGE_KEYS = {
    "job_id",
    "state",
    "profile",
    "tasks",
    "archive_dir",
    "review_dir",
    "started_at",
    "finished_at",
    "updated_at",
    "error",
    "error_code",
}


def compact_gpu_result_for_storage(value: Any) -> Any:
    if not isinstance(value, dict):
        return value
    compact = {key: value[key] for key in GPU_RESULT_STORAGE_KEYS if key in value}
    items = value.get("items")
    if isinstance(items, dict):
        item_counts: dict[str, int] = {}
        for task_name, task_items in items.items():
            if isinstance(task_items, list):
                item_counts[str(task_name)] = len(task_items)
            elif task_items is not None:
                item_counts[str(task_name)] = 1
        if item_counts:
            compact["item_counts"] = item_counts
    return compact


def compact_eager_archive_for_storage(eager: Any) -> None:
    if not isinstance(eager, dict):
        return
    batches = eager.get("batches")
    if isinstance(batches, dict):
        for batch in batches.values():
            if isinstance(batch, dict) and isinstance(batch.get("gpu_result"), dict):
                batch["gpu_result"] = compact_gpu_result_for_storage(batch["gpu_result"])
    gpu_results = eager.get("gpu_results")
    if isinstance(gpu_results, dict):
        for batch_id, result in list(gpu_results.items()):
            gpu_results[batch_id] = compact_gpu_result_for_storage(result)


def compact_job_for_storage(payload: dict[str, Any]) -> dict[str, Any]:
    compact_eager_archive_for_storage(payload.get("eager_archive"))
    return payload


TERMINAL_CLEANUP_JOB_KEYS = (
    "cleanup_completed_at",
    "cleanup_error",
    "cleanup_failed_at",
    "cleanup_removed",
    "cleanup_removed_count",
    "cleanup_removed_sample",
    "input_upload_deleted_at",
    "local_work_cleaned_at",
    "local_work_removed",
    "local_work_removed_count",
    "local_work_removed_sample",
    "riverhog_cancel_error",
    "riverhog_cancel_failed_at",
)


def save_job(job: dict[str, Any]) -> dict[str, Any]:
    payload = dict(job)
    allow_clear_cancel = bool(payload.pop("_allow_clear_cancel", False))
    reset_runtime_state = bool(payload.pop("_reset_runtime_state", False))
    job_id = str(payload["job_id"])
    with job_state_lock:
        current = read_state("job", job_id)
        if (
            not allow_clear_cancel
            and isinstance(current, dict)
            and current.get("state") in TERMINAL_JOB_STATES
            and payload.get("state") not in TERMINAL_JOB_STATES
        ):
            return current
        if (
            isinstance(current, dict)
            and payload.get("state") not in TERMINAL_JOB_STATES
            and not reset_runtime_state
        ):
            current_riverhog = current.get("riverhog_session_upload")
            payload_riverhog = payload.get("riverhog_session_upload")
            if isinstance(current_riverhog, dict) and isinstance(payload_riverhog, dict):
                payload["riverhog_session_upload"] = merge_riverhog_session_upload_state(
                    current_riverhog,
                    payload_riverhog,
                )
            elif isinstance(current_riverhog, dict) and "riverhog_session_upload" not in payload:
                payload["riverhog_session_upload"] = current_riverhog
            current_eager = current.get("eager_archive")
            payload_eager = payload.get("eager_archive")
            if isinstance(current_eager, dict) and isinstance(payload_eager, dict):
                payload["eager_archive"] = merge_eager_archive_state(current_eager, payload_eager)
            elif isinstance(current_eager, dict) and "eager_archive" not in payload:
                payload["eager_archive"] = current_eager
        if (
            not allow_clear_cancel
            and isinstance(current, dict)
            and current.get("cancel_requested")
            and not payload.get("cancel_requested")
            and payload.get("state") not in TERMINAL_JOB_STATES
        ):
            payload["cancel_requested"] = True
            payload["cancel_requested_at"] = (
                current.get("cancel_requested_at") or utc_timestamp_now()
            )
            if current.get("phase") == "cancel_requested":
                payload["phase"] = "cancel_requested"
        if (
            isinstance(current, dict)
            and current.get("state") in TERMINAL_JOB_STATES
            and payload.get("state") in TERMINAL_JOB_STATES
        ):
            for key in (
                "cleanup_completed_at",
                "cleanup_removed",
                "cleanup_removed_count",
                "cleanup_removed_sample",
                "cleanup_failed_at",
                "cleanup_error",
                "input_upload_deleted_at",
                "local_work_cleaned_at",
                "local_work_removed",
                "local_work_removed_count",
                "local_work_removed_sample",
                "riverhog_cancel_failed_at",
                "riverhog_cancel_error",
                "riverhog_handoff_metrics",
                "terminal_state_compacted_at",
                "debug_bundle_dir",
                "debug_bundle_created_at",
                "debug_bundle_reason",
            ):
                if key in current and key not in payload:
                    payload[key] = current[key]
        if payload.get("state") not in TERMINAL_JOB_STATES:
            for key in TERMINAL_CLEANUP_JOB_KEYS:
                payload.pop(key, None)
        payload = compact_job_for_storage(payload)
        return write_state("job", job_id, payload)


def load_job(job_id: str) -> dict[str, Any]:
    job = read_state("job", job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"unknown job: {job_id}")
    return job


def raise_if_job_canceled(job_id: str) -> None:
    job = read_state("job", job_id)
    if job is None:
        raise RuntimeError(f"unknown job: {job_id}")
    if job.get("cancel_requested") or job.get("state") == "canceled":
        raise JobCanceled(f"job canceled: {job_id}")


def normalize_public_tusd_url(location: str) -> str:
    joined = urljoin(f"{TUSD_PUBLIC_BASE_URL}/", location)
    parsed = urlsplit(joined)
    public = urlsplit(TUSD_PUBLIC_BASE_URL)
    base_path = public.path.rstrip("/")
    prefix = f"{base_path}/"
    if not parsed.path.startswith(prefix):
        return joined
    upload_id = parsed.path.removeprefix(prefix)
    normalized_path = f"{prefix}{quote(upload_id, safe='+%')}"
    return urlunsplit(
        (
            public.scheme,
            public.netloc,
            normalized_path,
            parsed.query,
            parsed.fragment,
        )
    )


def tusd_upload_expires_at() -> str:
    expires_at = utc_now() + timedelta(hours=INPUT_UPLOAD_TTL_HOURS)
    return format_utc_timestamp(expires_at)


def upload_expires_epoch(expires_at: str) -> int | None:
    try:
        parsed = parse_utc_timestamp(expires_at)
    except ValueError:
        return None
    return int(parsed.timestamp())


def signed_tusd_query(path: str, *, expires_at: str, secret: str) -> dict[str, str]:
    expires = upload_expires_epoch(expires_at)
    if expires is None:
        return {}
    normalized_uri = unquote(path)
    digest = hashlib.md5(f"{expires}{normalized_uri} {secret}".encode()).digest()
    token = base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
    return {"md5": token, "expires": str(expires)}


def public_tusd_upload_url(upload_url: str) -> str:
    if not TUSD_PUBLIC_SIGNING_SECRET:
        return upload_url
    parsed = urlsplit(upload_url)
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    query.update(
        signed_tusd_query(
            parsed.path,
            expires_at=tusd_upload_expires_at(),
            secret=TUSD_PUBLIC_SIGNING_SECRET,
        )
    )
    return urlunsplit(
        (parsed.scheme, parsed.netloc, parsed.path, urlencode(query), parsed.fragment)
    )


def tus_headers(**headers: str) -> dict[str, str]:
    out = {"Tus-Resumable": "1.0.0", **headers}
    if TUSD_HOOK_SECRET:
        out["X-Munchy-Runner-Tusd-Hook-Secret"] = TUSD_HOOK_SECRET
    return out


def create_tusd_upload(target_path: str, length: int) -> str:
    encoded = base64.b64encode(target_path.encode("utf-8")).decode("ascii")
    metadata = f"target_path {encoded}"
    with httpx.Client(timeout=300.0) as client:
        response = client.post(
            TUSD_INTERNAL_BASE_URL,
            headers=tus_headers(**{"Upload-Length": str(length), "Upload-Metadata": metadata}),
        )
        response.raise_for_status()
        return normalize_public_tusd_url(response.headers["Location"])


def head_tusd_upload(upload_url: str) -> int:
    internal = upload_url.replace(TUSD_PUBLIC_BASE_URL, TUSD_INTERNAL_BASE_URL, 1)
    with httpx.Client(timeout=60.0) as client:
        response = client.head(internal, headers=tus_headers())
    if response.status_code == 404:
        return -1
    response.raise_for_status()
    return int(response.headers.get("Upload-Offset", "0"))


def find_upload_file(upload: dict[str, Any], rel_path: str) -> dict[str, Any]:
    files = upload.get("files")
    if not isinstance(files, list):
        files = []
    for file_state in files:
        if not isinstance(file_state, dict):
            continue
        if file_state.get("path") == rel_path:
            return cast(dict[str, Any], file_state)
    raise HTTPException(status_code=404, detail=f"unknown upload file: {rel_path}")


LINK_COPY_FALLBACK_ERRNOS = {
    errno.EXDEV,
    errno.EPERM,
    errno.EACCES,
}
if hasattr(errno, "ENOTSUP"):
    LINK_COPY_FALLBACK_ERRNOS.add(errno.ENOTSUP)
if hasattr(errno, "EOPNOTSUPP"):
    LINK_COPY_FALLBACK_ERRNOS.add(errno.EOPNOTSUPP)


def link_or_copy(source: Path, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(f".{dest.name}.{uuid.uuid4().hex}.part")
    try:
        try:
            os.link(source, part)
        except OSError as exc:
            if exc.errno not in LINK_COPY_FALLBACK_ERRNOS:
                raise RuntimeError(f"failed to link {source} to {dest}: {exc}") from exc
            try:
                shutil.copy2(source, part)
            except OSError as copy_exc:
                raise RuntimeError(f"failed to copy {source} to {dest}: {copy_exc}") from copy_exc
        part.replace(dest)
    finally:
        part.unlink(missing_ok=True)


def file_matches_expected(
    path: Path,
    expected_bytes: int,
    *,
    expected_sha256: str | None = None,
    verify_sha256: bool = False,
) -> bool:
    try:
        if path.stat().st_size != expected_bytes:
            return False
        return not verify_sha256 or not expected_sha256 or file_sha256(path) == expected_sha256
    except OSError:
        return False


def copy_tree_files(source_root: Path, dest_root: Path) -> None:
    if not source_root.is_dir():
        raise RuntimeError(f"input group is missing: {source_root}")
    for source in source_root.rglob("*"):
        if not source.is_file():
            continue
        rel_path = source.relative_to(source_root)
        dest = dest_root / rel_path
        if dest.exists() and dest.stat().st_size == source.stat().st_size:
            continue
        link_or_copy(source, dest)


def copy_preserve_group_files(
    upload: dict[str, Any],
    *,
    group_name: str,
    source_root: Path,
    dest_root: Path,
) -> None:
    if not source_root.is_dir():
        raise RuntimeError(f"input group is missing: {source_root}")
    for file_state in primary_upload_files_for_groups(upload, {group_name}):
        rel_path = upload_file_group_rel_for_state(file_state, group_name)
        source = source_root / rel_path
        dest = dest_root / rel_path
        if not source.is_file():
            raise RuntimeError(f"preserve source file is missing: {source}")
        if dest.exists() and dest.stat().st_size == source.stat().st_size:
            continue
        link_or_copy(source, dest)


def build_preserve_group_source_artifacts(
    upload: dict[str, Any],
    *,
    group_name: str,
    source_root: Path,
    output_root: Path,
) -> dict[str, Any]:
    filesystem_metadata = load_filesystem_metadata_map(source_root)
    if not filesystem_metadata:
        raise RuntimeError(
            "unresumable: source filesystem metadata sidecar is missing for preserve group"
        )
    items: list[dict[str, Any]] = []
    for file_state in primary_upload_files_for_groups(upload, {group_name}):
        rel_path = upload_file_group_rel_for_state(file_state, group_name)
        source = source_root / rel_path
        output = output_root / rel_path
        metadata = filesystem_metadata.get(rel_path.as_posix())
        if not isinstance(metadata, Mapping):
            raise RuntimeError(
                "unresumable: source filesystem metadata sidecar is missing entries for "
                f"{rel_path.as_posix()}"
            )
        artifacts = build_preserve_source_artifacts(
            source=source,
            output=output,
            source_filesystem_metadata=metadata,
            source_sidecars=source_artifacts_sidecar_entries(
                upload,
                [file_state],
                group_name=group_name,
                materialized_group_root=source_root,
            ).get(rel_path.as_posix(), []),
        )
        file_state["source_artifacts"] = artifacts
        items.append(
            {
                "source": str(source),
                "output": str(output),
                "source_artifacts": artifacts,
            }
        )
    return {"status": "succeeded", "items": items, "count": len(items)}


def upload_file_group(rel_path: str) -> str:
    return input_path_group(rel_path)


def upload_file_group_rel(rel_path: str, group_name: str) -> Path:
    prefix = f"{group_name}/"
    if not rel_path.startswith(prefix):
        raise RuntimeError(f"input file {rel_path!r} is not in group {group_name!r}")
    group_rel = rel_path[len(prefix) :]
    if not group_rel:
        raise RuntimeError(f"input file {rel_path!r} does not include a file name")
    return Path(group_rel)


def upload_files_for_groups(
    upload: dict[str, Any],
    group_names: set[str],
) -> list[dict[str, Any]]:
    upload = normalized_input_upload(upload)
    return [
        file_state
        for file_state in upload.get("files", [])
        if upload_file_resolved_group(file_state) in group_names
    ]


def mutable_upload_files_for_groups(
    upload: dict[str, Any],
    group_names: set[str],
) -> list[dict[str, Any]]:
    return [
        file_state
        for file_state in upload.get("files", [])
        if isinstance(file_state, dict) and upload_file_resolved_group(file_state) in group_names
    ]


def upload_file_is_sidecar_evidence(file_state: Mapping[str, Any]) -> bool:
    return str(file_state.get("route_action") or "") == "evidence"


def primary_upload_files_for_groups(
    upload: dict[str, Any],
    group_names: set[str],
) -> list[dict[str, Any]]:
    return [
        file_state
        for file_state in upload_files_for_groups(upload, group_names)
        if not upload_file_is_sidecar_evidence(file_state)
    ]


def mutable_primary_upload_files_for_groups(
    upload: dict[str, Any],
    group_names: set[str],
) -> list[dict[str, Any]]:
    return [
        file_state
        for file_state in mutable_upload_files_for_groups(upload, group_names)
        if not upload_file_is_sidecar_evidence(file_state)
    ]


def sidecar_evidence_files_for_primary(
    upload: dict[str, Any],
    primary_file_state: Mapping[str, Any],
) -> list[dict[str, Any]]:
    primary_path = str(primary_file_state.get("path") or "")
    evidence = [
        file_state
        for file_state in upload.get("files", [])
        if isinstance(file_state, dict)
        and upload_file_is_sidecar_evidence(file_state)
        and str(file_state.get("sidecar_for") or "") == primary_path
    ]
    return sorted(evidence, key=lambda item: str(item.get("path") or ""))


def sidecar_evidence_files_for_primaries(
    upload: dict[str, Any],
    primary_file_states: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    by_path: dict[str, dict[str, Any]] = {}
    for primary in primary_file_states:
        for evidence in sidecar_evidence_files_for_primary(upload, primary):
            by_path[str(evidence["path"])] = evidence
    return [by_path[path] for path in sorted(by_path)]


def source_artifacts_sidecar_entries(
    upload: dict[str, Any],
    primary_file_states: Sequence[Mapping[str, Any]],
    *,
    group_name: str,
    materialized_group_root: Path,
    container_group_root: str | Path | None = None,
) -> dict[str, list[dict[str, Any]]]:
    out: dict[str, list[dict[str, Any]]] = {}
    root_for_payload = (
        Path(container_group_root) if container_group_root else materialized_group_root
    )
    for primary in primary_file_states:
        primary_rel = upload_file_group_rel_for_state(cast(dict[str, Any], primary), group_name)
        entries: list[dict[str, Any]] = []
        for evidence in sidecar_evidence_files_for_primary(upload, primary):
            evidence_rel = upload_file_group_rel_for_state(evidence, group_name)
            evidence_path = materialized_group_root / evidence_rel
            if not evidence_path.is_file():
                raise RuntimeError(f"source sidecar evidence is missing: {evidence_path}")
            entries.append(
                {
                    "id": str(evidence.get("sidecar_id") or ""),
                    "format": str(evidence.get("sidecar_format") or "opaque"),
                    "path": str(root_for_payload / evidence_rel),
                    "arcname": normalize_posix(
                        PurePosixPath("sidecars", evidence_rel.as_posix()).as_posix()
                    ),
                    "source_rel_path": str(evidence.get("path") or ""),
                }
            )
        if entries:
            out[primary_rel.as_posix()] = entries
    return out


def upload_bytes_for_groups(upload: dict[str, Any], group_names: set[str]) -> int:
    return sum(
        int(file_state["bytes"]) for file_state in upload_files_for_groups(upload, group_names)
    )


def upload_group_names_with_files(upload: dict[str, Any], group_names: set[str]) -> set[str]:
    present_groups = input_upload_routed_groups(upload)
    return {str(group_name) for group_name in group_names if str(group_name) in present_groups}


def upload_groups_complete(upload: dict[str, Any], group_names: set[str]) -> bool:
    files = upload_files_for_groups(upload, group_names)
    return bool(files) and all(upload_file_status(file_state)["complete"] for file_state in files)


def shared_input_tree_progress(upload: dict[str, Any], group_names: set[str]) -> dict[str, int]:
    root = shared_input_upload_root(str(upload["input_upload_id"]))
    files_ready = 0
    bytes_ready = 0
    for file_state in upload_files_for_groups(upload, group_names):
        expected_bytes = int(file_state["bytes"])
        if file_state.get("consumed_at"):
            files_ready += 1
            bytes_ready += expected_bytes
            continue
        dest = root / materialized_input_rel_path(file_state)
        if file_matches_expected(dest, expected_bytes):
            files_ready += 1
            bytes_ready += expected_bytes
    return {
        "input_tree_files_ready": files_ready,
        "input_tree_bytes_ready": bytes_ready,
    }


def upload_group_progress(upload: dict[str, Any], group_names: set[str]) -> dict[str, Any]:
    files = [
        upload_file_status(file_state)
        for file_state in upload_files_for_groups(upload, group_names)
    ]
    bytes_total = sum(int(item["bytes"]) for item in files)
    uploaded_bytes = sum(int(item["uploaded_bytes"]) for item in files)
    tree_progress = shared_input_tree_progress(upload, group_names)
    return {
        "files_total": len(files),
        "files_uploaded": sum(1 for item in files if item["complete"]),
        "bytes_total": bytes_total,
        "uploaded_bytes": uploaded_bytes,
        **tree_progress,
    }


def cleanup_consumed_shared_input_files(
    upload: dict[str, Any],
    group_names: set[str] | None = None,
) -> int:
    upload = normalized_input_upload(upload)
    selected_groups = set(group_names or input_upload_groups(upload))
    removed = 0
    for file_state in upload_files_for_groups(upload, selected_groups):
        if not file_state.get("consumed_at"):
            continue
        shared_path = shared_input_file_path(file_state)
        if shared_path is None or not shared_path.exists():
            continue
        remove_shared_input_file_data(file_state)
        removed += 1
    return removed


def wait_for_upload_groups(
    job: dict[str, Any],
    upload_id: str,
    group_names: set[str],
    groups: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    job_id = str(job["job_id"])
    requested_group_names = {str(group_name) for group_name in group_names}
    while True:
        raise_if_job_canceled(job_id)
        upload = load_input_upload(upload_id)
        if groups is not None:
            upload = route_completed_input_files(job, upload, groups)
        structured_pending = (
            isinstance(job.get("routing"), dict) and str(upload.get("state") or "") != "uploaded"
        )
        active_group_names = requested_group_names
        if not structured_pending:
            active_group_names = upload_group_names_with_files(upload, requested_group_names)
            if not active_group_names:
                job["upload_progress"] = upload_group_progress(upload, active_group_names)
                save_job(job)
                return upload
        sync_shared_input_tree(upload, active_group_names)
        progress = upload_group_progress(upload, active_group_names)
        job["upload_progress"] = progress
        if not structured_pending and upload_groups_complete(upload, active_group_names):
            save_job(job)
            return upload
        job["phase"] = f"waiting_for_upload:{progress['files_uploaded']}/{progress['files_total']}"
        save_job(job)
        retry_sleep(EAGER_ARCHIVE_WAIT_SECONDS, job_id=job_id)


def materialize_upload_file(
    file_state: dict[str, Any],
    dest_root: Path,
    *,
    verify_sha256: bool = False,
    consume_upload_source: bool = False,
) -> None:
    rel_path = str(file_state["path"])
    expected_bytes = int(file_state["bytes"])
    expected_sha256 = file_state.get("sha256")
    dest = dest_root / materialized_input_rel_path(file_state)
    if file_matches_expected(
        dest,
        expected_bytes,
        expected_sha256=expected_sha256,
        verify_sha256=verify_sha256,
    ):
        if consume_upload_source:
            remove_tusd_file_data(file_state)
        return
    status = upload_file_status(file_state)
    if status["upload_state"] == "consumed":
        raise RuntimeError(f"input file has already been consumed: {rel_path}")
    if not status["complete"]:
        raise RuntimeError(f"input file is incomplete: {rel_path}")
    tusd_source = tusd_data_path(str(file_state["file_upload_id"]))
    shared_source = shared_input_file_path(file_state)
    source = tusd_source if tusd_source.exists() else shared_source
    if source is None:
        raise RuntimeError(f"input file data is missing: {rel_path} ({tusd_source})")
    try:
        source_bytes = source.stat().st_size
    except FileNotFoundError as exc:
        if file_matches_expected(
            dest,
            expected_bytes,
            expected_sha256=expected_sha256,
            verify_sha256=verify_sha256,
        ):
            return
        raise RuntimeError(f"input file data is missing: {rel_path} ({source})") from exc
    if source_bytes < expected_bytes:
        raise RuntimeError(f"input file is incomplete: {rel_path}")
    if verify_sha256 and expected_sha256 and file_sha256(source) != expected_sha256:
        raise RuntimeError(f"input file sha256 mismatch: {rel_path}")
    try:
        link_or_copy(source, dest)
    except RuntimeError:
        alternate_source = shared_source if source == tusd_source else tusd_source
        if not source.exists() and alternate_source is not None and alternate_source.exists():
            link_or_copy(alternate_source, dest)
        else:
            raise
    if not file_matches_expected(
        dest,
        expected_bytes,
        expected_sha256=expected_sha256,
        verify_sha256=verify_sha256,
    ):
        raise RuntimeError(f"input file materialization failed: {rel_path}")
    if consume_upload_source:
        remove_tusd_file_data(file_state)


def materialize_upload_groups(
    upload: dict[str, Any],
    dest_root: Path,
    group_names: set[str],
) -> None:
    upload = refresh_input_upload(upload)
    if not upload_groups_complete(upload, group_names):
        raise RuntimeError("input upload groups are not complete")
    for file_state in upload_files_for_groups(upload, group_names):
        materialize_upload_file(file_state, dest_root)


def write_group_filesystem_metadata(
    root: Path,
    group_name: str,
    file_states: list[dict[str, Any]],
) -> None:
    records: dict[str, dict[str, Any]] = {}
    for file_state in file_states:
        metadata = file_state.get("filesystem_metadata")
        if not isinstance(metadata, dict):
            continue
        rel_path = upload_file_group_rel_for_state(file_state, group_name).as_posix()
        records[rel_path] = metadata
    write_filesystem_metadata_map(root / group_name, records, created_at=utc_timestamp_now())


def sync_shared_input_tree(
    upload: dict[str, Any],
    group_names: set[str] | None = None,
    *,
    job: dict[str, Any] | None = None,
) -> dict[str, int]:
    upload = refresh_input_upload(upload)
    selected_groups = set(group_names or input_upload_groups(upload))
    input_upload_id = str(upload["input_upload_id"])
    with shared_input_tree_lock(input_upload_id):
        upload = refresh_input_upload(upload)
        root = shared_input_upload_root(input_upload_id)
        files = upload_files_for_groups(upload, selected_groups)
        root.mkdir(parents=True, exist_ok=True)
        cleanup_consumed_shared_input_files(upload, selected_groups)
        linked = 0
        skipped = 0
        for index, file_state in enumerate(files, start=1):
            status = upload_file_status(file_state)
            if status["upload_state"] == "consumed":
                remove_shared_input_file_data(file_state)
                skipped += 1
                continue
            if not status["complete"]:
                skipped += 1
                continue
            materialize_upload_file(file_state, root, consume_upload_source=True)
            linked += 1
            if job is not None and (index == len(files) or index % 100 == 0):
                progress = shared_input_tree_progress(upload, selected_groups)
                job["phase"] = f"preparing_input:{progress['input_tree_files_ready']}/{len(files)}"
                job["upload_progress"] = upload_group_progress(upload, selected_groups)
                save_job(job)
        for group_name in selected_groups:
            group_files = upload_files_for_groups(upload, {group_name})
            write_group_filesystem_metadata(root, group_name, group_files)
        return {"linked": linked, "skipped": skipped, "files": len(files)}


def sync_shared_input_file(upload_id: str, rel_path: str) -> bool:
    upload = load_input_upload_raw(upload_id)
    file_state = find_upload_file(upload, rel_path)
    if input_upload_storage_hint(upload).structured_routing and not file_state.get(
        "resolved_group"
    ):
        return False
    with shared_input_tree_lock(upload_id):
        root = shared_input_upload_root(upload_id)
        root.mkdir(parents=True, exist_ok=True)
        status = upload_file_status(file_state)
        if status["upload_state"] == "consumed":
            remove_shared_input_file_data(file_state)
            return False
        if not status["complete"]:
            return False
        materialize_upload_file(file_state, root, consume_upload_source=True)
        return True


def shared_input_tree_metadata(
    upload: dict[str, Any],
    group_names: set[str],
) -> dict[str, Any]:
    files = upload_files_for_groups(upload, group_names)
    return {
        "input_upload_id": str(upload["input_upload_id"]),
        "groups": sorted(group_names),
        "files": len(files),
        "bytes": sum(int(file_state["bytes"]) for file_state in files),
    }


def shared_input_tree_ready(
    root: Path,
    upload: dict[str, Any],
    group_names: set[str],
) -> bool:
    marker = root / ".munchy-input-upload.json"
    if not marker.is_file():
        return False
    try:
        metadata = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    expected = shared_input_tree_metadata(upload, group_names)
    return all(metadata.get(key) == value for key, value in expected.items())


def prepare_shared_input_tree(
    upload: dict[str, Any],
    group_names: set[str],
    *,
    job: dict[str, Any] | None = None,
) -> Path:
    upload = refresh_input_upload(upload)
    if not upload_groups_complete(upload, group_names):
        raise RuntimeError("input upload groups are not complete")
    upload_id = str(upload["input_upload_id"])
    root = shared_input_upload_root(upload_id)
    files = upload_files_for_groups(upload, group_names)
    if shared_input_tree_ready(root, upload, group_names):
        return root
    sync_shared_input_tree(upload, group_names, job=job)
    progress = shared_input_tree_progress(upload, group_names)
    if progress["input_tree_files_ready"] != len(files):
        raise RuntimeError(
            "input upload groups are complete but shared input tree is incomplete: "
            f"{progress['input_tree_files_ready']}/{len(files)}"
        )
    metadata = {
        **shared_input_tree_metadata(upload, group_names),
        "prepared_at": utc_timestamp_now(),
    }
    marker = root / ".munchy-input-upload.json"
    part = marker.with_suffix(marker.suffix + ".part")
    part.write_text(json.dumps(metadata, indent=2, sort_keys=True), encoding="utf-8")
    part.replace(marker)
    return root


def load_shared_review_plan(
    input_upload_id: str,
    group_name: str,
    task_name: str,
) -> dict[str, Any] | None:
    path = shared_review_plan_path(input_upload_id, group_name, task_name)
    if not path.is_file():
        return None
    try:
        plan = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return plan if isinstance(plan, dict) else None


def store_shared_review_plan(
    input_upload_id: str,
    group_name: str,
    task_name: str,
    plan: dict[str, Any],
) -> None:
    path = shared_review_plan_path(input_upload_id, group_name, task_name)
    if path.exists():
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        **plan,
        "shared_plan": {
            "input_upload_id": input_upload_id,
            "group": group_name,
            "task": task_name,
            "stored_at": utc_timestamp_now(),
        },
    }
    part = path.with_suffix(path.suffix + ".part")
    part.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    try:
        part.replace(path)
    finally:
        part.unlink(missing_ok=True)


def remember_review_plans_from_gpu_result(
    job: dict[str, Any],
    group_name: str,
    gpu_result: dict[str, Any],
) -> None:
    upload_id = str(job.get("input_upload_id") or "")
    if not upload_id:
        return
    items = gpu_result.get("items")
    if not isinstance(items, dict):
        return
    for task_name in ("qcut_video", "audio_review"):
        item = items.get(task_name)
        if not isinstance(item, dict):
            continue
        plan = item.get("plan")
        if isinstance(plan, dict):
            store_shared_review_plan(upload_id, group_name, task_name, plan)


def materialize_upload(upload: dict[str, Any], dest_root: Path) -> None:
    upload = refresh_input_upload(upload)
    if upload["state"] != "uploaded":
        raise RuntimeError("input upload is not complete")
    for file_state in upload["files"]:
        materialize_upload_file(file_state, dest_root)


def input_upload_groups(upload: dict[str, Any]) -> list[str]:
    groups = sorted(
        group
        for group in {
            upload_file_resolved_group(file_state) for file_state in upload.get("files", [])
        }
        if group
    )
    if not groups:
        raise RuntimeError("input upload does not contain any files")
    return groups


def input_upload_routed_groups(upload: dict[str, Any]) -> set[str]:
    return {
        group
        for group in {
            upload_file_resolved_group(file_state) for file_state in upload.get("files", [])
        }
        if group
    }


def profile_name_for(encode_profile: dict[str, Any] | None) -> str:
    if isinstance(encode_profile, dict) and encode_profile.get("name"):
        return str(encode_profile["name"])
    return "av1-nvenc-high"


def group_dump(group: GroupConfig) -> dict[str, Any]:
    encode_profile = (
        group.encode_profile.runner_payload() if group.encode_profile is not None else None
    )
    metadata_projection: bool | dict[str, Any]
    if group.metadata_projection is False:
        metadata_projection = False
    else:
        metadata_projection = group.metadata_projection.model_dump(exclude_none=True)
    payload: dict[str, Any] = {
        "output_mode": group.output_mode,
        "tasks": group.tasks,
        "profile": profile_name_for(encode_profile),
        "encode_profile": encode_profile,
        "metadata_projection": metadata_projection,
    }
    if group.max_parallel_encodes is not None:
        payload["max_parallel_encodes"] = group.max_parallel_encodes
    if group.eager_pipeline_batches is not None:
        payload["eager_pipeline_batches"] = group.eager_pipeline_batches
    return payload


def default_group_config(req: CreateJobRequest) -> GroupConfig:
    return GroupConfig(
        output_mode=req.output_mode,
        tasks=req.tasks,
        encode_profile=req.encode_profile,
    )


def storage_hint_for_job_request(req: CreateJobRequest) -> InputUploadStorageHint:
    groups = {
        name: StorageGroupHint(
            output_mode=group.output_mode,
            tasks=group.tasks,
            eager_pipeline_batches=group.eager_pipeline_batches,
        )
        for name, group in req.groups.items()
    }
    if req.workflow_mode == "collection_archive":
        collection_archive_destination = req.collection_archive.destination
    else:
        collection_archive_destination = "target"
    return InputUploadStorageHint(
        workflow_mode=req.workflow_mode,
        collection_archive_destination=collection_archive_destination,
        output_mode=req.output_mode,
        tasks=req.tasks,
        groups=groups,
        structured_routing=req.routing is not None,
    )


def validate_job_storage_hint(input_upload: dict[str, Any], req: CreateJobRequest) -> None:
    try:
        upload_hint = input_upload_storage_hint(input_upload).model_dump(exclude_none=True)
    except (RuntimeError, ValidationError) as exc:
        raise HTTPException(
            status_code=409,
            detail={
                "error": "input_upload_storage_hint_invalid",
                "message": "input upload storage_hint is missing or invalid",
                "input_upload_id": input_upload.get("input_upload_id"),
            },
        ) from exc
    job_hint = storage_hint_for_job_request(req).model_dump(exclude_none=True)
    if upload_hint != job_hint:
        raise HTTPException(
            status_code=409,
            detail={
                "error": "storage_hint_mismatch",
                "message": "input upload storage_hint does not match requested job",
                "input_upload_storage_hint": upload_hint,
                "job_storage_hint": job_hint,
            },
        )


def resolve_job_groups(
    upload: dict[str, Any],
    req: CreateJobRequest,
) -> dict[str, dict[str, Any]]:
    if req.routing is not None:
        return {name: group_dump(group) for name, group in req.groups.items()}
    try:
        input_groups = input_upload_groups(upload)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if req.groups:
        requested = set(req.groups)
        missing = sorted(set(input_groups) - requested)
        extra = sorted(requested - set(input_groups))
        if missing:
            raise HTTPException(
                status_code=400,
                detail=f"missing group config for input directories: {', '.join(missing)}",
            )
        if extra:
            raise HTTPException(
                status_code=400,
                detail=f"group config does not match any input directory: {', '.join(extra)}",
            )
        return {name: group_dump(req.groups[name]) for name in input_groups}

    default_group = group_dump(default_group_config(req))
    return {name: dict(default_group) for name in input_groups}


def ffprobe_for_routing(path: Path) -> dict[str, Any]:
    proc = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-print_format",
            "json",
            "-show_format",
            "-show_streams",
            str(path),
        ],
        text=True,
        capture_output=True,
        check=False,
        timeout=120,
    )
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "ffprobe failed")[-1000:]
        raise RoutingFailed(f"ffprobe failed for {path.name}: {detail}")
    try:
        payload = json.loads(proc.stdout or "{}")
    except json.JSONDecodeError as exc:
        raise RoutingFailed(f"ffprobe returned invalid JSON for {path.name}") from exc
    if not isinstance(payload, dict):
        raise RoutingFailed(f"ffprobe returned non-object JSON for {path.name}")
    return payload


def exiftool_for_routing(path: Path, *, tags: Sequence[str] | None = None) -> dict[str, Any]:
    proc = subprocess.run(
        [
            "exiftool",
            "-j",
            "-a",
            "-G1:4",
            "-s",
            "-ee",
            "-c",
            "%.8f",
            *[f"-{tag}" for tag in (tags or routing_exiftool_tags())],
            str(path),
        ],
        text=True,
        capture_output=True,
        check=False,
        timeout=120,
    )
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "exiftool failed")[-1000:]
        raise RoutingFailed(f"exiftool failed for {path.name}: {detail}")
    try:
        payload = json.loads(proc.stdout or "[]")
    except json.JSONDecodeError as exc:
        raise RoutingFailed(f"exiftool returned invalid JSON for {path.name}") from exc
    if not isinstance(payload, list) or not payload or not isinstance(payload[0], dict):
        raise RoutingFailed(f"exiftool returned no metadata object for {path.name}")
    return cast(dict[str, Any], payload[0])


def upload_file_data_path(file_state: dict[str, Any]) -> Path:
    tusd_source = tusd_data_path(str(file_state["file_upload_id"]))
    if tusd_source.exists():
        return tusd_source
    shared_source = shared_input_file_path(file_state)
    if shared_source is not None and shared_source.exists():
        return shared_source
    raise RoutingFailed(f"input file data is missing for routing: {file_state.get('path')}")


def routing_needs_exiftool(routing: Mapping[str, Any]) -> bool:
    return routing_requires_exiftool(routing)


def routing_needs_probe(routing: Mapping[str, Any]) -> bool:
    return routing_requires_probe(routing)


def runner_routing_file(
    routing: Mapping[str, Any],
    file_state: dict[str, Any],
    *,
    base_routing_facts: Mapping[str, Any] | None = None,
    sidecar_exiftool_tags: Sequence[str] = (),
    sidecar_fact_extractors: Sequence[Mapping[str, Any]] = (),
    sidecar_facts: Mapping[str, Any] | None = None,
    sidecar_facts_error: str | None = None,
) -> RoutingFile:
    rel_path = str(file_state["path"])
    path = upload_file_data_path(file_state)
    base_facts = dict(base_routing_facts or {})
    is_sidecar_evidence = base_facts.get("sidecar.role") == "evidence"
    path_facts = routing_file_facts(rel_path, routing_facts=base_facts)
    probe_summary = None
    if not is_sidecar_evidence and routing_file_requires_probe(routing, path_facts):
        probe_summary = routing_probe_summary(ffprobe_for_routing(path))
    probe_facts = routing_file_facts(
        rel_path,
        probe_summary=probe_summary,
        routing_facts=base_facts,
    )
    exiftool_summary = None
    if not is_sidecar_evidence and routing_file_requires_exiftool(
        routing,
        probe_facts,
    ):
        exiftool_summary = routing_exiftool_summary(
            exiftool_for_routing(path, tags=routing_exiftool_tags(routing))
        )
    collected_sidecar_facts = dict(sidecar_facts) if sidecar_facts is not None else None
    collected_sidecar_facts_error = sidecar_facts_error
    if (
        sidecar_exiftool_tags
        and collected_sidecar_facts is None
        and collected_sidecar_facts_error is None
    ):
        try:
            collected_sidecar_facts = exiftool_routing_facts(
                routing_exiftool_summary(exiftool_for_routing(path, tags=sidecar_exiftool_tags)),
                fact_extractors=sidecar_fact_extractors,
            )
        except RoutingFailed as exc:
            collected_sidecar_facts_error = str(exc)[:1000]
    return RoutingFile(
        path=rel_path,
        bytes=int(file_state.get("bytes") or 0),
        sha256=str(file_state.get("sha256") or "") or None,
        probe_summary=probe_summary,
        routing_facts=routing_file_facts(
            rel_path,
            probe_summary=probe_summary,
            exiftool_summary=exiftool_summary,
            routing_facts=base_facts,
        ),
        sidecar_facts=collected_sidecar_facts,
        sidecar_facts_error=collected_sidecar_facts_error,
    )


def routing_path_facts_for_files(
    routing: Mapping[str, Any],
    file_states: Sequence[dict[str, Any]],
    *,
    sidecar_facts_by_path: Mapping[str, Mapping[str, Any] | None] | None = None,
    sidecar_facts_errors_by_path: Mapping[str, str | None] | None = None,
) -> dict[str, dict[str, Any]]:
    facts_by_path = {
        str(file_state["path"]): routing_file_facts(str(file_state["path"]))
        for file_state in file_states
    }
    if routing.get("sidecars"):
        return apply_sidecar_rules(
            routing,
            facts_by_path,
            sidecar_facts_by_path=sidecar_facts_by_path,
            sidecar_facts_errors_by_path=sidecar_facts_errors_by_path,
            require_configured_facts=False,
        )
    return facts_by_path


def apply_routing_decision(
    file_state: dict[str, Any],
    decision: Mapping[str, Any],
) -> bool:
    if file_state.get("routed_at"):
        return False
    action = str(decision.get("action") or "upload")
    file_state["route_id"] = str(decision.get("route_id") or "")
    file_state["route_action"] = action
    if decision.get("pair_kind"):
        file_state["pair_kind"] = str(decision["pair_kind"])
    if decision.get("pairing_id"):
        file_state["pair_id"] = str(decision["pairing_id"])
    if decision.get("pair_role"):
        file_state["pair_role"] = str(decision["pair_role"])
    if decision.get("pair_with"):
        file_state["pair_with"] = str(decision["pair_with"])
    if isinstance(decision.get("matched_facts"), dict):
        file_state["route_matched_facts"] = dict(decision["matched_facts"])
    if action == "leave":
        file_state["routed_at"] = utc_timestamp_now()
        return True
    if action == "evidence":
        group = validate_group_name(str(decision.get("group") or ""))
        file_state["resolved_group"] = group
        file_state["resolved_group_rel"] = str(
            decision.get("collection_rel_path") or file_state["path"]
        )
        file_state["sidecar_id"] = str(decision.get("sidecar_id") or "")
        file_state["sidecar_format"] = str(decision.get("sidecar_format") or "opaque")
        file_state["sidecar_for"] = str(decision.get("sidecar_for") or "")
        file_state["routed_at"] = utc_timestamp_now()
        return True
    group = validate_group_name(str(decision.get("group") or ""))
    file_state["resolved_group"] = group
    file_state["resolved_group_rel"] = str(
        decision.get("collection_rel_path") or file_state["path"]
    )
    file_state["routed_at"] = utc_timestamp_now()
    return True


def route_completed_file(
    job: dict[str, Any],
    file_state: dict[str, Any],
    groups: dict[str, dict[str, Any]],
) -> bool:
    if file_state.get("resolved_group") or file_state.get("route_action") == "leave":
        return False
    routing = job.get("routing")
    if not isinstance(routing, dict):
        return False
    rel_path = str(file_state["path"])

    match = match_route(
        routing,
        rel_path,
        routing_facts=runner_routing_file(routing, file_state).routing_facts,
    )
    if match is None:
        raise RoutingFailed(f"routing failed for {rel_path}: no matching route")
    if match.action == "leave":
        return apply_routing_decision(
            file_state,
            {
                "route_id": match.route_id,
                "action": "leave",
                "pair_kind": match.pair_kind,
                "pairing_id": match.pairing_id,
                "pair_role": match.pair_role,
                "pair_with": match.pair_with,
                "matched_facts": matched_fact_values(
                    match.route,
                    match.facts,
                    routing=routing,
                ),
            },
        )
    group = validate_group_name(match.group)
    if group not in groups:
        raise RoutingFailed(f"routing failed for {rel_path}: unknown group {group}")
    return apply_routing_decision(
        file_state,
        {
            "route_id": match.route_id,
            "action": match.action,
            "group": group,
            "collection_rel_path": match.collection_rel_path or rel_path,
            "pair_kind": match.pair_kind,
            "pairing_id": match.pairing_id,
            "pair_role": match.pair_role,
            "pair_with": match.pair_with,
            "matched_facts": matched_fact_values(
                match.route,
                match.facts,
                routing=routing,
            ),
        },
    )


def predicate_requires_non_path_facts(predicate: Mapping[str, Any]) -> bool:
    if not predicate:
        return False
    fact = predicate.get("fact")
    if isinstance(fact, str) and not fact.startswith("path."):
        return True
    for key in ("all", "any"):
        items = predicate.get(key)
        if isinstance(items, list) and any(
            isinstance(item, Mapping) and predicate_requires_non_path_facts(item) for item in items
        ):
            return True
    not_item = predicate.get("not")
    if isinstance(not_item, Mapping) and predicate_requires_non_path_facts(not_item):
        return True
    return bool(predicate.get("gate"))


def sidecar_rules_are_path_resolvable(routing: Mapping[str, Any]) -> bool:
    for rule in sidecar_rules(routing):
        for key in ("primary", "sidecar"):
            predicate = rule.get(key)
            if isinstance(predicate, Mapping) and predicate_requires_non_path_facts(predicate):
                return False
    return True


def completed_routing_files_to_route(
    routing: Mapping[str, Any],
    pending_files: Sequence[dict[str, Any]],
    complete_files: Sequence[dict[str, Any]],
) -> list[dict[str, Any]]:
    if not routing.get("pairings") and not routing.get("sidecars"):
        return list(complete_files)
    if routing.get("pairings"):
        return list(pending_files) if len(complete_files) == len(pending_files) else []
    if not sidecar_rules_are_path_resolvable(routing):
        return list(pending_files) if len(complete_files) == len(pending_files) else []

    complete_paths = {str(file_state["path"]) for file_state in complete_files}
    pending_by_path = {str(file_state["path"]): file_state for file_state in pending_files}
    path_facts_by_path = {path: routing_file_facts(path) for path in pending_by_path}
    sidecar_marked_facts = apply_sidecar_rules(
        routing,
        path_facts_by_path,
        require_configured_facts=False,
    )
    evidence_by_primary: dict[str, set[str]] = {}
    for path, facts in sidecar_marked_facts.items():
        if facts.get("sidecar.role") != "evidence":
            continue
        primary_path = str(facts.get("sidecar.for") or "")
        if primary_path:
            evidence_by_primary.setdefault(primary_path, set()).add(path)

    selected_paths: set[str] = set()
    for file_state in complete_files:
        path = str(file_state["path"])
        facts = sidecar_marked_facts.get(path, {})
        if facts.get("sidecar.role") == "evidence":
            continue
        evidence_paths = evidence_by_primary.get(path, set())
        if any(evidence_path not in complete_paths for evidence_path in evidence_paths):
            continue
        selected_paths.add(path)
        selected_paths.update(evidence_paths)

    return [pending_by_path[path] for path in pending_by_path if path in selected_paths]


def route_completed_input_files(
    job: dict[str, Any],
    upload: dict[str, Any],
    groups: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    if not isinstance(job.get("routing"), dict):
        return upload
    changed = False
    pending_files = [
        file_state
        for file_state in upload.get("files", [])
        if not file_state.get("resolved_group") and file_state.get("route_action") != "leave"
    ]
    if not pending_files:
        return upload
    routing = cast(Mapping[str, Any], job["routing"])
    complete_files = [
        file_state for file_state in pending_files if upload_file_status(file_state)["complete"]
    ]
    if not complete_files:
        return upload
    files_to_route = completed_routing_files_to_route(
        routing,
        pending_files,
        complete_files,
    )
    if not files_to_route:
        return upload
    path_facts_by_path = {
        str(file_state["path"]): routing_file_facts(str(file_state["path"]))
        for file_state in files_to_route
    }
    sidecar_fact_requests = sidecar_exiftool_fact_requests(routing, path_facts_by_path)
    sidecar_facts_by_path: dict[str, dict[str, Any]] = {}
    sidecar_facts_errors_by_path: dict[str, str] = {}
    for file_state in files_to_route:
        rel_path = str(file_state["path"])
        sidecar_request = sidecar_fact_requests.get(rel_path)
        if sidecar_request is None or not sidecar_request.tags:
            continue
        try:
            sidecar_facts_by_path[rel_path] = exiftool_routing_facts(
                routing_exiftool_summary(
                    exiftool_for_routing(
                        upload_file_data_path(file_state),
                        tags=sidecar_request.tags,
                    )
                ),
                fact_extractors=sidecar_request.fact_extractors,
            )
        except RoutingFailed as exc:
            sidecar_facts_errors_by_path[rel_path] = str(exc)[:1000]
    base_facts_by_path = routing_path_facts_for_files(
        routing,
        files_to_route,
        sidecar_facts_by_path=sidecar_facts_by_path,
        sidecar_facts_errors_by_path=sidecar_facts_errors_by_path,
    )
    routing_files: list[RoutingFile] = []
    for file_state in files_to_route:
        rel_path = str(file_state["path"])
        sidecar_request = sidecar_fact_requests.get(rel_path)
        routing_files.append(
            runner_routing_file(
                routing,
                file_state,
                base_routing_facts=base_facts_by_path.get(rel_path),
                sidecar_exiftool_tags=sidecar_request.tags if sidecar_request else (),
                sidecar_fact_extractors=(
                    sidecar_request.fact_extractors if sidecar_request else ()
                ),
                sidecar_facts=sidecar_facts_by_path.get(rel_path),
                sidecar_facts_error=sidecar_facts_errors_by_path.get(rel_path),
            )
        )
    plan = routing_plan(routing, routing_files, group_names=set(groups))
    if not plan.ok:
        first = plan.unmatched[0] if plan.unmatched else {}
        reason = str(first.get("reason") or "no matching route").replace("_", " ")
        raise RoutingFailed(f"routing failed for {first.get('path') or 'input upload'}: {reason}")
    decisions = {item["path"]: item for item in [*plan.matches, *plan.left]}
    for file_state in files_to_route:
        changed = apply_routing_decision(file_state, decisions[str(file_state["path"])]) or changed
    if not changed:
        return upload

    group_counts: dict[str, int] = {}
    route_counts: dict[str, int] = {}
    left_count = 0
    for file_state in upload.get("files", []):
        group = file_state.get("resolved_group")
        route_id = file_state.get("route_id")
        if file_state.get("route_action") == "leave":
            left_count += 1
        if isinstance(group, str) and group:
            group_counts[group] = group_counts.get(group, 0) + 1
        if isinstance(route_id, str) and route_id:
            route_counts[route_id] = route_counts.get(route_id, 0) + 1
    job["routing_result"] = {
        "updated_at": utc_timestamp_now(),
        "files": sum(group_counts.values()),
        "left_files": left_count,
        "groups": group_counts,
        "routes": route_counts,
    }
    job["riverhog_expected_primary_files_total"] = expected_riverhog_primary_files_total(
        upload,
        groups,
    )
    with state_lock:
        save_input_upload_raw(upload)
        save_job(job)
    return load_input_upload(str(upload["input_upload_id"]))


def grouped_task_union(groups: dict[str, dict[str, Any]]) -> list[TaskName]:
    tasks: list[TaskName] = []
    for group in groups.values():
        for task in group.get("tasks") or []:
            if task not in tasks:
                tasks.append(task)
    return tasks


def gpu_group_job_id(job_id: str, group_name: str) -> str:
    digest = hashlib.sha256(f"{job_id}/{group_name}".encode()).hexdigest()[:10]
    safe_group = group_name[:48]
    suffix = f"__{safe_group}__{digest}"
    return f"{job_id[: max(1, 180 - len(suffix))]}{suffix}"


def gpu_eager_batch_job_id(job_id: str, batch_id: str) -> str:
    digest = hashlib.sha256(f"{job_id}/eager/{batch_id}".encode()).hexdigest()[:10]
    safe_batch = batch_id[:48]
    suffix = f"__eager__{safe_batch}__{digest}"
    return f"{job_id[: max(1, 180 - len(suffix))]}{suffix}"


def gpu_job_work_roots(job: dict[str, Any]) -> list[Path]:
    job_id = str(job["job_id"])
    roots: list[Path] = []
    seen: set[Path] = set()

    def add_gpu_job_root(gpu_job_id: str) -> None:
        if not gpu_job_id:
            return
        root = GPU_RUNTIME_DIR / "jobs" / gpu_job_id
        if root in seen:
            return
        seen.add(root)
        roots.append(root)

    add_gpu_job_root(job_id)
    jobs_root = GPU_RUNTIME_DIR / "jobs"
    if jobs_root.exists():
        for root in jobs_root.iterdir():
            if root.name.startswith(f"{job_id}__"):
                add_gpu_job_root(root.name)
    groups = job.get("groups")
    if isinstance(groups, dict):
        for group_name in groups:
            add_gpu_job_root(gpu_group_job_id(job_id, str(group_name)))
    eager = job.get("eager_archive")
    batches = eager.get("batches") if isinstance(eager, dict) else None
    if isinstance(batches, dict):
        for batch_key, batch in batches.items():
            if not isinstance(batch, dict):
                continue
            gpu_job_id = str(batch.get("gpu_job_id") or "")
            add_gpu_job_root(gpu_job_id)
            payload = batch.get("payload")
            if isinstance(payload, dict):
                add_gpu_job_root(str(payload.get("job_id") or ""))
            batch_id = str(batch.get("batch_id") or batch_key)
            add_gpu_job_root(gpu_eager_batch_job_id(job_id, batch_id))
    return roots


def group_archive_container(group_config: dict[str, Any]) -> ArchiveContainer:
    profile = group_config.get("encode_profile")
    archive: dict[str, Any] = {}
    if isinstance(profile, dict) and isinstance(profile.get("archive"), dict):
        archive = profile["archive"]
    output_mode = normalize_output_mode(str(group_config.get("output_mode") or "video"))
    default_container = "opus" if output_mode == "audio" else "mkv"
    container = str(archive.get("container") or default_container)
    if container not in {"mkv", "webm", "opus"}:
        raise RuntimeError(f"unsupported archive container: {container}")
    return container  # type: ignore[return-value]


def archive_container_suffix(group_config: dict[str, Any]) -> str:
    return f".{group_archive_container(group_config)}"


def archive_output_for_upload_file(
    file_state: dict[str, Any],
    *,
    group_name: str,
    group_config: dict[str, Any],
    archive_dir: Path,
) -> Path:
    group_rel = upload_file_group_rel_for_state(file_state, group_name)
    return (archive_dir / group_name / group_rel).with_suffix(
        archive_container_suffix(group_config)
    )


def eager_archive_executor(group_config: dict[str, Any]) -> str | None:
    output_mode = normalize_output_mode(str(group_config.get("output_mode") or "video"))
    tasks = set(str(task) for task in group_config.get("tasks") or [])
    if output_mode == "video" and tasks == {"archive_video"}:
        return "gpu"
    if output_mode == "audio" and tasks == {"archive_audio"}:
        return "local_audio"
    return None


def group_is_eager_archive_only(group_config: dict[str, Any]) -> bool:
    return eager_archive_executor(group_config) is not None


def group_produces_primary_archive_output(group_config: dict[str, Any]) -> bool:
    if normalize_output_mode(str(group_config.get("output_mode") or "video")) == "preserve":
        return True
    tasks = set(str(task) for task in group_config.get("tasks") or [])
    return bool(tasks & {"archive_video", "archive_audio"})


def archive_output_path_for_routed_file(
    file_state: dict[str, Any],
    *,
    group_name: str,
    group_config: dict[str, Any],
    archive_dir: Path,
) -> Path:
    if normalize_output_mode(str(group_config.get("output_mode") or "video")) == "preserve":
        return archive_dir / group_name / upload_file_group_rel_for_state(file_state, group_name)
    return archive_output_for_upload_file(
        file_state,
        group_name=group_name,
        group_config=group_config,
        archive_dir=archive_dir,
    )


def routing_manifest_output_entry(path: Path, *, archive_dir: Path) -> dict[str, Any]:
    rel_path = path.relative_to(archive_dir).as_posix()
    entry: dict[str, Any] = {
        "path": rel_path,
        "exists": path.exists(),
    }
    if path.exists():
        entry["bytes"] = path.stat().st_size
    xmp_sidecar = immich_xmp_sidecar_path(path)
    if xmp_sidecar.exists():
        entry["metadata_sidecars"] = [
            {
                "target": "immich_xmp",
                "path": xmp_sidecar.relative_to(archive_dir).as_posix(),
                "bytes": xmp_sidecar.stat().st_size,
            }
        ]
    return entry


def routing_manifest_file_entry(
    file_state: dict[str, Any],
    *,
    upload: dict[str, Any],
    groups: dict[str, dict[str, Any]],
    archive_dir: Path,
) -> dict[str, Any]:
    action = str(file_state.get("route_action") or "")
    group_name = upload_file_resolved_group(file_state)
    entry: dict[str, Any] = {
        "source": {
            "path": str(file_state.get("path") or ""),
            "bytes": int(file_state.get("bytes") or 0),
        },
        "route": {
            "id": str(file_state.get("route_id") or ""),
            "action": action or ("upload" if group_name else ""),
        },
    }
    if file_state.get("sha256"):
        entry["source"]["sha256"] = str(file_state["sha256"])
    pair: dict[str, Any] = {}
    for source_key, output_key in (
        ("pair_kind", "kind"),
        ("pair_id", "id"),
        ("pair_role", "role"),
        ("pair_with", "with"),
    ):
        if file_state.get(source_key):
            pair[output_key] = str(file_state[source_key])
    if pair:
        entry["pair"] = pair
    matched_facts = file_state.get("route_matched_facts")
    if isinstance(matched_facts, dict) and matched_facts:
        entry["route"]["matched_facts"] = matched_facts
    if group_name:
        group_config = groups[group_name]
        group_rel = upload_file_group_rel_for_state(file_state, group_name).as_posix()
        entry["route"]["group"] = group_name
        entry["route"]["group_rel_path"] = group_rel
        if upload_file_is_sidecar_evidence(file_state):
            entry["route"]["sidecar"] = {
                "id": str(file_state.get("sidecar_id") or ""),
                "format": str(file_state.get("sidecar_format") or "opaque"),
                "for": str(file_state.get("sidecar_for") or ""),
            }
            entry["output"] = {
                "kind": "none",
                "reason": "sidecar_evidence",
            }
            custody = routing_manifest_sidecar_custody_entry(
                file_state,
                upload=upload,
                groups=groups,
                archive_dir=archive_dir,
            )
            if custody:
                entry["custody"] = custody
            return entry
        entry["output"] = routing_manifest_output_entry(
            archive_output_path_for_routed_file(
                file_state,
                group_name=group_name,
                group_config=group_config,
                archive_dir=archive_dir,
            ),
            archive_dir=archive_dir,
        )
    return entry


def routing_manifest_sidecar_custody_entry(
    evidence_state: Mapping[str, Any],
    *,
    upload: dict[str, Any],
    groups: dict[str, dict[str, Any]],
    archive_dir: Path,
) -> dict[str, Any] | None:
    primary_source = str(evidence_state.get("sidecar_for") or "")
    evidence_dict = cast(dict[str, Any], evidence_state)
    group_name = upload_file_resolved_group(evidence_dict)
    if not primary_source or not group_name:
        return None
    primary_state = next(
        (
            file_state
            for file_state in upload.get("files", [])
            if isinstance(file_state, dict) and str(file_state.get("path") or "") == primary_source
        ),
        None,
    )
    if primary_state is None:
        return None
    primary_group = upload_file_resolved_group(primary_state)
    if not primary_group or primary_group not in groups:
        return None
    primary_group_config = groups[primary_group]
    if not group_produces_primary_archive_output(primary_group_config):
        return None
    primary_output = archive_output_path_for_routed_file(
        primary_state,
        group_name=primary_group,
        group_config=primary_group_config,
        archive_dir=archive_dir,
    )
    evidence_group_rel = upload_file_group_rel_for_state(
        evidence_dict,
        group_name,
    )
    return {
        "kind": "source_artifact_sidecar",
        "primary_source": primary_source,
        "source_artifacts_path": source_artifact_sidecar_for_archive_output(primary_output)
        .relative_to(archive_dir)
        .as_posix(),
        "source_artifacts_entry": normalize_posix(
            PurePosixPath("sidecars", evidence_group_rel.as_posix()).as_posix()
        ),
    }


def write_routing_manifest(
    job: dict[str, Any],
    upload: dict[str, Any],
    groups: dict[str, dict[str, Any]],
    archive_dir: Path,
) -> None:
    if not isinstance(job.get("routing"), dict):
        return
    files = [
        routing_manifest_file_entry(
            file_state,
            upload=upload,
            groups=groups,
            archive_dir=archive_dir,
        )
        for file_state in upload.get("files", [])
        if file_state.get("routed_at")
    ]
    payload = {
        "schema": "munchy.routing-manifest",
        "schema_version": 1,
        "created_at": utc_timestamp_now(),
        "job_id": str(job.get("job_id") or ""),
        "input_upload_id": str(job.get("input_upload_id") or ""),
        "collection_slug": str(job.get("collection_slug") or ""),
        "collection_timestamp": str(job.get("collection_timestamp") or ""),
        "files": sorted(files, key=lambda item: str(item["source"]["path"])),
    }
    archive_dir.mkdir(parents=True, exist_ok=True)
    (archive_dir / ROUTING_MANIFEST_FILENAME).write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def metadata_projection_config(group_config: dict[str, Any]) -> dict[str, Any]:
    raw = group_config.get("metadata_projection")
    if raw is False:
        return {
            "enabled": False,
            "target": "immich_xmp",
            "allow_missing_capture_date": False,
            "allow_missing_gps": False,
            "allow_missing_device_make": False,
            "allow_missing_device_model": False,
            "allow_missing_creators": False,
            "capture_date_sources": None,
            "gps_sources": None,
            "configured_gps": None,
            "device_make": None,
            "device_model": None,
            "creators": [],
            "tags": [],
            "include_context_tags": False,
        }
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise RuntimeError("metadata_projection must be a table")
    target = str(raw.get("target") or "immich_xmp")
    if target != "immich_xmp":
        raise RuntimeError(f"unsupported metadata projection target: {target}")
    tags = raw.get("tags") or []
    if not isinstance(tags, list):
        raise RuntimeError("metadata_projection.tags must be a list")
    capture_date_sources = raw.get("capture_date_sources")
    if capture_date_sources is not None and not isinstance(capture_date_sources, list):
        raise RuntimeError("metadata_projection.capture_date_sources must be a list")
    if isinstance(capture_date_sources, list):
        for source in capture_date_sources:
            if not isinstance(source, dict):
                raise RuntimeError(
                    "metadata_projection.capture_date_sources entries must be tables"
                )
    gps_sources = raw.get("gps_sources")
    if gps_sources is not None and not isinstance(gps_sources, list):
        raise RuntimeError("metadata_projection.gps_sources must be a list")
    if isinstance(gps_sources, list):
        for source in gps_sources:
            if not isinstance(source, dict):
                raise RuntimeError("metadata_projection.gps_sources entries must be tables")
    device = raw.get("device") or {}
    if not isinstance(device, dict):
        raise RuntimeError("metadata_projection.device must be a table")
    device_make = str(device.get("make") or "").strip() or None
    device_model = str(device.get("model") or "").strip() or None
    configured_gps = raw.get("gps")
    if configured_gps is not None and not isinstance(configured_gps, dict):
        raise RuntimeError("metadata_projection.gps must be a table")
    if "creator" in raw:
        raise RuntimeError("metadata_projection.creator is not supported; use creators = [...]")
    creators = raw.get("creators") or []
    if not isinstance(creators, list):
        raise RuntimeError("metadata_projection.creators must be a list")
    return {
        "enabled": bool(raw.get("enabled", True)),
        "target": target,
        "allow_missing_capture_date": bool(raw.get("allow_missing_capture_date", False)),
        "allow_missing_gps": bool(raw.get("allow_missing_gps", False)),
        "allow_missing_device_make": bool(raw.get("allow_missing_device_make", False)),
        "allow_missing_device_model": bool(raw.get("allow_missing_device_model", False)),
        "allow_missing_creators": bool(raw.get("allow_missing_creators", False)),
        "capture_date_sources": copy.deepcopy(capture_date_sources),
        "gps_sources": copy.deepcopy(gps_sources),
        "configured_gps": copy.deepcopy(configured_gps),
        "device_make": device_make,
        "device_model": device_model,
        "creators": [str(creator).strip() for creator in creators if str(creator).strip()],
        "tags": [str(tag).strip() for tag in tags if str(tag).strip()],
        "include_context_tags": bool(raw.get("include_context_tags", True)),
    }


def metadata_projection_enabled(group_config: dict[str, Any]) -> bool:
    return bool(metadata_projection_config(group_config)["enabled"])


def metadata_projection_facts_for_path(
    rel_path: str,
    path: Path,
    *,
    filesystem_metadata: Mapping[str, Any] | None = None,
    sidecar_facts: Mapping[str, Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    probe_summary: dict[str, Any] | None = None
    try:
        probe_summary = routing_probe_summary(ffprobe_for_routing(path))
    except RoutingFailed as exc:
        log.debug("ffprobe metadata projection summary skipped for %s: %s", rel_path, exc)
    exiftool_summary: dict[str, Any] | None = None
    try:
        exiftool_summary = routing_exiftool_summary(exiftool_for_routing(path))
    except RoutingFailed as exc:
        log.debug("exiftool metadata projection summary skipped for %s: %s", rel_path, exc)
    facts = routing_file_facts(
        rel_path,
        probe_summary=probe_summary,
        exiftool_summary=exiftool_summary,
    )
    if filesystem_metadata:
        facts["filesystem"] = dict(filesystem_metadata)
        facts["source_filesystem_metadata"] = dict(filesystem_metadata)
    if sidecar_facts:
        ids: list[str] = []
        for sidecar_id, payload in sorted(sidecar_facts.items()):
            if not isinstance(payload, Mapping):
                continue
            sidecar_key = str(sidecar_id).strip()
            if not sidecar_key:
                continue
            ids.append(sidecar_key)
            facts[f"sidecars.{sidecar_key}.path"] = str(payload.get("path") or "")
            facts[f"sidecars.{sidecar_key}.format"] = str(payload.get("format") or "")
            nested = payload.get("facts")
            if isinstance(nested, Mapping):
                for key, value in nested.items():
                    facts[f"sidecars.{sidecar_key}.facts.{key}"] = value
        if ids:
            facts["sidecars.ids"] = ids
    return facts


def projection_metadata_from_source(
    rel_path: str,
    source_path: Path,
    *,
    group_config: dict[str, Any],
    filesystem_metadata: Mapping[str, Any] | None = None,
    sidecar_facts: Mapping[str, Mapping[str, Any]] | None = None,
    tags: list[str] | None = None,
) -> ProjectionMetadata:
    config = metadata_projection_config(group_config)
    try:
        return project_immich_metadata(
            metadata_projection_facts_for_path(
                rel_path,
                source_path,
                filesystem_metadata=filesystem_metadata,
                sidecar_facts=sidecar_facts,
            ),
            allow_missing_capture_date=bool(config["allow_missing_capture_date"]),
            allow_missing_gps=bool(config["allow_missing_gps"]),
            allow_missing_device_make=bool(config["allow_missing_device_make"]),
            allow_missing_device_model=bool(config["allow_missing_device_model"]),
            allow_missing_creators=bool(config["allow_missing_creators"]),
            capture_date_sources=cast(
                list[dict[str, Any]] | None,
                config.get("capture_date_sources"),
            ),
            gps_sources=cast(
                list[dict[str, Any]] | None,
                config.get("gps_sources"),
            ),
            configured_gps=cast(dict[str, Any] | None, config.get("configured_gps")),
            device_make=cast(str | None, config.get("device_make")),
            device_model=cast(str | None, config.get("device_model")),
            creators=cast(list[str], config.get("creators")),
            tags=tags if tags is not None else cast(list[str], config["tags"]),
        )
    except MetadataProjectionError as exc:
        raise RuntimeError(f"metadata projection failed for {rel_path}: {exc}") from exc


def file_state_filesystem_metadata(file_state: Mapping[str, Any]) -> Mapping[str, Any] | None:
    metadata = file_state.get("filesystem_metadata")
    return cast(Mapping[str, Any], metadata) if isinstance(metadata, Mapping) else None


def metadata_projection_sidecar_facts(
    upload: dict[str, Any],
    file_state: Mapping[str, Any],
    *,
    routing: Mapping[str, Any] | None = None,
    source_paths_by_path: Mapping[str, Path] | None = None,
) -> dict[str, dict[str, Any]]:
    sidecars: dict[str, dict[str, Any]] = {}
    for evidence in sidecar_evidence_files_for_primary(upload, file_state):
        sidecar_id = str(evidence.get("sidecar_id") or "").strip()
        sidecar_format = str(evidence.get("sidecar_format") or "opaque").strip()
        if not sidecar_id:
            continue
        tags = metadata_projection_sidecar_exiftool_tags(routing, sidecar_id=sidecar_id)
        if not tags:
            continue
        fact_extractors = metadata_projection_sidecar_fact_extractors(
            routing,
            sidecar_id=sidecar_id,
        )
        rel_path = str(evidence.get("path") or "")
        source_path = (
            source_paths_by_path.get(rel_path) if source_paths_by_path is not None else None
        )
        if source_path is None:
            source_path = upload_file_data_path(evidence)
        exiftool_summary = routing_exiftool_summary(exiftool_for_routing(source_path, tags=tags))
        sidecars[sidecar_id] = {
            "path": rel_path,
            "format": sidecar_format,
            "facts": routing_file_facts(
                rel_path,
                exiftool_summary=exiftool_summary,
                exiftool_fact_extractors=fact_extractors,
            ),
        }
    return sidecars


def job_routing(job: Mapping[str, Any] | None) -> Mapping[str, Any] | None:
    if not isinstance(job, Mapping):
        return None
    routing = job.get("routing")
    return cast(Mapping[str, Any], routing) if isinstance(routing, Mapping) else None


def metadata_projection_sidecar_exiftool_tags(
    routing: Mapping[str, Any] | None,
    *,
    sidecar_id: str,
) -> tuple[str, ...]:
    if not isinstance(routing, Mapping):
        return ()
    for rule in sidecar_rules(routing):
        if str(rule.get("id") or "").strip() == sidecar_id:
            return sidecar_rule_exiftool_tags(rule)
    return ()


def metadata_projection_sidecar_fact_extractors(
    routing: Mapping[str, Any] | None,
    *,
    sidecar_id: str,
) -> tuple[Mapping[str, Any], ...]:
    if not isinstance(routing, Mapping):
        return ()
    for rule in sidecar_rules(routing):
        if str(rule.get("id") or "").strip() == sidecar_id:
            return sidecar_rule_fact_extractors(rule)
    return ()


def xmp_evidence_sidecar_path(
    upload: dict[str, Any],
    file_state: Mapping[str, Any],
) -> Path | None:
    for evidence in sidecar_evidence_files_for_primary(upload, file_state):
        if str(evidence.get("sidecar_format") or "").casefold() != "xmp":
            continue
        return upload_file_data_path(evidence)
    return None


def metadata_projection_with_tags(
    metadata: ProjectionMetadata,
    tags: list[str],
) -> ProjectionMetadata:
    return ProjectionMetadata(
        capture_date=metadata.capture_date,
        capture_date_source=metadata.capture_date_source,
        gps=metadata.gps,
        gps_source=metadata.gps_source,
        device_make=metadata.device_make,
        device_model=metadata.device_model,
        creators=metadata.creators,
        tags=tuple(tags),
    )


def projection_metadata_satisfies_config(
    metadata: ProjectionMetadata,
    config: dict[str, Any],
) -> bool:
    if not config["allow_missing_capture_date"] and not metadata.capture_date:
        return False
    if not config["allow_missing_gps"] and metadata.gps is None:
        return False
    expected_make = cast(str | None, config.get("device_make"))
    if expected_make and metadata.device_make != expected_make:
        return False
    if not expected_make and not config["allow_missing_device_make"] and not metadata.device_make:
        return False
    expected_model = cast(str | None, config.get("device_model"))
    if expected_model and metadata.device_model != expected_model:
        return False
    if (
        not expected_model
        and not config["allow_missing_device_model"]
        and not metadata.device_model
    ):
        return False
    expected_creators = tuple(cast(list[str], config.get("creators") or []))
    if expected_creators and metadata.creators != expected_creators:
        return False
    if not expected_creators and not config["allow_missing_creators"] and not metadata.creators:
        return False
    return True


def container_metadata_for_gpu_payload(
    job: dict[str, Any],
    upload: dict[str, Any],
    file_states: list[dict[str, Any]],
    *,
    group_name: str,
    group_config: dict[str, Any],
    tasks: Sequence[str],
    source_paths_by_path: Mapping[str, Path] | None = None,
) -> tuple[dict[str, dict[str, Any]], bool]:
    if not gpu_tasks_require_container_metadata(tasks, group_config):
        return {}, False
    metadata_by_rel_path: dict[str, dict[str, Any]] = {}
    changed = False
    for file_state in file_states:
        if ensure_file_projection_metadata(
            upload,
            file_state,
            job=job,
            group_config=group_config,
            source_path=source_paths_by_path.get(str(file_state["path"]))
            if source_paths_by_path is not None
            else None,
            sidecar_source_paths_by_path=source_paths_by_path,
        ):
            changed = True
        stored = file_state.get("metadata_projection_metadata")
        if isinstance(stored, dict):
            rel_path = upload_file_group_rel_for_state(file_state, group_name).as_posix()
            metadata_by_rel_path[rel_path] = copy.deepcopy(stored)
    return metadata_by_rel_path, changed


def gpu_tasks_require_container_metadata(
    tasks: Sequence[str],
    group_config: dict[str, Any],
) -> bool:
    return "archive_video" in {str(task) for task in tasks} and metadata_projection_enabled(
        group_config
    )


def ensure_file_projection_metadata(
    upload: dict[str, Any],
    file_state: dict[str, Any],
    *,
    job: Mapping[str, Any] | None = None,
    group_config: dict[str, Any],
    source_path: Path | None = None,
    sidecar_source_paths_by_path: Mapping[str, Path] | None = None,
) -> bool:
    if not metadata_projection_enabled(group_config):
        return False
    config = metadata_projection_config(group_config)
    stored = file_state.get("metadata_projection_metadata")
    if isinstance(stored, dict) and projection_metadata_satisfies_config(
        ProjectionMetadata.from_dict(stored),
        config,
    ):
        return False
    metadata = projection_metadata_from_source(
        str(file_state["path"]),
        source_path if source_path is not None else upload_file_data_path(file_state),
        group_config=group_config,
        filesystem_metadata=file_state_filesystem_metadata(file_state),
        sidecar_facts=metadata_projection_sidecar_facts(
            upload,
            file_state,
            routing=job_routing(job),
            source_paths_by_path=sidecar_source_paths_by_path,
        ),
    )
    file_state["metadata_projection_metadata"] = metadata.as_dict()
    file_state["metadata_projection_captured_at"] = utc_timestamp_now()
    return True


def projection_metadata_for_file_output(
    upload: dict[str, Any],
    file_state: dict[str, Any],
    *,
    job: dict[str, Any],
    group_name: str,
    group_config: dict[str, Any],
    output_path: Path,
) -> ProjectionMetadata:
    tags = metadata_projection_tags_for_file(
        job,
        file_state,
        group_name=group_name,
        group_config=group_config,
    )
    stored = file_state.get("metadata_projection_metadata")
    if isinstance(stored, dict):
        return metadata_projection_with_tags(
            ProjectionMetadata.from_dict(stored),
            tags,
        )
    source_path: Path | None
    try:
        source_path = upload_file_data_path(file_state)
    except RoutingFailed:
        source_path = output_path if output_path.exists() else None
    if source_path is None:
        raise RuntimeError(
            f"metadata projection cannot locate source metadata for {file_state.get('path')}"
        )
    return projection_metadata_from_source(
        str(file_state["path"]),
        source_path,
        group_config=group_config,
        filesystem_metadata=file_state_filesystem_metadata(file_state),
        sidecar_facts=metadata_projection_sidecar_facts(
            upload,
            file_state,
            routing=job_routing(job),
        ),
        tags=tags,
    )


def metadata_projection_tags_for_file(
    job: dict[str, Any],
    file_state: dict[str, Any],
    *,
    group_name: str,
    group_config: dict[str, Any],
) -> list[str]:
    config = metadata_projection_config(group_config)
    tags = list(cast(list[str], config["tags"]))
    if not config["include_context_tags"]:
        return dedup_metadata_projection_tags(tags)

    collection_slug = str(job.get("collection_slug") or "").strip()
    if collection_slug:
        tags.append(f"munchy/collection/{collection_slug}")
    if group_name:
        tags.append(f"munchy/group/{group_name}")
    route_id = str(file_state.get("route_id") or "").strip()
    if route_id:
        tags.append(f"munchy/route/{route_id}")
    group_rel = str(file_state.get("resolved_group_rel") or "").strip()
    if group_rel:
        parent = Path(normalize_posix(group_rel)).parent.as_posix()
        if parent and parent != ".":
            tags.append(f"munchy/output/{parent}")
    pair_kind = str(file_state.get("pair_kind") or "").strip()
    pair_role = str(file_state.get("pair_role") or "").strip()
    if pair_kind:
        tags.append(f"munchy/pair/{pair_kind}")
    if pair_kind and pair_role:
        tags.append(f"munchy/pair/{pair_kind}/{pair_role}")
    return dedup_metadata_projection_tags(tags)


def dedup_metadata_projection_tags(tags: list[str]) -> list[str]:
    return list(dict.fromkeys(tag.strip() for tag in tags if tag.strip()))


def write_atomic_text(path: Path, text: str) -> bool:
    if path.exists() and path.read_text(encoding="utf-8") == text:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)
    return True


def metadata_projection_handed_off_paths(job: dict[str, Any]) -> set[str]:
    paths = uploaded_riverhog_paths(job)
    job_id = str(job.get("job_id") or "")
    if not job_id:
        return paths
    current = read_state("job", job_id)
    if not isinstance(current, dict):
        return paths
    paths.update(uploaded_riverhog_paths(current))
    current_riverhog = current.get("riverhog_session_upload")
    payload_riverhog = job.get("riverhog_session_upload")
    if isinstance(current_riverhog, dict) and isinstance(payload_riverhog, dict):
        job["riverhog_session_upload"] = merge_riverhog_session_upload_state(
            current_riverhog,
            payload_riverhog,
        )
    elif isinstance(current_riverhog, dict):
        job["riverhog_session_upload"] = current_riverhog
    return paths


def write_metadata_projection_sidecars(
    job: dict[str, Any],
    upload: dict[str, Any],
    groups: dict[str, dict[str, Any]],
    archive_dir: Path,
) -> dict[str, Any]:
    sidecars_written = 0
    groups_written: dict[str, int] = {}
    upload_changed = False
    handed_off_paths: set[str] | None = None
    for group_name, group_config in sorted(groups.items()):
        config = metadata_projection_config(group_config)
        if not config["enabled"]:
            continue
        if not group_produces_primary_archive_output(group_config):
            continue
        for file_state in mutable_primary_upload_files_for_groups(upload, {group_name}):
            output = archive_output_path_for_routed_file(
                file_state,
                group_name=group_name,
                group_config=group_config,
                archive_dir=archive_dir,
            )
            if not output.exists():
                if handed_off_paths is None:
                    handed_off_paths = metadata_projection_handed_off_paths(job)
                output_rel = path_relative_to_archive(output, archive_dir)
                if output_rel not in handed_off_paths:
                    raise RuntimeError(
                        "metadata projection output is missing for "
                        f"{file_state.get('path')}: {output}"
                    )
            if ensure_file_projection_metadata(
                upload,
                file_state,
                job=job,
                group_config=group_config,
            ):
                upload_changed = True
            metadata = projection_metadata_for_file_output(
                upload,
                file_state,
                job=job,
                group_name=group_name,
                group_config=group_config,
                output_path=output,
            )
            sidecar = immich_xmp_sidecar_path(output)
            metadata_date = utc_timestamp_now()
            xmp_evidence = (
                xmp_evidence_sidecar_path(upload, file_state)
                if normalize_output_mode(str(group_config.get("output_mode") or "video"))
                == "preserve"
                else None
            )
            if xmp_evidence is not None:
                try:
                    rendered = merge_immich_xmp_sidecar(
                        xmp_evidence.read_text(encoding="utf-8"),
                        metadata,
                        metadata_date=metadata_date,
                    )
                except MetadataProjectionError as exc:
                    raise RuntimeError(
                        f"metadata projection failed for {file_state.get('path')}: {exc}"
                    ) from exc
            else:
                rendered = render_immich_xmp_sidecar(metadata, metadata_date=metadata_date)
            if write_atomic_text(sidecar, rendered):
                sidecars_written += 1
            groups_written[group_name] = groups_written.get(group_name, 0) + 1
            sidecar_rel = sidecar.relative_to(archive_dir).as_posix()
            if file_state.get("metadata_projection_sidecar") != sidecar_rel:
                file_state["metadata_projection_sidecar"] = sidecar_rel
                upload_changed = True
    job["metadata_projection_result"] = {
        "updated_at": utc_timestamp_now(),
        "target": "immich_xmp",
        "sidecars": sum(groups_written.values()),
        "sidecars_written": sidecars_written,
        "groups": groups_written,
    }
    if upload_changed:
        upload = save_input_upload_raw(upload)
    save_job(job)
    return upload


def expected_riverhog_primary_files_total(
    input_upload: dict[str, Any],
    groups: dict[str, dict[str, Any]],
) -> int:
    return sum(
        len(primary_upload_files_for_groups(input_upload, {str(group_name)}))
        for group_name, group_config in groups.items()
        if group_produces_primary_archive_output(group_config)
    )


def routing_path_resolvable(routing: Mapping[str, Any]) -> bool:
    for gate in dict_or_empty(routing.get("gates")).values():
        if isinstance(gate, Mapping) and predicate_requires_non_path_facts(gate):
            return False
    pairings = routing.get("pairings")
    if isinstance(pairings, list):
        for pairing in pairings:
            if not isinstance(pairing, Mapping):
                return False
            key = str(pairing.get("key") or "exif.content_identifier")
            if not key.startswith("path."):
                return False
            for predicate_name in ("still", "movie"):
                predicate = pairing.get(predicate_name)
                if not isinstance(predicate, Mapping):
                    return False
                if predicate_requires_non_path_facts(predicate):
                    return False
    for rule in sidecar_rules(routing):
        if isinstance(rule.get("facts"), Mapping):
            return False
    if not sidecar_rules_are_path_resolvable(routing):
        return False
    for route in routing.get("routes") or []:
        if not isinstance(route, Mapping):
            return False
        when = route.get("when")
        if isinstance(when, Mapping) and predicate_requires_non_path_facts(when):
            return False
    return True


def expected_riverhog_primary_files_total_from_path_routing(
    input_upload: dict[str, Any],
    groups: dict[str, dict[str, Any]],
    routing: Mapping[str, Any],
) -> int | None:
    if not routing_path_resolvable(routing):
        return None
    files = [
        RoutingFile(
            path=str(file_state.get("path") or ""),
            bytes=int(file_state.get("bytes") or 0),
            routing_facts=routing_file_facts(str(file_state.get("path") or "")),
        )
        for file_state in input_upload.get("files", [])
        if isinstance(file_state, Mapping) and str(file_state.get("path") or "")
    ]
    if not files:
        return None
    plan = routing_plan(routing, files, group_names=set(groups))
    if not plan.ok:
        return None
    primary_groups = {
        str(group_name)
        for group_name, group_config in groups.items()
        if group_produces_primary_archive_output(group_config)
    }
    return sum(
        1
        for match in plan.matches
        if match.get("action") == "upload" and str(match.get("group") or "") in primary_groups
    )


def eager_archive_group_names(groups: dict[str, dict[str, Any]]) -> set[str]:
    return {
        str(group_name)
        for group_name, group_config in groups.items()
        if group_is_eager_archive_only(group_config)
    }


def eager_archive_batch_limit(group_config: dict[str, Any]) -> int:
    executor = eager_archive_executor(group_config)
    if executor == "local_audio":
        return AUDIO_ARCHIVE_MAX_PARALLEL
    return EAGER_ARCHIVE_BATCH_FILES


def eager_archive_pipeline_limit(group_config: dict[str, Any]) -> int:
    configured = group_config.get("eager_pipeline_batches")
    if configured is None:
        return EAGER_ARCHIVE_PIPELINE_BATCHES
    try:
        value = int(configured)
    except (TypeError, ValueError):
        return EAGER_ARCHIVE_PIPELINE_BATCHES
    return max(1, min(value, EAGER_ARCHIVE_PIPELINE_BATCHES))


def remove_job_local_work(job: dict[str, Any]) -> list[str]:
    removed: list[str] = []
    for root in gpu_job_work_roots(job):
        if not root.exists():
            continue
        shutil.rmtree(root, ignore_errors=True)
        if root.exists():
            log.warning("failed to remove local job work: %s", root)
            continue
        removed.append(str(root))
    if removed:
        job["local_work_cleaned_at"] = utc_timestamp_now()
        job["local_work_removed"] = removed
    return removed


def compact_command_result(result: Any) -> Any:
    if not isinstance(result, dict):
        return result
    keep_keys = [
        "artifact_count",
        "attempt",
        "collection_id",
        "destination",
        "duration_s",
        "method",
        "mode",
        "returncode",
        "source_label",
        "succeeded_at",
        "wait",
    ]
    compact = {key: result[key] for key in keep_keys if key in result}
    for key in ("stdout", "stderr"):
        value = str(result.get(key) or "")
        if value:
            compact[f"{key}_tail"] = value[-4000:]
    return compact


def compact_list_field(
    job: dict[str, Any],
    key: str,
    *,
    sample: int = 8,
) -> bool:
    value = job.get(key)
    if not isinstance(value, list):
        return False
    count_key = f"{key}_count"
    sample_key = f"{key}_sample"
    changed = False
    if job.get(count_key) != len(value):
        job[count_key] = len(value)
        changed = True
    if len(value) > sample:
        new_sample = value[:sample]
        if job.get(sample_key) != new_sample:
            job[sample_key] = new_sample
            changed = True
        job.pop(key, None)
        changed = True
    return changed


def append_cleanup_removed(job: dict[str, Any], removed: list[str]) -> None:
    if not removed:
        if not job.get("cleanup_completed_at") and "cleanup_removed_count" not in job:
            job["cleanup_removed"] = []
        return

    current = job.get("cleanup_removed")
    if isinstance(current, list):
        job["cleanup_removed"] = current + removed
        return

    count_key = "cleanup_removed_count"
    sample_key = "cleanup_removed_sample"
    if count_key not in job and sample_key not in job:
        job["cleanup_removed"] = removed
        return

    previous_count = int(job.get(count_key) or 0)
    sample = list(job.get(sample_key) or [])
    if len(sample) < 8:
        sample.extend(removed[: 8 - len(sample)])
    job[count_key] = previous_count + len(removed)
    job[sample_key] = sample


def compact_terminal_job_state(job: dict[str, Any]) -> bool:
    if job.get("state") not in TERMINAL_JOB_STATES:
        return False
    changed = snapshot_terminal_progress(job)

    for result_key in (
        "review_handoff_result",
        "collection_archive_target_upload_result",
        "riverhog_upload_result",
    ):
        compact = compact_command_result(job.get(result_key))
        if compact != job.get(result_key):
            job[result_key] = compact
            changed = True

    for key in (
        "eager_archive",
        "gpu_payloads",
        "gpu_result",
        "gpu_results",
        "gpu_statuses",
        "group_results",
        "riverhog_session_upload",
    ):
        if key in job:
            job.pop(key, None)
            changed = True
    if job.get("state") == "canceled" and "cancel_requested" in job:
        job.pop("cancel_requested", None)
        changed = True

    changed = compact_list_field(job, "cleanup_removed") or changed
    changed = compact_list_field(job, "local_work_removed") or changed
    if changed and not job.get("terminal_state_compacted_at"):
        job["terminal_state_compacted_at"] = utc_timestamp_now()
    return changed


RESUMABLE_RUNTIME_JOB_KEYS = (
    *TERMINAL_CLEANUP_JOB_KEYS,
    "collection_archive_target_upload_result",
    "debug_bundle_created_at",
    "debug_bundle_dir",
    "debug_bundle_reason",
    "eager_archive",
    "gpu_payloads",
    "gpu_result",
    "gpu_results",
    "gpu_statuses",
    "group_results",
    "upload_progress",
    "routing_result",
    "review_handoff_result",
    "review_sweep_result",
    "riverhog_handoff_metrics",
    "riverhog_session_upload",
    "riverhog_upload_result",
    "terminal_progress",
    "terminal_state_compacted_at",
)


def reset_resumable_job_runtime_state(job: dict[str, Any]) -> None:
    for key in RESUMABLE_RUNTIME_JOB_KEYS:
        job.pop(key, None)


def cleanup_terminal_job(job: dict[str, Any]) -> list[str]:
    snapshot_terminal_progress(job)
    removed = remove_job_local_work(job)
    job_id = str(job.get("job_id") or "")
    upload_id = str(job.get("input_upload_id") or "")
    if upload_id:
        upload = read_state("input-upload", upload_id)
        if upload is not None and not jobs_referencing_input_upload(
            upload_id,
            exclude_job_id=job_id,
        ):
            remove_input_upload_data(upload)
            delete_state("input-upload", upload_id)
            removed.append(f"input-upload:{upload_id}")
            job["input_upload_deleted_at"] = utc_timestamp_now()
    append_cleanup_removed(job, removed)
    if removed and int(job.get("cleanup_removed_count") or 0) < len(removed):
        job["cleanup_removed"] = removed
        job["cleanup_removed_count"] = len(removed)
        job["cleanup_removed_sample"] = removed[:8]
    job["cleanup_completed_at"] = utc_timestamp_now()
    return removed


def cleanup_canceled_job(job: dict[str, Any]) -> list[str]:
    return cleanup_terminal_job(job)


def snapshot_terminal_progress(job: dict[str, Any]) -> bool:
    changed = False
    if "encode_progress" not in job:
        progress = encode_progress_for_job(job)
        if progress is not None:
            job["encode_progress"] = progress
            changed = True
    if "upload_progress" not in job:
        progress = upload_progress_for_job(job)
        if progress is not None:
            job["upload_progress"] = progress
            changed = True
    if "riverhog_upload_progress" not in job:
        progress = riverhog_upload_progress_for_job(job)
        if progress is not None:
            job["riverhog_upload_progress"] = progress
            changed = True
    return changed


def mark_job_canceled(job: dict[str, Any], *, reason: str) -> dict[str, Any]:
    snapshot_terminal_progress(job)
    canceled_at = job.get("canceled_at") or utc_timestamp_now()
    job["state"] = "canceled"
    job["phase"] = "canceled"
    job["canceled_at"] = canceled_at
    job["finished_at"] = job.get("finished_at") or canceled_at
    job["cleanup_requested"] = True
    job["cancel_reason"] = reason
    job.pop("error", None)
    job.pop("cancel_requested", None)
    return save_job(job)


def finalize_canceled_job(job: dict[str, Any], *, reason: str) -> dict[str, Any]:
    job = mark_job_canceled(job, reason=reason)
    try:
        cancel_riverhog_upload_session(job, reason=reason)
    except Exception as exc:
        job["riverhog_cancel_failed_at"] = utc_timestamp_now()
        job["riverhog_cancel_error"] = str(exc)
        log.exception(
            "unexpected failure while cancelling riverhog session for %s", job.get("job_id")
        )
        save_job(job)

    try:
        cleanup_canceled_job(job)
        compact_terminal_job_state(job)
    except Exception as exc:
        job["cleanup_failed_at"] = utc_timestamp_now()
        job["cleanup_error"] = str(exc)
        log.exception("failed to clean canceled job %s", job.get("job_id"))
    return save_job(job)


def should_cleanup_local_work_on_success(job: dict[str, Any]) -> bool:
    workflow_mode = str(job.get("workflow_mode") or "collection_archive")
    if workflow_mode == "review":
        return True
    collection_archive = dict_or_empty(job.get("collection_archive"))
    if str(collection_archive.get("destination") or "riverhog") == "target":
        return True
    riverhog = job.get("riverhog")
    return isinstance(riverhog, dict) and bool(riverhog.get("enabled"))


def should_cleanup_terminal_local_work(job: dict[str, Any], cutoff: datetime) -> bool:
    state = str(job.get("state") or "")
    if state == "succeeded":
        return should_cleanup_local_work_on_success(job)
    if state == "canceled":
        return True
    if state not in {"failed", "canceled"}:
        return False
    finished_at = safe_parse_timestamp(job.get("finished_at"))
    return finished_at is not None and finished_at <= cutoff


def run_command(
    cmd: list[str],
    *,
    action: str,
    env: dict[str, str] | None = None,
    timeout: float | None = None,
) -> dict[str, Any]:
    started = time.monotonic()
    log.info("%s: %s", action, " ".join(cmd))
    proc = subprocess.run(
        cmd,
        text=True,
        capture_output=True,
        env=env,
        timeout=timeout,
        check=False,
    )
    result = {
        "command": cmd,
        "returncode": proc.returncode,
        "duration_s": round(time.monotonic() - started, 3),
        "stdout": proc.stdout[-4000:],
        "stderr": proc.stderr[-4000:],
    }
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "command failed")[-2000:]
        raise RuntimeError(f"{action} failed with {proc.returncode}: {detail}")
    return result


def notify_webhook_url(recipient: str) -> str | None:
    raw = os.getenv("MUNCHY_RUNNER_NOTIFY_WEBHOOKS", "").strip()
    if raw:
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            log.warning("MUNCHY_RUNNER_NOTIFY_WEBHOOKS is not valid JSON")
            payload = {}
        if isinstance(payload, dict):
            value = payload.get(recipient)
            if isinstance(value, str) and value.strip():
                return value.strip()
    env_suffix = "".join(ch.upper() if ch.isalnum() else "_" for ch in recipient)
    value = os.getenv(f"MUNCHY_RUNNER_NOTIFY_WEBHOOK_{env_suffix}", "").strip()
    return value or None


def notify_payload(
    job: dict[str, Any],
    *,
    event: NotifyEvent,
    message: str,
    severity: str,
    recipient: str,
    extra: dict[str, Any] | None,
) -> dict[str, Any]:
    return dict(
        build_munchy_job_payload(
            event=event,
            job=job,
            message=message,
            severity=severity,
            delivered_at=utc_now(),
            recipient=recipient,
            details=extra,
        )
    )


def notify_recipients(config: dict[str, Any]) -> list[str]:
    return [str(item) for item in config.get("recipients") or [] if str(item).strip()]


def send_notify_deliveries(
    job: dict[str, Any],
    *,
    event: NotifyEvent,
    message: str,
    severity: str,
    recipients: list[str],
    extra: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    deliveries: list[dict[str, Any]] = []
    for recipient in recipients:
        url = notify_webhook_url(recipient)
        if not url:
            deliveries.append({"recipient": recipient, "status": "missing_webhook"})
            log.warning("notification recipient %s has no configured webhook", recipient)
            continue
        payload = notify_payload(
            job,
            event=event,
            message=message,
            severity=severity,
            recipient=recipient,
            extra=extra,
        )
        try:
            with httpx.Client(timeout=NOTIFY_TIMEOUT_SECONDS) as client:
                response = client.post(url, json=payload)
            deliveries.append({"recipient": recipient, "status": response.status_code})
            if response.status_code >= 400:
                log.warning(
                    "notification webhook for %s returned HTTP %s", recipient, response.status_code
                )
        except Exception as exc:
            deliveries.append({"recipient": recipient, "status": "error", "error": str(exc)})
            log.warning("notification webhook for %s failed: %s", recipient, exc)
    return deliveries


def notify_job_event(
    job: dict[str, Any],
    event: NotifyEvent,
    message: str,
    *,
    severity: str = "info",
    extra: dict[str, Any] | None = None,
    dedupe_key: str | None = None,
    fingerprint: str | None = None,
) -> dict[str, Any] | None:
    config = dict_or_empty(job.get("notify"))
    if not NOTIFY_ENABLED or not config.get("enabled"):
        return None
    events = config.get("events") or DEFAULT_NOTIFY_EVENTS
    if event not in events:
        return None
    recipients = notify_recipients(config)
    if not recipients:
        return None

    key = dedupe_key or event
    notifications = job.setdefault("notifications", {})
    event_state = notifications.setdefault(key, {})
    now = utc_now()
    now_text = format_utc_timestamp(now)
    send_extra = dict(extra or {})

    if event == "job.issue":
        last_fingerprint = str(event_state.get("fingerprint") or "")
        last_attempt = safe_parse_timestamp(event_state.get("last_attempt_at"))
        if (
            fingerprint
            and fingerprint == last_fingerprint
            and last_attempt is not None
            and not operator_reminder_due(
                last_sent_at=last_attempt,
                current=now,
                interval=NOTIFY_REMINDER_INTERVAL_SECONDS,
                reminder_time=NOTIFY_REMINDER_TIME,
                reminder_timezone=NOTIFY_REMINDER_TIMEZONE,
            )
        ):
            return {"status": "suppressed", "reason": "issue_repeat_limit"}
        event_state["fingerprint"] = fingerprint or ""
        event_state["last_attempt_at"] = now_text
    elif event == "job.upload_waiting.reminder":
        interval = max(0, NOTIFY_REMINDER_INTERVAL_SECONDS)
        if interval <= 0:
            return {"status": "suppressed", "reason": "reminders_disabled"}
        last_attempt = safe_parse_timestamp(event_state.get("last_attempt_at"))
        if last_attempt is not None and not operator_reminder_due(
            last_sent_at=last_attempt,
            current=now,
            interval=interval,
            reminder_time=NOTIFY_REMINDER_TIME,
            reminder_timezone=NOTIFY_REMINDER_TIMEZONE,
        ):
            return {"status": "suppressed", "reason": "reminder_repeat_limit"}
        event_state["last_attempt_at"] = now_text
        reminder_count = int(event_state.get("reminder_count") or 0) + 1
        event_state["reminder_count"] = reminder_count
        send_extra.setdefault("reminder_count", reminder_count)
        send_extra.setdefault("reminder_interval_seconds", interval)
    elif event_state.get("sent_at"):
        return {"status": "suppressed", "reason": "already_sent"}
    else:
        event_state["last_attempt_at"] = now_text

    deliveries = send_notify_deliveries(
        job,
        event=event,
        message=message,
        severity=severity,
        recipients=recipients,
        extra=send_extra,
    )

    event_state["deliveries"] = deliveries
    if any(
        isinstance(item.get("status"), int) and int(item["status"]) < 400 for item in deliveries
    ):
        if event in {"job.issue", "job.upload_waiting.reminder"}:
            event_state["last_sent_at"] = now_text
        else:
            event_state["sent_at"] = now_text
    save_job(job)
    return {"status": "attempted", "deliveries": deliveries}


def notify_job_issue(
    job: dict[str, Any],
    *,
    component: str,
    error: Exception | str,
    severity: str = "warning",
    attempt: int | None = None,
    next_retry_at: str | None = None,
) -> dict[str, Any] | None:
    error_text = str(error)
    fingerprint = hashlib.sha256(f"{component}:{error_text}".encode()).hexdigest()
    extra: dict[str, Any] = {
        "component": component,
        "error": error_text[-1000:],
    }
    if attempt is not None:
        extra["attempt"] = attempt
    if next_retry_at:
        extra["next_retry_at"] = next_retry_at
    return notify_job_event(
        job,
        "job.issue",
        f"{component} needs attention: {error_text[-240:]}",
        severity=severity,
        extra=extra,
        dedupe_key=f"job.issue:{component}",
        fingerprint=fingerprint,
    )


def notify_upload_waiting_reminder(
    job: dict[str, Any],
    upload: dict[str, Any],
    progress: dict[str, Any],
) -> dict[str, Any] | None:
    interval = max(0, NOTIFY_REMINDER_INTERVAL_SECONDS)
    if interval <= 0:
        return None
    if int(progress.get("files_uploaded") or 0) >= int(progress.get("files_total") or 0):
        return None

    now = datetime.now(UTC)
    last_activity = input_upload_data_last_activity(upload)
    stalled_seconds = max(0.0, (now - last_activity).total_seconds())
    if stalled_seconds < interval:
        return None
    next_due = next_operator_reminder_at(
        last_activity,
        interval=interval,
        reminder_time=NOTIFY_REMINDER_TIME,
        reminder_timezone=NOTIFY_REMINDER_TIMEZONE,
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
    return notify_job_event(
        job,
        "job.upload_waiting.reminder",
        message,
        severity="warning",
        extra=extra,
    )


def retry_sleep(seconds: float, *, job_id: str | None = None) -> None:
    deadline = time.monotonic() + max(0.0, seconds)
    while True:
        if job_id is not None:
            raise_if_job_canceled(job_id)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        time.sleep(min(5.0, remaining))


def retry_handoff_until_success(
    job: dict[str, Any],
    *,
    result_key: str,
    phase: str,
    action: str,
    component: str,
    operation: Callable[[], dict[str, Any]],
) -> dict[str, Any]:
    existing = job.get(result_key)
    if isinstance(existing, dict):
        return existing
    delay = max(1.0, HANDOFF_RETRY_INITIAL_SECONDS)
    max_delay = max(delay, HANDOFF_RETRY_MAX_SECONDS)
    job_id = str(job["job_id"])
    while True:
        raise_if_job_canceled(job_id)
        latest = read_state("job", job_id)
        if isinstance(latest, dict):
            job.clear()
            job.update(latest)
            existing = job.get(result_key)
            if isinstance(existing, dict):
                return existing
        attempts = job.setdefault("handoff_attempts", {})
        attempt = int(attempts.get(result_key) or 0) + 1
        attempts[result_key] = attempt
        attempts[f"{result_key}_last_attempt_at"] = utc_timestamp_now()
        job["phase"] = phase if attempt == 1 else f"{phase}_retrying"
        save_job(job)
        try:
            result = operation()
            result["attempt"] = attempt
            result["succeeded_at"] = utc_timestamp_now()
            job[result_key] = result
            job["phase"] = phase
            attempts[f"{result_key}_succeeded_at"] = result["succeeded_at"]
            attempts.pop(f"{result_key}_next_retry_at", None)
            attempts.pop(f"{result_key}_last_error", None)
            save_job(job)
            return result
        except JobCanceled:
            raise
        except Exception as exc:
            next_retry_at = format_utc_timestamp(utc_now() + timedelta(seconds=delay))
            attempts[f"{result_key}_last_error"] = str(exc)
            attempts[f"{result_key}_next_retry_at"] = next_retry_at
            job["phase"] = f"{phase}_retrying"
            save_job(job)
            log.warning(
                "%s attempt %s failed; retrying at %s: %s", action, attempt, next_retry_at, exc
            )
            retry_sleep(delay, job_id=job_id)
            delay = min(max_delay, delay * 2)


def manager_request(
    method: str, path: str, payload: dict[str, Any] | None = None
) -> dict[str, Any]:
    with httpx.Client(timeout=None) as client:
        response = client.request(method, f"{GPU_MANAGER_URL}{path}", json=payload)
    if response.status_code >= 400:
        raise RuntimeError(f"gpu manager returned {response.status_code}: {response.text}")
    data = response.json()
    if not isinstance(data, dict):
        raise RuntimeError("gpu manager returned non-object JSON")
    return data


def acquire_gpu(job_id: str, lease_token: str = "") -> str:
    token = lease_token
    deadline = time.monotonic() + GPU_LEASE_TTL_S
    while time.monotonic() < deadline:
        raise_if_job_canceled(job_id)
        payload = {
            "target": GPU_TARGET,
            "owner": f"munchy-runner:{job_id}",
            "lease_token": token,
            "lease_ttl_s": GPU_LEASE_TTL_S,
            "wait_s": GPU_WAIT_S,
            "wait_ready": True,
            "priority": 0,
        }
        try:
            result = manager_request("POST", "/acquire", payload)
        except RuntimeError as exc:
            if "gpu busy" not in str(exc) and "queued" not in str(exc):
                raise
            retry_sleep(5, job_id=job_id)
            continue
        token = str(result.get("lease_token") or token)
        if not result.get("queued"):
            return token
        retry_sleep(5, job_id=job_id)
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
    save_job(job)
    return token


def release_job_gpu(job: dict[str, Any], token: str) -> None:
    if release_gpu(token):
        if job.get("gpu_lease_token") == token:
            job.pop("gpu_lease_token", None)
            job["gpu_lease_released_at"] = utc_timestamp_now()
            save_job(job)


def gpu_target_request(
    method: str, path: str, payload: dict[str, Any] | None = None
) -> dict[str, Any]:
    with httpx.Client(timeout=60.0) as client:
        response = client.request(method, f"{GPU_TARGET_URL}{path}", json=payload)
    if response.status_code >= 400:
        raise RuntimeError(f"gpu target returned {response.status_code}: {response.text}")
    data = response.json()
    if not isinstance(data, dict):
        raise RuntimeError("gpu target returned non-object JSON")
    return data


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
    save_job(job)


def wait_gpu_job(
    gpu_job_id: str,
    *,
    gpu_payload: dict[str, Any],
    job: dict[str, Any],
) -> dict[str, Any]:
    job_id = str(job["job_id"])
    next_repost = time.monotonic() + max(30.0, GPU_REPOST_SECONDS)
    while True:
        raise_if_job_canceled(job_id)
        try:
            status = gpu_target_request("GET", f"/v1/jobs/{gpu_job_id}")
        except Exception as exc:
            log.warning("gpu target status check failed; retrying: %s", exc)
            retry_sleep(15)
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
                next_repost = time.monotonic() + max(30.0, GPU_REPOST_SECONDS)
                time.sleep(5)
                continue
            error = f"gpu job failed: {status.get('error')}"
            notify_job_issue(job, component="encoding", error=error, severity="critical")
            raise EncodingFailed(error)
        if time.monotonic() >= next_repost:
            try:
                start_gpu_job(gpu_payload)
            except Exception as exc:
                log.warning("gpu target re-submit failed; retrying: %s", exc)
            next_repost = time.monotonic() + max(30.0, GPU_REPOST_SECONDS)
        time.sleep(5)


def riverhog_config_enabled(job: dict[str, Any]) -> bool:
    riverhog = job.get("riverhog")
    return isinstance(riverhog, dict) and bool(riverhog.get("enabled"))


def riverhog_collection_id_for_job(job: dict[str, Any]) -> str | None:
    state = job.get("riverhog_session_upload")
    if isinstance(state, dict) and state.get("collection_id"):
        return str(state["collection_id"])
    for key in ("riverhog_upload_result", "riverhog_upload_progress"):
        value = job.get(key)
        if isinstance(value, dict) and value.get("collection_id"):
            return str(value["collection_id"])
    return derived_riverhog_collection_id(job)


def derived_riverhog_collection_id(job: dict[str, Any]) -> str | None:
    timestamp = str(job.get("collection_timestamp") or "").strip()
    slug = str(job.get("collection_slug") or "").strip()
    if len(timestamp) < 4 or not slug:
        return None
    return f"{timestamp[:4]}/{timestamp}__{slug}"


def riverhog_session_state(job: dict[str, Any]) -> dict[str, Any]:
    state = job.setdefault("riverhog_session_upload", {})
    if not isinstance(state, dict):
        state = {}
        job["riverhog_session_upload"] = state
    state.setdefault("state", "not_started")
    state.setdefault("files", {})
    return state


def compact_riverhog_payload(payload: dict[str, Any]) -> dict[str, Any]:
    keep_keys = [
        "collection_id",
        "state",
        "files_total",
        "files_pending",
        "files_partial",
        "files_uploaded",
        "bytes_total",
        "uploaded_bytes",
        "missing_bytes",
        "upload_state_expires_at",
        "latest_failure",
        "archive_phase",
        "archive_phase_updated_at",
        "archive_uploaded_bytes",
        "archive_total_bytes",
        "archive_uploaded_parts",
        "archive_total_parts",
        "archive_store",
        "retain_hot",
        "hot_materialized_files",
        "hot_materialized_bytes",
    ]
    return {key: payload[key] for key in keep_keys if key in payload}


def finalized_riverhog_payload_from_collection(
    collection_id: str,
    collection: dict[str, Any],
) -> dict[str, Any]:
    files_total = int(collection.get("files") or 0)
    bytes_total = int(collection.get("bytes") or 0)
    archive_copies = collection.get("archive_copies")
    archive = archive_copies[0] if isinstance(archive_copies, list) and archive_copies else None
    archived_bytes = 0
    if isinstance(archive, dict):
        archived_bytes = int(archive.get("stored_bytes") or 0)
    return {
        "collection_id": collection_id,
        "state": "finalized",
        "archive_store": archive.get("store") if isinstance(archive, dict) else None,
        "retain_hot": int(collection.get("hot_files") or 0) == files_total,
        "files_total": files_total,
        "files_pending": 0,
        "files_partial": 0,
        "files_uploaded": files_total,
        "hot_materialized_files": int(collection.get("hot_files") or 0),
        "bytes_total": bytes_total,
        "uploaded_bytes": bytes_total,
        "hot_materialized_bytes": int(collection.get("hot_bytes") or 0),
        "missing_bytes": 0,
        "upload_state_expires_at": None,
        "latest_failure": None,
        "archive_phase": "completed",
        "archive_uploaded_bytes": archived_bytes or bytes_total,
        "archive_total_bytes": archived_bytes or bytes_total,
        "archive_uploaded_parts": None,
        "archive_total_parts": None,
        "collection": collection,
    }


def update_riverhog_state_from_payload(
    job: dict[str, Any],
    payload: dict[str, Any],
) -> dict[str, Any]:
    with riverhog_upload_lock(str(job.get("job_id") or "")):
        state = riverhog_session_state(job)
        if payload.get("collection_id"):
            state["collection_id"] = str(payload["collection_id"])
        if payload.get("state"):
            state["riverhog_state"] = str(payload["state"])
            state["state"] = str(payload["state"])
        state["last_payload"] = compact_riverhog_payload(payload)
        state["updated_at"] = utc_timestamp_now()

        files = state.setdefault("files", {})
        if isinstance(files, dict):
            file_items: list[dict[str, Any]] = []
            single_file = payload.get("file")
            if isinstance(single_file, dict):
                file_items.append(single_file)
            for item in payload.get("files") or []:
                if isinstance(item, dict):
                    file_items.append(item)
            for item in file_items:
                if not item.get("path"):
                    continue
                rel_path = str(item["path"])
                record = files.setdefault(rel_path, {"path": rel_path})
                if not isinstance(record, dict):
                    record = {"path": rel_path}
                    files[rel_path] = record
                record["bytes"] = int(item.get("bytes") or record.get("bytes") or 0)
                if item.get("sha256"):
                    record["sha256"] = str(item["sha256"])
                existing_uploaded = int(record.get("uploaded_bytes") or 0)
                incoming_uploaded = int(item.get("uploaded_bytes") or 0)
                record["uploaded_bytes"] = max(existing_uploaded, incoming_uploaded)
                record["upload_state"] = str(item.get("upload_state") or "")
                if (
                    int(record.get("bytes") or 0) > 0
                    and int(record.get("uploaded_bytes") or 0) >= int(record.get("bytes") or 0)
                    and record.get("state") not in {"deleted", "uploaded"}
                ):
                    record["state"] = "uploaded"
                    record["uploaded_at"] = record.get("uploaded_at") or utc_timestamp_now()
        return state


def sync_riverhog_session_from_remote(job: dict[str, Any], api: ApiClient) -> dict[str, Any] | None:
    collection_id = riverhog_collection_id_for_job(job) or ""
    if not collection_id:
        return None
    state = riverhog_session_state(job)
    state["collection_id"] = collection_id
    try:
        payload = api.get_collection_upload(collection_id)
    except NotFound:
        collection = api.get_collection(collection_id)
        payload = finalized_riverhog_payload_from_collection(collection_id, collection)
    update_riverhog_state_from_payload(job, payload)
    return payload


def refresh_riverhog_session_from_remote(job: dict[str, Any]) -> None:
    if not riverhog_config_enabled(job):
        return
    collection_id = riverhog_collection_id_for_job(job)
    if not collection_id:
        return
    state = riverhog_session_state(job)
    state["collection_id"] = collection_id
    api = ApiClient()
    try:
        sync_riverhog_session_from_remote(job, api)
        save_job(job)
    except Exception as exc:
        log.debug("riverhog session status refresh failed for %s: %s", job.get("job_id"), exc)
    finally:
        close = getattr(api, "close", None)
        if callable(close):
            close()


def touch_riverhog_session_state(job: dict[str, Any]) -> None:
    with riverhog_upload_lock(str(job.get("job_id") or "")):
        riverhog_session_state(job)["updated_at"] = utc_timestamp_now()


def riverhog_collection_notify_config(job: dict[str, Any]) -> dict[str, Any] | None:
    notify = dict_or_empty(job.get("notify"))
    if not notify:
        return None
    return {
        "enabled": bool(notify.get("enabled", DEFAULT_NOTIFY_ENABLED)),
        "recipients": notify_recipients(notify),
    }


def ensure_riverhog_session(
    job: dict[str, Any],
    api: ApiClient,
    archive_dir: Path,
) -> str:
    if not riverhog_config_enabled(job):
        raise RuntimeError("riverhog upload is not enabled for this job")
    if not RIVERHOG_UPLOAD_ENABLED:
        raise RuntimeError("riverhog upload requested, but runner Riverhog upload is disabled")
    timestamp = job.get("collection_timestamp")
    if not timestamp:
        raise RuntimeError("riverhog upload requires collection_timestamp")

    lock = riverhog_upload_lock(str(job.get("job_id") or ""))
    with lock:
        state = riverhog_session_state(job)
        collection_id = str(state.get("collection_id") or "")
        if collection_id:
            return collection_id

        payload = api.create_or_resume_collection_upload_session(
            str(job["collection_slug"]),
            ingest_source=str(archive_dir),
            upload_timestamp=str(timestamp),
            archive_store=cast(
                str | None,
                dict_or_empty(job.get("riverhog")).get("archive_store"),
            ),
            retain_hot=bool(dict_or_empty(job.get("riverhog")).get("retain_hot", True)),
            notify=riverhog_collection_notify_config(job),
        )
        update_riverhog_state_from_payload(job, payload)
        state = riverhog_session_state(job)
        state["opened_at"] = state.get("opened_at") or utc_timestamp_now()
        save_job(job)
        collection_id = str(state.get("collection_id") or "")
        if not collection_id:
            raise RuntimeError("riverhog upload session did not return a collection_id")
        return collection_id


def riverhog_file_record(
    job: dict[str, Any],
    archive_dir: Path,
    source_path: Path,
) -> dict[str, Any]:
    rel_path = source_path.relative_to(archive_dir).as_posix()
    if not source_path.exists():
        raise RuntimeError(f"riverhog upload source file disappeared before upload: {source_path}")
    stat = source_path.stat()
    lock = riverhog_upload_lock(str(job.get("job_id") or ""))
    with lock:
        state = riverhog_session_state(job)
        files = state.setdefault("files", {})
        if not isinstance(files, dict):
            files = {}
            state["files"] = files
        record = files.setdefault(rel_path, {"path": rel_path})
        if not isinstance(record, dict):
            record = {"path": rel_path}
            files[rel_path] = record
        if record.get("state") in {"uploaded", "deleted"}:
            return record
        existing_bytes = int(record.get("bytes") or 0)
        if existing_bytes and existing_bytes != stat.st_size:
            raise RuntimeError(f"riverhog upload file size changed after registration: {rel_path}")
        record["path"] = rel_path
        record["source"] = str(source_path)
        record["bytes"] = stat.st_size
        needs_sha256 = not record.get("sha256")
        record["state"] = record.get("state") or "pending"
        touch_riverhog_session_state(job)
    if needs_sha256:
        digest = file_sha256(source_path)
        with lock:
            state = riverhog_session_state(job)
            files = state.setdefault("files", {})
            if not isinstance(files, dict):
                files = {}
                state["files"] = files
            record = files.setdefault(rel_path, {"path": rel_path})
            if isinstance(record, dict) and not record.get("sha256"):
                record["sha256"] = digest
                touch_riverhog_session_state(job)
    with lock:
        files = dict_or_empty(riverhog_session_state(job).get("files"))
        record = files.get(rel_path)
        if isinstance(record, dict):
            return cast(dict[str, Any], record)
    raise RuntimeError(f"missing Riverhog file record for {rel_path}")


def riverhog_upload_file_complete(record: dict[str, Any]) -> bool:
    bytes_total = int(record.get("bytes") or 0)
    return record.get("state") in {"uploaded", "deleted"} or (
        bytes_total > 0 and int(record.get("uploaded_bytes") or 0) >= bytes_total
    )


def riverhog_payload_confirms_file_uploaded(
    payload: dict[str, Any],
    rel_path: str,
    length: int,
) -> bool:
    items: list[dict[str, Any]] = []
    single_file = payload.get("file")
    if isinstance(single_file, dict):
        items.append(single_file)
    files = payload.get("files")
    if isinstance(files, list):
        items.extend(item for item in files if isinstance(item, dict))
    for item in items:
        if str(item.get("path") or "") != rel_path:
            continue
        uploaded = int(item.get("uploaded_bytes") or 0)
        state = str(item.get("upload_state") or "")
        return uploaded >= length and state == "uploaded"
    return False


def confirm_riverhog_artifact_uploaded(
    job: dict[str, Any],
    api: ApiClient,
    collection_id: str,
    file_payload: dict[str, object],
) -> dict[str, Any]:
    rel_path = str(file_payload["path"])
    length_value = file_payload.get("bytes")
    if not isinstance(length_value, int):
        length_value = int(str(length_value))
    length = length_value
    payload = api.create_or_resume_registered_collection_file_upload(collection_id, file_payload)
    update_riverhog_state_from_payload(job, payload)
    if not riverhog_payload_confirms_file_uploaded(payload, rel_path, length):
        raise RuntimeError(f"riverhog did not acknowledge completed upload for {rel_path}")
    return payload


def remove_uploaded_riverhog_artifact(
    job: dict[str, Any],
    archive_dir: Path,
    source_path: Path,
    record: dict[str, Any],
    *,
    persist: bool = True,
) -> None:
    if source_path.exists():
        source_path.unlink()
    parent = source_path.parent
    while parent != archive_dir and parent.is_dir():
        try:
            parent.rmdir()
        except OSError:
            break
        parent = parent.parent
    with riverhog_upload_lock(str(job.get("job_id") or "")):
        record["state"] = "deleted"
        record["deleted_at"] = record.get("deleted_at") or utc_timestamp_now()
        touch_riverhog_session_state(job)
    log.debug(
        "riverhog upload accepted; removed local artifact job=%s path=%s bytes=%s",
        job.get("job_id"),
        record.get("path"),
        format_log_bytes(record.get("bytes")),
    )
    if persist:
        save_job(job)


def riverhog_upload_artifact(
    job: dict[str, Any],
    api: ApiClient,
    archive_dir: Path,
    source_path: Path,
    *,
    persist: bool = True,
) -> bool:
    job_id = str(job["job_id"])
    collection_id = ensure_riverhog_session(job, api, archive_dir)
    record = riverhog_file_record(job, archive_dir, source_path)
    rel_path = str(record["path"])
    length = int(record["bytes"])
    if riverhog_upload_file_complete(record):
        if source_path.exists() and record.get("state") != "deleted":
            remove_uploaded_riverhog_artifact(
                job,
                archive_dir,
                source_path,
                record,
                persist=persist,
            )
            return True
        return False

    file_payload = {
        "path": rel_path,
        "bytes": length,
        "sha256": str(record["sha256"]),
    }
    session = api.create_or_resume_registered_collection_file_upload(collection_id, file_payload)
    update_riverhog_state_from_payload(job, session)
    with riverhog_upload_lock(job_id):
        record = (
            riverhog_session_state(job)
            .setdefault("files", {})
            .setdefault(
                rel_path,
                {"path": rel_path},
            )
        )
        if not isinstance(record, dict):
            record = {"path": rel_path}
            riverhog_session_state(job).setdefault("files", {})[rel_path] = record
        record["registered_at"] = record.get("registered_at") or utc_timestamp_now()
        record["state"] = "registered"
        touch_riverhog_session_state(job)
    offset = int(session["offset"])
    if offset > length:
        raise RuntimeError(f"riverhog upload offset for {rel_path} is past expected length")
    with riverhog_upload_lock(job_id):
        record["uploaded_bytes"] = max(int(record.get("uploaded_bytes") or 0), offset)
        record["state"] = "uploading" if offset < length else "uploaded"
        touch_riverhog_session_state(job)

    if offset >= length:
        confirm_riverhog_artifact_uploaded(job, api, collection_id, file_payload)
        with riverhog_upload_lock(job_id):
            record["uploaded_at"] = record.get("uploaded_at") or utc_timestamp_now()
        remove_uploaded_riverhog_artifact(
            job,
            archive_dir,
            source_path,
            record,
            persist=persist,
        )
        return True

    while offset < length:

        def mark_progress(bytes_sent: int) -> None:
            nonlocal offset
            offset += bytes_sent
            with riverhog_upload_lock(job_id):
                record["uploaded_bytes"] = max(int(record.get("uploaded_bytes") or 0), offset)
                record["state"] = "uploading"
                touch_riverhog_session_state(job)

        try:
            result = upload_path_to_tus(
                client=api.tus_client(),
                source_path=source_path,
                lease=TusUploadLease(
                    upload_url=str(session["upload_url"]),
                    offset=offset,
                    length=length,
                    checksum_algorithm=str(session["checksum_algorithm"]),
                ),
                chunk_bytes=RIVERHOG_UPLOAD_CHUNK_BYTES,
                cancel_check=lambda: raise_if_job_canceled(job_id),
                progress=mark_progress,
            )
        except (httpx.TransportError, Conflict, ServiceUnavailable) as exc:
            log.warning(
                "riverhog upload interrupted job=%s path=%s offset=%s: %s",
                job_id,
                rel_path,
                offset,
                exc,
            )
            session = api.create_or_resume_collection_file_upload(collection_id, rel_path)
            recovered_offset = int(session["offset"])
            if recovered_offset < offset:
                raise RuntimeError(
                    f"riverhog upload offset for {rel_path} moved backward to "
                    f"{recovered_offset}; expected at least {offset}"
                ) from exc
            if recovered_offset > length:
                raise RuntimeError(
                    f"riverhog upload offset for {rel_path} is past expected length"
                ) from exc
            offset = recovered_offset
            with riverhog_upload_lock(job_id):
                record["uploaded_bytes"] = max(int(record.get("uploaded_bytes") or 0), offset)
                touch_riverhog_session_state(job)
            continue
        offset = result.offset

    if offset != length:
        raise RuntimeError(f"riverhog upload for {rel_path} stopped at {offset} of {length} bytes")
    confirm_riverhog_artifact_uploaded(job, api, collection_id, file_payload)
    with riverhog_upload_lock(job_id):
        record["uploaded_bytes"] = length
        record["state"] = "uploaded"
        record["uploaded_at"] = record.get("uploaded_at") or utc_timestamp_now()
        touch_riverhog_session_state(job)
    remove_uploaded_riverhog_artifact(
        job,
        archive_dir,
        source_path,
        record,
        persist=persist,
    )
    return True


def source_artifact_sidecar_for_archive_output(output: Path) -> Path:
    return Path(f"{output}.source-artifacts.tar.zst")


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
    args.extend(["-b:a", str(audio.get("bitrate") or ARCHIVE_AUDIO_BITRATE)])
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
    filesystem_metadata: Mapping[str, Any],
) -> ProjectionMetadata | None:
    if not metadata_projection_enabled(group_config):
        return None
    return projection_metadata_from_source(
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
    if not isinstance(metadata, Mapping):
        raise RuntimeError(
            f"unresumable: source filesystem metadata sidecar is missing entries for {rel_path}"
        )
    audio_metadata = audio_archive_metadata_for_source(
        source,
        rel_path=rel_path,
        group_config=group_config,
        filesystem_metadata=metadata,
    )
    sidecar = source_artifact_sidecar_for_archive_output(dest)
    if dest.is_file() and sidecar.is_file():
        return {
            "source": str(source),
            "output": str(dest),
            "bytes": dest.stat().st_size,
            "sha256": file_sha256(dest),
            "source_artifacts": {"path": str(sidecar), "reused": True},
            "container_metadata": audio_metadata.as_dict() if audio_metadata else None,
            "reused": True,
        }
    result = run_command(
        archive_audio_command(source, dest, group_config, metadata=audio_metadata),
        action="archive audio",
    )
    artifacts = build_strict_source_artifacts(
        source=source,
        archive_mkv=dest,
        encode_command=cast(list[str], result["command"]),
        encode_profile=profile,
        source_filesystem_metadata=metadata,
        source_sidecars=source_sidecars,
    )
    return {
        "source": str(source),
        "output": str(dest),
        "command": result["command"],
        "duration_s": result["duration_s"],
        "bytes": dest.stat().st_size if dest.exists() else 0,
        "sha256": file_sha256(dest) if dest.exists() else "",
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
    if not filesystem_metadata:
        raise RuntimeError(
            "unresumable: source filesystem metadata sidecar is missing for audio archive group"
        )
    output_root.mkdir(parents=True, exist_ok=True)
    items: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=AUDIO_ARCHIVE_MAX_PARALLEL) as pool:
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


def eager_riverhog_artifact_paths(job: dict[str, Any]) -> list[Path]:
    eager = job.get("eager_archive")
    if not isinstance(eager, dict):
        return []
    files = eager.get("files")
    if not isinstance(files, dict):
        return []
    paths: list[Path] = []
    for item in files.values():
        if not isinstance(item, dict) or item.get("state") != "encoded":
            continue
        output_value = item.get("output")
        if not output_value:
            continue
        output = Path(str(output_value))
        if output.exists():
            paths.append(output)
        sidecar = source_artifact_sidecar_for_archive_output(output)
        if sidecar.exists():
            paths.append(sidecar)
    return paths


def archive_dir_artifact_paths(archive_dir: Path) -> list[Path]:
    if not archive_dir.is_dir():
        return []
    return sorted(path for path in archive_dir.rglob("*") if path.is_file())


def uploaded_riverhog_paths(job: dict[str, Any]) -> set[str]:
    state = job.get("riverhog_session_upload")
    if not isinstance(state, dict):
        return set()
    files = state.get("files")
    if not isinstance(files, dict):
        return set()
    uploaded: set[str] = set()
    for rel_path, item in files.items():
        if isinstance(item, dict) and riverhog_upload_file_complete(item):
            uploaded.add(str(rel_path))
    return uploaded


def zero_riverhog_upload_metrics(started: float | None = None) -> dict[str, int | float]:
    elapsed = 0.0 if started is None else round(max(0.0, time.monotonic() - started), 6)
    return {
        "processed_files": 0,
        "uploaded_files": 0,
        "uploaded_bytes": 0,
        "elapsed_seconds": elapsed,
    }


def path_relative_to_archive(path: Path, archive_dir: Path) -> str | None:
    try:
        return path.relative_to(archive_dir).as_posix()
    except ValueError:
        return None


def primary_archive_output_paths(job: dict[str, Any], archive_dir: Path) -> list[str]:
    eager = job.get("eager_archive")
    if not isinstance(eager, dict):
        return []
    files = eager.get("files")
    if not isinstance(files, dict):
        return []
    paths: list[str] = []
    seen: set[str] = set()
    for item in files.values():
        if not isinstance(item, dict) or item.get("state") != "encoded":
            continue
        output_value = item.get("output")
        if not output_value:
            continue
        rel_path = path_relative_to_archive(Path(str(output_value)), archive_dir)
        if rel_path is None or rel_path in seen:
            continue
        seen.add(rel_path)
        paths.append(rel_path)
    return paths


def riverhog_artifact_paths(
    job: dict[str, Any],
    archive_dir: Path,
    *,
    final: bool,
) -> list[Path]:
    paths = archive_dir_artifact_paths(archive_dir) if final else eager_riverhog_artifact_paths(job)
    seen: set[str] = set()
    ordered: list[Path] = []
    uploaded = uploaded_riverhog_paths(job)
    for path in sorted(paths):
        if not path.exists() or not path.is_file():
            continue
        rel_path = path.relative_to(archive_dir).as_posix()
        if rel_path in uploaded or rel_path in seen:
            continue
        seen.add(rel_path)
        ordered.append(path)
    return ordered


def upload_riverhog_artifacts(
    job: dict[str, Any],
    archive_dir: Path,
    *,
    final: bool,
    max_files: int | None = None,
    max_bytes: int | None = None,
    max_seconds: float | None = None,
) -> dict[str, int | float]:
    if not riverhog_config_enabled(job):
        return zero_riverhog_upload_metrics()
    job_id = str(job["job_id"])
    lock = riverhog_upload_call_lock(job_id)
    with lock:
        if not final:
            current = read_state("job", job_id)
            if isinstance(current, dict):
                if not riverhog_eager_upload_allowed(current):
                    return zero_riverhog_upload_metrics()
                job.clear()
                job.update(current)
        return _upload_riverhog_artifacts_unlocked(
            job,
            archive_dir,
            final=final,
            max_files=max_files,
            max_bytes=max_bytes,
            max_seconds=max_seconds,
        )


def _upload_riverhog_artifacts_unlocked(
    job: dict[str, Any],
    archive_dir: Path,
    *,
    final: bool,
    max_files: int | None = None,
    max_bytes: int | None = None,
    max_seconds: float | None = None,
) -> dict[str, int | float]:
    uploaded = 0
    processed = 0
    uploaded_bytes = 0
    started = time.monotonic()
    selected: list[tuple[Path, int]] = []
    for source_path in riverhog_artifact_paths(job, archive_dir, final=final):
        elapsed = time.monotonic() - started
        if max_files is not None and len(selected) >= max_files:
            break
        if max_seconds is not None and selected and elapsed >= max_seconds:
            break
        source_bytes = source_path.stat().st_size if source_path.exists() else 0
        if (
            max_bytes is not None
            and selected
            and sum(item[1] for item in selected) + source_bytes > max_bytes
        ):
            break
        selected.append((source_path, source_bytes))
    if not selected:
        return {
            "processed_files": 0,
            "uploaded_files": 0,
            "uploaded_bytes": 0,
            "elapsed_seconds": round(max(0.0, time.monotonic() - started), 6),
        }

    worker_count = min(max(1, RIVERHOG_UPLOAD_WORKERS), len(selected))
    last_save_at = started

    def persist_progress_if_due(*, force: bool = False) -> None:
        nonlocal last_save_at
        now = time.monotonic()
        if (
            force
            or processed % RIVERHOG_UPLOAD_SAVE_EVERY_FILES == 0
            or now - last_save_at >= RIVERHOG_UPLOAD_SAVE_EVERY_SECONDS
        ):
            save_job(job)
            last_save_at = now

    worker_clients: list[ApiClient] = []
    worker_clients_lock = threading.Lock()
    worker_local = threading.local()

    def api_for_worker() -> ApiClient:
        worker_api = getattr(worker_local, "riverhog_api", None)
        if worker_api is None:
            worker_api = ApiClient()
            worker_local.riverhog_api = worker_api
            with worker_clients_lock:
                worker_clients.append(worker_api)
        return worker_api

    def upload_one(item: tuple[Path, int]) -> tuple[int, int, int]:
        source_path, source_bytes = item
        did_upload = riverhog_upload_artifact(
            job,
            api_for_worker(),
            archive_dir,
            source_path,
            persist=False,
        )
        return 1, 1 if did_upload else 0, source_bytes if did_upload else 0

    if worker_count > 1:
        try:
            with ThreadPoolExecutor(max_workers=worker_count) as executor:
                futures = [executor.submit(upload_one, item) for item in selected]
                for future in as_completed(futures):
                    item_processed, item_uploaded, item_bytes = future.result()
                    processed += item_processed
                    uploaded += item_uploaded
                    uploaded_bytes += item_bytes
                    persist_progress_if_due()
        finally:
            with worker_clients_lock:
                clients = list(worker_clients)
                worker_clients.clear()
            for worker_api in clients:
                worker_api.close()
            persist_progress_if_due(force=True)
        return {
            "processed_files": processed,
            "uploaded_files": uploaded,
            "uploaded_bytes": uploaded_bytes,
            "elapsed_seconds": round(max(0.0, time.monotonic() - started), 6),
        }

    api = ApiClient()
    try:
        ensure_riverhog_session(job, api, archive_dir)
        for source_path, source_bytes in selected:
            if riverhog_upload_artifact(
                job,
                api,
                archive_dir,
                source_path,
                persist=False,
            ):
                uploaded += 1
                uploaded_bytes += source_bytes
            processed += 1
            persist_progress_if_due()
        return {
            "processed_files": processed,
            "uploaded_files": uploaded,
            "uploaded_bytes": uploaded_bytes,
            "elapsed_seconds": round(max(0.0, time.monotonic() - started), 6),
        }
    finally:
        persist_progress_if_due(force=True)
        api.close()


def maybe_upload_riverhog_artifacts(job: dict[str, Any], archive_dir: Path) -> None:
    if not riverhog_config_enabled(job) or not RIVERHOG_UPLOAD_ENABLED:
        return
    try:
        result = upload_riverhog_artifacts(
            job,
            archive_dir,
            final=False,
            max_files=RIVERHOG_EAGER_UPLOAD_FILES_PER_TICK,
            max_bytes=RIVERHOG_EAGER_UPLOAD_BYTES_PER_TICK,
            max_seconds=RIVERHOG_EAGER_UPLOAD_SECONDS_PER_TICK,
        )
        if result["processed_files"]:
            state = riverhog_session_state(job)
            state["last_eager_upload_at"] = utc_timestamp_now()
            state["last_eager_upload_files"] = int(result["uploaded_files"])
            state["last_eager_upload_bytes"] = int(result["uploaded_bytes"])
            state["last_eager_upload_elapsed_seconds"] = float(result["elapsed_seconds"])
            touch_riverhog_session_state(job)
            save_job(job)
    except JobCanceled:
        raise
    except HashMismatch as exc:
        notify_job_issue(job, component="riverhog_upload", error=exc, severity="critical")
        log.error("riverhog eager upload failed integrity check: %s", exc)
    except RuntimeError as exc:
        log.warning("riverhog eager upload failed; will retry later: %s", exc)
    except Exception as exc:
        log.warning("riverhog eager upload issue; will retry later: %s", exc)


RIVERHOG_EAGER_UPLOAD_BLOCKED_PHASES = {"metadata_projection", "riverhog_upload"}


def riverhog_eager_upload_allowed(job: dict[str, Any]) -> bool:
    if job.get("state") != "running" or job.get("cancel_requested"):
        return False
    if str(job.get("workflow_mode") or "collection_archive") != "collection_archive":
        return False
    collection_archive = dict_or_empty(job.get("collection_archive"))
    if str(collection_archive.get("destination") or "riverhog") != "riverhog":
        return False
    if not riverhog_config_enabled(job):
        return False
    if str(job.get("phase") or "") in RIVERHOG_EAGER_UPLOAD_BLOCKED_PHASES:
        return False
    state = job.get("riverhog_session_upload")
    return not (
        isinstance(state, dict) and state.get("state") in {"canceled", "archiving", "finalized"}
    )


def all_riverhog_session_files_uploaded(job: dict[str, Any]) -> bool:
    state = job.get("riverhog_session_upload")
    if not isinstance(state, dict):
        return False
    files = state.get("files")
    if not isinstance(files, dict) or not files:
        return False
    return all(
        isinstance(item, dict) and riverhog_upload_file_complete(item) for item in files.values()
    )


def can_resume_preserving_riverhog_session(job: dict[str, Any]) -> bool:
    state = job.get("riverhog_session_upload")
    if not riverhog_config_enabled(job) or not isinstance(state, dict):
        return False
    if state.get("canceled_at") or state.get("state") in {"canceled"}:
        return False
    return all_riverhog_session_files_uploaded(job)


def riverhog_upload_session_on_failure(job: dict[str, Any]) -> str:
    value = str(
        dict_or_empty(job.get("riverhog")).get("upload_session_on_failure") or "preserve_for_resume"
    )
    if value not in {"preserve_for_resume", "cancel"}:
        return "preserve_for_resume"
    return value


def should_cancel_riverhog_upload_session_on_failure(job: dict[str, Any], exc: Exception) -> bool:
    if isinstance(exc, EncodingFailed):
        return True
    if riverhog_upload_session_on_failure(job) == "cancel":
        return True
    return not can_resume_preserving_riverhog_session(job)


def riverhog_session_visible_for_resume(job: dict[str, Any]) -> bool:
    if not RIVERHOG_UPLOAD_ENABLED:
        return True
    api = ApiClient()
    try:
        sync_riverhog_session_from_remote(job, api)
        return True
    except NotFound:
        return False
    except Exception as exc:
        log.warning(
            "could not verify riverhog session before preserving resume for %s: %s",
            job.get("job_id"),
            exc,
        )
        return True
    finally:
        api.close()


def complete_riverhog_session(
    job: dict[str, Any],
    api: ApiClient,
    archive_dir: Path,
) -> dict[str, Any]:
    collection_id = ensure_riverhog_session(job, api, archive_dir)
    payload = api.complete_collection_upload_session(collection_id)
    update_riverhog_state_from_payload(job, payload)
    state = riverhog_session_state(job)
    state["completed_at"] = state.get("completed_at") or utc_timestamp_now()
    save_job(job)
    return payload


def compact_riverhog_progress_metrics(progress: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(progress, dict):
        return {}
    keys = (
        "primary_files_uploaded",
        "primary_files_total",
        "artifact_files_uploaded",
        "artifact_files_known",
        "artifact_files_registered",
        "artifact_files_deleted",
        "uploaded_bytes",
        "bytes_total",
        "percent_bytes",
        "percent_files",
        "state",
        "archive_phase",
        "archive_uploaded_bytes",
        "archive_total_bytes",
        "archive_uploaded_parts",
        "archive_total_parts",
        "hot_materialized_files",
        "hot_materialized_bytes",
        "finalized",
        "safe_to_delete",
    )
    return {key: progress[key] for key in keys if key in progress}


def wait_for_riverhog_finalized(
    job: dict[str, Any],
    api: ApiClient,
    collection_id: str,
) -> dict[str, Any]:
    while True:
        raise_if_job_canceled(str(job["job_id"]))
        try:
            collection = api.get_collection(collection_id)
            payload = finalized_riverhog_payload_from_collection(collection_id, collection)
            state = riverhog_session_state(job)
            state["riverhog_state"] = "finalized"
            state["state"] = "finalized"
            state["finalized_at"] = state.get("finalized_at") or utc_timestamp_now()
            state["last_payload"] = compact_riverhog_payload(payload)
            save_job(job)
            return payload
        except NotFound as exc:
            payload = api.get_collection_upload(collection_id)
            update_riverhog_state_from_payload(job, payload)
            save_job(job)
            if str(payload.get("state") or "") == "failed":
                raise RuntimeError(
                    f"riverhog collection upload failed: {payload.get('latest_failure')}"
                ) from exc
        retry_sleep(RIVERHOG_FINALIZE_POLL_SECONDS, job_id=str(job["job_id"]))


def upload_to_riverhog(job: dict[str, Any], archive_dir: Path) -> dict[str, Any] | None:
    if not riverhog_config_enabled(job):
        return None
    wait = str(job.get("riverhog", {}).get("wait") or RIVERHOG_WAIT)
    notify_job_event(
        job,
        "archive.handoff",
        "Archive collection is complete; handing off to Riverhog.",
        extra={"archive_dir": str(archive_dir), "method": "session"},
    )

    def operation() -> dict[str, Any]:
        if not RIVERHOG_UPLOAD_ENABLED:
            raise RuntimeError("riverhog upload requested, but runner Riverhog upload is disabled")
        api = ApiClient()
        metrics: dict[str, Any] = {
            "started_at": utc_timestamp_now(),
            "wait": wait,
        }
        job["riverhog_handoff_metrics"] = metrics
        save_job(job)
        try:
            metrics["final_sweep_started_at"] = utc_timestamp_now()
            metrics["final_sweep_before"] = compact_riverhog_progress_metrics(
                riverhog_upload_progress_for_job(job)
            )
            final_sweep = upload_riverhog_artifacts(job, archive_dir, final=True)
            metrics["final_sweep_finished_at"] = utc_timestamp_now()
            metrics["final_sweep_elapsed_seconds"] = final_sweep["elapsed_seconds"]
            metrics["final_sweep_processed_files"] = final_sweep["processed_files"]
            metrics["final_sweep_uploaded_files"] = final_sweep["uploaded_files"]
            metrics["final_sweep_uploaded_bytes"] = final_sweep["uploaded_bytes"]
            sync_riverhog_session_from_remote(job, api)
            metrics["final_sweep_after"] = compact_riverhog_progress_metrics(
                riverhog_upload_progress_for_job(job)
            )
            save_job(job)
            if not all_riverhog_session_files_uploaded(job):
                raise RuntimeError("riverhog upload did not upload every registered file")
            complete_started = time.monotonic()
            metrics["session_complete_started_at"] = utc_timestamp_now()
            save_job(job)
            payload = complete_riverhog_session(job, api, archive_dir)
            metrics["session_complete_finished_at"] = utc_timestamp_now()
            metrics["session_complete_elapsed_seconds"] = round(
                max(0.0, time.monotonic() - complete_started),
                6,
            )
            collection_id = str(payload.get("collection_id") or "")
            if wait == "finalized" and collection_id:
                finalize_started = time.monotonic()
                metrics["wait_finalized_started_at"] = utc_timestamp_now()
                save_job(job)
                payload = wait_for_riverhog_finalized(job, api, collection_id)
                metrics["wait_finalized_finished_at"] = utc_timestamp_now()
                metrics["wait_finalized_elapsed_seconds"] = round(
                    max(0.0, time.monotonic() - finalize_started),
                    6,
                )
            metrics["finished_at"] = utc_timestamp_now()
            save_job(job)
            return {
                "method": "session",
                "wait": wait,
                "collection_id": collection_id,
                "metrics": dict(metrics),
                "payload": compact_riverhog_payload(payload),
            }
        except JobCanceled:
            metrics["canceled_at"] = utc_timestamp_now()
            save_job(job)
            raise
        except Exception as exc:
            metrics["failed_at"] = utc_timestamp_now()
            metrics["error"] = str(exc)
            save_job(job)
            raise
        finally:
            api.close()

    return retry_handoff_until_success(
        job,
        result_key="riverhog_upload_result",
        phase="riverhog_upload",
        action="riverhog upload",
        component="riverhog_upload",
        operation=operation,
    )


def riverhog_eager_upload_candidate_jobs() -> list[dict[str, Any]]:
    if not RIVERHOG_UPLOAD_ENABLED:
        return []
    candidates: list[dict[str, Any]] = []
    for job in job_states():
        if not riverhog_eager_upload_allowed(job):
            continue
        if eager_riverhog_artifact_paths(job):
            candidates.append(job)
    candidates.sort(key=lambda item: str(item.get("created_at") or ""))
    return candidates


def riverhog_upload_loop() -> None:
    while not riverhog_upload_stop.wait(RIVERHOG_EAGER_UPLOAD_INTERVAL_SECONDS):
        try:
            for job in riverhog_eager_upload_candidate_jobs():
                if riverhog_upload_stop.is_set():
                    return
                archive_dir = GPU_RUNTIME_DIR / "jobs" / str(job.get("job_id") or "") / "archive"
                maybe_upload_riverhog_artifacts(job, archive_dir)
        except JobCanceled as exc:
            log.info("riverhog eager upload worker noticed cancellation: %s", exc)
        except Exception:
            log.exception("riverhog eager upload worker failed")


def wait_for_riverhog_eager_upload_quiescent(job: dict[str, Any]) -> None:
    if not riverhog_config_enabled(job):
        return
    job_id = str(job.get("job_id") or "")
    if not job_id:
        return
    with riverhog_upload_call_lock(job_id):
        return


def cancel_riverhog_upload_session(job: dict[str, Any], *, reason: str) -> None:
    if not riverhog_config_enabled(job):
        return
    state = job.get("riverhog_session_upload")
    if not isinstance(state, dict):
        state = riverhog_session_state(job)
    collection_id = str(state.get("collection_id") or derived_riverhog_collection_id(job) or "")
    if not collection_id:
        return
    state["collection_id"] = collection_id
    if state.get("state") in {"canceled", "archiving", "finalized"} or state.get("canceled_at"):
        return
    state["cancel_reason"] = reason
    if not RIVERHOG_UPLOAD_ENABLED:
        state["cancel_skipped_at"] = utc_timestamp_now()
        state["cancel_skipped_reason"] = "riverhog upload disabled"
        save_job(job)
        return
    api = ApiClient()
    try:
        payload = api.cancel_collection_upload_session(collection_id)
        update_riverhog_state_from_payload(job, payload)
        state = riverhog_session_state(job)
        state["canceled_at"] = utc_timestamp_now()
        state["cancel_reason"] = reason
        log.info(
            "canceled riverhog upload session job=%s collection=%s reason=%s",
            job.get("job_id"),
            collection_id,
            reason,
        )
    except NotFound:
        state["state"] = "canceled"
        state["riverhog_state"] = "absent"
        state["canceled_at"] = utc_timestamp_now()
        state["cancel_reason"] = reason
        state["cancel_not_found"] = True
        log.info(
            "riverhog upload session already absent job=%s collection=%s reason=%s",
            job.get("job_id"),
            collection_id,
            reason,
        )
    except Exception as exc:
        state["cancel_failed_at"] = utc_timestamp_now()
        state["cancel_error"] = str(exc)
        log.warning(
            "failed to cancel riverhog upload session job=%s collection=%s: %s",
            job.get("job_id"),
            collection_id,
            exc,
        )
    finally:
        api.close()
        save_job(job)


def render_job_template(
    value: str,
    job: dict[str, Any],
    *,
    context: Mapping[str, str] | None = None,
) -> str:
    review = dict_or_empty(job.get("review"))
    mapping = {
        "job_id": str(job.get("job_id") or ""),
        "run_id": str(job.get("run_id") or job.get("collection_timestamp") or ""),
        "device_id": str(review.get("device_id") or ""),
        "route_id": str(review.get("route_id") or ""),
        "profile_id": str(review.get("profile_id") or ""),
        "collection_slug": str(job.get("collection_slug") or ""),
        "collection_timestamp": str(job.get("collection_timestamp") or ""),
    }
    if context is not None:
        mapping.update({str(key): str(value) for key, value in context.items()})
    try:
        return value.format(**mapping)
    except KeyError as exc:
        raise RuntimeError(f"unknown target upload template field: {exc.args[0]}") from exc


def target_upload_excludes(config: Mapping[str, Any]) -> list[str]:
    excludes = list(DEFAULT_TARGET_UPLOAD_EXCLUDES)
    raw_excludes = config.get("exclude") or []
    if not isinstance(raw_excludes, Sequence) or isinstance(raw_excludes, (str, bytes)):
        raise RuntimeError("target upload exclude must be a list")
    raw_patterns: list[str] = []
    for item in raw_excludes:
        if not isinstance(item, str):
            raise RuntimeError("target upload exclude entries must be strings")
        raw_patterns.append(item)
    try:
        extra_excludes = normalize_exclude_patterns(
            raw_patterns,
            label="target upload exclude",
        )
    except ValueError as exc:
        raise RuntimeError(str(exc)) from exc
    for pattern in extra_excludes:
        if pattern not in excludes:
            excludes.append(pattern)
    return excludes


def target_upload_path_excluded(rel_path: str, excludes: Sequence[str]) -> bool:
    return path_matches_exclude_patterns(rel_path, excludes)


def target_artifact_count(source_dir: Path, *, excludes: Sequence[str] = ()) -> int:
    if not source_dir.is_dir():
        return 0
    return sum(
        1
        for path in source_dir.rglob("*")
        if path.is_file()
        and not target_upload_path_excluded(path.relative_to(source_dir).as_posix(), excludes)
    )


def run_target_command(
    cmd: list[str],
    *,
    action: str,
    env: dict[str, str] | None = None,
) -> dict[str, Any]:
    return run_command(cmd, action=action, env=env)


def upload_target(
    job: dict[str, Any],
    source_dir: Path,
    *,
    config: Mapping[str, Any] | None = None,
    source_label: str = "review",
    result_key: str = "review_handoff_result",
    phase: str = "review_handoff",
    component: str = "review_handoff",
    event: NotifyEvent = "review.handoff",
    allow_empty: bool = True,
    emit_notification: bool = True,
    template_context: Mapping[str, str] | None = None,
) -> dict[str, Any] | None:
    if config is None:
        config = dict_or_empty(dict_or_empty(job.get("review")).get("target"))
    if not config.get("enabled"):
        return None
    if not TARGET_UPLOAD_ENABLED:
        raise RuntimeError("target upload requested, but runner target upload is disabled")
    excludes = target_upload_excludes(config)
    artifact_count = target_artifact_count(source_dir, excludes=excludes)
    if artifact_count == 0:
        if not allow_empty:
            raise RuntimeError(f"{source_label} artifacts are empty: {source_dir}")
        return {
            "status": "skipped",
            "reason": f"no {source_label} artifacts",
            "source": str(source_dir),
        }
    method = str(config.get("method") or "command")
    if emit_notification:
        notify_job_event(
            job,
            event,
            f"{source_label.title()} artifacts are complete; handing off for upload.",
            extra={
                "source_dir": str(source_dir),
                "source_label": source_label,
                "method": method,
                "artifact_count": artifact_count,
            },
        )
    if method == "rclone":
        destination = str(config.get("destination") or "").strip()
        if not destination:
            raise RuntimeError("target upload destination is required for rclone")
        rendered_destination = render_job_template(
            destination,
            job,
            context=template_context,
        )
        mode = str(config.get("mode") or "copy")
        if mode not in {"copy", "sync"}:
            raise RuntimeError(f"unsupported target upload rclone mode: {mode}")
        cmd = [
            TARGET_RCLONE_COMMAND,
            mode,
            *[arg for pattern in excludes for arg in ("--exclude", pattern)],
            str(source_dir),
            rendered_destination,
            "--retries",
            str(max(1, UPLOAD_ATTEMPTS)),
            "--low-level-retries",
            "10",
            "--stats",
            "30s",
        ]

        def rclone_operation() -> dict[str, Any]:
            result = run_target_command(cmd, action=f"{source_label} rclone upload")
            result["method"] = "rclone"
            result["mode"] = mode
            result["source"] = str(source_dir)
            result["source_label"] = source_label
            result["destination"] = rendered_destination
            result["artifact_count"] = artifact_count
            return result

        return retry_handoff_until_success(
            job,
            result_key=result_key,
            phase=phase,
            action=f"{source_label} rclone upload",
            component=component,
            operation=rclone_operation,
        )
    if method != "command":
        raise RuntimeError(f"unsupported target upload method: {method}")
    if not TARGET_UPLOAD_COMMAND:
        raise RuntimeError(
            "target upload requested, but MUNCHY_RUNNER_TARGET_UPLOAD_COMMAND is empty"
        )
    env = os.environ.copy()
    env["MUNCHY_TARGET_SOURCE"] = str(source_dir)
    env["MUNCHY_TARGET_SOURCE_LABEL"] = source_label
    env["MUNCHY_JOB_ID"] = str(job["job_id"])
    env["MUNCHY_COLLECTION_SLUG"] = str(job.get("collection_slug") or "")
    env["MUNCHY_COLLECTION_TIMESTAMP"] = str(job.get("collection_timestamp") or "")
    review = dict_or_empty(job.get("review"))
    review_context = {
        "device_id": str(review.get("device_id") or ""),
        "route_id": str(review.get("route_id") or ""),
        "profile_id": str(review.get("profile_id") or ""),
    }
    if template_context is not None:
        review_context.update({str(key): str(value) for key, value in template_context.items()})
    env["MUNCHY_REVIEW_DEVICE_ID"] = review_context["device_id"]
    env["MUNCHY_REVIEW_ROUTE_ID"] = review_context["route_id"]
    env["MUNCHY_REVIEW_PROFILE_ID"] = review_context["profile_id"]

    def command_operation() -> dict[str, Any]:
        result = run_target_command(
            ["/bin/sh", "-lc", TARGET_UPLOAD_COMMAND],
            action=f"{source_label} command upload",
            env=env,
        )
        result["method"] = "command"
        result["source"] = str(source_dir)
        result["source_label"] = source_label
        result["artifact_count"] = artifact_count
        return result

    return retry_handoff_until_success(
        job,
        result_key=result_key,
        phase=phase,
        action=f"{source_label} command upload",
        component=component,
        operation=command_operation,
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


def review_tasks_for_group(group_config: Mapping[str, Any]) -> list[TaskName]:
    return [
        cast(TaskName, str(task))
        for task in group_config.get("tasks") or []
        if str(task) in {"qcut_video", "audio_review"}
    ]


def group_base_encode_profile(group_config: Mapping[str, Any]) -> dict[str, Any]:
    profile = group_config.get("encode_profile")
    if isinstance(profile, Mapping):
        return copy.deepcopy(dict(profile))
    output_mode = normalize_output_mode(str(group_config.get("output_mode") or "video"))
    return default_encode_profile_for_output_mode(output_mode)


def review_sweep_route_file_states(
    upload: dict[str, Any],
    groups: dict[str, dict[str, Any]],
    *,
    requested_route_ids: set[str],
) -> dict[str, dict[str, Any]]:
    routes: dict[str, dict[str, Any]] = {}
    selected_groups = set(groups)
    for file_state in mutable_primary_upload_files_for_groups(upload, selected_groups):
        group_name = upload_file_resolved_group(file_state)
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
    all_file_states = [*file_states, *sidecar_evidence_files_for_primaries(upload, file_states)]
    for file_state in all_file_states:
        rel_path = upload_file_group_rel_for_state(file_state, group_name)
        source = input_dir / group_name / rel_path
        if not source.is_file():
            raise RuntimeError(f"review sweep source file is missing: {source}")
        link_or_copy(source, route_input_root / group_name / rel_path)
    write_group_filesystem_metadata(route_input_root, group_name, all_file_states)


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
    save_job(job)

    token = acquire_job_gpu(job)
    notified_handoff = False
    completed = len(result.get("variants") or [])
    try:
        for route_id, route in sorted(routes.items()):
            group_name = str(route["group_name"])
            group_config = groups[group_name]
            tasks = cast(list[TaskName], list(route["tasks"]))
            route_input_root = gpu_job_root / "review-sweep-input" / safe_local_id(route_id)
            prepare_review_sweep_route_input(
                upload=input_upload,
                input_dir=input_dir,
                route_input_root=route_input_root,
                group_name=group_name,
                file_states=cast(list[dict[str, Any]], route["file_states"]),
            )
            for variant in route_variants[route_id]:
                raise_if_job_canceled(job_id)
                profile_id = validate_group_name(str(variant["profile_id"]))
                variant_key = f"{route_id}/{profile_id}"
                if any(
                    isinstance(item, dict) and item.get("variant") == variant_key
                    for item in result.get("variants") or []
                ):
                    continue
                variant_archive_dir = (
                    gpu_job_root
                    / "review-sweep-archive"
                    / safe_local_id(route_id)
                    / safe_local_id(profile_id)
                )
                variant_review_dir = (
                    review_dir / safe_local_id(route_id) / safe_local_id(profile_id)
                )
                gpu_job_id = gpu_group_job_id(job_id, f"{route_id}-{profile_id}")
                gpu_payload = {
                    "job_id": gpu_job_id,
                    "input_dir": gpu_runtime_container_path(route_input_root / group_name),
                    "archive_dir": gpu_runtime_container_path(variant_archive_dir),
                    "review_dir": gpu_runtime_container_path(variant_review_dir),
                    "profile": profile_id,
                    "tasks": tasks,
                    "collection_slug": str(job.get("collection_slug") or ""),
                    "collection_timestamp": job.get("collection_timestamp"),
                    "riverhog": {"enabled": False},
                    "review_upload": {"enabled": False},
                    "container_metadata_required": gpu_tasks_require_container_metadata(
                        tasks,
                        group_config,
                    ),
                    "encode_profile": variant["encode_profile"],
                }
                if group_config.get("max_parallel_encodes") is not None:
                    gpu_payload["max_parallel_encodes"] = group_config["max_parallel_encodes"]
                review_clip_plan = dict_or_empty(dict_or_empty(job.get("review")).get("clip_plan"))
                if review_clip_plan:
                    gpu_payload["review_clip_plan"] = copy.deepcopy(review_clip_plan)
                for task_name in ("qcut_video", "audio_review"):
                    if task_name not in tasks:
                        continue
                    review_plan = load_shared_review_plan(
                        str(job["input_upload_id"]),
                        route_id,
                        task_name,
                    )
                    if review_plan is not None:
                        gpu_payload.setdefault("review_plans", {})[task_name] = review_plan
                job["phase"] = f"review_sweep:{route_id}:{profile_id}"
                job.setdefault("gpu_payloads", {})[variant_key] = gpu_payload
                save_job(job)
                start_gpu_job(gpu_payload)
                gpu_result = wait_gpu_job(gpu_job_id, gpu_payload=gpu_payload, job=job)
                job.setdefault("gpu_results", {})[variant_key] = gpu_result
                remember_review_plans_from_gpu_result(job, route_id, gpu_result)
                save_job(job)

                if not notified_handoff:
                    notify_job_event(
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
                result_key = f"review_sweep_upload_{safe_local_id(variant_key)}"
                upload_result = upload_target(
                    job,
                    variant_review_dir,
                    config=dict_or_empty(dict_or_empty(job.get("review")).get("target")),
                    source_label="review sweep",
                    result_key=result_key,
                    phase="review_sweep_handoff",
                    component="review_sweep_handoff",
                    event="review.handoff",
                    allow_empty=False,
                    emit_notification=False,
                    template_context={
                        "route_id": route_id,
                        "profile_id": profile_id,
                    },
                )
                latest = read_state("job", job_id)
                if isinstance(latest, dict):
                    job.clear()
                    job.update(latest)
                clear_handoff_attempt_state(job, result_key)
                result = review_sweep_result_state(job)
                result.setdefault("variants", []).append(
                    {
                        "variant": variant_key,
                        "route_id": route_id,
                        "profile_id": profile_id,
                        "tasks": tasks,
                        "encode_settings": variant.get("encode_settings") or {},
                        "axis_values": variant.get("axis_values") or {},
                        "upload_result": upload_result,
                        "completed_at": utc_timestamp_now(),
                    }
                )
                completed += 1
                result["variants_completed"] = completed
                job["phase"] = f"review_sweep:{completed}/{total_variants}"
                save_job(job)
    finally:
        release_job_gpu(job, token)

    result = review_sweep_result_state(job)
    result["finished_at"] = utc_timestamp_now()
    result["variants_completed"] = len(result.get("variants") or [])
    job["review_handoff_result"] = result
    job["collection_archive_target_upload_result"] = None
    job["riverhog_upload_result"] = None
    save_job(job)


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
        for name in input_upload_groups(input_upload)
    }
    job["groups"] = groups
    save_job(job)
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
    output = archive_output_for_upload_file(
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
    output = archive_output_for_upload_file(
        file_state,
        group_name=group_name,
        group_config=group_config,
        archive_dir=archive_dir,
    )
    files = eager_archive_state(job).setdefault("files", {})
    previous = files.get(rel_path) if isinstance(files.get(rel_path), dict) else {}
    started = (
        safe_parse_timestamp(previous.get("started_at")) if isinstance(previous, dict) else None
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
    output = archive_output_for_upload_file(
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
    remove_input_file_data(file_state)
    return True


def consume_input_upload_files(upload_id: str, rel_paths: set[str]) -> dict[str, Any]:
    with state_lock:
        upload = load_input_upload(upload_id)
        changed = False
        by_path = {str(file_state["path"]): file_state for file_state in upload.get("files", [])}
        for rel_path in sorted(rel_paths):
            file_state = by_path.get(rel_path)
            if file_state is None:
                raise RuntimeError(f"unknown input file while consuming source: {rel_path}")
            changed = consume_input_upload_file(upload, file_state) or changed
        if changed:
            return save_input_upload(upload)
        return upload


def mark_existing_eager_outputs(
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
        for file_state in mutable_primary_upload_files_for_groups(upload, {group_name}):
            rel_path = str(file_state["path"])
            if eager_file_claimed(job, rel_path):
                continue
            output = archive_output_for_upload_file(
                file_state,
                group_name=group_name,
                group_config=group_config,
                archive_dir=archive_dir,
            )
            if not output.exists():
                continue
            source_artifacts_sidecar = source_artifact_sidecar_for_archive_output(output)
            if (
                sidecar_evidence_files_for_primary(
                    upload,
                    file_state,
                )
                and not source_artifacts_sidecar.exists()
            ):
                continue
            if ensure_file_projection_metadata(
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
            status = upload_file_status(file_state)
            if status["upload_state"] == "uploaded":
                consume_paths.add(rel_path)
    if changed:
        save_job(job)
    if upload_changed:
        upload = save_input_upload_raw(upload)
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
        save_job(job)
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
        for file_state in primary_upload_files_for_groups(upload, {group_name})
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
    files_state = dict_or_empty(eager.get("files"))
    if not eager_groups:
        eager_groups = {
            str(item.get("group") or upload_file_group(str(rel_path)))
            for rel_path, item in files_state.items()
            if isinstance(item, dict)
        }
    if not eager_groups:
        return None

    upload: dict[str, Any] | None = None
    upload_id = str(job.get("input_upload_id") or "")
    if upload_id:
        try:
            stored_upload = read_state("input-upload", upload_id)
            upload = refresh_input_upload(stored_upload) if stored_upload is not None else None
        except Exception:
            upload = None

    upload_files: list[dict[str, Any]] = []
    if upload is not None:
        upload_files = primary_upload_files_for_groups(upload, eager_groups)
    by_path = {str(item.get("path")): item for item in upload_files}
    known_paths = set(by_path) | {
        str(path)
        for path, item in files_state.items()
        if isinstance(item, dict)
        and str(item.get("group") or upload_file_group(str(path))) in eager_groups
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
        started = safe_parse_timestamp(state.get("started_at"))
        finished = safe_parse_timestamp(state.get("encoded_at"))
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

    batches = dict_or_empty(eager.get("batches"))
    for batch in batches.values():
        if not isinstance(batch, dict):
            continue
        batch_started = safe_parse_timestamp(batch.get("started_at"))
        batch_finished = safe_parse_timestamp(batch.get("finished_at"))
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
    pipeline_batches = EAGER_ARCHIVE_PIPELINE_BATCHES
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
        stored_upload = read_state("input-upload", upload_id)
        upload = refresh_input_upload(stored_upload) if stored_upload is not None else None
    except Exception:
        upload = None
    if upload is None:
        return None
    files_total = int(upload.get("files_total") or 0)
    files_uploaded = int(upload.get("files_uploaded") or 0)
    bytes_total = int(upload.get("bytes_total") or 0)
    uploaded_bytes = int(upload.get("uploaded_bytes") or 0)
    tree_progress: dict[str, int] = {}
    group_names = input_upload_routed_groups(upload)
    groups = job.get("groups")
    if isinstance(groups, dict):
        eager_groups = eager_archive_group_names(groups)
        shared_tree_groups = group_names - eager_groups
    else:
        shared_tree_groups = group_names
    if shared_tree_groups:
        tree_progress = shared_input_tree_progress(upload, shared_tree_groups)
        shared_tree_files = upload_files_for_groups(upload, shared_tree_groups)
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


def riverhog_upload_progress_for_job(job: dict[str, Any]) -> dict[str, Any] | None:
    state = job.get("riverhog_session_upload")
    if not isinstance(state, dict):
        return None
    files = state.get("files")
    if not isinstance(files, dict):
        files = {}
    file_items = [item for item in files.values() if isinstance(item, dict)]
    if not file_items and not state.get("collection_id"):
        return None

    registered_files_total = len(file_items)
    local_artifacts_total = 0
    local_artifact_paths: set[str] = set()
    if riverhog_config_enabled(job):
        archive_dir = GPU_RUNTIME_DIR / "jobs" / str(job.get("job_id") or "") / "archive"
        local_artifact_paths = {
            rel_path
            for path in eager_riverhog_artifact_paths(job)
            if (rel_path := path_relative_to_archive(path, archive_dir)) is not None
        }
        local_artifacts_total = len(local_artifact_paths)
        if not local_artifacts_total:
            local_artifact_paths = {
                rel_path
                for path in archive_dir_artifact_paths(archive_dir)
                if (rel_path := path_relative_to_archive(path, archive_dir)) is not None
            }
            local_artifacts_total = len(local_artifact_paths)
    else:
        archive_dir = GPU_RUNTIME_DIR / "jobs" / str(job.get("job_id") or "") / "archive"
    registered_artifact_paths = {str(path) for path in files if isinstance(path, str)}
    known_artifact_paths = registered_artifact_paths | local_artifact_paths
    expected_primary_files_total = int(job.get("riverhog_expected_primary_files_total") or 0)
    encode_progress = encode_progress_for_job(job)
    encode_files_total = 0
    encode_files_encoded = 0
    if isinstance(encode_progress, dict):
        encode_files_total = int(encode_progress.get("files_total") or 0)
        encode_files_encoded = int(encode_progress.get("files_encoded") or 0)
    artifact_files_uploaded = 0
    artifact_files_deleted = 0
    bytes_total = 0
    uploaded_bytes = 0
    for item in file_items:
        item_bytes = int(item.get("bytes") or 0)
        item_uploaded = min(int(item.get("uploaded_bytes") or 0), item_bytes)
        bytes_total += item_bytes
        if riverhog_upload_file_complete(item):
            artifact_files_uploaded += 1
            item_uploaded = item_bytes
        if item.get("state") == "deleted":
            artifact_files_deleted += 1
        uploaded_bytes += item_uploaded

    uploaded_paths = uploaded_riverhog_paths(job)
    primary_paths = primary_archive_output_paths(job, archive_dir)
    primary_files_total = max(expected_primary_files_total, encode_files_total, len(primary_paths))
    primary_files_encoded = max(encode_files_encoded, len(primary_paths))
    if primary_paths:
        primary_files_uploaded = sum(1 for path in primary_paths if path in uploaded_paths)
    elif primary_files_total:
        primary_files_uploaded = min(artifact_files_uploaded, primary_files_total)
    else:
        primary_files_uploaded = artifact_files_uploaded
        primary_files_total = max(
            registered_files_total, len(known_artifact_paths), primary_files_uploaded
        )
    primary_files_uploaded = min(primary_files_uploaded, primary_files_total)
    primary_files_encoded = min(
        max(primary_files_encoded, primary_files_uploaded), primary_files_total
    )
    artifact_files_known = max(
        len(known_artifact_paths), registered_files_total, local_artifacts_total
    )

    started_at = safe_parse_timestamp(state.get("opened_at"))
    if started_at is None:
        started_at = safe_parse_timestamp(state.get("started_at"))
    elapsed_seconds = (
        max(0.001, (datetime.now(UTC) - started_at).total_seconds()) if started_at else 0.0
    )
    average_rate = int(uploaded_bytes / elapsed_seconds) if elapsed_seconds else 0
    recent_rate = 0
    last_eager_upload_at = safe_parse_timestamp(state.get("last_eager_upload_at"))
    if last_eager_upload_at is not None:
        recent_age = (datetime.now(UTC) - last_eager_upload_at).total_seconds()
        recent_elapsed = float(state.get("last_eager_upload_elapsed_seconds") or 0.0)
        recent_bytes = int(state.get("last_eager_upload_bytes") or 0)
        if recent_age <= 120 and recent_elapsed > 0 and recent_bytes > 0:
            recent_rate = int(recent_bytes / recent_elapsed)
    rate = recent_rate or average_rate
    state_name = str(state.get("riverhog_state") or state.get("state") or "not_started")
    handoff_completed = state_name in {"archiving", "finalized"} or (
        primary_files_total > 0
        and primary_files_uploaded == primary_files_total
        and bool(state.get("completed_at"))
    )
    last_payload = state.get("last_payload")
    last_payload = last_payload if isinstance(last_payload, dict) else {}
    archive_uploaded_bytes = int(last_payload.get("archive_uploaded_bytes") or 0)
    archive_total_bytes = int(last_payload.get("archive_total_bytes") or 0)
    archive_uploaded_parts = last_payload.get("archive_uploaded_parts")
    archive_total_parts = last_payload.get("archive_total_parts")
    hot_materialized_files = int(last_payload.get("hot_materialized_files") or 0)
    hot_materialized_bytes = int(last_payload.get("hot_materialized_bytes") or 0)
    retain_hot = bool(
        last_payload.get("retain_hot", dict_or_empty(job.get("riverhog")).get("retain_hot", True))
    )
    riverhog_files_total = int(last_payload.get("files_total") or primary_files_total)
    riverhog_bytes_total = int(last_payload.get("bytes_total") or bytes_total)
    finalized = state_name == "finalized" or str(last_payload.get("state") or "") == "finalized"
    return {
        "collection_id": str(state.get("collection_id") or ""),
        "state": state_name,
        "files_total": primary_files_total,
        "registered_files_total": registered_files_total,
        "local_artifacts_total": local_artifacts_total,
        "expected_primary_files_total": expected_primary_files_total,
        "files_uploaded": primary_files_uploaded,
        "files_deleted": artifact_files_deleted,
        "primary_files_total": primary_files_total,
        "primary_files_encoded": primary_files_encoded,
        "primary_files_uploaded": primary_files_uploaded,
        "artifact_files_known": artifact_files_known,
        "artifact_files_registered": registered_files_total,
        "artifact_files_uploaded": artifact_files_uploaded,
        "artifact_files_deleted": artifact_files_deleted,
        "artifact_files_pending_local": local_artifacts_total,
        "bytes_total": bytes_total,
        "uploaded_bytes": uploaded_bytes,
        "percent_bytes": round((uploaded_bytes / bytes_total * 100.0) if bytes_total else 0.0, 2),
        "percent_files": round(
            (primary_files_uploaded / primary_files_total * 100.0) if primary_files_total else 0.0,
            2,
        ),
        "percent_primary_files": round(
            (primary_files_uploaded / primary_files_total * 100.0) if primary_files_total else 0.0,
            2,
        ),
        "percent_artifact_files": round(
            (artifact_files_uploaded / artifact_files_known * 100.0)
            if artifact_files_known
            else 0.0,
            2,
        ),
        "rate_bytes_per_second": rate,
        "average_rate_bytes_per_second": average_rate,
        "recent_rate_bytes_per_second": recent_rate,
        "completed": handoff_completed,
        "handoff_completed": handoff_completed,
        "archive_phase": str(last_payload.get("archive_phase") or ""),
        "archive_uploaded_bytes": archive_uploaded_bytes,
        "archive_total_bytes": archive_total_bytes,
        "archive_uploaded_parts": archive_uploaded_parts,
        "archive_total_parts": archive_total_parts,
        "retain_hot": retain_hot,
        "hot_materialized_files": hot_materialized_files,
        "hot_materialized_bytes": hot_materialized_bytes,
        "riverhog_files_total": riverhog_files_total,
        "riverhog_bytes_total": riverhog_bytes_total,
        "finalized": finalized,
        "safe_to_delete": finalized,
    }


def job_response(job: dict[str, Any], *, include_queue: bool = True) -> dict[str, Any]:
    response = dict(job)
    if include_queue and (queue := queue_info_for_job(str(job.get("job_id") or ""))):
        response["queue"] = queue
    if progress := upload_progress_for_job(job):
        response["upload_progress"] = progress
    if progress := encode_progress_for_job(job):
        response["encode_progress"] = progress
    if progress := riverhog_upload_progress_for_job(job):
        response["riverhog_upload_progress"] = progress
    return response


def compact_job_response(job: dict[str, Any], *, include_queue: bool = True) -> dict[str, Any]:
    response = job_response(job, include_queue=include_queue)
    keys = [
        "job_id",
        "state",
        "phase",
        "created_at",
        "updated_at",
        "started_at",
        "finished_at",
        "input_upload_id",
        "run_id",
        "collection_slug",
        "collection_timestamp",
        "workflow_mode",
        "review",
        "output_mode",
        "profile",
        "upload_progress",
        "encode_progress",
        "riverhog_upload_progress",
        "riverhog_handoff_metrics",
        "review_sweep_result",
        "review_handoff_result",
        "collection_archive_target_upload_result",
        "riverhog_upload_result",
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
        "debug_bundle_dir",
        "debug_bundle_created_at",
        "debug_bundle_reason",
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
        for file_state in mutable_primary_upload_files_for_groups(upload, {group_name}):
            rel_path = str(file_state["path"])
            if eager_file_claimed(job, rel_path):
                continue
            output = archive_output_for_upload_file(
                file_state,
                group_name=group_name,
                group_config=group_config,
                archive_dir=archive_dir,
            )
            if output.exists():
                source_artifacts_sidecar = source_artifact_sidecar_for_archive_output(output)
                if (
                    sidecar_evidence_files_for_primary(
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
            status = upload_file_status(file_state)
            if status["upload_state"] == "consumed":
                raise RuntimeError(f"eager source was consumed before output existed: {rel_path}")
            if status["upload_state"] != "uploaded":
                continue
            evidence = sidecar_evidence_files_for_primary(upload, file_state)
            if not all(upload_file_status(item)["complete"] for item in evidence):
                continue
            ready.append(file_state)
            if len(ready) >= group_limit:
                break
        if ready:
            save_job(job)
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
    if len(global_running) >= EAGER_ARCHIVE_PIPELINE_BATCHES:
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
    return f"eager_archive:pipeline={running_count}/{EAGER_ARCHIVE_PIPELINE_BATCHES}"


def next_eager_batch_id(job: dict[str, Any], group_name: str, paths: list[str]) -> str:
    eager = eager_archive_state(job)
    batch_number = int(eager.get("next_batch_number") or 1)
    eager["next_batch_number"] = batch_number + 1
    digest = hashlib.sha256("\n".join(paths).encode()).hexdigest()[:10]
    safe_group = group_name[:36]
    return f"{safe_group}-{batch_number:06d}-{digest}"


def eager_batch_input_root(job_id: str, batch_id: str) -> Path:
    return GPU_RUNTIME_DIR / "jobs" / job_id / "eager-input" / batch_id


def build_eager_gpu_payload(
    job: dict[str, Any],
    *,
    batch_id: str,
    group_name: str,
    group_config: dict[str, Any],
    tasks: list[TaskName],
    container_metadata: dict[str, dict[str, Any]] | None = None,
    source_artifacts_sidecars: dict[str, list[dict[str, Any]]] | None = None,
) -> dict[str, Any]:
    job_id = str(job["job_id"])
    gpu_job_id = gpu_eager_batch_job_id(job_id, batch_id)
    payload = {
        "job_id": gpu_job_id,
        "input_dir": f"/data/jobs/{job_id}/eager-input/{batch_id}/{group_name}",
        "archive_dir": f"/data/jobs/{job_id}/archive/{group_name}",
        "review_dir": f"/data/jobs/{job_id}/review/{group_name}",
        "profile": group_config.get("profile", "av1-nvenc-high"),
        "tasks": tasks,
        "collection_slug": str(job.get("collection_slug") or ""),
        "collection_timestamp": job.get("collection_timestamp"),
        "riverhog": {"enabled": False},
        "review_upload": {"enabled": False},
        "container_metadata_required": gpu_tasks_require_container_metadata(tasks, group_config),
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
    for file_state in primary_upload_files_for_groups(upload, {group_name}):
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
    save_job(job)
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
    last_submitted = safe_parse_timestamp(batch.get("last_submitted_at"))
    if (
        not force
        and last_submitted is not None
        and (datetime.now(UTC) - last_submitted).total_seconds() < max(30.0, GPU_REPOST_SECONDS)
    ):
        return False
    start_gpu_job(payload)
    batch["last_submitted_at"] = utc_timestamp_now()
    batch["submit_count"] = int(batch.get("submit_count") or 0) + 1
    save_job(job)
    return True


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
    job_id = str(job["job_id"])
    paths = [str(file_state["path"]) for file_state in file_states]
    evidence_file_states = sidecar_evidence_files_for_primaries(upload, file_states)
    evidence_paths = [str(file_state["path"]) for file_state in evidence_file_states]
    batch_id = next_eager_batch_id(job, group_name, paths)
    batch_bytes = sum(
        int(file_state["bytes"]) for file_state in [*file_states, *evidence_file_states]
    )
    storage_hint = input_upload_storage_hint(upload)
    required_gpu_free = gpu_scratch_required_bytes(batch_bytes, storage_hint) + MIN_FREE_BYTES
    if not space_checked:
        wait_for_free_space(job, GPU_RUNTIME_DIR, required_gpu_free, label="gpu eager scratch")

    batch_root = eager_batch_input_root(job_id, batch_id)
    if batch_root.exists():
        shutil.rmtree(batch_root, ignore_errors=True)
    batch_root.mkdir(parents=True, exist_ok=True)
    for file_state in [*file_states, *evidence_file_states]:
        materialize_upload_file(file_state, batch_root)
    source_paths_by_path = {
        str(file_state["path"]): batch_root / materialized_input_rel_path(file_state)
        for file_state in [*file_states, *evidence_file_states]
    }
    tasks: list[TaskName] = ["archive_video"]
    container_metadata, container_metadata_changed = container_metadata_for_gpu_payload(
        job,
        upload,
        file_states,
        group_name=group_name,
        group_config=group_config,
        tasks=tasks,
        source_paths_by_path=source_paths_by_path,
    )
    if container_metadata_changed:
        upload = save_input_upload_raw(upload)
    write_group_filesystem_metadata(batch_root, group_name, [*file_states, *evidence_file_states])
    source_artifacts_sidecars = source_artifacts_sidecar_entries(
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
    save_job(job)

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

    upload_changed = False
    for file_state in file_states:
        if ensure_file_projection_metadata(
            upload,
            file_state,
            job=job,
            group_config=group_config,
        ):
            upload_changed = True
    if upload_changed:
        upload = save_input_upload_raw(upload)

    batch_root = eager_batch_input_root(job_id, batch_id)
    if batch_root.exists():
        shutil.rmtree(batch_root, ignore_errors=True)
    batch_root.mkdir(parents=True, exist_ok=True)
    for file_state in file_states:
        materialize_upload_file(file_state, batch_root)
    write_group_filesystem_metadata(batch_root, group_name, file_states)

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
    save_job(job)

    try:
        result = run_archive_audio_group(
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
        save_job(job)
        notify_job_issue(job, component="encoding", error=error, severity="critical")
        raise EncodingFailed(error) from exc

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
    save_job(job)
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
        status = gpu_target_request("GET", f"/v1/jobs/{gpu_job_id}")
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
        notify_job_issue(job, component="encoding", error=error, severity="critical")
        raise EncodingFailed(error)
    save_job(job)
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
            raise_if_job_canceled(job_id)
            upload = load_input_upload(str(job["input_upload_id"]))
            upload = route_completed_input_files(job, upload, groups)
            cleanup_consumed_shared_input_files(upload, eager_groups)
            upload, _ = mark_existing_eager_outputs(job, upload, groups, eager_groups, archive_dir)
            claim_running_eager_batch_files(job, upload, groups, archive_dir)
            running_gpu = running_eager_batches(job, executor="gpu")
            if running_gpu and not token:
                token = acquire_job_gpu(job)
            for batch in running_gpu:
                upload = poll_eager_gpu_batch(job, upload, batch, groups, archive_dir)
            if eager_groups_complete(job, upload, eager_groups):
                eager = eager_archive_state(job)
                eager["completed_at"] = eager.get("completed_at") or utc_timestamp_now()
                save_job(job)
                return upload

            while len(running_eager_batches(job)) < EAGER_ARCHIVE_PIPELINE_BATCHES:
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
                executor = eager_archive_executor(group_config)
                if executor is None:
                    raise RuntimeError(f"group {group_name} is not eager-archive eligible")
                batch_bytes = sum(int(file_state["bytes"]) for file_state in file_states)
                if executor == "gpu":
                    storage_hint = input_upload_storage_hint(upload)
                    required_gpu_free = (
                        gpu_scratch_required_bytes(batch_bytes, storage_hint) + MIN_FREE_BYTES
                    )
                    wait_for_free_space(
                        job,
                        GPU_RUNTIME_DIR,
                        required_gpu_free,
                        label="gpu eager scratch",
                    )
                    if not token:
                        token = acquire_job_gpu(job)
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
                    wait_for_free_space(
                        job,
                        GPU_RUNTIME_DIR,
                        batch_bytes + MIN_FREE_BYTES,
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
                save_job(job)
                retry_sleep(EAGER_ARCHIVE_WAIT_SECONDS, job_id=job_id)
                continue

            if token:
                release_job_gpu(job, token)
                token = ""
            progress = upload_group_progress(upload, eager_groups)
            job["upload_progress"] = progress
            job["phase"] = (
                f"waiting_for_eager_files:{progress['files_uploaded']}/{progress['files_total']}"
            )
            save_job(job)
            notify_upload_waiting_reminder(job, upload, progress)
            retry_sleep(EAGER_ARCHIVE_WAIT_SECONDS, job_id=job_id)
    finally:
        if token:
            release_job_gpu(job, token)


def run_job(job_id: str) -> None:
    with state_lock:
        scheduled_jobs.discard(job_id)
        if job_id in active_jobs:
            return
        active_jobs.add(job_id)
    try:
        job = load_job(job_id)
        raise_if_job_canceled(job_id)
        job["state"] = "running"
        job.setdefault("started_at", utc_timestamp_now())
        save_job(job)

        input_upload = load_input_upload(str(job["input_upload_id"]))
        storage_hint = input_upload_storage_hint(input_upload)
        gpu_job_root = GPU_RUNTIME_DIR / "jobs" / job_id
        input_dir = gpu_job_root / "input"
        archive_dir = gpu_job_root / "archive"
        review_dir = gpu_job_root / "review"

        groups = ensure_job_groups(job, input_upload)

        group_results = job.setdefault("group_results", {})
        gpu_payloads = job.setdefault("gpu_payloads", {})
        gpu_results = job.setdefault("gpu_results", {})
        review_clip_plan = dict_or_empty(dict_or_empty(job.get("review")).get("clip_plan"))

        eager_groups = eager_archive_group_names(groups)
        if eager_groups:
            input_upload = run_eager_archive_groups(
                job,
                input_upload,
                groups,
                eager_groups,
                archive_dir,
            )
            raise_if_job_canceled(job_id)

        non_eager_groups = set(str(group_name) for group_name in groups) - eager_groups
        if non_eager_groups and (
            not isinstance(job.get("routing"), dict)
            or str(input_upload.get("state") or "") == "uploaded"
        ):
            input_upload = route_completed_input_files(job, input_upload, groups)
            non_eager_groups = upload_group_names_with_files(input_upload, non_eager_groups)
        input_dir = gpu_job_root / "input"
        if non_eager_groups:
            input_upload = wait_for_upload_groups(
                job,
                str(job["input_upload_id"]),
                non_eager_groups,
                groups,
            )
            non_eager_groups = upload_group_names_with_files(input_upload, non_eager_groups)
        if non_eager_groups:
            non_eager_bytes = upload_bytes_for_groups(input_upload, non_eager_groups)
            required_gpu_free = (
                gpu_scratch_required_bytes(non_eager_bytes, storage_hint) + MIN_FREE_BYTES
            )
            wait_for_free_space(job, GPU_RUNTIME_DIR, required_gpu_free, label="gpu scratch")
            job["phase"] = "preparing_input"
            save_job(job)
            input_dir = prepare_shared_input_tree(
                input_upload,
                non_eager_groups,
                job=job,
            )
            raise_if_job_canceled(job_id)

        if is_review_sweep_job(job):
            run_review_sweep_job(
                job,
                input_upload=input_upload,
                groups=groups,
                input_dir=input_dir,
                gpu_job_root=gpu_job_root,
                review_dir=review_dir,
            )
            raise_if_job_canceled(job_id)
            job["phase"] = "done"
            job["state"] = "succeeded"
            job["finished_at"] = utc_timestamp_now()
            save_job(job)
            if should_cleanup_local_work_on_success(job):
                cleanup_terminal_job(job)
            compact_terminal_job_state(job)
            save_job(job)
            notify_job_event(job, "job.succeeded", "Munchy job completed successfully.")
            return

        gpu_work: list[tuple[str, dict[str, Any], list[TaskName]]] = []
        for group_name, group_config in groups.items():
            if str(group_name) in eager_groups:
                continue
            if str(group_name) not in non_eager_groups:
                continue
            validate_group_name(str(group_name))
            group_output_mode = normalize_output_mode(
                str(group_config.get("output_mode") or "video")
            )
            if group_output_mode not in {"video", "audio", "preserve"}:
                raise RuntimeError(
                    f"unsupported output_mode for group {group_name}: {group_output_mode}"
                )
            if group_output_mode == "preserve" and not group_results.get(group_name, {}).get(
                "preserve_copied"
            ):
                job["phase"] = f"copying_preserve:{group_name}"
                save_job(job)
                copy_preserve_group_files(
                    input_upload,
                    group_name=group_name,
                    source_root=input_dir / group_name,
                    dest_root=archive_dir / group_name,
                )
                preserve_source_artifacts = build_preserve_group_source_artifacts(
                    input_upload,
                    group_name=group_name,
                    source_root=input_dir / group_name,
                    output_root=archive_dir / group_name,
                )
                input_upload = save_input_upload_raw(input_upload)
                group_results[group_name] = {
                    **group_results.get(group_name, {}),
                    "preserve_copied": True,
                    "preserve_source_artifacts": preserve_source_artifacts,
                    "copied_at": utc_timestamp_now(),
                }
                save_job(job)
                raise_if_job_canceled(job_id)

            tasks = list(group_config.get("tasks") or [])
            if group_output_mode == "preserve":
                tasks = [task for task in tasks if task not in {"archive_video", "archive_audio"}]
            if "archive_audio" in tasks and not group_results.get(group_name, {}).get(
                "archive_audio"
            ):
                job["phase"] = f"archive_audio:{group_name}"
                save_job(job)
                audio_file_states = mutable_primary_upload_files_for_groups(
                    input_upload,
                    {group_name},
                )
                audio_rel_paths = {
                    upload_file_group_rel_for_state(file_state, group_name).as_posix()
                    for file_state in audio_file_states
                }
                group_results[group_name] = {
                    **group_results.get(group_name, {}),
                    "archive_audio": run_archive_audio_group(
                        input_root=input_dir / group_name,
                        output_root=archive_dir / group_name,
                        group_config=group_config,
                        source_rel_paths=audio_rel_paths,
                        source_artifacts_sidecars=source_artifacts_sidecar_entries(
                            input_upload,
                            audio_file_states,
                            group_name=group_name,
                            materialized_group_root=input_dir / group_name,
                        ),
                    ),
                    "archive_audio_at": utc_timestamp_now(),
                }
                save_job(job)
                raise_if_job_canceled(job_id)
            gpu_target_tasks = [task for task in tasks if str(task) in GPU_TARGET_TASKS]
            if gpu_target_tasks and group_name not in gpu_results:
                gpu_work.append((str(group_name), group_config, gpu_target_tasks))

        if gpu_work:
            token = acquire_job_gpu(job)
            try:
                for group_name, group_config, tasks in gpu_work:
                    raise_if_job_canceled(job_id)
                    group_file_states = mutable_primary_upload_files_for_groups(
                        input_upload,
                        {group_name},
                    )
                    container_metadata, container_metadata_changed = (
                        container_metadata_for_gpu_payload(
                            job,
                            input_upload,
                            group_file_states,
                            group_name=group_name,
                            group_config=group_config,
                            tasks=tasks,
                        )
                    )
                    if container_metadata_changed:
                        input_upload = save_input_upload_raw(input_upload)
                    gpu_job_id = gpu_group_job_id(job_id, group_name)
                    job["phase"] = f"gpu:{group_name}"
                    save_job(job)
                    gpu_payload = {
                        "job_id": gpu_job_id,
                        "input_dir": gpu_runtime_container_path(input_dir / group_name),
                        "archive_dir": gpu_runtime_container_path(archive_dir / group_name),
                        "review_dir": gpu_runtime_container_path(review_dir / group_name),
                        "profile": group_config.get("profile", "av1-nvenc-high"),
                        "tasks": tasks,
                        "collection_slug": str(job.get("collection_slug") or ""),
                        "collection_timestamp": job.get("collection_timestamp"),
                        "riverhog": {"enabled": False},
                        "review_upload": {"enabled": False},
                        "container_metadata_required": gpu_tasks_require_container_metadata(
                            tasks,
                            group_config,
                        ),
                    }
                    if group_config.get("encode_profile") is not None:
                        gpu_payload["encode_profile"] = group_config["encode_profile"]
                    if group_config.get("max_parallel_encodes") is not None:
                        gpu_payload["max_parallel_encodes"] = group_config["max_parallel_encodes"]
                    if review_clip_plan and any(
                        task in tasks for task in ("qcut_video", "audio_review")
                    ):
                        gpu_payload["review_clip_plan"] = copy.deepcopy(review_clip_plan)
                    if container_metadata:
                        gpu_payload["container_metadata"] = container_metadata
                    source_artifacts_sidecars = source_artifacts_sidecar_entries(
                        input_upload,
                        group_file_states,
                        group_name=group_name,
                        materialized_group_root=input_dir / group_name,
                        container_group_root=gpu_runtime_container_path(input_dir / group_name),
                    )
                    if source_artifacts_sidecars:
                        gpu_payload["source_artifacts_sidecars"] = source_artifacts_sidecars
                    for task_name in ("qcut_video", "audio_review"):
                        if task_name not in tasks:
                            continue
                        review_plan = load_shared_review_plan(
                            str(job["input_upload_id"]),
                            group_name,
                            task_name,
                        )
                        if review_plan is not None:
                            gpu_payload.setdefault("review_plans", {})[task_name] = review_plan
                    gpu_payloads[group_name] = gpu_payload
                    save_job(job)
                    start_gpu_job(gpu_payload)
                    gpu_results[group_name] = wait_gpu_job(
                        gpu_job_id,
                        gpu_payload=gpu_payload,
                        job=job,
                    )
                    remember_review_plans_from_gpu_result(
                        job,
                        group_name,
                        gpu_results[group_name],
                    )
                    if len(groups) == 1:
                        job["gpu_result"] = gpu_results[group_name]
                    else:
                        job["gpu_result"] = {"state": "succeeded", "groups": gpu_results}
                    save_job(job)
            finally:
                release_job_gpu(job, token)
        raise_if_job_canceled(job_id)
        input_upload = load_input_upload(str(job["input_upload_id"]))
        job["phase"] = "metadata_projection"
        save_job(job)
        wait_for_riverhog_eager_upload_quiescent(job)
        input_upload = write_metadata_projection_sidecars(job, input_upload, groups, archive_dir)
        raise_if_job_canceled(job_id)
        if isinstance(job.get("routing"), dict):
            write_routing_manifest(job, input_upload, groups, archive_dir)

        workflow_mode = str(job.get("workflow_mode") or "collection_archive")
        if workflow_mode == "review":
            job["phase"] = "review_handoff"
            save_job(job)
            review_config = dict_or_empty(job.get("review"))
            job["review_handoff_result"] = upload_target(
                job,
                review_dir,
                config=dict_or_empty(review_config.get("target")),
            )
            job["collection_archive_target_upload_result"] = None
            job["riverhog_upload_result"] = None
            save_job(job)
            raise_if_job_canceled(job_id)
        else:
            collection_archive = dict_or_empty(job.get("collection_archive"))
            destination = str(collection_archive.get("destination") or "riverhog")
            if destination == "target":
                target_config = dict_or_empty(collection_archive.get("target"))
                job["phase"] = "collection_archive_target_upload"
                save_job(job)
                job["collection_archive_target_upload_result"] = upload_target(
                    job,
                    archive_dir,
                    config=target_config,
                    source_label="collection archive",
                    result_key="collection_archive_target_upload_result",
                    phase="collection_archive_target_upload",
                    component="collection_archive_target_upload",
                    event="collection_archive.handoff",
                    allow_empty=False,
                )
                job["review_handoff_result"] = None
                job["riverhog_upload_result"] = None
                save_job(job)
                raise_if_job_canceled(job_id)
            elif destination == "riverhog":
                job["phase"] = "riverhog_upload"
                save_job(job)
                job["collection_archive_target_upload_result"] = None
                job["review_handoff_result"] = None
                job["riverhog_upload_result"] = upload_to_riverhog(job, archive_dir)
                save_job(job)
                raise_if_job_canceled(job_id)
            else:
                raise RuntimeError(f"unsupported collection archive destination: {destination}")

        job["phase"] = "done"
        job["state"] = "succeeded"
        job["finished_at"] = utc_timestamp_now()
        save_job(job)
        if should_cleanup_local_work_on_success(job):
            cleanup_terminal_job(job)
        compact_terminal_job_state(job)
        save_job(job)
        notify_job_event(job, "job.succeeded", "Munchy job completed successfully.")
    except JobCanceled as exc:
        log.info("job %s canceled: %s", job_id, exc)
        try:
            job = load_job(job_id)
        except HTTPException:
            job = {"job_id": job_id}
        finalize_canceled_job(job, reason="job_canceled")
    except Exception as exc:
        log.exception("job %s failed", job_id)
        try:
            job = load_job(job_id)
        except HTTPException:
            job = {"job_id": job_id}
        job["state"] = "failed"
        job["error"] = str(exc)
        job["finished_at"] = utc_timestamp_now()
        write_job_debug_bundle(
            job,
            reason="encoding_failed" if isinstance(exc, EncodingFailed) else "job_failed",
            error=exc,
        )
        if should_cancel_riverhog_upload_session_on_failure(job, exc):
            cancel_riverhog_upload_session(
                job,
                reason="encoding_failed" if isinstance(exc, EncodingFailed) else "job_failed",
            )
        else:
            riverhog_session_state(job)["preserved_after_failure_at"] = utc_timestamp_now()
        save_job(job)
        if isinstance(exc, EncodingFailed):
            cleanup_terminal_job(job)
            compact_terminal_job_state(job)
            save_job(job)
        elif isinstance(exc, RoutingFailed):
            notify_job_issue(job, component="routing", error=exc, severity="critical")
        else:
            notify_job_issue(job, component="job", error=exc, severity="error")
    finally:
        with state_lock:
            active_jobs.discard(job_id)
        schedule_pending_jobs()


def schedule_job(
    job_id: str,
    background_tasks: BackgroundTasks | None = None,
    *,
    ignore_capacity: bool = False,
) -> bool:
    if scheduling_paused():
        log.info("scheduler is paused; leaving job queued: %s", job_id)
        return False
    with state_lock:
        if job_id in active_jobs or job_id in scheduled_jobs:
            return False
        if not ignore_capacity and running_job_slots_available() <= 0:
            return False
        scheduled_jobs.add(job_id)
    if background_tasks is None:
        thread = threading.Thread(target=run_job, args=(job_id,), daemon=True)
        thread.start()
        return True
    background_tasks.add_task(run_job, job_id)
    return True


def schedule_pending_jobs(background_tasks: BackgroundTasks | None = None) -> list[str]:
    if scheduling_paused():
        return []
    scheduled: list[str] = []
    for job in runnable_jobs_in_order():
        if running_job_slots_available() <= 0:
            break
        if not runnable_job(job):
            continue
        job_id = str(job["job_id"])
        if job_id in active_jobs or job_id in scheduled_jobs:
            continue
        if schedule_job(job_id, background_tasks, ignore_capacity=True):
            scheduled.append(job_id)
    return scheduled


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
    return {
        "status": "ok",
        "state_dir": str(STATE_DIR),
        "work_dir": str(WORK_DIR),
        "tusd_public_base_url": TUSD_PUBLIC_BASE_URL,
        "gpu_target": GPU_TARGET,
        "riverhog_upload_enabled": RIVERHOG_UPLOAD_ENABLED,
        "target_upload_enabled": TARGET_UPLOAD_ENABLED,
        "target_rclone_command": TARGET_RCLONE_COMMAND,
        "notify_enabled": NOTIFY_ENABLED,
        "scheduler_paused": scheduling_paused(),
        "running_job_limit": MAX_RUNNING_JOBS,
        "running_jobs": len(active_jobs),
        "scheduled_jobs": len(scheduled_jobs),
        "riverhog_upload_workers": RIVERHOG_UPLOAD_WORKERS,
        "riverhog_upload_worker": bool(
            riverhog_upload_thread is not None and riverhog_upload_thread.is_alive()
        ),
    }


@app.get("/v1/capabilities")
def capabilities() -> dict[str, Any]:
    return {
        "workflow_modes": ["collection_archive", "review"],
        "collection_archive": {
            "destinations": ["target", "riverhog"],
            "target_upload": {
                "methods": ["rclone", "command"],
                "modes": ["copy", "sync"],
                "template_fields": [
                    "job_id",
                    "collection_slug",
                    "collection_timestamp",
                    "run_id",
                ],
            },
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
            "methods": ["rclone", "command"],
            "modes": ["copy", "sync"],
            "template_fields": ["job_id", "device_id", "route_id", "profile_id", "run_id"],
            "sweep": {
                "axes": ["quality", "max_height", "audio_bitrate"],
                "custom_axes": "encode-profile dotted paths",
                "variants": True,
                "single_runner_job": True,
            },
            "clip_plan": {
                "target_seconds": DEFAULT_REVIEW_CLIP_TARGET_SECONDS,
                "min_seconds": DEFAULT_REVIEW_CLIP_MIN_SECONDS,
                "max_seconds": DEFAULT_REVIEW_CLIP_MAX_SECONDS,
            },
        },
        "target_upload": {
            "methods": ["rclone", "command"],
            "modes": ["copy", "sync"],
            "template_fields": [
                "job_id",
                "collection_slug",
                "collection_timestamp",
                "run_id",
            ],
        },
        "storage": {
            "same_filesystem_hardlink_discount": path_device(TUSD_DIR)
            == path_device(GPU_RUNTIME_DIR),
            "max_active_input_uploads": MAX_ACTIVE_INPUT_UPLOADS,
            "max_running_jobs": MAX_RUNNING_JOBS,
            "eager_archive_only_encoding": True,
            "eager_archive_batch_files": EAGER_ARCHIVE_BATCH_FILES,
            "eager_archive_pipeline_batches": EAGER_ARCHIVE_PIPELINE_BATCHES,
            "storage_wait_seconds": STORAGE_WAIT_SECONDS,
            "scratch_extra_multipliers": {
                "review": REVIEW_SCRATCH_EXTRA_MULTIPLIER,
                "collection_archive.target": COLLECTION_ARCHIVE_TARGET_SCRATCH_EXTRA_MULTIPLIER,
                "collection_archive.riverhog": GPU_SCRATCH_MULTIPLIER,
            },
        },
        "notify": {
            "enabled": NOTIFY_ENABLED,
            "default_enabled": DEFAULT_NOTIFY_ENABLED,
            "default_recipients": list(DEFAULT_NOTIFY_RECIPIENTS),
            "events": DEFAULT_NOTIFY_EVENTS,
            "reminder_time": NOTIFY_REMINDER_TIME,
            "reminder_timezone": NOTIFY_REMINDER_TIMEZONE,
            "operator_reminder_interval_seconds": NOTIFY_REMINDER_INTERVAL_SECONDS,
            "client_preflight_failed": True,
            "webhook_config": [
                "MUNCHY_RUNNER_NOTIFY_WEBHOOKS",
                "MUNCHY_RUNNER_NOTIFY_WEBHOOK_<RECIPIENT>",
                "MUNCHY_RUNNER_NOTIFY_DEFAULT_RECIPIENTS",
                "MUNCHY_RUNNER_NOTIFY_DEFAULT_ENABLED",
                "MUNCHY_RUNNER_NOTIFY_REMINDER_TIME",
                "MUNCHY_RUNNER_NOTIFY_REMINDER_TIMEZONE",
                "MUNCHY_RUNNER_NOTIFY_REMINDER_INTERVAL",
            ],
        },
        "operations": {
            "submit": True,
            "preflight_submission": True,
            "cancel_submission": True,
            "cancel_job": True,
            "list_jobs": True,
            "compact_job_status": True,
            "notify_preflight_failed": True,
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


def preflight_issue_notification_error(
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


@app.post("/v1/notifications/preflight-failed", status_code=202)
def notify_preflight_failed(req: ClientPreflightFailedNotificationRequest) -> dict[str, Any]:
    config = req.notify.model_dump()
    if not NOTIFY_ENABLED or not config.get("enabled"):
        return {"status": "suppressed", "reason": "notifications_disabled"}
    recipients = notify_recipients(config)
    if not recipients:
        return {"status": "suppressed", "reason": "no_recipients"}
    job = {
        "job_id": req.job_id or "",
        "run_id": req.run_id or req.collection_timestamp or "",
        "collection_slug": req.collection_slug or "",
        "collection_timestamp": req.collection_timestamp or "",
        "phase": "preflight_failed",
        "state": "failed",
    }
    first_issue = ""
    if req.failed_files and req.failed_files[0].issues:
        first = req.failed_files[0]
        first_issue = preflight_issue_notification_error(
            path=first.path,
            issue_message=first.issues[0].message,
        )
    extra: dict[str, Any] = {
        "component": "preflight",
        "error": first_issue or req.message,
        "client_source": req.source,
        "device_id": req.device_id,
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
    }
    if req.elapsed_seconds is not None:
        extra["elapsed_seconds"] = req.elapsed_seconds
    deliveries = send_notify_deliveries(
        job,
        event="job.issue",
        message=req.message,
        severity="critical",
        recipients=recipients,
        extra=extra,
    )
    return {"status": "attempted", "deliveries": deliveries}


@app.post("/v1/admin/job-templates/validate")
def validate_job_template(req: JobTemplateCreateRequest) -> dict[str, Any]:
    definition, resolved_job, digest = validated_job_template_definition(req.definition)
    return {
        "name": req.name,
        "valid": True,
        "digest": digest,
        "definition": definition,
        "resolved_job": resolved_job,
    }


@app.get("/v1/admin/job-templates")
def list_job_templates(
    page: int = 1,
    per_page: int = 25,
    sort: str = "name",
    order: str = "asc",
    q: str | None = None,
    query: str | None = None,
    enabled: bool | None = None,
) -> dict[str, Any]:
    return list_job_templates_page(
        page=page,
        per_page=per_page,
        sort=sort,
        order=order,
        query=q if q is not None else query,
        enabled=enabled,
    )


@app.post("/v1/admin/job-templates", status_code=201)
def create_job_template(req: JobTemplateCreateRequest) -> dict[str, Any]:
    return create_job_template_record(req)


@app.get("/v1/admin/job-templates/{name}")
def get_job_template(name: str) -> dict[str, Any]:
    return load_job_template(name)


@app.put("/v1/admin/job-templates/{name}")
def replace_job_template(
    name: str,
    req: JobTemplateReplaceRequest,
) -> dict[str, Any]:
    return replace_job_template_record(name, req)


@app.post("/v1/admin/job-templates/{name}/enable")
def enable_job_template(name: str, req: JobTemplateEnabledRequest) -> dict[str, Any]:
    return set_job_template_enabled_record(
        name,
        enabled=True,
        expected_revision=req.expected_revision,
    )


@app.post("/v1/admin/job-templates/{name}/disable")
def disable_job_template(name: str, req: JobTemplateEnabledRequest) -> dict[str, Any]:
    return set_job_template_enabled_record(
        name,
        enabled=False,
        expected_revision=req.expected_revision,
    )


@app.delete("/v1/admin/job-templates/{name}")
def delete_job_template(name: str, expected_revision: int) -> dict[str, Any]:
    if expected_revision < 1:
        raise HTTPException(status_code=400, detail="expected_revision must be >= 1")
    return delete_job_template_record(name, expected_revision=expected_revision)


def load_submission(submission_id: str) -> dict[str, Any]:
    job = load_job(submission_id)
    if job.get("submission_id") != submission_id:
        raise HTTPException(status_code=404, detail=f"unknown submission: {submission_id}")
    return job


@app.post("/v1/submissions/preflight")
def preflight_submission(req: SubmissionSpec) -> dict[str, Any]:
    provisional_id = f"preflight-{uuid.uuid4().hex}"
    template, job_request, storage_hint = resolved_submission(
        req,
        submission_id=provisional_id,
    )
    require_input_upload_capacity(req.files, storage_hint)
    return {
        "accepted": True,
        "template": {
            "name": template["name"],
            "revision": template["revision"],
            "digest": template["digest"],
        },
        "workflow_mode": job_request.workflow_mode,
        "files_total": len(req.files),
        "bytes_total": sum(item.bytes for item in req.files),
        "storage_hint": storage_hint.model_dump(exclude_none=True),
        "content_inspection": "after_upload",
    }


@app.post("/v1/submissions", status_code=202)
def create_submission(
    req: CreateSubmissionRequest,
    background_tasks: BackgroundTasks,
) -> dict[str, Any]:
    with state_lock:
        job, created = create_submission_state(req)
    if created:
        notify_job_event(job, "job.received", "Munchy submission received.")
    schedule_pending_jobs(background_tasks)
    return submission_response(job)


@app.get("/v1/submissions/{submission_id}")
def get_submission(submission_id: str) -> dict[str, Any]:
    return submission_response(load_submission(submission_id))


@app.post(
    "/v1/submissions/{submission_id}/files/{rel_path:path}/upload",
    status_code=201,
)
def create_or_resume_submission_file_upload(
    submission_id: str,
    rel_path: str,
) -> dict[str, Any]:
    load_submission(submission_id)
    with input_file_upload_setup_lock(submission_id, rel_path):
        return _create_or_resume_input_file_upload(submission_id, rel_path)


@app.delete("/v1/submissions/{submission_id}", status_code=202)
def cancel_submission(submission_id: str) -> dict[str, Any]:
    load_submission(submission_id)
    cancel_job(submission_id, cleanup=True)
    return submission_response(load_submission(submission_id))


@app.get("/v1/admin/scheduler")
def scheduler_status() -> dict[str, Any]:
    control = scheduler_control()
    return {
        **control,
        "active_jobs": sorted(active_jobs),
        "scheduled_jobs": sorted(scheduled_jobs),
        "running_job_limit": MAX_RUNNING_JOBS,
        "running_job_slots_available": running_job_slots_available(),
        "runnable_jobs": [str(job["job_id"]) for job in job_states() if runnable_job(job)],
    }


@app.post("/v1/admin/scheduler/pause")
def pause_scheduler() -> dict[str, Any]:
    return set_scheduling_paused(True)


@app.post("/v1/admin/scheduler/resume")
def resume_scheduler(background_tasks: BackgroundTasks) -> dict[str, Any]:
    control = set_scheduling_paused(False)
    scheduled = schedule_pending_jobs(background_tasks)
    return {**control, "scheduled_jobs": scheduled}


@app.post("/internal/tusd/hooks")
async def tusd_hooks(request: Request) -> JSONResponse:
    if (
        TUSD_HOOK_SECRET
        and request.headers.get("X-Munchy-Runner-Tusd-Hook-Secret") != TUSD_HOOK_SECRET
    ):
        return hook_error("invalid hook secret", status_code=403)
    payload = await request.json()
    event = payload.get("Event", {})
    upload = event.get("Upload", {}) if isinstance(event, dict) else {}
    metadata = upload.get("MetaData", {}) if isinstance(upload, dict) else {}
    if payload.get("Type") == "post-finish":
        target_path = str(metadata.get("target_path", "")).lstrip("/")
        upload_id = upload_id_from_target_path(target_path)
        rel_path = rel_path_from_target_path(target_path)
        if upload_id and rel_path:
            try:
                sync_shared_input_file(upload_id, rel_path)
            except Exception:
                log.exception("failed to sync shared input file after tusd post-finish")
        return JSONResponse({})
    if payload.get("Type") != "pre-create":
        return JSONResponse({})
    target_path = str(metadata.get("target_path", "")).lstrip("/")
    if not target_path:
        return hook_error("missing target_path metadata")
    prefix = ".munchy-runner/uploads/"
    if not target_path.startswith(prefix):
        return hook_error("target_path must stay within .munchy-runner/uploads/")
    if any(part in {"", ".", ".."} for part in target_path.split("/")):
        return hook_error("target_path must be normalized")
    return JSONResponse({"ChangeFileInfo": {"ID": tusd_upload_id_for_target_path(target_path)}})


def create_input_upload_state(
    *,
    input_upload_id: str,
    files: list[InputFileSpec],
    storage_hint: InputUploadStorageHint,
) -> dict[str, Any]:
    if state_exists("input-upload", input_upload_id):
        raise HTTPException(
            status_code=409,
            detail=f"input upload already exists: {input_upload_id}",
        )
    require_input_upload_capacity(files, storage_hint)
    file_states = []
    for item in files:
        target_path = target_path_for(input_upload_id, item.path)
        file_states.append(
            {
                "path": item.path,
                "bytes": item.bytes,
                "sha256": item.sha256,
                "filesystem_metadata": item.filesystem_metadata,
                "target_path": target_path,
                "input_upload_id": input_upload_id,
                "file_upload_id": tusd_upload_id_for_target_path(target_path),
                "upload_url": None,
                "structured_routing": storage_hint.structured_routing,
            }
        )
    upload = {
        "input_upload_id": input_upload_id,
        "state": "uploading",
        "created_at": utc_timestamp_now(),
        "files": file_states,
        "storage_hint": storage_hint.model_dump(exclude_none=True),
        "tusd_creation_url": TUSD_PUBLIC_BASE_URL,
    }
    return save_input_upload(upload)


def input_file_upload_response(
    *,
    upload_url: object,
    offset: int,
    length: int,
    status: dict[str, Any],
) -> dict[str, Any]:
    return {
        "protocol": "tus",
        "upload_url": public_tusd_upload_url(str(upload_url)) if upload_url else upload_url,
        "offset": offset,
        "length": length,
        "checksum_algorithm": "sha256",
        "headers": {"Tus-Resumable": "1.0.0"},
        "file": status,
    }


def _create_or_resume_input_file_upload(
    input_upload_id: str,
    rel_path: str,
) -> dict[str, Any]:
    with state_lock:
        upload = load_input_upload_raw(input_upload_id)
        file_state = find_upload_file(upload, rel_path)
        if file_state.get("consumed_at"):
            status = upload_file_status(file_state)
            return input_file_upload_response(
                upload_url=file_state.get("upload_url"),
                offset=int(file_state["bytes"]),
                length=int(file_state["bytes"]),
                status=status,
            )
        upload_url = file_state.get("upload_url")
        target_path = str(file_state["target_path"])
        length = int(file_state["bytes"])

    offset = head_tusd_upload(str(upload_url)) if upload_url else -1
    if offset < 0:
        created_upload_url = create_tusd_upload(target_path, length)
        with state_lock:
            upload = load_input_upload_raw(input_upload_id)
            file_state = find_upload_file(upload, rel_path)
            if file_state.get("consumed_at"):
                status = upload_file_status(file_state)
                return input_file_upload_response(
                    upload_url=file_state.get("upload_url") or created_upload_url,
                    offset=int(file_state["bytes"]),
                    length=int(file_state["bytes"]),
                    status=status,
                )
            existing_upload_url = file_state.get("upload_url")
            if existing_upload_url:
                upload_url = str(existing_upload_url)
                should_head_existing = True
            else:
                upload_url = created_upload_url
                offset = 0
                should_head_existing = False
                file_state["upload_url"] = upload_url
                upload = save_input_upload_raw(upload)
        if should_head_existing:
            offset = head_tusd_upload(upload_url)
            if offset < 0:
                offset = 0

    with state_lock:
        upload = load_input_upload_raw(input_upload_id)
        file_state = find_upload_file(upload, rel_path)
        if file_state.get("consumed_at"):
            status = upload_file_status(file_state)
            return input_file_upload_response(
                upload_url=file_state.get("upload_url") or upload_url,
                offset=int(file_state["bytes"]),
                length=int(file_state["bytes"]),
                status=status,
            )
        if upload_url and not file_state.get("upload_url"):
            file_state["upload_url"] = upload_url
            upload = save_input_upload_raw(upload)
        status = upload_file_status(file_state)
        length = int(file_state["bytes"])
    if offset < 0 and upload_url:
        offset = head_tusd_upload(upload_url)
    if offset < 0:
        offset = 0
    return input_file_upload_response(
        upload_url=upload_url,
        offset=offset,
        length=length,
        status=status,
    )


def create_job_state_from_request(req: CreateJobRequest) -> dict[str, Any]:
    if req.input_upload_id is None:
        raise HTTPException(status_code=400, detail="input_upload_id is required")
    input_upload = load_input_upload(req.input_upload_id)
    validate_job_storage_hint(input_upload, req)
    groups = resolve_job_groups(input_upload, req)
    routing = req.routing.model_dump(exclude_none=True) if req.routing is not None else None
    job_id = req.job_id or uuid.uuid4().hex
    if state_exists("job", job_id):
        raise HTTPException(status_code=409, detail=f"job already exists: {job_id}")
    collection_archive = req.collection_archive.model_dump()
    riverhog = {
        **req.collection_archive.riverhog.model_dump(exclude_none=True),
        "enabled": req.workflow_mode == "collection_archive"
        and req.collection_archive.destination == "riverhog",
        "upload_session_on_failure": req.riverhog_upload_session_on_failure,
    }
    job = {
        "job_id": job_id,
        "state": "queued",
        "phase": "queued",
        "created_at": utc_timestamp_now(),
        "input_upload_id": req.input_upload_id,
        "run_id": req.run_id or req.collection_timestamp or "",
        "collection_slug": req.collection_slug or "",
        "collection_timestamp": req.collection_timestamp or "",
        "workflow_mode": req.workflow_mode,
        "output_mode": req.output_mode,
        "tasks": grouped_task_union(groups) if req.groups else req.tasks,
        "profile": req.encode_profile.name
        if req.encode_profile and req.encode_profile.name
        else "av1-nvenc-high",
        "encode_profile": req.encode_profile.runner_payload()
        if req.encode_profile is not None
        else None,
        "groups": groups,
        "routing": routing,
        "riverhog_expected_primary_files_total": (
            expected_riverhog_primary_files_total_from_path_routing(
                input_upload,
                groups,
                routing,
            )
            if riverhog["enabled"] and routing is not None
            else (
                expected_riverhog_primary_files_total(input_upload, groups)
                if riverhog["enabled"]
                else 0
            )
        )
        or 0,
        "collection_archive": collection_archive,
        "riverhog": riverhog,
        "review": req.review.model_dump(exclude_none=True) if req.review is not None else None,
        "notify": req.notify.model_dump(),
        "cleanup_local_on_success": req.cleanup_local_on_success,
    }
    return save_job(job)


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
    collection_archive_destination: CollectionArchiveDestination | None = None,
    cancel_requested: bool | None = None,
    storage_wait: bool | None = None,
) -> dict[str, Any]:
    return list_job_summaries_page(
        page=page,
        per_page=per_page,
        sort=sort,
        order=order,
        query=q if q is not None else query,
        terminal=terminal,
        state=state,
        workflow_mode=workflow_mode,
        collection_archive_destination=collection_archive_destination,
        cancel_requested=cancel_requested,
        storage_wait=storage_wait,
    )


@app.get("/v1/jobs/{job_id}")
def get_job(job_id: str, compact: bool = False) -> dict[str, Any]:
    job = load_job(job_id)
    refresh_riverhog_session_from_remote(job)
    return compact_job_response(job) if compact else job_response(job)


@app.post("/v1/jobs/{job_id}/resume", status_code=202)
def resume_job(job_id: str, background_tasks: BackgroundTasks) -> dict[str, Any]:
    with state_lock:
        job = load_job(job_id)
        if job.get("state") == "succeeded":
            return job
        preserve_riverhog_session = can_resume_preserving_riverhog_session(
            job
        ) and riverhog_session_visible_for_resume(job)
        if preserve_riverhog_session:
            for key in (
                "debug_bundle_created_at",
                "debug_bundle_dir",
                "debug_bundle_reason",
                "terminal_progress",
                "terminal_state_compacted_at",
            ):
                job.pop(key, None)
            job["riverhog_resume_preserved_at"] = utc_timestamp_now()
        else:
            cancel_riverhog_upload_session(job, reason="job_resume_reset")
            reset_resumable_job_runtime_state(job)
        job["state"] = "queued"
        job["phase"] = "queued"
        job.pop("cancel_requested", None)
        job.pop("cancel_requested_at", None)
        job.pop("canceled_at", None)
        job.pop("error", None)
        job.pop("finished_at", None)
        job["_allow_clear_cancel"] = True
        job["_reset_runtime_state"] = not preserve_riverhog_session
        save_job(job)
    schedule_pending_jobs(background_tasks)
    return job


@app.post("/v1/jobs/{job_id}/cancel", status_code=202)
def cancel_job(job_id: str, cleanup: bool = False) -> dict[str, Any]:
    finalize_now = False
    with state_lock:
        job = load_job(job_id)
        if job.get("state") in TERMINAL_JOB_STATES:
            if cleanup:
                if job.get("state") == "failed":
                    write_job_debug_bundle(job, reason="terminal_failed_cleanup")
                cancel_riverhog_upload_session(job, reason="terminal_cleanup")
                cleanup_terminal_job(job)
                compact_terminal_job_state(job)
                return compact_job_response(save_job(job))
            return compact_job_response(job)
        now = utc_timestamp_now()
        job["cancel_requested"] = True
        job["cancel_requested_at"] = now
        job["cleanup_requested"] = True
        if job_id not in active_jobs:
            scheduled_jobs.discard(job_id)
            job = save_job(job)
            finalize_now = True
        else:
            job["phase"] = "cancel_requested"
            return save_job(job)
    if finalize_now:
        return finalize_canceled_job(job, reason="job_canceled")
    return job


def cleanup_once() -> dict[str, Any]:
    removed: list[str] = []
    compacted: list[str] = []
    repaired_canceled: list[str] = []
    upload_cutoff = datetime.now(UTC) - timedelta(hours=INPUT_UPLOAD_TTL_HOURS)
    orphan_upload_cutoff = datetime.now(UTC) - timedelta(hours=ORPHAN_INPUT_UPLOAD_TTL_HOURS)
    stale_canceled_jobs: list[dict[str, Any]] = []
    with state_lock:
        for job in job_states():
            job_id = str(job.get("job_id") or "")
            if (
                job_id
                and job_id not in active_jobs
                and job.get("cancel_requested")
                and job.get("state") not in TERMINAL_JOB_STATES
            ):
                stale_canceled_jobs.append(job)
    for job in stale_canceled_jobs:
        finalize_canceled_job(job, reason="stale_cancel_requested")
        repaired_canceled.append(str(job.get("job_id") or ""))

    with state_lock:
        referenced_uploads = referenced_input_upload_ids()
        for upload_state in input_upload_states():
            upload = refresh_input_upload(upload_state)
            upload_id = str(upload["input_upload_id"])
            last_activity = input_upload_last_activity(upload)
            if upload.get("state") == "uploaded":
                if upload_id in referenced_uploads or last_activity > orphan_upload_cutoff:
                    continue
                remove_input_upload_data(upload)
                delete_state("input-upload", upload_id)
                removed.append(f"orphan-input-upload:{upload_id}")
                continue
            if last_activity > upload_cutoff:
                continue
            remove_input_upload_data(upload)
            delete_state("input-upload", upload_id)
            removed.append(f"input-upload:{upload_id}")

        job_cutoff = datetime.now(UTC) - timedelta(hours=LOCAL_CLEANUP_MIN_AGE_HOURS)
        for job in job_states():
            job_id = str(job.get("job_id") or "")
            if not job_id or job_id in active_jobs:
                continue
            cleanup_due = should_cleanup_terminal_local_work(job, job_cutoff)
            removed_for_job: list[str] = []
            if cleanup_due:
                if job.get("state") == "failed":
                    write_job_debug_bundle(job, reason="maintenance_failed_cleanup")
                removed_for_job = cleanup_terminal_job(job)
            compacted_for_job = (
                job.get("state") in TERMINAL_JOB_STATES
                and (cleanup_due or bool(job.get("cleanup_completed_at")))
                and compact_terminal_job_state(job)
            )
            if removed_for_job:
                removed.append(f"job-cleanup:{job_id}")
            if compacted_for_job:
                compacted.append(job_id)
            if removed_for_job or compacted_for_job:
                save_job(job)

    vacuumed = False
    if removed or compacted or repaired_canceled:
        with state_lock:
            if not active_jobs:
                vacuum_state_store()
                vacuumed = True
    return {
        "removed": removed,
        "compacted": compacted,
        "repaired_canceled": repaired_canceled,
        "vacuumed": vacuumed,
    }


def cleanup_loop() -> None:
    while not cleanup_stop.wait(CLEANUP_INTERVAL_SECONDS):
        try:
            result = cleanup_once()
            if result["removed"] or result["compacted"] or result["repaired_canceled"]:
                log.info(
                    "maintenance cleanup removed=%s compacted=%s repaired_canceled=%s vacuumed=%s",
                    ", ".join(result["removed"]) or "-",
                    len(result["compacted"]),
                    ", ".join(result["repaired_canceled"]) or "-",
                    result["vacuumed"],
                )
        except Exception:
            log.exception("maintenance cleanup failed")


@app.post("/v1/maintenance/cleanup")
def cleanup() -> dict[str, Any]:
    return cleanup_once()


def main() -> None:
    uvicorn.run(
        "app.main:app",
        host=os.getenv("MUNCHY_RUNNER_HOST", "127.0.0.1"),
        port=int(os.getenv("MUNCHY_RUNNER_PORT", "8092")),
        log_level=os.getenv("MUNCHY_RUNNER_UVICORN_LOG_LEVEL", "info"),
        log_config=uvicorn_log_config_without_health_access_logs(),
    )


if __name__ == "__main__":
    main()
