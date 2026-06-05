from __future__ import annotations

import base64
import hashlib
import json
import logging
import logging.config
import os
import shutil
import sqlite3
import subprocess
import threading
import time
import uuid
from contextlib import asynccontextmanager, closing
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal
from urllib.parse import quote, urljoin, urlsplit, urlunsplit

import httpx
import uvicorn
from fastapi import BackgroundTasks, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from munchy.notifications import MUNCHY_WEBHOOK_EMOJI
from munchy.profiles import (
    MUNCHY_PROFILE_TARGET,
    normalize_artifact_drop_selector,
)

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
}
logging.config.dictConfig(LOGGING)
log = logging.getLogger("munchy_runner")


STATE_DIR = Path(os.getenv("MUNCHY_RUNNER_STATE_DIR", "/state")).resolve()
STATE_DB_PATH = Path(
    os.getenv("MUNCHY_RUNNER_STATE_DB", str(STATE_DIR / "runner.sqlite3"))
).resolve()
WORK_DIR = Path(os.getenv("MUNCHY_RUNNER_WORK_DIR", "/work")).resolve()
TUSD_DIR = Path(os.getenv("MUNCHY_RUNNER_TUSD_DIR", "/tusd")).resolve()
TUSD_INTERNAL_BASE_URL = os.getenv(
    "MUNCHY_RUNNER_TUSD_INTERNAL_BASE_URL", "http://127.0.0.1:8093/files"
).rstrip("/")
TUSD_PUBLIC_BASE_URL = os.getenv(
    "MUNCHY_RUNNER_TUSD_PUBLIC_BASE_URL", TUSD_INTERNAL_BASE_URL
).rstrip("/")
TUSD_HOOK_SECRET = os.getenv("MUNCHY_RUNNER_TUSD_HOOK_SECRET", "").strip()
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
REVIEW_SCRATCH_EXTRA_MULTIPLIER = float(
    os.getenv("MUNCHY_RUNNER_REVIEW_SCRATCH_EXTRA_MULTIPLIER", "0.35")
)
COLLECTION_PREVIEW_SCRATCH_EXTRA_MULTIPLIER = float(
    os.getenv("MUNCHY_RUNNER_COLLECTION_PREVIEW_SCRATCH_EXTRA_MULTIPLIER", "1.25")
)
MAX_ACTIVE_INPUT_UPLOADS = int(os.getenv("MUNCHY_RUNNER_MAX_ACTIVE_INPUT_UPLOADS", "8"))
MAX_ACTIVE_JOBS = int(os.getenv("MUNCHY_RUNNER_MAX_ACTIVE_JOBS", "2"))
RIVERHOG_UPLOAD_ENABLED = os.getenv("MUNCHY_RUNNER_RIVERHOG_UPLOAD_ENABLED", "0").lower() in {
    "1",
    "true",
    "yes",
    "on",
}
RIVERHOG_WAIT = os.getenv("MUNCHY_RUNNER_RIVERHOG_WAIT", "staged").strip() or "staged"
RIVERHOG_COMMAND = os.getenv("MUNCHY_RUNNER_RIVERHOG_COMMAND", "riverhog")
UPLOAD_ATTEMPTS = int(os.getenv("MUNCHY_RUNNER_UPLOAD_ATTEMPTS", "3"))
REVIEW_UPLOAD_ENABLED = os.getenv("MUNCHY_RUNNER_REVIEW_UPLOAD_ENABLED", "0").lower() in {
    "1",
    "true",
    "yes",
    "on",
}
REVIEW_UPLOAD_COMMAND = os.getenv("MUNCHY_RUNNER_REVIEW_UPLOAD_COMMAND", "").strip()
REVIEW_RCLONE_COMMAND = os.getenv("MUNCHY_RUNNER_REVIEW_RCLONE_COMMAND", "rclone")
NOTIFY_ENABLED = os.getenv("MUNCHY_RUNNER_NOTIFY_ENABLED", "0").lower() in {
    "1",
    "true",
    "yes",
    "on",
}
NOTIFY_ISSUE_REPEAT_SECONDS = int(os.getenv("MUNCHY_RUNNER_NOTIFY_ISSUE_REPEAT_SECONDS", "86400"))
NOTIFY_TIMEOUT_SECONDS = float(os.getenv("MUNCHY_RUNNER_NOTIFY_TIMEOUT_SECONDS", "5"))
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

UploadState = Literal["pending", "partial", "uploaded"]
ArchiveMode = Literal["av1_nvenc", "originals"]
WorkflowMode = Literal["archive", "review_only", "collection_preview"]
TaskName = Literal["archive_video", "qcut_video", "audio_review"]
ArchiveContainer = Literal["mkv", "webm"]
NotifyEvent = Literal[
    "job.received",
    "review.handoff",
    "archive.handoff",
    "job.issue",
    "job.succeeded",
]
DEFAULT_NOTIFY_EVENTS: list[NotifyEvent] = [
    "job.received",
    "review.handoff",
    "archive.handoff",
    "job.issue",
    "job.succeeded",
]
SAFE_GROUP_NAME_CHARS = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-")
TERMINAL_JOB_STATES = {"succeeded", "failed", "cancelled"}
cleanup_stop = threading.Event()
cleanup_thread: threading.Thread | None = None


def validate_profile_group_name(value: str) -> str:
    name = str(value).strip()
    if not name or name in {".", ".."}:
        raise ValueError("profile group name must not be blank, '.', or '..'")
    if "/" in name or "\\" in name:
        raise ValueError("profile group name must be a single path segment")
    if any(ch not in SAFE_GROUP_NAME_CHARS for ch in name):
        raise ValueError(
            "profile group name may contain only letters, digits, dots, underscores, and dashes"
        )
    return name


def input_path_group(path: str) -> str:
    parts = path.split("/")
    if len(parts) < 2:
        raise ValueError("input file paths must be '<profile-group>/<file>'")
    return validate_profile_group_name(parts[0])


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


class JobCancelled(RuntimeError):
    pass


@asynccontextmanager
async def lifespan(_app: FastAPI):
    global cleanup_thread
    ensure_dirs()
    init_state_store()
    if RESUME_ON_START:
        schedule_pending_jobs()
    if CLEANUP_INTERVAL_SECONDS > 0:
        cleanup_stop.clear()
        cleanup_thread = threading.Thread(target=cleanup_loop, name="cleanup-loop", daemon=True)
        cleanup_thread.start()
    try:
        yield
    finally:
        cleanup_stop.set()
        if cleanup_thread is not None:
            cleanup_thread.join(timeout=5)


app = FastAPI(title="munchy-runner", version="0.1.0", lifespan=lifespan)
state_lock = threading.RLock()
active_jobs: set[str] = set()


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
    enabled: bool = False
    wait: Literal["staged", "finalized"] = "staged"


class ReviewUploadConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    method: Literal["command", "rclone"] = "command"
    destination: str | None = Field(default=None, min_length=1, max_length=4096)
    mode: Literal["copy", "sync"] = "copy"

    @field_validator("destination")
    @classmethod
    def normalize_destination(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if not normalized:
            raise ValueError("destination must not be blank")
        return normalized

    @model_validator(mode="after")
    def require_rclone_destination(self) -> ReviewUploadConfig:
        if self.enabled and self.method == "rclone" and not self.destination:
            raise ValueError("review_upload.destination is required for rclone uploads")
        return self


class NotifyConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    recipients: list[str] = Field(default_factory=list)
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


class ArchiveAudioProfile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    codec: Literal["opus"] = "opus"
    bitrate: str | None = Field(default=None, min_length=2, max_length=16)
    sample_rate: int | None = Field(default=None, ge=8000, le=192000)
    channels: int | None = Field(default=None, ge=1, le=8)
    application: Literal["audio", "voip", "lowdelay"] | None = None
    frame_duration: float | None = None
    cutoff: int | None = Field(default=None, ge=0, le=24000)
    compression_level: int | None = Field(default=None, ge=0, le=10)
    vbr: Literal["on", "off", "constrained"] | bool | None = None

    @field_validator("bitrate")
    @classmethod
    def validate_bitrate(cls, value: str | None) -> str | None:
        if value is None:
            return None
        lowered = value.strip().lower()
        number = lowered[:-1] if lowered.endswith(("k", "m")) else lowered
        try:
            parsed = float(number)
        except ValueError as exc:
            raise ValueError("bitrate must look like 28k, 128k, or 1m") from exc
        if parsed <= 0:
            raise ValueError("bitrate must look like 28k, 128k, or 1m")
        return lowered

    @field_validator("frame_duration")
    @classmethod
    def validate_frame_duration(cls, value: float | None) -> float | None:
        if value is None:
            return None
        allowed = {2.5, 5.0, 10.0, 20.0, 40.0, 60.0}
        if float(value) not in allowed:
            raise ValueError("frame_duration must be one of 2.5, 5, 10, 20, 40, or 60")
        return float(value)


class ArchiveEncodeProfile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    codec: Literal["av1_nvenc"] = "av1_nvenc"
    container: ArchiveContainer = "mkv"
    quality: int | None = Field(default=None, ge=0, le=63)
    max_height: int | None = Field(default=None, ge=2, le=4320)
    fps_mode: Literal["passthrough", "halve_60_to_30"] = "passthrough"
    output_fps: float | None = Field(default=None, gt=0, le=240)
    scale_flags: Literal["fast_bilinear", "bilinear", "bicubic", "lanczos", "spline"] = "lanczos"
    pix_fmt: Literal["p010le", "yuv420p"] | None = None
    preset: str | None = Field(default=None, min_length=2, max_length=8)
    tune: Literal["hq", "ll", "ull", "lossless", "uhq"] | None = None
    audio: ArchiveAudioProfile = Field(default_factory=ArchiveAudioProfile)

    @field_validator("preset")
    @classmethod
    def validate_preset(cls, value: str | None) -> str | None:
        if value is None:
            return None
        lowered = value.strip().lower()
        if lowered not in {"p1", "p2", "p3", "p4", "p5", "p6", "p7"}:
            raise ValueError("preset must be p1 through p7")
        return lowered


class SourceArtifactDropProfile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    selector: str = Field(min_length=1, max_length=120)
    reason: str = Field(min_length=1, max_length=1000)

    @field_validator("selector")
    @classmethod
    def validate_selector(cls, value: str) -> str:
        return normalize_artifact_drop_selector(value)

    @field_validator("reason")
    @classmethod
    def validate_reason(cls, value: str) -> str:
        reason = value.strip()
        if not reason:
            raise ValueError("artifact drop reason must not be blank")
        return reason


class SourcePreservationProfile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    artifact_drops: list[SourceArtifactDropProfile] = Field(default_factory=list)

    @field_validator("artifact_drops")
    @classmethod
    def validate_unique_selectors(
        cls,
        value: list[SourceArtifactDropProfile],
    ) -> list[SourceArtifactDropProfile]:
        selectors = [item.selector for item in value]
        if len(selectors) != len(set(selectors)):
            raise ValueError("source artifact drop selectors must be unique")
        return value


class EncodeProfile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = 1
    target: Literal["munchy-av1-nvenc"] = MUNCHY_PROFILE_TARGET
    name: str | None = Field(default=None, min_length=1, max_length=120)
    source: SourcePreservationProfile | None = None
    archive: ArchiveEncodeProfile = Field(default_factory=ArchiveEncodeProfile)


class ProfileGroupConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    archive_mode: ArchiveMode = "av1_nvenc"
    gpu_tasks: list[TaskName] = Field(
        default_factory=lambda: ["archive_video", "qcut_video", "audio_review"]
    )
    encode_profile: EncodeProfile | None = None

    @field_validator("gpu_tasks")
    @classmethod
    def normalize_tasks(cls, value: list[TaskName]) -> list[TaskName]:
        return list(dict.fromkeys(value))


class StorageGroupHint(BaseModel):
    model_config = ConfigDict(extra="forbid")

    archive_mode: ArchiveMode = "av1_nvenc"
    gpu_tasks: list[TaskName] = Field(default_factory=list)

    @field_validator("gpu_tasks")
    @classmethod
    def normalize_tasks(cls, value: list[TaskName]) -> list[TaskName]:
        return list(dict.fromkeys(value))


class InputUploadStorageHint(BaseModel):
    model_config = ConfigDict(extra="forbid")

    workflow_mode: WorkflowMode
    archive_mode: ArchiveMode = "av1_nvenc"
    gpu_tasks: list[TaskName] = Field(default_factory=list)
    groups: dict[str, StorageGroupHint] = Field(default_factory=dict)

    @field_validator("gpu_tasks")
    @classmethod
    def normalize_tasks(cls, value: list[TaskName]) -> list[TaskName]:
        return list(dict.fromkeys(value))

    @field_validator("groups")
    @classmethod
    def normalize_groups(
        cls,
        value: dict[str, StorageGroupHint],
    ) -> dict[str, StorageGroupHint]:
        return {validate_profile_group_name(name): group for name, group in value.items()}


class CreateInputUploadRequest(BaseModel):
    upload_id: str | None = Field(default=None, min_length=1, max_length=160)
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
        for path in paths:
            input_path_group(path)
        return value


class CreateJobRequest(BaseModel):
    job_id: str | None = Field(default=None, min_length=1, max_length=180)
    input_upload_id: str | None = Field(default=None, min_length=1, max_length=180)
    collection_slug: str = Field(min_length=1, max_length=180)
    collection_timestamp: str | None = Field(default=None, min_length=16, max_length=32)
    workflow_mode: WorkflowMode = "archive"
    archive_mode: ArchiveMode = "av1_nvenc"
    gpu_tasks: list[TaskName] = Field(
        default_factory=lambda: ["archive_video", "qcut_video", "audio_review"]
    )
    encode_profile: EncodeProfile | None = None
    groups: dict[str, ProfileGroupConfig] = Field(default_factory=dict)
    riverhog: RiverhogConfig = Field(default_factory=RiverhogConfig)
    review_upload: ReviewUploadConfig = Field(default_factory=ReviewUploadConfig)
    notify: NotifyConfig = Field(default_factory=NotifyConfig)
    cleanup_local_on_success: bool = False

    @field_validator("gpu_tasks")
    @classmethod
    def normalize_tasks(cls, value: list[TaskName]) -> list[TaskName]:
        return list(dict.fromkeys(value))

    @field_validator("groups")
    @classmethod
    def normalize_groups(
        cls,
        value: dict[str, ProfileGroupConfig],
    ) -> dict[str, ProfileGroupConfig]:
        return {validate_profile_group_name(name): group for name, group in value.items()}

    @model_validator(mode="after")
    def validate_workflow_mode(self) -> CreateJobRequest:
        if self.workflow_mode == "archive":
            return self
        if self.riverhog.enabled:
            raise ValueError(f"{self.workflow_mode} jobs cannot enable Riverhog upload")
        task_lists = (
            [(name, group.archive_mode, group.gpu_tasks) for name, group in self.groups.items()]
            if self.groups
            else [("default", self.archive_mode, self.gpu_tasks)]
        )
        for name, archive_mode, tasks in task_lists:
            if self.workflow_mode == "review_only" and "archive_video" in tasks:
                raise ValueError(f"review_only group {name!r} cannot run archive_video")
            if self.workflow_mode == "review_only" and not any(
                task in tasks for task in ("qcut_video", "audio_review")
            ):
                raise ValueError(f"review_only group {name!r} requires qcut_video or audio_review")
            if (
                self.workflow_mode == "collection_preview"
                and archive_mode == "av1_nvenc"
                and "archive_video" not in tasks
            ):
                raise ValueError(f"collection_preview group {name!r} requires archive_video")
        if not self.review_upload.enabled:
            raise ValueError(f"{self.workflow_mode} jobs require review_upload.enabled")
        if self.cleanup_local_on_success:
            raise ValueError(
                f"{self.workflow_mode} jobs cannot cleanup local archive work on success"
            )
        return self


def now_iso() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def parse_iso(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def safe_parse_iso(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        return parse_iso(str(value))
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
        conn.commit()


def write_state(kind: str, item_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    payload = dict(payload)
    payload["updated_at"] = now_iso()
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
    return json.loads(str(row["payload"]))


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


def delete_state(kind: str, item_id: str) -> None:
    with closing(state_db()) as conn:
        conn.execute("DELETE FROM states WHERE kind = ? AND id = ?", (kind, item_id))
        conn.commit()


def ensure_dirs() -> None:
    for path in (STATE_DIR, WORK_DIR, TUSD_DIR, GPU_RUNTIME_DIR):
        path.mkdir(parents=True, exist_ok=True)


def tusd_upload_id_for_target_path(target_path: str) -> str:
    normalized = target_path.lstrip("/")
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
    return f".munchy-runner/uploads/by-target/{digest}"


def tusd_data_path(upload_id: str) -> Path:
    return TUSD_DIR / upload_id


def target_path_for(upload_id: str, rel_path: str) -> str:
    return f".munchy-runner/uploads/{upload_id}/{rel_path}"


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
    payload["changed_at"] = now_iso()
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


def active_job_count() -> int:
    count = 0
    for job in job_states():
        try:
            state = job.get("state")
        except Exception:
            log.exception("failed to read job state")
            continue
        if state not in TERMINAL_JOB_STATES:
            count += 1
    return count


def input_upload_remaining_bytes(upload: dict[str, Any]) -> int:
    return max(0, int(upload.get("bytes_total", 0)) - int(upload.get("uploaded_bytes", 0)))


def storage_hint_group_configs(hint: InputUploadStorageHint) -> list[StorageGroupHint]:
    if hint.groups:
        return list(hint.groups.values())
    return [
        StorageGroupHint(
            archive_mode=hint.archive_mode,
            gpu_tasks=hint.gpu_tasks,
        )
    ]


def storage_hint_has_gpu_work(hint: InputUploadStorageHint) -> bool:
    return any(group.gpu_tasks for group in storage_hint_group_configs(hint))


def storage_hint_scratch_extra_multiplier(hint: InputUploadStorageHint) -> float:
    if not storage_hint_has_gpu_work(hint):
        return 0.0
    if hint.workflow_mode == "review_only":
        return REVIEW_SCRATCH_EXTRA_MULTIPLIER
    if hint.workflow_mode == "collection_preview":
        return COLLECTION_PREVIEW_SCRATCH_EXTRA_MULTIPLIER
    return GPU_SCRATCH_MULTIPLIER


def gpu_input_copy_multiplier() -> float:
    return 0.0 if path_device(TUSD_DIR) == path_device(GPU_RUNTIME_DIR) else 1.0


def gpu_scratch_required_bytes(total_bytes: int, hint: InputUploadStorageHint) -> int:
    multiplier = storage_hint_scratch_extra_multiplier(hint)
    if multiplier <= 0:
        return 0
    return int(total_bytes * (gpu_input_copy_multiplier() + multiplier))


def input_upload_storage_hint(upload: dict[str, Any]) -> InputUploadStorageHint:
    raw = upload.get("storage_hint")
    if not isinstance(raw, dict):
        raise RuntimeError(f"input upload {upload.get('upload_id')} is missing storage_hint")
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
    gpu_required = gpu_scratch_required_bytes(total_bytes, storage_hint)

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


def upload_file_status(file_state: dict[str, Any]) -> dict[str, Any]:
    upload_id = str(file_state["upload_id"])
    data_path = tusd_data_path(upload_id)
    expected = int(file_state["bytes"])
    uploaded = data_path.stat().st_size if data_path.exists() else 0
    if uploaded >= expected:
        state: UploadState = "uploaded"
    elif uploaded > 0:
        state = "partial"
    else:
        state = "pending"
    out = dict(file_state)
    out["uploaded_bytes"] = min(uploaded, expected)
    out["upload_state"] = state
    out["complete"] = state == "uploaded"
    return out


def refresh_input_upload(upload: dict[str, Any]) -> dict[str, Any]:
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
    return write_state("input-upload", str(upload["upload_id"]), upload)


def remove_input_upload_data(upload: dict[str, Any]) -> None:
    for file_state in upload.get("files", []):
        tus_path = tusd_data_path(str(file_state["upload_id"]))
        tus_path.unlink(missing_ok=True)
        tus_path.with_suffix(tus_path.suffix + ".info").unlink(missing_ok=True)
        tus_path.with_suffix(tus_path.suffix + ".lock").unlink(missing_ok=True)


def input_upload_last_activity(upload: dict[str, Any]) -> datetime:
    timestamps = [
        parsed
        for value in (upload.get("updated_at"), upload.get("created_at"))
        if (parsed := safe_parse_iso(value)) is not None
    ]
    for file_state in upload.get("files", []):
        tus_path = tusd_data_path(str(file_state["upload_id"]))
        for path in (
            tus_path,
            tus_path.with_suffix(tus_path.suffix + ".info"),
            tus_path.with_suffix(tus_path.suffix + ".lock"),
        ):
            if path.exists():
                timestamps.append(datetime.fromtimestamp(path.stat().st_mtime, UTC))
    return max(timestamps) if timestamps else datetime.now(UTC)


def load_input_upload(upload_id: str) -> dict[str, Any]:
    upload = read_state("input-upload", upload_id)
    if upload is None:
        raise HTTPException(status_code=404, detail=f"unknown input upload: {upload_id}")
    return save_input_upload(upload)


def save_job(job: dict[str, Any]) -> dict[str, Any]:
    return write_state("job", str(job["job_id"]), job)


def load_job(job_id: str) -> dict[str, Any]:
    job = read_state("job", job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"unknown job: {job_id}")
    return job


def raise_if_job_cancelled(job_id: str) -> None:
    job = read_state("job", job_id)
    if job is None:
        raise RuntimeError(f"unknown job: {job_id}")
    if job.get("cancel_requested") or job.get("state") == "cancelled":
        raise JobCancelled(f"job cancelled: {job_id}")


def require_job_capacity() -> None:
    count = active_job_count()
    if MAX_ACTIVE_JOBS > 0 and count >= MAX_ACTIVE_JOBS:
        raise HTTPException(
            status_code=429,
            detail={
                "error": "too_many_active_jobs",
                "active": count,
                "limit": MAX_ACTIVE_JOBS,
            },
        )


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
    for file_state in upload.get("files", []):
        if file_state.get("path") == rel_path:
            return file_state
    raise HTTPException(status_code=404, detail=f"unknown upload file: {rel_path}")


def link_or_copy(source: Path, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        dest.unlink()
    try:
        os.link(source, dest)
    except OSError:
        shutil.copy2(source, dest)


def copy_tree_files(source_root: Path, dest_root: Path) -> None:
    if not source_root.is_dir():
        raise RuntimeError(f"input profile group is missing: {source_root}")
    for source in source_root.rglob("*"):
        if not source.is_file():
            continue
        rel_path = source.relative_to(source_root)
        dest = dest_root / rel_path
        if dest.exists() and dest.stat().st_size == source.stat().st_size:
            continue
        link_or_copy(source, dest)


def materialize_upload(upload: dict[str, Any], dest_root: Path) -> None:
    upload = refresh_input_upload(upload)
    if upload["state"] != "uploaded":
        raise RuntimeError("input upload is not complete")
    for file_state in upload["files"]:
        rel_path = str(file_state["path"])
        source = tusd_data_path(str(file_state["upload_id"]))
        expected_bytes = int(file_state["bytes"])
        expected_sha256 = file_state.get("sha256")
        if not source.exists() or source.stat().st_size < expected_bytes:
            raise RuntimeError(f"input file is incomplete: {rel_path}")
        if expected_sha256 and file_sha256(source) != expected_sha256:
            raise RuntimeError(f"input file sha256 mismatch: {rel_path}")
        dest = dest_root / rel_path
        if dest.exists() and dest.stat().st_size == expected_bytes:
            if not expected_sha256 or file_sha256(dest) == expected_sha256:
                continue
        link_or_copy(source, dest)


def input_upload_groups(upload: dict[str, Any]) -> list[str]:
    groups = sorted(
        {input_path_group(str(file_state["path"])) for file_state in upload.get("files", [])}
    )
    if not groups:
        raise RuntimeError("input upload does not contain any files")
    return groups


def profile_name_for(encode_profile: dict[str, Any] | None) -> str:
    if isinstance(encode_profile, dict) and encode_profile.get("name"):
        return str(encode_profile["name"])
    return "av1-nvenc-high"


def profile_group_dump(group: ProfileGroupConfig) -> dict[str, Any]:
    encode_profile = (
        group.encode_profile.model_dump(exclude_none=True)
        if group.encode_profile is not None
        else None
    )
    return {
        "archive_mode": group.archive_mode,
        "gpu_tasks": group.gpu_tasks,
        "profile": profile_name_for(encode_profile),
        "encode_profile": encode_profile,
    }


def default_profile_group(req: CreateJobRequest) -> ProfileGroupConfig:
    return ProfileGroupConfig(
        archive_mode=req.archive_mode,
        gpu_tasks=req.gpu_tasks,
        encode_profile=req.encode_profile,
    )


def storage_hint_for_job_request(req: CreateJobRequest) -> InputUploadStorageHint:
    groups = {
        name: StorageGroupHint(
            archive_mode=group.archive_mode,
            gpu_tasks=group.gpu_tasks,
        )
        for name, group in req.groups.items()
    }
    return InputUploadStorageHint(
        workflow_mode=req.workflow_mode,
        archive_mode=req.archive_mode,
        gpu_tasks=req.gpu_tasks,
        groups=groups,
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
                "input_upload_id": input_upload.get("upload_id"),
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
        return {name: profile_group_dump(req.groups[name]) for name in input_groups}

    default_group = profile_group_dump(default_profile_group(req))
    return {name: dict(default_group) for name in input_groups}


def grouped_task_union(groups: dict[str, dict[str, Any]]) -> list[TaskName]:
    tasks: list[TaskName] = []
    for group in groups.values():
        for task in group.get("gpu_tasks") or []:
            if task not in tasks:
                tasks.append(task)
    return tasks


def gpu_group_job_id(job_id: str, group_name: str) -> str:
    digest = hashlib.sha256(f"{job_id}/{group_name}".encode()).hexdigest()[:10]
    safe_group = group_name[:48]
    suffix = f"__{safe_group}__{digest}"
    return f"{job_id[: max(1, 180 - len(suffix))]}{suffix}"


def gpu_job_work_roots(job: dict[str, Any]) -> list[Path]:
    job_id = str(job["job_id"])
    roots = [GPU_RUNTIME_DIR / "jobs" / job_id]
    groups = job.get("groups")
    if isinstance(groups, dict):
        for group_name in groups:
            roots.append(GPU_RUNTIME_DIR / "jobs" / gpu_group_job_id(job_id, str(group_name)))
    return roots


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
        job["local_work_cleaned_at"] = now_iso()
        job["local_work_removed"] = removed
    return removed


def should_cleanup_local_work_on_success(job: dict[str, Any]) -> bool:
    workflow_mode = str(job.get("workflow_mode") or "archive")
    if workflow_mode in {"review_only", "collection_preview"}:
        return True
    riverhog = job.get("riverhog")
    riverhog_enabled = isinstance(riverhog, dict) and bool(riverhog.get("enabled"))
    return bool(job.get("cleanup_local_on_success") and riverhog_enabled)


def should_cleanup_terminal_local_work(job: dict[str, Any], cutoff: datetime) -> bool:
    state = str(job.get("state") or "")
    if state == "succeeded":
        return should_cleanup_local_work_on_success(job)
    if state == "cancelled":
        return True
    if state not in {"failed", "cancelled"}:
        return False
    finished_at = safe_parse_iso(job.get("finished_at"))
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
    payload: dict[str, Any] = {
        "source": "munchy",
        "emoji": MUNCHY_WEBHOOK_EMOJI,
        "event": event,
        "severity": severity,
        "message": message,
        "recipient": recipient,
        "job_id": str(job.get("job_id") or ""),
        "collection_slug": str(job.get("collection_slug") or ""),
        "collection_timestamp": str(job.get("collection_timestamp") or ""),
        "phase": str(job.get("phase") or ""),
        "state": str(job.get("state") or ""),
        "sent_at": now_iso(),
    }
    if extra:
        payload.update(extra)
    return payload


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
    config = job.get("notify") if isinstance(job.get("notify"), dict) else {}
    if not NOTIFY_ENABLED or not config.get("enabled"):
        return None
    events = config.get("events") or DEFAULT_NOTIFY_EVENTS
    if event not in events:
        return None
    recipients = [str(item) for item in config.get("recipients") or [] if str(item).strip()]
    if not recipients:
        return None

    key = dedupe_key or event
    notifications = job.setdefault("notifications", {})
    event_state = notifications.setdefault(key, {})
    now = datetime.now(UTC)
    now_text = now.isoformat().replace("+00:00", "Z")

    if event == "job.issue":
        last_fingerprint = str(event_state.get("fingerprint") or "")
        last_attempt = safe_parse_iso(event_state.get("last_attempt_at"))
        if (
            fingerprint
            and fingerprint == last_fingerprint
            and last_attempt is not None
            and (now - last_attempt).total_seconds() < NOTIFY_ISSUE_REPEAT_SECONDS
        ):
            return {"status": "suppressed", "reason": "issue_repeat_limit"}
        event_state["fingerprint"] = fingerprint or ""
        event_state["last_attempt_at"] = now_text
    elif event_state.get("sent_at"):
        return {"status": "suppressed", "reason": "already_sent"}
    else:
        event_state["last_attempt_at"] = now_text

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

    event_state["deliveries"] = deliveries
    if any(
        isinstance(item.get("status"), int) and int(item["status"]) < 400 for item in deliveries
    ):
        if event == "job.issue":
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


def retry_sleep(seconds: float, *, job_id: str | None = None) -> None:
    deadline = time.monotonic() + max(0.0, seconds)
    while True:
        if job_id is not None:
            raise_if_job_cancelled(job_id)
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
    operation: Any,
) -> dict[str, Any]:
    existing = job.get(result_key)
    if isinstance(existing, dict):
        return existing
    delay = max(1.0, HANDOFF_RETRY_INITIAL_SECONDS)
    max_delay = max(delay, HANDOFF_RETRY_MAX_SECONDS)
    job_id = str(job["job_id"])
    while True:
        raise_if_job_cancelled(job_id)
        attempts = job.setdefault("handoff_attempts", {})
        attempt = int(attempts.get(result_key) or 0) + 1
        attempts[result_key] = attempt
        attempts[f"{result_key}_last_attempt_at"] = now_iso()
        job["phase"] = phase if attempt == 1 else f"{phase}_retrying"
        save_job(job)
        try:
            result = operation()
            result["attempt"] = attempt
            result["succeeded_at"] = now_iso()
            job[result_key] = result
            job["phase"] = phase
            attempts[f"{result_key}_succeeded_at"] = result["succeeded_at"]
            attempts.pop(f"{result_key}_next_retry_at", None)
            attempts.pop(f"{result_key}_last_error", None)
            save_job(job)
            return result
        except Exception as exc:
            next_retry_at = (
                (datetime.now(UTC) + timedelta(seconds=delay))
                .isoformat()
                .replace(
                    "+00:00",
                    "Z",
                )
            )
            attempts[f"{result_key}_last_error"] = str(exc)
            attempts[f"{result_key}_next_retry_at"] = next_retry_at
            job["phase"] = f"{phase}_retrying"
            save_job(job)
            notify_job_issue(
                job,
                component=component,
                error=exc,
                attempt=attempt,
                next_retry_at=next_retry_at,
            )
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


def acquire_gpu(job_id: str) -> str:
    token = ""
    deadline = time.monotonic() + GPU_LEASE_TTL_S
    while time.monotonic() < deadline:
        raise_if_job_cancelled(job_id)
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


def release_gpu(token: str) -> None:
    if not token:
        return
    try:
        manager_request("POST", "/release", {"lease_token": token, "stop": False})
    except Exception:
        log.exception("failed to release gpu lease")


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


def wait_gpu_job(
    gpu_job_id: str,
    *,
    gpu_payload: dict[str, Any],
    job: dict[str, Any],
) -> dict[str, Any]:
    job_id = str(job["job_id"])
    next_repost = time.monotonic() + max(30.0, GPU_REPOST_SECONDS)
    while True:
        raise_if_job_cancelled(job_id)
        try:
            status = gpu_target_request("GET", f"/v1/jobs/{gpu_job_id}")
        except Exception as exc:
            notify_job_issue(job, component="gpu_target", error=exc)
            log.warning("gpu target status check failed; retrying: %s", exc)
            retry_sleep(15)
            try:
                start_gpu_job(gpu_payload)
            except Exception as start_exc:
                notify_job_issue(job, component="gpu_target", error=start_exc)
                log.warning("gpu target restart attempt failed; retrying: %s", start_exc)
            continue
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
                notify_job_issue(job, component="gpu_target", error=exc)
                log.warning("gpu target re-submit failed; retrying: %s", exc)
            next_repost = time.monotonic() + max(30.0, GPU_REPOST_SECONDS)
        time.sleep(5)


def upload_to_riverhog(job: dict[str, Any], archive_dir: Path) -> dict[str, Any] | None:
    if not job.get("riverhog", {}).get("enabled"):
        return None
    timestamp = job.get("collection_timestamp")
    if not timestamp:
        raise RuntimeError("riverhog upload requires collection_timestamp")
    wait = str(job.get("riverhog", {}).get("wait") or RIVERHOG_WAIT)
    cmd = [
        RIVERHOG_COMMAND,
        "upload",
        str(job["collection_slug"]),
        str(archive_dir),
        "--timestamp",
        str(timestamp),
        "--wait",
        wait,
        "--json",
    ]
    notify_job_event(
        job,
        "archive.handoff",
        "Archive collection is complete; handing off to Riverhog.",
        extra={"archive_dir": str(archive_dir)},
    )

    def operation() -> dict[str, Any]:
        if not RIVERHOG_UPLOAD_ENABLED:
            raise RuntimeError("riverhog upload requested, but runner Riverhog upload is disabled")
        result = run_command(cmd, action="riverhog upload")
        try:
            result["payload"] = json.loads(result["stdout"])
        except json.JSONDecodeError:
            pass
        return result

    return retry_handoff_until_success(
        job,
        result_key="riverhog_upload_result",
        phase="riverhog_upload",
        action="riverhog upload",
        component="riverhog_upload",
        operation=operation,
    )


def render_job_template(value: str, job: dict[str, Any]) -> str:
    mapping = {
        "job_id": str(job.get("job_id") or ""),
        "collection_slug": str(job.get("collection_slug") or ""),
        "collection_timestamp": str(job.get("collection_timestamp") or ""),
    }
    try:
        return value.format(**mapping)
    except KeyError as exc:
        raise RuntimeError(f"unknown review upload template field: {exc.args[0]}") from exc


def review_artifact_count(review_dir: Path) -> int:
    if not review_dir.is_dir():
        return 0
    return sum(1 for path in review_dir.rglob("*") if path.is_file())


def run_review_command(
    cmd: list[str],
    *,
    action: str,
    env: dict[str, str] | None = None,
) -> dict[str, Any]:
    return run_command(cmd, action=action, env=env)


def upload_review(
    job: dict[str, Any],
    source_dir: Path,
    *,
    source_label: str = "review",
    result_key: str = "review_upload_result",
    phase: str = "review_upload",
    component: str = "review_upload",
    allow_empty: bool = True,
) -> dict[str, Any] | None:
    config = job.get("review_upload", {})
    if not config.get("enabled"):
        return None
    if not REVIEW_UPLOAD_ENABLED:
        raise RuntimeError("review upload requested, but runner review upload is disabled")
    artifact_count = review_artifact_count(source_dir)
    if artifact_count == 0:
        if not allow_empty:
            raise RuntimeError(f"{source_label} artifacts are empty: {source_dir}")
        return {
            "status": "skipped",
            "reason": f"no {source_label} artifacts",
            "source": str(source_dir),
        }
    method = str(config.get("method") or "command")
    notify_job_event(
        job,
        "review.handoff",
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
            raise RuntimeError("review upload destination is required for rclone")
        rendered_destination = render_job_template(destination, job)
        mode = str(config.get("mode") or "copy")
        if mode not in {"copy", "sync"}:
            raise RuntimeError(f"unsupported review upload rclone mode: {mode}")
        cmd = [
            REVIEW_RCLONE_COMMAND,
            mode,
            str(source_dir),
            rendered_destination,
            "--retries",
            str(max(1, UPLOAD_ATTEMPTS)),
            "--low-level-retries",
            "10",
            "--stats",
            "30s",
        ]

        def operation() -> dict[str, Any]:
            result = run_review_command(cmd, action=f"{source_label} rclone upload")
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
            operation=operation,
        )
    if method != "command":
        raise RuntimeError(f"unsupported review upload method: {method}")
    if not REVIEW_UPLOAD_COMMAND:
        raise RuntimeError(
            "review upload requested, but MUNCHY_RUNNER_REVIEW_UPLOAD_COMMAND is empty"
        )
    env = os.environ.copy()
    env["MUNCHY_REVIEW_SOURCE"] = str(source_dir)
    env["MUNCHY_REVIEW_SOURCE_LABEL"] = source_label
    env["MUNCHY_JOB_ID"] = str(job["job_id"])
    env["MUNCHY_COLLECTION_SLUG"] = str(job["collection_slug"])
    env["MUNCHY_COLLECTION_TIMESTAMP"] = str(job.get("collection_timestamp") or "")

    def operation() -> dict[str, Any]:
        result = run_review_command(
            ["/bin/sh", "-lc", REVIEW_UPLOAD_COMMAND],
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
        operation=operation,
    )


def ensure_job_groups(job: dict[str, Any], input_upload: dict[str, Any]) -> dict[str, Any]:
    groups = job.get("groups")
    if isinstance(groups, dict) and groups:
        return groups
    groups = {
        name: {
            "archive_mode": job.get("archive_mode", "av1_nvenc"),
            "gpu_tasks": list(job.get("gpu_tasks", [])),
            "profile": job.get("profile", "av1-nvenc-high"),
            "encode_profile": job.get("encode_profile"),
        }
        for name in input_upload_groups(input_upload)
    }
    job["groups"] = groups
    save_job(job)
    return groups


def run_job(job_id: str) -> None:
    with state_lock:
        if job_id in active_jobs:
            return
        active_jobs.add(job_id)
    try:
        job = load_job(job_id)
        raise_if_job_cancelled(job_id)
        job["state"] = "running"
        job.setdefault("started_at", now_iso())
        save_job(job)

        input_upload = load_input_upload(str(job["input_upload_id"]))
        input_bytes = int(input_upload["bytes_total"])
        storage_hint = input_upload_storage_hint(input_upload)
        gpu_job_root = GPU_RUNTIME_DIR / "jobs" / job_id
        input_dir = gpu_job_root / "input"
        archive_dir = gpu_job_root / "archive"
        review_dir = gpu_job_root / "review"

        groups = ensure_job_groups(job, input_upload)

        required_gpu_free = gpu_scratch_required_bytes(input_bytes, storage_hint) + MIN_FREE_BYTES
        require_free_space(GPU_RUNTIME_DIR, required_gpu_free, label="gpu scratch")
        job["phase"] = "materializing"
        save_job(job)
        materialize_upload(input_upload, input_dir)
        raise_if_job_cancelled(job_id)

        group_results = job.setdefault("group_results", {})
        gpu_payloads = job.setdefault("gpu_payloads", {})
        gpu_results = job.setdefault("gpu_results", {})

        gpu_work: list[tuple[str, dict[str, Any], list[TaskName]]] = []
        for group_name, group_config in groups.items():
            validate_profile_group_name(str(group_name))
            group_archive_mode = str(group_config.get("archive_mode") or "av1_nvenc")
            if group_archive_mode not in {"av1_nvenc", "originals"}:
                raise RuntimeError(
                    f"unsupported archive_mode for group {group_name}: {group_archive_mode}"
                )
            if group_archive_mode == "originals" and not group_results.get(group_name, {}).get(
                "originals_copied"
            ):
                job["phase"] = f"copying_originals:{group_name}"
                save_job(job)
                copy_tree_files(input_dir / group_name, archive_dir / group_name)
                group_results[group_name] = {
                    **group_results.get(group_name, {}),
                    "originals_copied": True,
                    "copied_at": now_iso(),
                }
                save_job(job)
                raise_if_job_cancelled(job_id)

            tasks = list(group_config.get("gpu_tasks") or [])
            if group_archive_mode == "originals":
                tasks = [task for task in tasks if task != "archive_video"]
            if tasks and group_name not in gpu_results:
                gpu_work.append((str(group_name), group_config, tasks))

        if gpu_work:
            token = acquire_gpu(job_id)
            try:
                for group_name, group_config, gpu_tasks in gpu_work:
                    raise_if_job_cancelled(job_id)
                    gpu_job_id = gpu_group_job_id(job_id, group_name)
                    job["phase"] = f"gpu:{group_name}"
                    save_job(job)
                    gpu_payload = {
                        "job_id": gpu_job_id,
                        "input_dir": f"/data/jobs/{job_id}/input/{group_name}",
                        "archive_dir": f"/data/jobs/{job_id}/archive/{group_name}",
                        "review_dir": f"/data/jobs/{job_id}/review/{group_name}",
                        "profile": group_config.get("profile", "av1-nvenc-high"),
                        "tasks": gpu_tasks,
                        "collection_slug": job["collection_slug"],
                        "collection_timestamp": job.get("collection_timestamp"),
                        "riverhog": {"enabled": False},
                        "review_upload": {"enabled": False},
                    }
                    if group_config.get("encode_profile") is not None:
                        gpu_payload["encode_profile"] = group_config["encode_profile"]
                    gpu_payloads[group_name] = gpu_payload
                    save_job(job)
                    start_gpu_job(gpu_payload)
                    gpu_results[group_name] = wait_gpu_job(
                        gpu_job_id,
                        gpu_payload=gpu_payload,
                        job=job,
                    )
                    if len(groups) == 1:
                        job["gpu_result"] = gpu_results[group_name]
                    else:
                        job["gpu_result"] = {"state": "succeeded", "groups": gpu_results}
                    save_job(job)
            finally:
                release_gpu(token)
        raise_if_job_cancelled(job_id)

        workflow_mode = str(job.get("workflow_mode") or "archive")
        if workflow_mode == "collection_preview":
            job["phase"] = "collection_preview_upload"
            save_job(job)
            job["collection_preview_upload_result"] = upload_review(
                job,
                archive_dir,
                source_label="collection preview",
                result_key="collection_preview_upload_result",
                phase="collection_preview_upload",
                component="collection_preview_upload",
                allow_empty=False,
            )
            job["review_upload_result"] = None
            job["riverhog_upload_result"] = None
            save_job(job)
            raise_if_job_cancelled(job_id)
        else:
            job["phase"] = "review_upload"
            save_job(job)
            job["review_upload_result"] = upload_review(job, review_dir)
            save_job(job)
            raise_if_job_cancelled(job_id)

            if workflow_mode == "review_only":
                job["riverhog_upload_result"] = None
            else:
                job["phase"] = "riverhog_upload"
                save_job(job)
                job["riverhog_upload_result"] = upload_to_riverhog(job, archive_dir)
                save_job(job)
                raise_if_job_cancelled(job_id)

        if should_cleanup_local_work_on_success(job):
            job["phase"] = "cleanup"
            save_job(job)
            remove_job_local_work(job)
            save_job(job)

        job["phase"] = "done"
        job["state"] = "succeeded"
        job["finished_at"] = now_iso()
        save_job(job)
        notify_job_event(job, "job.succeeded", "Munchy job completed successfully.")
    except JobCancelled as exc:
        log.info("job %s cancelled: %s", job_id, exc)
        try:
            job = load_job(job_id)
        except HTTPException:
            job = {"job_id": job_id}
        job["state"] = "cancelled"
        job["phase"] = "cancelled"
        job["cancelled_at"] = now_iso()
        job["finished_at"] = job["cancelled_at"]
        job.pop("error", None)
        save_job(job)
    except Exception as exc:
        log.exception("job %s failed", job_id)
        try:
            job = load_job(job_id)
        except HTTPException:
            job = {"job_id": job_id}
        job["state"] = "failed"
        job["error"] = str(exc)
        job["finished_at"] = now_iso()
        save_job(job)
        if not isinstance(exc, EncodingFailed):
            notify_job_issue(job, component="job", error=exc, severity="error")
    finally:
        with state_lock:
            active_jobs.discard(job_id)


def schedule_job(job_id: str, background_tasks: BackgroundTasks | None = None) -> None:
    if scheduling_paused():
        log.info("scheduler is paused; leaving job queued: %s", job_id)
        return
    if background_tasks is None:
        thread = threading.Thread(target=run_job, args=(job_id,), daemon=True)
        thread.start()
        return
    background_tasks.add_task(run_job, job_id)


def schedule_pending_jobs(background_tasks: BackgroundTasks | None = None) -> list[str]:
    if scheduling_paused():
        return []
    scheduled: list[str] = []
    for job in job_states():
        if not runnable_job(job):
            continue
        job_id = str(job["job_id"])
        if job_id in active_jobs:
            continue
        schedule_job(job_id, background_tasks)
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
        "review_upload_enabled": REVIEW_UPLOAD_ENABLED,
        "review_rclone_command": REVIEW_RCLONE_COMMAND,
        "notify_enabled": NOTIFY_ENABLED,
        "scheduler_paused": scheduling_paused(),
    }


@app.get("/v1/capabilities")
def capabilities() -> dict[str, Any]:
    return {
        "workflow_modes": ["archive", "review_only", "collection_preview"],
        "archive_modes": ["av1_nvenc", "originals"],
        "gpu_tasks": ["archive_video", "qcut_video", "audio_review"],
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
        },
        "profile_groups": {
            "input_path_shape": "<profile-group>/<file>",
            "group_name_chars": "letters, digits, dots, underscores, dashes",
            "job_groups": True,
        },
        "review_upload": {
            "methods": ["rclone", "command"],
            "modes": ["copy", "sync"],
            "template_fields": ["job_id", "collection_slug", "collection_timestamp"],
        },
        "storage": {
            "input_upload_storage_hint_required": True,
            "same_filesystem_hardlink_discount": path_device(TUSD_DIR)
            == path_device(GPU_RUNTIME_DIR),
            "scratch_extra_multipliers": {
                "review_only": REVIEW_SCRATCH_EXTRA_MULTIPLIER,
                "collection_preview": COLLECTION_PREVIEW_SCRATCH_EXTRA_MULTIPLIER,
                "archive": GPU_SCRATCH_MULTIPLIER,
            },
        },
        "notify": {
            "events": DEFAULT_NOTIFY_EVENTS,
            "webhook_config": [
                "MUNCHY_RUNNER_NOTIFY_WEBHOOKS",
                "MUNCHY_RUNNER_NOTIFY_WEBHOOK_<RECIPIENT>",
            ],
        },
        "operations": {
            "cancel_job": True,
            "delete_input_upload": True,
            "pause_scheduler": True,
            "resume_scheduler": True,
        },
    }


@app.get("/v1/admin/scheduler")
def scheduler_status() -> dict[str, Any]:
    control = scheduler_control()
    return {
        **control,
        "active_jobs": sorted(active_jobs),
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
    if payload.get("Type") != "pre-create":
        return JSONResponse({})
    event = payload.get("Event", {})
    upload = event.get("Upload", {}) if isinstance(event, dict) else {}
    metadata = upload.get("MetaData", {}) if isinstance(upload, dict) else {}
    target_path = str(metadata.get("target_path", "")).lstrip("/")
    if not target_path:
        return hook_error("missing target_path metadata")
    prefix = ".munchy-runner/uploads/"
    if not target_path.startswith(prefix):
        return hook_error("target_path must stay within .munchy-runner/uploads/")
    if any(part in {"", ".", ".."} for part in target_path.split("/")):
        return hook_error("target_path must be normalized")
    return JSONResponse({"ChangeFileInfo": {"ID": tusd_upload_id_for_target_path(target_path)}})


@app.post("/v1/input-uploads", status_code=201)
def create_input_upload(req: CreateInputUploadRequest) -> dict[str, Any]:
    with state_lock:
        upload_id = req.upload_id or uuid.uuid4().hex
        if state_exists("input-upload", upload_id):
            raise HTTPException(status_code=409, detail=f"input upload already exists: {upload_id}")
        require_input_upload_capacity(req.files, req.storage_hint)
        sum(item.bytes for item in req.files)
        files = []
        for item in req.files:
            target_path = target_path_for(upload_id, item.path)
            files.append(
                {
                    "path": item.path,
                    "bytes": item.bytes,
                    "sha256": item.sha256,
                    "target_path": target_path,
                    "upload_id": tusd_upload_id_for_target_path(target_path),
                    "upload_url": None,
                }
            )
        upload = {
            "upload_id": upload_id,
            "state": "uploading",
            "created_at": now_iso(),
            "files": files,
            "storage_hint": req.storage_hint.model_dump(exclude_none=True),
            "tusd_creation_url": TUSD_PUBLIC_BASE_URL,
        }
        return save_input_upload(upload)


@app.get("/v1/input-uploads/{upload_id}")
def get_input_upload(upload_id: str) -> dict[str, Any]:
    return load_input_upload(upload_id)


@app.delete("/v1/input-uploads/{upload_id}", status_code=202)
def delete_input_upload(upload_id: str) -> dict[str, Any]:
    with state_lock:
        upload = load_input_upload(upload_id)
        referenced_jobs = [
            str(job["job_id"])
            for job in job_states()
            if str(job.get("input_upload_id") or "") == upload_id
            and job.get("state") not in TERMINAL_JOB_STATES
        ]
        if referenced_jobs:
            raise HTTPException(
                status_code=409,
                detail={
                    "error": "input_upload_referenced",
                    "upload_id": upload_id,
                    "jobs": referenced_jobs,
                },
            )
        remove_input_upload_data(upload)
        delete_state("input-upload", upload_id)
        return {
            "upload_id": upload_id,
            "state": "deleted",
            "removed_files": len(upload.get("files", [])),
        }


@app.post("/v1/input-uploads/{upload_id}/files/{rel_path:path}/upload", status_code=201)
def create_or_resume_input_file_upload(upload_id: str, rel_path: str) -> dict[str, Any]:
    with state_lock:
        upload = load_input_upload(upload_id)
        file_state = find_upload_file(upload, rel_path)
        upload_url = file_state.get("upload_url")
        offset = -1
        if upload_url:
            offset = head_tusd_upload(str(upload_url))
        if offset < 0:
            upload_url = create_tusd_upload(
                str(file_state["target_path"]), int(file_state["bytes"])
            )
            offset = 0
            file_state["upload_url"] = upload_url
        upload = save_input_upload(upload)
    return {
        "protocol": "tus",
        "upload_url": upload_url,
        "offset": offset,
        "length": file_state["bytes"],
        "checksum_algorithm": "sha256",
        "headers": {"Tus-Resumable": "1.0.0"},
        "file": upload_file_status(file_state),
    }


@app.post("/v1/jobs", status_code=202)
def create_job(req: CreateJobRequest, background_tasks: BackgroundTasks) -> dict[str, Any]:
    if req.input_upload_id is None:
        raise HTTPException(status_code=400, detail="input_upload_id is required")
    with state_lock:
        input_upload = load_input_upload(req.input_upload_id)
        if input_upload["state"] != "uploaded":
            raise HTTPException(status_code=409, detail="input upload is not complete")
        validate_job_storage_hint(input_upload, req)
        require_job_capacity()
        groups = resolve_job_groups(input_upload, req)
        job_id = req.job_id or uuid.uuid4().hex
        if state_exists("job", job_id):
            raise HTTPException(status_code=409, detail=f"job already exists: {job_id}")
        job = {
            "job_id": job_id,
            "state": "queued",
            "phase": "queued",
            "created_at": now_iso(),
            "input_upload_id": req.input_upload_id,
            "collection_slug": req.collection_slug,
            "collection_timestamp": req.collection_timestamp,
            "workflow_mode": req.workflow_mode,
            "archive_mode": req.archive_mode,
            "gpu_tasks": grouped_task_union(groups) if req.groups else req.gpu_tasks,
            "profile": req.encode_profile.name
            if req.encode_profile and req.encode_profile.name
            else "av1-nvenc-high",
            "encode_profile": req.encode_profile.model_dump(exclude_none=True)
            if req.encode_profile is not None
            else None,
            "groups": groups,
            "riverhog": req.riverhog.model_dump(),
            "review_upload": req.review_upload.model_dump(),
            "notify": req.notify.model_dump(),
            "cleanup_local_on_success": req.cleanup_local_on_success,
        }
        save_job(job)
    notify_job_event(job, "job.received", "Munchy job received.")
    schedule_job(job_id, background_tasks)
    return job


@app.get("/v1/jobs/{job_id}")
def get_job(job_id: str) -> dict[str, Any]:
    return load_job(job_id)


@app.post("/v1/jobs/{job_id}/resume", status_code=202)
def resume_job(job_id: str, background_tasks: BackgroundTasks) -> dict[str, Any]:
    with state_lock:
        job = load_job(job_id)
        if job.get("state") == "succeeded":
            return job
        if job.get("state") not in {"queued", "running"}:
            require_job_capacity()
        job["state"] = "queued"
        job["phase"] = "queued"
        job.pop("cancel_requested", None)
        job.pop("cancel_requested_at", None)
        job.pop("cancelled_at", None)
        job.pop("error", None)
        save_job(job)
    schedule_job(job_id, background_tasks)
    return job


@app.post("/v1/jobs/{job_id}/cancel", status_code=202)
def cancel_job(job_id: str) -> dict[str, Any]:
    with state_lock:
        job = load_job(job_id)
        if job.get("state") in TERMINAL_JOB_STATES:
            return job
        now = now_iso()
        job["cancel_requested"] = True
        job["cancel_requested_at"] = now
        if job_id not in active_jobs:
            job["state"] = "cancelled"
            job["phase"] = "cancelled"
            job["cancelled_at"] = now
            job["finished_at"] = now
        else:
            job["phase"] = "cancel_requested"
        return save_job(job)


def cleanup_once() -> dict[str, Any]:
    removed: list[str] = []
    upload_cutoff = datetime.now(UTC) - timedelta(hours=INPUT_UPLOAD_TTL_HOURS)
    orphan_upload_cutoff = datetime.now(UTC) - timedelta(hours=ORPHAN_INPUT_UPLOAD_TTL_HOURS)
    with state_lock:
        referenced_uploads = referenced_input_upload_ids()
        for upload_state in input_upload_states():
            upload = refresh_input_upload(upload_state)
            upload_id = str(upload["upload_id"])
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
            if not should_cleanup_terminal_local_work(job, job_cutoff):
                continue
            local_removed = remove_job_local_work(job)
            if local_removed:
                save_job(job)
                removed.append(f"job-work:{job_id}")
    return {"removed": removed}


def cleanup_loop() -> None:
    while not cleanup_stop.wait(CLEANUP_INTERVAL_SECONDS):
        try:
            result = cleanup_once()
            if result["removed"]:
                log.info("maintenance cleanup removed: %s", ", ".join(result["removed"]))
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
    )


if __name__ == "__main__":
    main()
