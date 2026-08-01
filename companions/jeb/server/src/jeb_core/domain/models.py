from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

from media_preflight import (
    MP4_LIKE_EXTENSIONS,
)
from time_formats import format_utc_timestamp, utc_now

TERMINAL_STATES = {"target_succeeded", "cleanup_done", "superseded"}


SOURCE_REMOVAL_TTL = timedelta(minutes=15)


SOURCE_REMOVAL_CHALLENGE = re.compile(r"^(remove|purge)-source-(\d+)-([0-9a-f]{64})$")


SOURCE_PURGE_WARNING = (
    "DANGER: Jeb-managed upload, landing, or staged files selected by this plan may be "
    "the only copies. Purging permanently removes them, and Jeb cannot determine whether "
    "equivalent data exists elsewhere."
)


PREFLIGHT_MEDIA_EXTENSIONS = frozenset(MP4_LIKE_EXTENSIONS | {".mkv", ".webm"})


TARGET_PREFLIGHT_ERROR_LIMIT = 180


def format_progress_bytes(value: int) -> str:
    amount = float(value)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if amount < 1024.0 or unit == "TiB":
            if unit == "B":
                return f"{int(amount)} {unit}"
            return f"{amount:.2f} {unit}"
        amount /= 1024.0
    return f"{amount:.2f} TiB"


def target_preflight_error(*, source_id: str, error: BaseException) -> str:
    status = getattr(error, "status", None)
    reason = f"HTTP {status}" if status is not None else error.__class__.__name__
    base = f"Target rejected the submission preflight ({reason}); no upload started."
    message = (
        f"{base} Next: repair the target or template, then run "
        f"`jeb archive-now --source {source_id}`."
    )
    if len(message) <= TARGET_PREFLIGHT_ERROR_LIMIT:
        return message
    return (
        "Target rejected the submission preflight. Next: repair the target or template, "
        "then retry Jeb archive."
    )


def plural(count: int, singular: str, plural_form: str | None = None) -> str:
    if count == 1:
        return singular
    return plural_form or f"{singular}s"


class JebError(RuntimeError):
    """Base class for Jeb operational errors."""


class UnrecoverableJebError(JebError):
    """An operator-visible error that cannot be solved by retrying the same operation."""


class PreflightJebError(UnrecoverableJebError):
    """A pre-target media validation failure that needs operator repair."""


class TransientJebError(JebError):
    """A retryable transport or service issue."""


def current_time() -> datetime:
    return utc_now()


def event_timestamp(value: datetime | None = None) -> str:
    return format_utc_timestamp(value or current_time())


def run_id_for(value: datetime | None = None) -> str:
    return (value or current_time()).astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")


def parse_duration(value: Any, default: int | None = None) -> int:
    if value is None:
        if default is None:
            raise ValueError("duration is required")
        return default
    if isinstance(value, (int, float)):
        if value < 0:
            raise ValueError("duration must be non-negative")
        return int(value)
    text = str(value).strip().lower()
    match = re.fullmatch(r"(\d+(?:\.\d+)?)([smhd]?)", text)
    if not match:
        raise ValueError(f"invalid duration: {value!r}")
    number = float(match.group(1))
    unit = match.group(2) or "s"
    scale = {"s": 1, "m": 60, "h": 3600, "d": 86400}[unit]
    return int(number * scale)


def parse_size(value: Any) -> int:
    if isinstance(value, int):
        if value < 0:
            raise ValueError("size must be non-negative")
        return value
    text = str(value).strip().lower()
    match = re.fullmatch(r"(\d+(?:\.\d+)?)(b|kb|mb|gb|tb|kib|mib|gib|tib)?", text)
    if not match:
        raise ValueError(f"invalid size: {value!r}")
    number = float(match.group(1))
    unit = match.group(2) or "b"
    scale = {
        "b": 1,
        "kb": 1000,
        "mb": 1000**2,
        "gb": 1000**3,
        "tb": 1000**4,
        "kib": 1024,
        "mib": 1024**2,
        "gib": 1024**3,
        "tib": 1024**4,
    }[unit]
    return int(number * scale)


@dataclass(frozen=True)
class JebIngressConfig:
    landing_dir: Path
    tus_staging_dir: Path
    tusd_base_url: str = "http://jeb-tusd:1080/files/"
    tus_incomplete_max_age_seconds: int = 14 * 86_400
    ftp_projection: Path = Path("/state/ingress/ftp/passwd")
    ftp_uid: int = 1000
    ftp_gid: int = 1000


@dataclass(frozen=True)
class ServiceSettings:
    interval_seconds: int = 300
    state_db: Path = Path("/state/jeb.sqlite3")
    batch_dir: Path = Path("/landing/.jeb-batches")
    preflight_repair: Literal["off", "safe_remux"] = "safe_remux"
    preflight_repair_original: Literal["keep_corrupt", "delete"] = "keep_corrupt"
    preflight_repair_corrupt_dir: Path = Path("/landing/_corrupt")
    preflight_repair_ffmpeg: str = "ffmpeg"


@dataclass(frozen=True)
class LifecycleEventSettings:
    source: str = "urn:jeb"
    upstream_poll_seconds: float = 5.0
    context_retention_seconds: int = 30 * 86_400
    repeat_interval_seconds: int = 86_400
    repeat_time: str | None = None
    repeat_timezone: str = "UTC"


@dataclass(frozen=True)
class TargetConfig:
    name: str
    url: str = ""
    token: str = ""
    upload_workers: int = 4
    upload_chunk_bytes: int = 64 * 1024 * 1024
    wait_for_safe_delete: bool = True


@dataclass(frozen=True)
class JebConfig:
    service: ServiceSettings
    ingress: JebIngressConfig
    events: LifecycleEventSettings
    targets: Mapping[str, TargetConfig]


@dataclass(frozen=True)
class EligibleFile:
    path: Path
    rel: Path
    target_path: str
    bytes: int
    mtime: float
    mtime_ns: int


def stable_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))
