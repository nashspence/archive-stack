from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path

_DURATION_RE = re.compile(r"^(?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s)?$")


def _parse_duration(value: str) -> timedelta:
    m = _DURATION_RE.match(value.strip())
    if not m or not any(m.groups()):
        raise ValueError(f"invalid duration {value!r}: expected format like '24h', '30m', '90s'")
    hours = int(m.group(1) or 0)
    minutes = int(m.group(2) or 0)
    seconds = int(m.group(3) or 0)
    return timedelta(hours=hours, minutes=minutes, seconds=seconds)


def _parse_bool(value: str) -> bool:
    normalized = value.strip().casefold()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"invalid boolean {value!r}")


@dataclass(frozen=True, slots=True)
class RuntimeConfig:
    object_store: str
    s3_endpoint_url: str
    s3_region: str
    s3_bucket: str
    s3_access_key_id: str
    s3_secret_access_key: str
    s3_force_path_style: bool
    tusd_base_url: str
    tusd_hook_secret: str
    sqlite_path: Path
    incomplete_upload_ttl: timedelta = field(default_factory=lambda: timedelta(hours=24))
    upload_expiry_sweep_interval: timedelta = field(default_factory=lambda: timedelta(seconds=30))


def load_runtime_config() -> RuntimeConfig:
    object_store = os.getenv("ARC_OBJECT_STORE", "s3").strip().casefold() or "s3"
    if object_store != "s3":
        raise ValueError(f"unsupported ARC_OBJECT_STORE {object_store!r}: expected 's3'")

    sqlite_path_raw = os.getenv("ARC_DB_PATH", ".arc/state.sqlite3")
    ttl_raw = os.getenv("INCOMPLETE_UPLOAD_TTL", "24h")
    sweep_raw = os.getenv("UPLOAD_EXPIRY_SWEEP_INTERVAL", "30s")

    sqlite_path = Path(sqlite_path_raw).expanduser().resolve()
    sqlite_path.parent.mkdir(parents=True, exist_ok=True)
    incomplete_upload_ttl = _parse_duration(ttl_raw)
    upload_expiry_sweep_interval = _parse_duration(sweep_raw)

    return RuntimeConfig(
        object_store=object_store,
        s3_endpoint_url=os.getenv("ARC_S3_ENDPOINT_URL", "http://127.0.0.1:9000").rstrip("/"),
        s3_region=os.getenv("ARC_S3_REGION", "us-east-1"),
        s3_bucket=os.getenv("ARC_S3_BUCKET", "riverhog"),
        s3_access_key_id=os.getenv("ARC_S3_ACCESS_KEY_ID", "minioadmin"),
        s3_secret_access_key=os.getenv("ARC_S3_SECRET_ACCESS_KEY", "minioadmin"),
        s3_force_path_style=_parse_bool(os.getenv("ARC_S3_FORCE_PATH_STYLE", "true")),
        tusd_base_url=os.getenv("ARC_TUSD_BASE_URL", "http://127.0.0.1:1080/files").rstrip("/"),
        tusd_hook_secret=os.getenv("ARC_TUSD_HOOK_SECRET", "dev-tusd-hook-secret"),
        sqlite_path=sqlite_path,
        incomplete_upload_ttl=incomplete_upload_ttl,
        upload_expiry_sweep_interval=upload_expiry_sweep_interval,
    )
