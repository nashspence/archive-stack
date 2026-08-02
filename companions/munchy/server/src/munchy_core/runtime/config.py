from __future__ import annotations

import os
from pathlib import Path

from lifecycle_events.repeats import (
    normalize_event_repeat_time,
    parse_event_repeat_interval_seconds,
)
from time_formats import (
    parse_duration,
)


def env_flag(name: str, default: str = "0") -> bool:
    return os.getenv(name, default).lower() in {"1", "true", "yes", "on"}


def env_list(name: str) -> tuple[str, ...]:
    return tuple(item.strip() for item in os.getenv(name, "").split(",") if item.strip())


STATE_DIR = Path(os.getenv("MUNCHY_STATE_DIR", "/state")).resolve()


STATE_DB_PATH = Path(os.getenv("MUNCHY_STATE_DB", str(STATE_DIR / "munchy.sqlite3"))).resolve()


DIAGNOSTIC_DIR = Path(os.getenv("MUNCHY_DIAGNOSTIC_DIR", str(STATE_DIR / "diagnostics"))).resolve()


WORK_DIR = Path(os.getenv("MUNCHY_WORK_DIR", "/work")).resolve()


TUSD_DIR = Path(os.getenv("MUNCHY_TUSD_DIR", "/tusd")).resolve()


TUSD_INTERNAL_BASE_URL = os.getenv(
    "MUNCHY_TUSD_INTERNAL_BASE_URL", "http://127.0.0.1:8093/files"
).rstrip("/")


TUSD_PUBLIC_BASE_URL = os.getenv("MUNCHY_TUSD_PUBLIC_BASE_URL", TUSD_INTERNAL_BASE_URL).rstrip("/")


TUSD_HOOK_SECRET = os.getenv("MUNCHY_TUSD_HOOK_SECRET", "").strip()


ADMIN_TOKEN = os.getenv("MUNCHY_ADMIN_TOKEN", "").strip()


APPLICATION_AUTH_REQUIRED = os.getenv(
    "MUNCHY_APPLICATION_AUTH_REQUIRED", "1"
).strip().casefold() in {"1", "true", "yes", "on"}


GPU_RUNTIME_DIR = Path(
    os.getenv("MUNCHY_GPU_RUNTIME_DIR", "/gpu-runtime/munchy-av1-nvenc")
).resolve()


GPU_TARGET = os.getenv("MUNCHY_GPU_TARGET", "munchy-av1-nvenc")


GPU_LEASE_TTL_S = int(os.getenv("MUNCHY_GPU_LEASE_TTL_S", "28800"))


GPU_WAIT_S = int(os.getenv("MUNCHY_GPU_WAIT_S", "300"))


GPU_REPOST_SECONDS = float(os.getenv("MUNCHY_GPU_REPOST_SECONDS", "120"))


MIN_FREE_BYTES = int(os.getenv("MUNCHY_MIN_FREE_BYTES", str(10 * 1024 * 1024 * 1024)))


GPU_SCRATCH_MULTIPLIER = float(os.getenv("MUNCHY_GPU_SCRATCH_MULTIPLIER", "2.5"))


EAGER_ARCHIVE_SCRATCH_MULTIPLIER = float(
    os.getenv("MUNCHY_EAGER_ARCHIVE_SCRATCH_MULTIPLIER", "0.5")
)


REVIEW_SCRATCH_EXTRA_MULTIPLIER = float(os.getenv("MUNCHY_REVIEW_SCRATCH_EXTRA_MULTIPLIER", "0.35"))


BUFFERED_HANDOFF_SCRATCH_EXTRA_MULTIPLIER = float(
    os.getenv("MUNCHY_BUFFERED_HANDOFF_SCRATCH_EXTRA_MULTIPLIER", "1.25")
)


MAX_ACTIVE_INPUT_UPLOADS = int(os.getenv("MUNCHY_MAX_ACTIVE_INPUT_UPLOADS", "8"))


MAX_RUNNING_JOBS = int(os.getenv("MUNCHY_MAX_RUNNING_JOBS", "1"))


EVENT_REPEAT_INTERVAL_SECONDS = parse_event_repeat_interval_seconds(
    os.getenv("MUNCHY_EVENT_REPEAT_INTERVAL")
)


EVENT_REPEAT_TIME = normalize_event_repeat_time(os.getenv("MUNCHY_EVENT_REPEAT_TIME"))


EVENT_REPEAT_TIMEZONE = os.getenv("MUNCHY_EVENT_REPEAT_TIMEZONE", "UTC").strip() or "UTC"


EVENT_SOURCE = os.getenv("MUNCHY_EVENT_SOURCE", "urn:munchy").strip()


EVENT_CONTEXT_RETENTION = parse_duration(os.getenv("MUNCHY_EVENT_CONTEXT_RETENTION", "30d"))


ROUTING_MANIFEST_FILENAME = ".munchy-routing-manifest.json"


HANDOFF_RETRY_INITIAL_SECONDS = float(os.getenv("MUNCHY_HANDOFF_RETRY_INITIAL_SECONDS", "30"))


HANDOFF_RETRY_MAX_SECONDS = float(os.getenv("MUNCHY_HANDOFF_RETRY_MAX_SECONDS", "3600"))


RESUME_ON_START = os.getenv("MUNCHY_RESUME_ON_START", "1").lower() in {
    "1",
    "true",
    "yes",
    "on",
}


LOCAL_CLEANUP_MIN_AGE_HOURS = float(os.getenv("MUNCHY_LOCAL_CLEANUP_MIN_AGE_HOURS", "24"))


INPUT_UPLOAD_TTL_HOURS = float(os.getenv("MUNCHY_INPUT_UPLOAD_TTL_HOURS", "168"))


ORPHAN_INPUT_UPLOAD_TTL_HOURS = float(
    os.getenv("MUNCHY_ORPHAN_INPUT_UPLOAD_TTL_HOURS", str(INPUT_UPLOAD_TTL_HOURS))
)


CLEANUP_INTERVAL_SECONDS = int(os.getenv("MUNCHY_CLEANUP_INTERVAL_SECONDS", "3600"))


TERMINAL_JOB_RETENTION = parse_duration(os.getenv("MUNCHY_TERMINAL_JOB_RETENTION", "90d"))
if TERMINAL_JOB_RETENTION.total_seconds() <= 0:
    raise ValueError("MUNCHY_TERMINAL_JOB_RETENTION must be positive")


JOB_DIAGNOSTIC_RETENTION = parse_duration(os.getenv("MUNCHY_JOB_DIAGNOSTIC_RETENTION", "30d"))
if JOB_DIAGNOSTIC_RETENTION.total_seconds() <= 0:
    raise ValueError("MUNCHY_JOB_DIAGNOSTIC_RETENTION must be positive")


RETENTION_BATCH_SIZE = 500


EAGER_ARCHIVE_BATCH_FILES = max(1, int(os.getenv("MUNCHY_EAGER_ARCHIVE_BATCH_FILES", "32")))


EAGER_ARCHIVE_PIPELINE_BATCHES = max(
    1,
    int(os.getenv("MUNCHY_EAGER_ARCHIVE_PIPELINE_BATCHES", "3")),
)


EAGER_ARCHIVE_WAIT_SECONDS = max(
    1.0,
    float(os.getenv("MUNCHY_EAGER_ARCHIVE_WAIT_SECONDS", "5")),
)


STORAGE_WAIT_SECONDS = max(
    1.0,
    float(os.getenv("MUNCHY_STORAGE_WAIT_SECONDS", "60")),
)


AUDIO_ARCHIVE_MAX_PARALLEL = max(1, int(os.getenv("MUNCHY_AUDIO_ARCHIVE_WORKERS", "2")))


ARCHIVE_AUDIO_BITRATE = os.getenv("MUNCHY_AUDIO_BITRATE", "128k")
