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


@dataclass(frozen=True, slots=True)
class RuntimeConfig:
    seaweedfs_filer_url: str
    sqlite_path: Path
    incomplete_upload_ttl: timedelta = field(default_factory=lambda: timedelta(hours=24))


def load_runtime_config() -> RuntimeConfig:
    filer_url = os.getenv("ARC_SEAWEEDFS_FILER_URL", "http://localhost:8888")
    sqlite_path_raw = os.getenv("ARC_DB_PATH", ".arc/state.sqlite3")
    ttl_raw = os.getenv("INCOMPLETE_UPLOAD_TTL", "24h")

    sqlite_path = Path(sqlite_path_raw).expanduser().resolve()
    sqlite_path.parent.mkdir(parents=True, exist_ok=True)
    incomplete_upload_ttl = _parse_duration(ttl_raw)

    return RuntimeConfig(
        seaweedfs_filer_url=filer_url,
        sqlite_path=sqlite_path,
        incomplete_upload_ttl=incomplete_upload_ttl,
    )
