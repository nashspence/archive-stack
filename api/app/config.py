from __future__ import annotations

import os
from pathlib import Path

GiB = 1024**3


def _get_env(name: str, default: str) -> str:
    value = os.getenv(name, default).strip()
    if not value:
        raise RuntimeError(f"{name} must not be empty")
    return value


def _gb_env(name: str, default: str) -> int:
    return int(float(_get_env(name, default)) * GiB)


ARCHIVE_ROOT = Path(_get_env("ARCHIVE_ROOT", "/var/lib/archive"))
SQLITE_PATH = Path(_get_env("SQLITE_PATH", str(ARCHIVE_ROOT / "catalog" / "catalog.sqlite3")))
REDIS_URL = _get_env("REDIS_URL", "redis://redis:6379/0")
TUSD_BASE_URL = _get_env("TUSD_BASE_URL", "http://localhost:1080/files").rstrip("/")
API_BASE_URL = _get_env("API_BASE_URL", "http://localhost:8080").rstrip("/")
API_TOKEN = _get_env("API_TOKEN", "change-me")
HOOK_SECRET = _get_env("HOOK_SECRET", "change-me")
ISO_AUTHORING_COMMAND = _get_env("ISO_AUTHORING_COMMAND", "xorriso")
AGE_CLI = _get_env("AGE_CLI", "age")
AGE_BATCHPASS_PASSPHRASE = _get_env("AGE_BATCHPASS_PASSPHRASE", "change-me")
AGE_BATCHPASS_WORK_FACTOR = _get_env("AGE_BATCHPASS_WORK_FACTOR", "18")
AGE_BATCHPASS_MAX_WORK_FACTOR = _get_env("AGE_BATCHPASS_MAX_WORK_FACTOR", "30")
OTS_CLIENT_COMMAND = _get_env("OTS_CLIENT_COMMAND", "ots")

PARTITION_TARGET = _gb_env("PARTITION_TARGET_GB", "50")
PARTITION_FILL = _gb_env("PARTITION_FILL_GB", "45")
PARTITION_SPILL_FILL = _gb_env("PARTITION_SPILL_FILL_GB", "35")
PARTITION_BUFFER_MAX = _gb_env("PARTITION_BUFFER_MAX_GB", "250")

CATALOG_DIR = SQLITE_PATH.parent
TUSD_DIR = ARCHIVE_ROOT / "tusd"
INCOMING_DIR = TUSD_DIR / "incoming"

HOT_BUFFER_ROOT = ARCHIVE_ROOT / "hot" / "buffer" / "jobs"
HOT_CACHE_STAGING_ROOT = ARCHIVE_ROOT / "hot" / "cache" / "staging"
HOT_CACHE_ROOT = ARCHIVE_ROOT / "hot" / "cache" / "discs"
HOT_MATERIALIZED_ROOT = ARCHIVE_ROOT / "hot" / "materialized" / "jobs"
EXPORT_JOBS_ROOT = ARCHIVE_ROOT / "exports" / "jobs"
PARTITIONER_STATE_DIR = ARCHIVE_ROOT / "partitions" / "state"
PARTITION_ROOTS_DIR = ARCHIVE_ROOT / "partitions" / "roots"
COLD_ISO_ROOT = ARCHIVE_ROOT / "cold" / "isos"
COLD_JOB_ROOT = ARCHIVE_ROOT / "cold" / "jobs"

STREAM_MAXLEN = 2048
DOWNLOAD_CHUNK_SIZE = 1024 * 1024
DISC_WEBHOOK_DISPATCH_INTERVAL_SECONDS = float(_get_env("DISC_WEBHOOK_DISPATCH_INTERVAL_SECONDS", "5"))
DISC_WEBHOOK_TIMEOUT_SECONDS = float(_get_env("DISC_WEBHOOK_TIMEOUT_SECONDS", "10"))
DISC_WEBHOOK_RETRY_SECONDS = float(_get_env("DISC_WEBHOOK_RETRY_SECONDS", "60"))


PARTITION_CFG = {
    "target": PARTITION_TARGET,
    "fill": PARTITION_FILL,
    "spill_fill": PARTITION_SPILL_FILL,
    "buffer_max": PARTITION_BUFFER_MAX,
}


def ensure_directories() -> None:
    for path in [
        CATALOG_DIR,
        INCOMING_DIR,
        HOT_BUFFER_ROOT,
        HOT_CACHE_STAGING_ROOT,
        HOT_CACHE_ROOT,
        HOT_MATERIALIZED_ROOT,
        EXPORT_JOBS_ROOT,
        PARTITIONER_STATE_DIR,
        PARTITION_ROOTS_DIR,
        COLD_ISO_ROOT,
        COLD_JOB_ROOT,
    ]:
        path.mkdir(parents=True, exist_ok=True)
