from __future__ import annotations

import fcntl
import hashlib
import json
import logging
import os
import re
import shutil
import sqlite3
import subprocess
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath
from typing import Any, Literal, Protocol, cast

import httpx

from munchy.filesystem_metadata import collect_filesystem_metadata
from munchy.preflight import (
    MP4_LIKE_EXTENSIONS,
    MediaPreflightFile,
    MediaPreflightReport,
    MediaPreflightResult,
    run_media_preflight,
)
from munchy.profile_routing import (
    apply_sidecar_rules,
    exiftool_routing_facts,
    profile_routing_exiftool_tags,
    profile_routing_file_requires_exiftool,
    profile_routing_file_requires_probe,
    routing_exiftool_summary,
    routing_file_facts,
    routing_probe_summary,
    sidecar_exiftool_fact_requests,
)
from munchy.runner_client import (
    MunchyRunnerClient,
    RunnerInputFile,
    RunnerJobTerminalDuringUpload,
    RunnerProfileRoutingPreflightFile,
    RunnerUploadRequest,
    job_finished_cleanly,
)
from munchy.runner_client import (
    is_transient_upload_error as munchy_is_transient_upload_error,
)
from riverhog_core.domain.errors import Conflict, ServiceUnavailable
from riverhog_core.operator_reminders import (
    normalize_reminder_time,
    operator_reminder_due,
    reminder_zone,
)
from riverhog_core.runtime_config import parse_notify_webhook_map
from riverhog_core.webhooks import (
    WebhookConfig,
    build_jeb_event_payload,
    post_webhook,
    utcnow,
)

LOG = logging.getLogger("jeb")
SAFE_NAME = re.compile(r"^[A-Za-z0-9._-]+$")
TERMINAL_STATES = {"target_succeeded", "cleanup_done", "superseded"}
TRANSIENT_RETRY_INITIAL_SECONDS = 1.0
TRANSIENT_RETRY_MAX_SECONDS = 300.0
PREFLIGHT_MEDIA_EXTENSIONS = frozenset(MP4_LIKE_EXTENSIONS | {".mkv", ".webm"})
ROUTING_PREFLIGHT_NOTIFICATION_BODY_LIMIT = 180
JEB_CADENCES = frozenset({"weekly", "monthly", "seasonal", "manual"})
Cadence = Literal["weekly", "monthly", "seasonal", "manual"]


def format_progress_bytes(value: int) -> str:
    amount = float(value)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if amount < 1024.0 or unit == "TiB":
            if unit == "B":
                return f"{int(amount)} {unit}"
            return f"{amount:.2f} {unit}"
        amount /= 1024.0
    return f"{amount:.2f} TiB"


def routing_preflight_notification_message(
    *,
    source_id: str,
    file_count: int,
    unmatched_count: int,
) -> str:
    base = (
        f"Munchy routing preflight failed: {unmatched_count}/"
        f"{file_count} {plural(file_count, 'file')} unmatched."
    )
    message = f"{base} Next: fix routes, then run `jeb archive-now --account {source_id}`."
    if len(message) <= ROUTING_PREFLIGHT_NOTIFICATION_BODY_LIMIT:
        return message
    return f"{base} Next: fix routes, then retry Jeb archive."


def munchy_preflight_notification_message(*, source_id: str, error: BaseException) -> str:
    status = getattr(error, "status", None)
    reason = f"HTTP {status}" if status is not None else error.__class__.__name__
    base = f"Munchy routing preflight API failed ({reason}); no upload started."
    message = f"{base} Next: repair Munchy, then run `jeb archive-now --account {source_id}`."
    if len(message) <= ROUTING_PREFLIGHT_NOTIFICATION_BODY_LIMIT:
        return message
    return "Munchy routing preflight API failed. Next: repair Munchy, then retry Jeb archive."


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


def now() -> datetime:
    return datetime.now(UTC)


def iso(value: datetime | None = None) -> str:
    return (value or now()).isoformat().replace("+00:00", "Z")


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


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def normalize_posix(path: str | PurePosixPath) -> str:
    rel = PurePosixPath(str(path))
    if rel.is_absolute() or any(part in {"", ".", ".."} for part in rel.parts):
        raise ValueError(f"path is not normalized relative POSIX: {path}")
    return rel.as_posix()


def table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {str(row["name"]) for row in conn.execute(f"PRAGMA table_info({table})")}


def munchy_archive_mode(value: str) -> str:
    mode = str(value or "av1_nvenc").strip()
    if mode not in {"av1_nvenc", "audio", "preserve"}:
        raise ValueError("archive_mode must be av1_nvenc, audio, or preserve")
    return mode


def same_file_inode(left: Path, right: Path) -> bool:
    left_stat = left.stat()
    right_stat = right.stat()
    return left_stat.st_dev == right_stat.st_dev and left_stat.st_ino == right_stat.st_ino


def hardlink_stage_file(source: Path, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256(str(dest).encode()).hexdigest()[:12]
    part = dest.with_name(f".{dest.name}.{digest}.part")
    if dest.exists():
        if dest.is_dir():
            raise UnrecoverableJebError(f"staging path is a directory: {dest}")
        if same_file_inode(source, dest):
            return
        dest.unlink()
    try:
        os.link(source, part)
        part.replace(dest)
    except OSError as exc:
        raise UnrecoverableJebError(
            "could not hardlink source into Jeb batch; keep collector.batch_dir "
            f"on the same filesystem as source landing directories: {source} -> {dest}"
        ) from exc
    finally:
        part.unlink(missing_ok=True)


@dataclass(frozen=True)
class CollectorSettings:
    interval_seconds: int = 300
    state_db: Path = Path("/state/jeb.sqlite3")
    batch_dir: Path = Path("/landing/.jeb-batches")
    preflight_repair: Literal["off", "safe_remux"] = "safe_remux"
    preflight_repair_original: Literal["keep_corrupt", "delete"] = "keep_corrupt"
    preflight_repair_corrupt_dir: Path = Path("/landing/_corrupt")
    preflight_repair_ffmpeg: str = "ffmpeg"


@dataclass(frozen=True)
class NotifySettings:
    enabled: bool = False
    url: str = ""
    base_url: str = ""
    webhook_urls: Mapping[str, str] = field(default_factory=dict)
    recipients: tuple[str, ...] = ()
    timeout_seconds: float = 10.0
    reminder_interval_seconds: int = 86_400
    reminder_time: str | None = None
    reminder_timezone: str = "UTC"


@dataclass(frozen=True)
class TargetConfig:
    name: str
    url: str = ""
    upload_workers: int = 4
    upload_chunk_bytes: int = 64 * 1024 * 1024
    wait_for_safe_delete: bool = True


@dataclass(frozen=True)
class SourceConfig:
    id: str
    enabled: bool
    path: Path
    upload_prefix: str
    stable_seconds: int
    include_extensions: frozenset[str]


@dataclass(frozen=True)
class CollectionConfig:
    id: str
    enabled: bool
    collection_slug: str
    target: str
    notify: Mapping[str, Any]
    threshold_bytes: int
    cleanup: Literal["never", "after_target_success"]
    source_ids: tuple[str, ...]
    cadence: Cadence
    weekday: int
    hour: int
    minute: int
    munchy_job_defaults: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class JebConfig:
    collector: CollectorSettings
    notify: NotifySettings
    targets: Mapping[str, TargetConfig]
    sources: tuple[SourceConfig, ...]
    collections: tuple[CollectionConfig, ...]
    munchy_job_defaults: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class EligibleFile:
    path: Path
    rel: Path
    target_path: str
    bytes: int
    mtime: float
    mtime_ns: int


class Notifier(Protocol):
    def critical_batch_issue(
        self,
        *,
        batch: Mapping[str, Any],
        message: str,
        component: str,
        notify: Mapping[str, Any] | None = None,
    ) -> bool: ...

    def enrollment_issue(
        self,
        *,
        batch: Mapping[str, Any],
        message: str,
        component: str,
        notify: Mapping[str, Any] | None = None,
    ) -> bool: ...


class NullNotifier:
    def critical_batch_issue(
        self,
        *,
        batch: Mapping[str, Any],
        message: str,
        component: str,
        notify: Mapping[str, Any] | None = None,
    ) -> bool:
        _ = notify
        return True

    def enrollment_issue(
        self,
        *,
        batch: Mapping[str, Any],
        message: str,
        component: str,
        notify: Mapping[str, Any] | None = None,
    ) -> bool:
        _ = notify
        return True


class WebhookNotifier:
    def __init__(self, settings: NotifySettings) -> None:
        self.settings = settings

    def critical_batch_issue(
        self,
        *,
        batch: Mapping[str, Any],
        message: str,
        component: str,
        notify: Mapping[str, Any] | None = None,
    ) -> bool:
        return self.issue(
            batch=batch,
            message=message,
            component=component,
            severity="critical",
            log_label="critical jeb webhook",
            notify=notify,
        )

    def enrollment_issue(
        self,
        *,
        batch: Mapping[str, Any],
        message: str,
        component: str,
        notify: Mapping[str, Any] | None = None,
    ) -> bool:
        return self.issue(
            batch=batch,
            message=message,
            component=component,
            severity="warning",
            log_label="enrollment jeb webhook",
            notify=notify,
        )

    def issue(
        self,
        *,
        batch: Mapping[str, Any],
        message: str,
        component: str,
        severity: str,
        log_label: str,
        notify: Mapping[str, Any] | None = None,
    ) -> bool:
        if notify is None:
            if not self.settings.enabled:
                return True
            recipients: Sequence[str | None] = self.settings.recipients or (None,)
        else:
            if not bool(notify.get("enabled", True)):
                return True
            recipients = tuple(str(item) for item in sequence(notify.get("recipients"))) or (
                None,
            )
        delivered_at = utcnow()
        ok = True
        for recipient in recipients:
            url = (
                self.settings.url
                if recipient is None
                else self.settings.webhook_urls.get(recipient)
            )
            if not url:
                LOG.warning("Jeb notification recipient %s has no configured webhook", recipient)
                ok = False
                continue
            config = WebhookConfig(
                url=url,
                base_url=self.settings.base_url,
                timeout_seconds=self.settings.timeout_seconds,
            )
            payload = build_jeb_event_payload(
                event="jeb.issue",
                batch=batch,
                message=message,
                severity=severity,
                delivered_at=delivered_at,
                recipient=recipient,
                details={"component": component, "error": message},
            )
            try:
                post_webhook(config=config, payload=payload)
            except Exception:
                LOG.exception("failed to deliver %s for batch %s", log_label, batch["id"])
                ok = False
        return ok


class TargetRunner(Protocol):
    def advance(self, collector: Collector, batch_id: str) -> None: ...


class Collector:
    def __init__(
        self,
        config: JebConfig,
        *,
        target_runners: Mapping[str, TargetRunner] | None = None,
        notifier: Notifier | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.config = config
        self.sleep = sleep
        self.notifier = notifier or (
            WebhookNotifier(config.notify) if config.notify.enabled else NullNotifier()
        )
        self.target_runners: dict[str, TargetRunner] = {
            "munchy": MunchyTargetRunner(),
        }
        if target_runners:
            self.target_runners.update(target_runners)

    def connect(self) -> sqlite3.Connection:
        self.config.collector.state_db.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.config.collector.state_db, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=30000")
        return conn

    def init_db(self) -> None:
        self.config.collector.batch_dir.mkdir(parents=True, exist_ok=True)
        with self.connect() as conn:
            self.create_batch_schema(conn)
            self.ensure_routing_preflight_schema(conn)

    def create_batch_schema(self, conn: sqlite3.Connection) -> None:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS batches (
                id TEXT PRIMARY KEY,
                collection_id TEXT NOT NULL,
                target_name TEXT NOT NULL,
                collection_slug TEXT NOT NULL,
                collection_timestamp TEXT NOT NULL,
                cleanup TEXT NOT NULL,
                manifest_digest TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS batch_attempts (
                id TEXT PRIMARY KEY,
                batch_id TEXT NOT NULL,
                attempt_number INTEGER NOT NULL,
                state TEXT NOT NULL,
                input_upload_id TEXT,
                job_id TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                last_error TEXT,
                notified_error_fingerprint TEXT,
                notified_error_at TEXT,
                UNIQUE(batch_id, attempt_number),
                FOREIGN KEY(batch_id) REFERENCES batches(id)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS files (
                batch_id TEXT NOT NULL,
                source_path TEXT NOT NULL,
                target_path TEXT NOT NULL,
                bytes INTEGER NOT NULL,
                mtime_ns INTEGER NOT NULL,
                sha256 TEXT,
                PRIMARY KEY (batch_id, target_path),
                FOREIGN KEY(batch_id) REFERENCES batches(id)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS attempt_files (
                attempt_id TEXT NOT NULL,
                target_path TEXT NOT NULL,
                staging_path TEXT NOT NULL,
                staged_at TEXT,
                PRIMARY KEY (attempt_id, target_path),
                FOREIGN KEY(attempt_id) REFERENCES batch_attempts(id)
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_jeb_batches_collection_period "
            "ON batches(collection_id, collection_timestamp)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_jeb_batch_attempts_state "
            "ON batch_attempts(state, created_at)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_jeb_batch_attempts_batch_state "
            "ON batch_attempts(batch_id, state)"
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_jeb_files_batch ON files(batch_id)")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_jeb_attempt_files_attempt ON attempt_files(attempt_id)"
        )

    def ensure_routing_preflight_schema(self, conn: sqlite3.Connection) -> None:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS routing_preflight_failures (
                source_id TEXT PRIMARY KEY,
                state TEXT NOT NULL,
                failure_kind TEXT NOT NULL DEFAULT 'profile_routing',
                collection_id TEXT NOT NULL,
                collection_slug TEXT NOT NULL,
                target_name TEXT NOT NULL,
                source_paths_json TEXT NOT NULL,
                failure_json TEXT NOT NULL,
                fingerprint TEXT NOT NULL,
                message TEXT NOT NULL,
                file_count INTEGER NOT NULL,
                total_bytes INTEGER NOT NULL,
                unmatched_count INTEGER NOT NULL,
                first_seen_at TEXT NOT NULL,
                last_seen_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                resolved_at TEXT,
                notified_error_fingerprint TEXT,
                notified_error_at TEXT
            )
            """
        )
        columns = table_columns(conn, "routing_preflight_failures")
        if "failure_kind" not in columns:
            conn.execute(
                "ALTER TABLE routing_preflight_failures "
                "ADD COLUMN failure_kind TEXT NOT NULL DEFAULT 'profile_routing'"
            )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_jeb_routing_preflight_failures_state "
            "ON routing_preflight_failures(state, updated_at)"
        )

    def run_forever(self) -> None:
        self.init_db()
        while True:
            self.run_once()
            self.sleep(self.config.collector.interval_seconds)

    def run_once(self) -> None:
        self.init_db()
        self.resolve_inactive_routing_preflight_failures()
        for batch_id in self.active_batch_ids():
            self.process_batch(batch_id)
        active_collections = {str(row["collection_id"]) for row in self.active_batches()}
        for collection in self.config.collections:
            if collection.enabled and collection.id not in active_collections:
                self.discover_collection(collection)
        self.notify_routing_preflight_failures()

    def active_batches(self) -> list[sqlite3.Row]:
        terminal = tuple(sorted(TERMINAL_STATES))
        placeholders = ", ".join("?" for _ in terminal)
        with self.connect() as conn:
            return conn.execute(
                f"""
                SELECT
                    a.id,
                    a.batch_id,
                    a.attempt_number,
                    a.state,
                    b.collection_id,
                    b.target_name,
                    b.collection_slug,
                    b.collection_timestamp,
                    b.cleanup,
                    b.manifest_digest,
                    a.input_upload_id,
                    a.job_id,
                    a.created_at,
                    a.updated_at,
                    a.last_error,
                    a.notified_error_fingerprint,
                    a.notified_error_at
                FROM batch_attempts a
                JOIN batches b ON b.id = a.batch_id
                WHERE a.state NOT IN ({placeholders})
                ORDER BY a.created_at
                """,
                terminal,
            ).fetchall()

    def active_batch_ids(self) -> list[str]:
        return [str(row["id"]) for row in self.active_batches()]

    def source_by_id(self, source_id: str) -> SourceConfig:
        for source in self.config.sources:
            if source.id == source_id:
                return source
        raise KeyError(source_id)

    def collection_by_id(self, collection_id: str) -> CollectionConfig:
        for collection in self.config.collections:
            if collection.id == collection_id:
                return collection
        raise KeyError(collection_id)

    def target_by_name(self, target_name: str) -> TargetConfig:
        try:
            return self.config.targets[target_name]
        except KeyError as exc:
            raise UnrecoverableJebError(f"unknown target {target_name!r}") from exc

    def load_batch(self, batch_id: str) -> sqlite3.Row:
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT
                    a.id,
                    a.batch_id,
                    a.attempt_number,
                    a.state,
                    b.collection_id,
                    b.target_name,
                    b.collection_slug,
                    b.collection_timestamp,
                    b.cleanup,
                    b.manifest_digest,
                    a.input_upload_id,
                    a.job_id,
                    a.created_at,
                    a.updated_at,
                    a.last_error,
                    a.notified_error_fingerprint,
                    a.notified_error_at
                FROM batch_attempts a
                JOIN batches b ON b.id = a.batch_id
                WHERE a.id = ?
                """,
                (batch_id,),
            ).fetchone()
        if row is None:
            raise KeyError(batch_id)
        return cast(sqlite3.Row, row)

    def batch_files(self, batch_id: str) -> list[sqlite3.Row]:
        with self.connect() as conn:
            return conn.execute(
                """
                SELECT
                    f.batch_id,
                    af.attempt_id,
                    f.source_path,
                    af.staging_path,
                    f.target_path,
                    f.bytes,
                    f.mtime_ns,
                    f.sha256,
                    af.staged_at
                FROM batch_attempts a
                JOIN files f ON f.batch_id = a.batch_id
                JOIN attempt_files af
                  ON af.attempt_id = a.id
                 AND af.target_path = f.target_path
                WHERE a.id = ?
                ORDER BY f.target_path
                """,
                (batch_id,),
            ).fetchall()

    def set_batch_state(self, batch_id: str, state: str, error: str | None = None) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE batch_attempts
                SET state = ?, updated_at = ?, last_error = ?
                WHERE id = ?
                """,
                (state, iso(), error, batch_id),
            )

    def set_batch_fields(self, batch_id: str, **fields: object) -> None:
        if not fields:
            return
        allowed = {
            "state",
            "input_upload_id",
            "job_id",
            "last_error",
            "notified_error_fingerprint",
            "notified_error_at",
        }
        unknown = sorted(set(fields) - allowed)
        if unknown:
            raise ValueError(f"unsupported batch field(s): {', '.join(unknown)}")
        assignments = [f"{name} = ?" for name in fields]
        values = list(fields.values())
        assignments.append("updated_at = ?")
        values.append(iso())
        values.append(batch_id)
        with self.connect() as conn:
            conn.execute(
                f"UPDATE batch_attempts SET {', '.join(assignments)} WHERE id = ?",
                values,
            )

    def discover_collection(
        self,
        collection: CollectionConfig,
        *,
        only_source_ids: Sequence[str] | None = None,
        force: bool = False,
        allow_preflight_retry: bool = False,
    ) -> str | None:
        if not force and collection.cadence == "manual":
            return None
        period = now() if force else self.collection_period(collection)
        if only_source_ids is None:
            source_ids = collection.source_ids
        else:
            requested = set(only_source_ids)
            missing = sorted(requested - set(collection.source_ids))
            if missing:
                raise UnrecoverableJebError(
                    f"collection {collection.id} does not include source(s): " + ", ".join(missing)
                )
            source_ids = tuple(
                source_id for source_id in collection.source_ids if source_id in requested
            )
        sources = [self.source_by_id(source_id) for source_id in source_ids]
        files: list[EligibleFile] = []
        before = None if force else period
        for source in sources:
            if source.enabled:
                if not allow_preflight_retry and self.routing_preflight_failure_active(source.id):
                    LOG.info(
                        "source %s has an active routing preflight failure; "
                        "skipping until operator retry",
                        source.id,
                    )
                    continue
                source_files = self.eligible_files(source, before=before)
                if not source_files:
                    continue
                routed_files = self.preflight_source_routes(collection, source, source_files)
                if routed_files is None:
                    continue
                files.extend(routed_files)
        if not files:
            return None
        target_paths = [item.target_path for item in files]
        if len(target_paths) != len(set(target_paths)):
            duplicates = sorted(path for path in set(target_paths) if target_paths.count(path) > 1)
            raise UnrecoverableJebError(
                f"collection {collection.id} has duplicate upload path(s): "
                + ", ".join(duplicates[:5])
            )
        total = sum(item.bytes for item in files)
        if total < collection.threshold_bytes:
            LOG.info(
                "collection %s below threshold: %.2fGB eligible",
                collection.id,
                total / 1_000_000_000,
            )
            return None
        base_batch_id, base_digest = self.batch_identity(collection, files, period=period)
        if (
            not force
            and collection.cadence != "manual"
            and self.batch_exists_for_period(
                collection.id,
                period,
            )
        ):
            return None
        return self.create_batch(
            collection,
            files,
            period=period,
            batch_id=base_batch_id,
            digest=base_digest,
        )

    def collection_period(self, collection: CollectionConfig) -> datetime:
        current = now()
        if collection.cadence == "manual":
            return current
        if collection.cadence == "weekly":
            return self.last_weekly_boundary(collection, current)
        period_start = self.current_period_start(collection.cadence, current)
        candidate = self.first_weekly_boundary_on_or_after(collection, period_start)
        if candidate > current:
            period_start = self.previous_period_start(collection.cadence, period_start)
            candidate = self.first_weekly_boundary_on_or_after(collection, period_start)
        return candidate.astimezone(UTC)

    def last_weekly_boundary(
        self,
        collection: CollectionConfig,
        current: datetime,
    ) -> datetime:
        days_since = (current.weekday() - collection.weekday) % 7
        candidate = current.replace(
            hour=collection.hour,
            minute=collection.minute,
            second=0,
            microsecond=0,
        ) - timedelta(days=days_since)
        if candidate > current:
            candidate -= timedelta(days=7)
        return candidate.astimezone(UTC)

    def first_weekly_boundary_on_or_after(
        self,
        collection: CollectionConfig,
        period_start: datetime,
    ) -> datetime:
        candidate = period_start.replace(
            hour=collection.hour,
            minute=collection.minute,
            second=0,
            microsecond=0,
        )
        days_until = (collection.weekday - candidate.weekday()) % 7
        candidate += timedelta(days=days_until)
        if candidate < period_start:
            candidate += timedelta(days=7)
        return candidate.astimezone(UTC)

    def current_period_start(self, cadence: Cadence, current: datetime) -> datetime:
        if cadence == "monthly":
            return current.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        if cadence == "seasonal":
            month = current.month
            if month >= 12:
                start_month = 12
            elif month >= 9:
                start_month = 9
            elif month >= 6:
                start_month = 6
            elif month >= 3:
                start_month = 3
            else:
                return current.replace(
                    year=current.year - 1,
                    month=12,
                    day=1,
                    hour=0,
                    minute=0,
                    second=0,
                    microsecond=0,
                )
            return current.replace(
                month=start_month,
                day=1,
                hour=0,
                minute=0,
                second=0,
                microsecond=0,
            )
        raise ValueError(f"unsupported cadence for period start: {cadence}")

    def previous_period_start(self, cadence: Cadence, period_start: datetime) -> datetime:
        if cadence == "monthly":
            if period_start.month == 1:
                return period_start.replace(year=period_start.year - 1, month=12)
            return period_start.replace(month=period_start.month - 1)
        if cadence == "seasonal":
            if period_start.month == 12:
                return period_start.replace(month=9)
            if period_start.month == 9:
                return period_start.replace(month=6)
            if period_start.month == 6:
                return period_start.replace(month=3)
            if period_start.month == 3:
                return period_start.replace(year=period_start.year - 1, month=12)
        raise ValueError(f"unsupported cadence for previous period: {cadence}")

    def batch_exists_for_period(
        self,
        collection_id: str,
        period: datetime,
    ) -> bool:
        timestamp = period.strftime("%Y%m%dT%H%M%SZ")
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT a.id, a.state
                FROM batches b
                JOIN batch_attempts a ON a.batch_id = b.id
                WHERE b.collection_id = ? AND b.collection_timestamp = ?
                """,
                (collection_id, timestamp),
            ).fetchall()
        return any(str(row["state"]) != "superseded" for row in rows)

    def eligible_files(
        self,
        source: SourceConfig,
        *,
        before: datetime | None = None,
    ) -> list[EligibleFile]:
        if not source.path.exists():
            return []
        cutoff = time.time() - source.stable_seconds
        before_ts = before.timestamp() if before is not None else None
        out: list[EligibleFile] = []
        seen_target_paths: set[str] = set()
        for path in sorted(source.path.rglob("*")):
            if not path.is_file():
                continue
            rel = path.relative_to(source.path)
            if any(part.startswith(".") for part in rel.parts):
                continue
            if source.include_extensions and path.suffix.lower() not in source.include_extensions:
                continue
            stat = path.stat()
            if stat.st_mtime > cutoff:
                continue
            if before_ts is not None and stat.st_mtime >= before_ts:
                continue
            target_path = normalize_posix(PurePosixPath(source.upload_prefix, *rel.parts))
            if target_path in seen_target_paths:
                raise UnrecoverableJebError(
                    f"duplicate target path for source {source.id}: {target_path}"
                )
            seen_target_paths.add(target_path)
            out.append(
                EligibleFile(
                    path=path,
                    rel=rel,
                    target_path=target_path,
                    bytes=stat.st_size,
                    mtime=stat.st_mtime,
                    mtime_ns=stat.st_mtime_ns,
                )
            )
        return out

    def preflight_source_routes(
        self,
        collection: CollectionConfig,
        source: SourceConfig,
        files: Sequence[EligibleFile],
    ) -> list[EligibleFile] | None:
        if not files:
            return []
        munchy_job_defaults = collection_munchy_job_defaults(self.config, collection)
        profile_routing = mapping(munchy_job_defaults.get("profile_routing"))
        if not profile_routing:
            self.clear_routing_preflight_failure(source.id)
            return list(files)
        target = self.target_by_name(collection.target)
        groups = munchy_groups_from_defaults(munchy_job_defaults)
        path_facts_by_path = {
            item.target_path: routing_file_facts(item.target_path) for item in files
        }
        sidecar_fact_requests = sidecar_exiftool_fact_requests(
            profile_routing,
            path_facts_by_path,
        )
        sidecar_facts_by_path: dict[str, dict[str, Any]] = {}
        sidecar_facts_errors_by_path: dict[str, str] = {}
        for item in files:
            sidecar_request = sidecar_fact_requests.get(item.target_path)
            if sidecar_request is None or not sidecar_request.tags:
                continue
            try:
                sidecar_facts_by_path[item.target_path] = exiftool_routing_facts(
                    routing_exiftool_summary(
                        exiftool_for_routing_preflight(
                            item.path,
                            tags=sidecar_request.tags,
                        )
                    ),
                    fact_extractors=sidecar_request.fact_extractors,
                )
            except RoutingFactsError as exc:
                sidecar_facts_errors_by_path[item.target_path] = str(exc)[:1000]
        base_facts_by_path = apply_sidecar_rules(
            profile_routing,
            path_facts_by_path,
            sidecar_facts_by_path=sidecar_facts_by_path,
            sidecar_facts_errors_by_path=sidecar_facts_errors_by_path,
            require_configured_facts=False,
        )
        preflight_file_list: list[RunnerProfileRoutingPreflightFile] = []
        for item in files:
            sidecar_request = sidecar_fact_requests.get(item.target_path)
            preflight_file_list.append(
                self.routing_preflight_file(
                    item,
                    profile_routing=profile_routing,
                    sidecar_exiftool_tags=sidecar_request.tags if sidecar_request else (),
                    sidecar_fact_extractors=(
                        sidecar_request.fact_extractors if sidecar_request else ()
                    ),
                    base_routing_facts=base_facts_by_path.get(item.target_path),
                    sidecar_facts=sidecar_facts_by_path.get(item.target_path),
                    sidecar_facts_error=sidecar_facts_errors_by_path.get(item.target_path),
                )
            )
        preflight_files = tuple(preflight_file_list)
        try:
            result = MunchyRunnerClient(target.url).profile_routing_preflight(
                files=preflight_files,
                groups=groups,
                profile_routing=dict(profile_routing),
                enforce_metadata_projection=True,
            )
        except Exception as exc:
            if is_transient_error(exc):
                LOG.warning(
                    "source %s routing preflight hit transient Munchy issue; will retry later: %s",
                    source.id,
                    exc,
                )
                return None
            self.record_munchy_preflight_failure(
                collection=collection,
                source=source,
                files=files,
                error=exc,
            )
            self.notify_routing_preflight_failures(source_id=source.id)
            return None
        if result.get("ok"):
            self.clear_routing_preflight_failure(source.id)
            left_paths = {
                str(item.get("path") or "")
                for item in sequence(result.get("left"))
                if isinstance(item, Mapping)
            }
            return [item for item in files if item.target_path not in left_paths]
        self.record_routing_preflight_failure(
            collection=collection,
            source=source,
            files=files,
            result=result,
        )
        self.notify_routing_preflight_failures(source_id=source.id)
        return None

    def routing_preflight_file(
        self,
        item: EligibleFile,
        *,
        profile_routing: Mapping[str, Any],
        sidecar_exiftool_tags: Sequence[str] = (),
        sidecar_fact_extractors: Sequence[Mapping[str, Any]] = (),
        base_routing_facts: Mapping[str, Any] | None = None,
        sidecar_facts: Mapping[str, Any] | None = None,
        sidecar_facts_error: str | None = None,
    ) -> RunnerProfileRoutingPreflightFile:
        base_facts = dict(base_routing_facts or {})
        is_sidecar_evidence = base_facts.get("sidecar.role") == "evidence"
        path_facts = routing_file_facts(item.target_path, routing_facts=base_facts)
        probe_summary: dict[str, Any] | None = None
        probe_error: str | None = None
        if not is_sidecar_evidence and profile_routing_file_requires_probe(
            profile_routing,
            path_facts,
        ):
            try:
                probe_summary = routing_probe_summary(ffprobe_for_routing_preflight(item.path))
            except RoutingProbeError as exc:
                probe_error = str(exc)[:1000]
        exiftool_summary: dict[str, Any] | None = None
        facts_error: str | None = None
        probe_facts = routing_file_facts(
            item.target_path,
            probe_summary=probe_summary,
            routing_facts=base_facts,
        )
        if not is_sidecar_evidence and profile_routing_file_requires_exiftool(
            profile_routing,
            probe_facts,
        ):
            try:
                exiftool_summary = routing_exiftool_summary(
                    exiftool_for_routing_preflight(
                        item.path,
                        tags=profile_routing_exiftool_tags(profile_routing),
                    )
                )
            except RoutingFactsError as exc:
                facts_error = str(exc)[:1000]
        collected_sidecar_facts = dict(sidecar_facts) if sidecar_facts is not None else None
        collected_sidecar_facts_error = sidecar_facts_error
        if (
            sidecar_exiftool_tags
            and collected_sidecar_facts is None
            and collected_sidecar_facts_error is None
        ):
            try:
                collected_sidecar_facts = exiftool_routing_facts(
                    routing_exiftool_summary(
                        exiftool_for_routing_preflight(
                            item.path,
                            tags=sidecar_exiftool_tags,
                        )
                    ),
                    fact_extractors=sidecar_fact_extractors,
                )
            except RoutingFactsError as exc:
                collected_sidecar_facts_error = str(exc)[:1000]
        return RunnerProfileRoutingPreflightFile(
            rel_path=item.target_path,
            bytes=item.bytes,
            probe_summary=probe_summary,
            probe_error=probe_error,
            routing_facts=routing_file_facts(
                item.target_path,
                probe_summary=probe_summary,
                exiftool_summary=exiftool_summary,
                routing_facts=base_facts,
            ),
            facts_error=facts_error,
            sidecar_facts=collected_sidecar_facts,
            sidecar_facts_error=collected_sidecar_facts_error,
        )

    def record_routing_preflight_failure(
        self,
        *,
        collection: CollectionConfig,
        source: SourceConfig,
        files: Sequence[EligibleFile],
        result: Mapping[str, Any],
    ) -> None:
        unmatched = [
            dict(item) for item in sequence(result.get("unmatched")) if isinstance(item, Mapping)
        ]
        unmatched_count = int(result.get("unmatched_files") or len(unmatched) or 0)
        file_count = int(result.get("files_total") or len(files))
        total_bytes = sum(item.bytes for item in files)
        failure_payload = {
            "ok": False,
            "matched_files": int(result.get("matched_files") or 0),
            "unmatched_files": unmatched_count,
            "unmatched": unmatched[:20],
            "error": optional_str(result.get("error")),
        }
        fingerprint_payload = {
            "failure_kind": "profile_routing",
            "source_id": source.id,
            "unmatched": [
                {
                    "path": str(item.get("path") or ""),
                    "reason": str(item.get("reason") or ""),
                    "error": str(
                        item.get("error")
                        or item.get("metadata_projection_error")
                        or item.get("facts_error")
                        or item.get("probe_error")
                        or ""
                    )[:240],
                }
                for item in unmatched
            ],
            "error": optional_str(result.get("error")),
        }
        message = routing_preflight_notification_message(
            source_id=source.id,
            file_count=file_count,
            unmatched_count=unmatched_count,
        )
        self.store_routing_preflight_failure(
            collection=collection,
            source=source,
            files=files,
            failure_kind="profile_routing",
            failure_payload=failure_payload,
            fingerprint_payload=fingerprint_payload,
            message=message,
            file_count=file_count,
            total_bytes=total_bytes,
            unmatched_count=unmatched_count,
        )

    def record_munchy_preflight_failure(
        self,
        *,
        collection: CollectionConfig,
        source: SourceConfig,
        files: Sequence[EligibleFile],
        error: BaseException,
    ) -> None:
        error_text = str(error)
        status = getattr(error, "status", None)
        file_count = len(files)
        total_bytes = sum(item.bytes for item in files)
        failure_payload = {
            "ok": False,
            "failure_kind": "munchy_preflight",
            "error": error_text,
            "error_type": error.__class__.__name__,
            "status": status,
        }
        fingerprint_payload = {
            "failure_kind": "munchy_preflight",
            "source_id": source.id,
            "error": error_text[:500],
            "error_type": error.__class__.__name__,
            "status": status,
        }
        message = munchy_preflight_notification_message(source_id=source.id, error=error)
        self.store_routing_preflight_failure(
            collection=collection,
            source=source,
            files=files,
            failure_kind="munchy_preflight",
            failure_payload=failure_payload,
            fingerprint_payload=fingerprint_payload,
            message=message,
            file_count=file_count,
            total_bytes=total_bytes,
            unmatched_count=0,
        )

    def store_routing_preflight_failure(
        self,
        *,
        collection: CollectionConfig,
        source: SourceConfig,
        files: Sequence[EligibleFile],
        failure_kind: Literal["profile_routing", "munchy_preflight"],
        failure_payload: Mapping[str, Any],
        fingerprint_payload: Mapping[str, Any],
        message: str,
        file_count: int,
        total_bytes: int,
        unmatched_count: int,
    ) -> None:
        now_text = iso()
        fingerprint = hashlib.sha256(stable_json(fingerprint_payload).encode()).hexdigest()[:24]
        source_paths = [item.target_path for item in files[:20]]
        with self.connect() as conn:
            existing = conn.execute(
                """
                SELECT first_seen_at, notified_error_fingerprint, notified_error_at
                FROM routing_preflight_failures
                WHERE source_id = ?
                """,
                (source.id,),
            ).fetchone()
            first_seen_at = str(existing["first_seen_at"]) if existing is not None else now_text
            conn.execute(
                """
                INSERT INTO routing_preflight_failures(
                    source_id, state, failure_kind, collection_id, collection_slug, target_name,
                    source_paths_json, failure_json, fingerprint, message,
                    file_count, total_bytes, unmatched_count, first_seen_at,
                    last_seen_at, updated_at, resolved_at,
                    notified_error_fingerprint, notified_error_at
                )
                VALUES(?, 'failed', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?)
                ON CONFLICT(source_id) DO UPDATE SET
                    state = 'failed',
                    failure_kind = excluded.failure_kind,
                    collection_id = excluded.collection_id,
                    collection_slug = excluded.collection_slug,
                    target_name = excluded.target_name,
                    source_paths_json = excluded.source_paths_json,
                    failure_json = excluded.failure_json,
                    fingerprint = excluded.fingerprint,
                    message = excluded.message,
                    file_count = excluded.file_count,
                    total_bytes = excluded.total_bytes,
                    unmatched_count = excluded.unmatched_count,
                    last_seen_at = excluded.last_seen_at,
                    updated_at = excluded.updated_at,
                    resolved_at = NULL
                """,
                (
                    source.id,
                    failure_kind,
                    collection.id,
                    collection.collection_slug,
                    collection.target,
                    stable_json(source_paths),
                    stable_json(failure_payload),
                    fingerprint,
                    message,
                    file_count,
                    total_bytes,
                    unmatched_count,
                    first_seen_at,
                    now_text,
                    now_text,
                    (
                        str(existing["notified_error_fingerprint"])
                        if existing is not None
                        and existing["notified_error_fingerprint"] is not None
                        else None
                    ),
                    (
                        str(existing["notified_error_at"])
                        if existing is not None and existing["notified_error_at"] is not None
                        else None
                    ),
                ),
            )

    def clear_routing_preflight_failure(self, source_id: str) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE routing_preflight_failures
                SET state = 'resolved', resolved_at = ?, updated_at = ?
                WHERE source_id = ? AND state = 'failed'
                """,
                (iso(), iso(), source_id),
            )

    def routing_preflight_failure_active(self, source_id: str) -> bool:
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT 1 FROM routing_preflight_failures
                WHERE source_id = ? AND state = 'failed'
                """,
                (source_id,),
            ).fetchone()
        return row is not None

    def active_routing_preflight_source_ids(self) -> set[str]:
        enabled_sources = {source.id for source in self.config.sources if source.enabled}
        source_ids: set[str] = set()
        for collection in self.config.collections:
            if collection.enabled:
                source_ids.update(
                    source_id for source_id in collection.source_ids if source_id in enabled_sources
                )
        return source_ids

    def resolve_inactive_routing_preflight_failures(self) -> int:
        active_source_ids = sorted(self.active_routing_preflight_source_ids())
        now_text = iso()
        with self.connect() as conn:
            if active_source_ids:
                placeholders = ", ".join("?" for _ in active_source_ids)
                cursor = conn.execute(
                    f"""
                    UPDATE routing_preflight_failures
                    SET state = 'resolved', resolved_at = ?, updated_at = ?
                    WHERE state = 'failed' AND source_id NOT IN ({placeholders})
                    """,
                    (now_text, now_text, *active_source_ids),
                )
            else:
                cursor = conn.execute(
                    """
                    UPDATE routing_preflight_failures
                    SET state = 'resolved', resolved_at = ?, updated_at = ?
                    WHERE state = 'failed'
                    """,
                    (now_text, now_text),
                )
        resolved = cursor.rowcount if cursor.rowcount is not None else 0
        if resolved:
            LOG.info("resolved %s inactive routing preflight failure(s)", resolved)
        return resolved

    def routing_preflight_failures(
        self,
        *,
        source_id: str | None = None,
        state: Literal["failed", "resolved", "all"] = "failed",
    ) -> list[sqlite3.Row]:
        clauses: list[str] = []
        values: list[object] = []
        if source_id:
            clauses.append("source_id = ?")
            values.append(source_id)
        if state != "all":
            clauses.append("state = ?")
            values.append(state)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        with self.connect() as conn:
            return conn.execute(
                f"""
                SELECT * FROM routing_preflight_failures
                {where}
                ORDER BY state, source_id, updated_at DESC
                """,
                values,
            ).fetchall()

    def notify_routing_preflight_failures(self, source_id: str | None = None) -> None:
        self.resolve_inactive_routing_preflight_failures()
        for row in self.routing_preflight_failures(source_id=source_id, state="failed"):
            self.notify_routing_preflight_failure(row)

    def notify_routing_preflight_failure(self, row: sqlite3.Row) -> bool:
        row_payload = dict(row)
        fingerprint = str(row_payload["fingerprint"])
        if row_payload.get(
            "notified_error_fingerprint"
        ) == fingerprint and not self.notification_reminder_due(row_payload):
            return True
        source_id = str(row_payload["source_id"])
        batch = {
            "id": f"routing-preflight__{source_id}",
            "source_id": source_id,
            "target_name": str(row_payload["target_name"]),
            "target_type": "munchy",
            "collection_slug": str(row_payload["collection_slug"]),
            "collection_timestamp": iso(),
            "state": "failed",
        }
        component = str(row_payload.get("failure_kind") or "profile_routing")
        if component not in {"profile_routing", "munchy_preflight"}:
            component = "profile_routing"
        if not self.notifier.critical_batch_issue(
            batch=batch,
            message=str(row_payload["message"]),
            component=component,
            notify=self.collection_by_id(str(row_payload["collection_id"])).notify,
        ):
            return False
        now_text = iso()
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE routing_preflight_failures
                SET notified_error_fingerprint = ?, notified_error_at = ?, updated_at = ?
                WHERE source_id = ?
                """,
                (fingerprint, now_text, now_text, source_id),
            )
        return True

    def archive_now(
        self,
        *,
        source_id: str,
        process: bool = True,
    ) -> str | None:
        try:
            source = self.source_by_id(source_id)
        except KeyError as exc:
            raise UnrecoverableJebError(
                f"account {source_id!r} is not in the active Jeb env"
            ) from exc
        if not source.enabled:
            raise UnrecoverableJebError(
                f"account {source_id!r} is disabled in the active Jeb env"
            )
        collections = [
            collection
            for collection in self.config.collections
            if collection.enabled and source_id in collection.source_ids
        ]
        if not collections:
            raise UnrecoverableJebError(
                f"account {source_id!r} is not included in the active Jeb schedule"
            )
        if len(collections) > 1:
            raise UnrecoverableJebError(
                f"account {source_id!r} is in multiple scheduled collections"
            )
        collection = collections[0]
        failed_attempt = self.latest_failed_attempt_for_collection(collection.id)
        batch_id: str | None
        if failed_attempt is not None:
            if self.failed_attempt_target_paths_match_current_config(
                failed_attempt,
                collection,
            ):
                batch_id = self.create_retry_attempt(str(failed_attempt["id"]))
            else:
                LOG.info(
                    "failed attempt %s target paths no longer match current source "
                    "config; rediscovering collection %s",
                    failed_attempt["id"],
                    collection.id,
                )
                batch_id = self.discover_collection(
                    collection,
                    only_source_ids=(source_id,),
                    force=True,
                    allow_preflight_retry=True,
                )
                if batch_id is not None:
                    self.supersede_batch_attempt(str(failed_attempt["id"]))
        else:
            batch_id = self.discover_collection(
                collection,
                only_source_ids=(source_id,),
                force=True,
                allow_preflight_retry=True,
            )
        if batch_id is not None and process:
            self.process_batch(batch_id)
        return batch_id

    def latest_failed_attempt_for_collection(self, collection_id: str) -> sqlite3.Row | None:
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT a.*
                FROM batch_attempts a
                JOIN batches b ON b.id = a.batch_id
                WHERE b.collection_id = ?
                  AND a.state IN ('failed', 'failed_notified')
                ORDER BY a.created_at DESC, a.id DESC
                LIMIT 1
                """,
                (collection_id,),
            ).fetchone()
        return cast(sqlite3.Row | None, row)

    def failed_attempt_target_paths_match_current_config(
        self,
        failed_attempt: sqlite3.Row,
        collection: CollectionConfig,
    ) -> bool:
        sources = [self.source_by_id(source_id) for source_id in collection.source_ids]
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT source_path, target_path FROM files WHERE batch_id = ?",
                (failed_attempt["batch_id"],),
            ).fetchall()
        current_target_paths: list[str] = []
        for row in rows:
            current_target_path = self.current_target_path_for_source_path(
                Path(str(row["source_path"])),
                sources,
            )
            if current_target_path is None:
                return False
            current_target_paths.append(current_target_path)
        if len(current_target_paths) != len(set(current_target_paths)):
            return False
        stored_target_paths = [str(row["target_path"]) for row in rows]
        return sorted(stored_target_paths) == sorted(current_target_paths)

    def current_target_path_for_source_path(
        self,
        source_path: Path,
        sources: Sequence[SourceConfig],
    ) -> str | None:
        for source in sources:
            try:
                rel = source_path.relative_to(source.path)
            except ValueError:
                continue
            return normalize_posix(PurePosixPath(source.upload_prefix, *rel.parts))
        return None

    def supersede_batch_attempt(self, attempt_id: str) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE batch_attempts
                SET state = 'superseded', updated_at = ?
                WHERE id = ?
                """,
                (iso(), attempt_id),
            )

    def create_retry_attempt(self, failed_attempt_id: str) -> str:
        failed_attempt = self.load_batch(failed_attempt_id)
        batch_id = str(failed_attempt["batch_id"])
        with self.connect() as conn:
            row = conn.execute(
                "SELECT COALESCE(MAX(attempt_number), 0) + 1 AS attempt_number "
                "FROM batch_attempts WHERE batch_id = ?",
                (batch_id,),
            ).fetchone()
            attempt_number = int(row["attempt_number"] if row is not None else 1)
            attempt_id = batch_id if attempt_number == 1 else f"{batch_id}-r{attempt_number}"
            if conn.execute(
                "SELECT 1 FROM batch_attempts WHERE id = ?",
                (attempt_id,),
            ).fetchone():
                raise UnrecoverableJebError(f"retry attempt already exists: {attempt_id}")
            suffix = "" if attempt_number == 1 else f"-r{attempt_number}"
            input_upload_id = (
                f"jeb-{failed_attempt['collection_id']}-"
                f"{str(failed_attempt['collection_timestamp']).lower()}-"
                f"{failed_attempt['manifest_digest']}{suffix}"
            )
            job_id = f"{input_upload_id}-job"
            created_at = iso()
            conn.execute(
                """
                INSERT INTO batch_attempts(
                    id, batch_id, attempt_number, state, input_upload_id, job_id,
                    created_at, updated_at
                )
                VALUES(?, ?, ?, 'batching', ?, ?, ?, ?)
                """,
                (
                    attempt_id,
                    batch_id,
                    attempt_number,
                    input_upload_id,
                    job_id,
                    created_at,
                    created_at,
                ),
            )
            file_rows = conn.execute(
                "SELECT target_path FROM files WHERE batch_id = ? ORDER BY target_path",
                (batch_id,),
            ).fetchall()
            for file_row in file_rows:
                target_path = str(file_row["target_path"])
                staging = (
                    self.config.collector.batch_dir
                    / attempt_id
                    / "input"
                    / PurePosixPath(target_path)
                )
                conn.execute(
                    """
                    INSERT INTO attempt_files(attempt_id, target_path, staging_path, staged_at)
                    VALUES(?, ?, ?, NULL)
                    """,
                    (attempt_id, target_path, str(staging)),
                )
            conn.execute(
                """
                UPDATE batch_attempts
                SET state = 'superseded', updated_at = ?
                WHERE id = ?
                """,
                (created_at, failed_attempt_id),
            )
            conn.execute(
                "UPDATE batches SET updated_at = ? WHERE id = ?",
                (created_at, batch_id),
            )
        shutil.rmtree(self.config.collector.batch_dir / failed_attempt_id, ignore_errors=True)
        LOG.info(
            "created retry attempt %s for batch %s after failed attempt %s",
            attempt_id,
            batch_id,
            failed_attempt_id,
        )
        return attempt_id

    def create_batch(
        self,
        collection: CollectionConfig,
        files: Sequence[EligibleFile],
        *,
        period: datetime,
        batch_id: str | None = None,
        digest: str | None = None,
    ) -> str:
        collection_timestamp = period.strftime("%Y%m%dT%H%M%SZ")
        if batch_id is None or digest is None:
            batch_id, digest = self.batch_identity(collection, files, period=period)
        target = self.target_by_name(collection.target)
        input_upload_id = f"jeb-{collection.id}-{collection_timestamp.lower()}-{digest}"
        job_id = f"{input_upload_id}-job"
        batch_root = self.config.collector.batch_dir / batch_id / "input"
        created_at = iso()
        with self.connect() as conn:
            exists = conn.execute("SELECT 1 FROM batches WHERE id = ?", (batch_id,)).fetchone()
            if exists:
                return batch_id
            conn.execute(
                """
                INSERT INTO batches(
                    id, collection_id, target_name, collection_slug,
                    collection_timestamp, cleanup, manifest_digest, created_at, updated_at
                )
                VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    batch_id,
                    collection.id,
                    target.name,
                    collection.collection_slug,
                    collection_timestamp,
                    collection.cleanup,
                    digest,
                    created_at,
                    created_at,
                ),
            )
            conn.execute(
                """
                INSERT INTO batch_attempts(
                    id, batch_id, attempt_number, state, input_upload_id, job_id,
                    created_at, updated_at
                )
                VALUES(?, ?, 1, 'batching', ?, ?, ?, ?)
                """,
                (
                    batch_id,
                    batch_id,
                    input_upload_id,
                    job_id,
                    created_at,
                    created_at,
                ),
            )
            for item in files:
                staging = batch_root / PurePosixPath(item.target_path)
                conn.execute(
                    """
                    INSERT INTO files(batch_id, source_path, target_path, bytes, mtime_ns, sha256)
                    VALUES(?, ?, ?, ?, ?, NULL)
                    """,
                    (
                        batch_id,
                        str(item.path),
                        item.target_path,
                        item.bytes,
                        item.mtime_ns,
                    ),
                )
                conn.execute(
                    """
                    INSERT INTO attempt_files(attempt_id, target_path, staging_path, staged_at)
                    VALUES(?, ?, ?, NULL)
                    """,
                    (batch_id, item.target_path, str(staging)),
                )
        LOG.info(
            "created batch %s for collection %s with %d files",
            batch_id,
            collection.id,
            len(files),
        )
        return batch_id

    def batch_identity(
        self,
        collection: CollectionConfig,
        files: Sequence[EligibleFile],
        *,
        period: datetime,
    ) -> tuple[str, str]:
        collection_timestamp = period.strftime("%Y%m%dT%H%M%SZ")
        manifest = "\n".join(f"{item.target_path} {item.bytes} {item.mtime_ns}" for item in files)
        digest = hashlib.sha256(manifest.encode("utf-8")).hexdigest()[:12]
        return f"{collection_timestamp}__{collection.id}__{digest}", digest

    def batch_process_lock_path(self, batch_id: str) -> Path:
        digest = hashlib.sha256(batch_id.encode("utf-8")).hexdigest()[:16]
        return self.config.collector.state_db.parent / "locks" / f"batch-process-{digest}.lock"

    @contextmanager
    def batch_process_lock(self, batch_id: str) -> Iterator[bool]:
        lock_path = self.batch_process_lock_path(batch_id)
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with lock_path.open("a+") as lock_file:
            try:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                LOG.info("batch %s is already being processed; skipping concurrent run", batch_id)
                yield False
                return
            try:
                lock_file.seek(0)
                lock_file.truncate()
                lock_file.write(f"pid={os.getpid()}\nbatch_id={batch_id}\nacquired_at={iso()}\n")
                lock_file.flush()
                yield True
            finally:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

    def process_batch(self, batch_id: str) -> None:
        with self.batch_process_lock(batch_id) as acquired:
            if not acquired:
                return
            self._process_batch_locked(batch_id)

    def _process_batch_locked(self, batch_id: str) -> None:
        try:
            batch = self.load_batch(batch_id)
            state = str(batch["state"])
            if state in {"failed", "failed_notified"}:
                self.notify_failed_batch(batch_id)
                return
            if state in {"cleanup_pending", "cleanup_failed"}:
                self.cleanup_batch(batch_id)
                return
            if state == "batching":
                self.move_batch_files(batch_id)
            if self.load_batch(batch_id)["state"] == "batched":
                self.ensure_hashes(batch_id)
            if self.load_batch(batch_id)["state"] == "hashed":
                self.ensure_media_preflight(batch_id)
            batch = self.load_batch(batch_id)
            runner = self.target_runners["munchy"]
            runner.advance(self, batch_id)
            self.finish_target_success(batch_id)
        except PreflightJebError as exc:
            LOG.exception("batch %s failed media preflight", batch_id)
            self.mark_unrecoverable(batch_id, str(exc), component="preflight")
        except UnrecoverableJebError as exc:
            LOG.exception("batch %s has unrecoverable error", batch_id)
            self.mark_unrecoverable(batch_id, str(exc), component="target")
        except TransientJebError as exc:
            LOG.warning("batch %s hit transient issue; will retry: %s", batch_id, exc)
            self.set_batch_fields(batch_id, last_error=str(exc))
        except Exception as exc:
            if is_transient_error(exc):
                LOG.warning("batch %s hit transient issue; will retry: %s", batch_id, exc)
                self.set_batch_fields(batch_id, last_error=str(exc))
                return
            LOG.exception("batch %s failed with unrecoverable target error", batch_id)
            self.mark_unrecoverable(batch_id, str(exc), component="target")

    def move_batch_files(self, batch_id: str) -> None:
        for row in self.batch_files(batch_id):
            if row["staged_at"]:
                continue
            source = Path(str(row["source_path"]))
            staging = Path(str(row["staging_path"]))
            if source.exists():
                hardlink_stage_file(source, staging)
            elif not staging.exists():
                raise UnrecoverableJebError(
                    f"source and staging file are both missing: {source} -> {staging}"
                )
            with self.connect() as conn:
                conn.execute(
                    """
                    UPDATE attempt_files
                    SET staged_at = ?
                    WHERE attempt_id = ? AND target_path = ?
                    """,
                    (iso(), batch_id, row["target_path"]),
                )
        self.set_batch_state(batch_id, "batched")

    def ensure_hashes(self, batch_id: str) -> None:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT f.batch_id, f.target_path, af.staging_path, f.bytes
                FROM batch_attempts a
                JOIN files f ON f.batch_id = a.batch_id
                JOIN attempt_files af
                  ON af.attempt_id = a.id
                 AND af.target_path = f.target_path
                WHERE a.id = ? AND f.sha256 IS NULL
                ORDER BY f.target_path
                """,
                (batch_id,),
            ).fetchall()
        if not rows:
            self.set_batch_state(batch_id, "hashed")
            return
        started_at = time.monotonic()
        last_logged_at = started_at
        total_files = len(rows)
        total_bytes = sum(int(row["bytes"] or 0) for row in rows)
        done_bytes = 0
        LOG.info(
            "batch %s hashing %d file(s), %s",
            batch_id,
            total_files,
            format_progress_bytes(total_bytes),
        )
        for index, row in enumerate(rows, start=1):
            path = Path(str(row["staging_path"]))
            if not path.exists():
                raise UnrecoverableJebError(f"staged file disappeared before hashing: {path}")
            digest = file_sha256(path)
            done_bytes += int(row["bytes"] or 0)
            with self.connect() as conn:
                conn.execute(
                    "UPDATE files SET sha256 = ? WHERE batch_id = ? AND target_path = ?",
                    (digest, row["batch_id"], row["target_path"]),
                )
            now = time.monotonic()
            is_final = index == total_files
            if is_final or now - last_logged_at >= 15:
                elapsed = max(now - started_at, 0.001)
                percent = (done_bytes / total_bytes * 100.0) if total_bytes else 100.0
                LOG.info(
                    ("batch %s hash progress: %d/%d file(s), %s/%s, %.2f%%, %.1fs"),
                    batch_id,
                    index,
                    total_files,
                    format_progress_bytes(done_bytes),
                    format_progress_bytes(total_bytes),
                    percent,
                    elapsed,
                )
                last_logged_at = now
        self.set_batch_state(batch_id, "hashed")

    def ensure_media_preflight(self, batch_id: str) -> None:
        files = self.media_preflight_files(batch_id)
        if not files:
            self.set_batch_state(batch_id, "preflighted")
            return
        total_bytes = sum(file.bytes for file in files)
        LOG.info(
            "batch %s media preflight starting: %d file(s), %s",
            batch_id,
            len(files),
            format_progress_bytes(total_bytes),
        )

        def log_preflight_progress(payload: dict[str, Any]) -> None:
            LOG.info(
                (
                    "batch %s media preflight progress: %d/%d file(s), %s/%s, "
                    "%.2f%%, %d failed, %.1fs"
                ),
                batch_id,
                int(payload.get("files_done") or 0),
                int(payload.get("files_total") or 0),
                format_progress_bytes(int(payload.get("bytes_done") or 0)),
                format_progress_bytes(int(payload.get("bytes_total") or 0)),
                float(payload.get("percent_bytes") or 0.0),
                int(payload.get("failures") or 0),
                float(payload.get("elapsed_seconds") or 0.0),
            )

        report = run_media_preflight(
            files,
            progress=False,
            progress_callback=log_preflight_progress,
        )
        repair_notes: list[str] = []
        if not report.ok and self.config.collector.preflight_repair == "safe_remux":
            LOG.info(
                "batch %s media preflight found %d failed file(s); attempting safe remux repair",
                batch_id,
                len(report.failed_results),
            )
            report, repair_notes = self.repair_media_preflight_failures(batch_id, report)
        if not report.ok:
            message = format_media_preflight_error(report)
            if repair_notes:
                message = f"{message}; safe remux repair failed: {repair_notes[0]}"
            raise PreflightJebError(message)
        LOG.info(
            "batch %s media preflight ok: %d file(s), %s, %.1fs",
            batch_id,
            len(report.results),
            format_progress_bytes(sum(result.file.bytes for result in report.results)),
            report.elapsed_seconds,
        )
        self.set_batch_state(batch_id, "preflighted")

    def media_preflight_files(self, batch_id: str) -> list[MediaPreflightFile]:
        return [
            MediaPreflightFile(
                source=Path(str(row["staging_path"])),
                label=str(row["target_path"]),
                bytes=int(row["bytes"]),
            )
            for row in self.batch_files(batch_id)
            if Path(str(row["target_path"])).suffix.lower() in PREFLIGHT_MEDIA_EXTENSIONS
        ]

    def repair_media_preflight_failures(
        self,
        batch_id: str,
        report: MediaPreflightReport,
    ) -> tuple[MediaPreflightReport, list[str]]:
        rows = {str(row["target_path"]): row for row in self.batch_files(batch_id)}
        results_by_label = {result.file.label: result for result in report.results if result.ok}
        notes: list[str] = []
        repaired = 0
        quarantined = 0
        failed_results = report.failed_results
        for index, result in enumerate(failed_results, start=1):
            label = result.file.label
            if Path(label).suffix.lower() not in PREFLIGHT_MEDIA_EXTENSIONS:
                notes.append(f"{label}: not a preflighted media file")
                continue
            row = rows.get(label)
            if row is None:
                notes.append(f"{label}: batch row disappeared")
                continue
            try:
                LOG.info(
                    "batch %s safe remux repair %d/%d: %s",
                    batch_id,
                    index,
                    len(failed_results),
                    label,
                )
                results_by_label[label] = self.safe_remux_batch_file(batch_id, row)
            except PreflightJebError as exc:
                LOG.warning("safe remux repair failed for %s in %s: %s", label, batch_id, exc)
                notes.append(f"{label}: {exc}")
                self.quarantine_batch_file(batch_id, row)
                quarantined += 1
                continue
            repaired += 1
        if repaired:
            LOG.info("safe remux repaired %d file(s) for batch %s", repaired, batch_id)
        if quarantined:
            LOG.info("quarantined %d unrepaired media file(s) for batch %s", quarantined, batch_id)
        remaining_labels = {
            str(row["target_path"])
            for row in self.batch_files(batch_id)
            if Path(str(row["target_path"])).suffix.lower() in PREFLIGHT_MEDIA_EXTENSIONS
        }
        final_results: list[MediaPreflightResult] = []
        for result in report.results:
            label = result.file.label
            if label not in remaining_labels:
                continue
            final_results.append(results_by_label.get(label, result))
        return MediaPreflightReport(final_results, elapsed_seconds=report.elapsed_seconds), notes

    def safe_remux_batch_file(
        self,
        batch_id: str,
        row: sqlite3.Row,
    ) -> MediaPreflightResult:
        target_path = str(row["target_path"])
        source = Path(str(row["source_path"]))
        staging = Path(str(row["staging_path"]))
        input_path = source if source.exists() else staging
        if not input_path.exists():
            raise UnrecoverableJebError(
                f"source and staging file are both missing: {source} -> {staging}"
            )
        source_stat = input_path.stat()
        suffix = Path(target_path).suffix or staging.suffix or ".mkv"
        temp = staging.with_name(f".{staging.name}.safe-remux-{os.getpid()}{suffix}")
        temp.unlink(missing_ok=True)
        try:
            run_safe_remux(
                ffmpeg_path=self.config.collector.preflight_repair_ffmpeg,
                source=input_path,
                dest=temp,
            )
            os.utime(temp, ns=(source_stat.st_atime_ns, source_stat.st_mtime_ns))
            repair_report = run_media_preflight(
                [
                    MediaPreflightFile(
                        source=temp,
                        label=target_path,
                        bytes=temp.stat().st_size,
                    )
                ],
                progress=False,
            )
            if not repair_report.ok:
                raise PreflightJebError(format_media_preflight_error(repair_report))

            if source.exists():
                if self.config.collector.preflight_repair_original == "keep_corrupt":
                    corrupt_dest = unique_corrupt_path(
                        self.config.collector.preflight_repair_corrupt_dir
                        / PurePosixPath(target_path)
                    )
                    corrupt_dest.parent.mkdir(parents=True, exist_ok=True)
                    source.replace(corrupt_dest)
                else:
                    source.unlink()
            elif self.config.collector.preflight_repair_original == "keep_corrupt":
                corrupt_dest = unique_corrupt_path(
                    self.config.collector.preflight_repair_corrupt_dir / PurePosixPath(target_path)
                )
                corrupt_dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(input_path, corrupt_dest)
            source.parent.mkdir(parents=True, exist_ok=True)
            temp.replace(source)
            os.utime(source, ns=(source_stat.st_atime_ns, source_stat.st_mtime_ns))
            hardlink_stage_file(source, staging)
            stat = source.stat()
            digest = file_sha256(staging)
            with self.connect() as conn:
                conn.execute(
                    """
                    UPDATE files
                    SET bytes = ?, mtime_ns = ?, sha256 = ?
                    WHERE batch_id = ? AND target_path = ?
                    """,
                    (stat.st_size, stat.st_mtime_ns, digest, row["batch_id"], target_path),
                )
                conn.execute(
                    """
                    UPDATE attempt_files
                    SET staged_at = ?
                    WHERE attempt_id = ? AND target_path = ?
                    """,
                    (iso(), batch_id, target_path),
                )
            return MediaPreflightResult(
                file=MediaPreflightFile(
                    source=staging,
                    label=target_path,
                    bytes=stat.st_size,
                ),
                issues=[],
            )
        finally:
            temp.unlink(missing_ok=True)

    def quarantine_batch_file(self, batch_id: str, row: sqlite3.Row) -> None:
        target_path = str(row["target_path"])
        source = Path(str(row["source_path"]))
        staging = Path(str(row["staging_path"]))
        corrupt_dest = unique_corrupt_path(
            self.config.collector.preflight_repair_corrupt_dir / PurePosixPath(target_path)
        )
        corrupt_dest.parent.mkdir(parents=True, exist_ok=True)
        if source.exists():
            source.replace(corrupt_dest)
            staging.unlink(missing_ok=True)
        elif staging.exists():
            staging.replace(corrupt_dest)
        else:
            raise UnrecoverableJebError(
                f"source and staging file are both missing: {source} -> {staging}"
            )
        with self.connect() as conn:
            conn.execute(
                "DELETE FROM attempt_files WHERE attempt_id = ? AND target_path = ?",
                (batch_id, target_path),
            )
            conn.execute(
                "DELETE FROM files WHERE batch_id = ? AND target_path = ?",
                (row["batch_id"], target_path),
            )

    def finish_target_success(self, batch_id: str) -> None:
        batch = self.load_batch(batch_id)
        state = str(batch["state"])
        if state == "target_succeeded":
            return
        if state != "target_complete":
            return
        if batch["cleanup"] == "after_target_success":
            self.set_batch_state(batch_id, "cleanup_pending")
            self.cleanup_batch(batch_id)
            return
        self.set_batch_state(batch_id, "target_succeeded")

    def cleanup_batch(self, batch_id: str) -> None:
        try:
            for row in self.batch_files(batch_id):
                Path(str(row["source_path"])).unlink(missing_ok=True)
            shutil.rmtree(self.config.collector.batch_dir / batch_id)
        except FileNotFoundError:
            pass
        except Exception as exc:
            message = f"failed to delete completed batch files: {exc}"
            self.set_batch_state(batch_id, "cleanup_failed", message)
            self.notify_cleanup_failed(batch_id, message)
            return
        self.set_batch_state(batch_id, "cleanup_done")

    def mark_unrecoverable(self, batch_id: str, message: str, *, component: str) -> None:
        self.set_batch_state(batch_id, "failed", message)
        self.notify_failed_batch(batch_id, component=component)

    def notify_failed_batch(self, batch_id: str, *, component: str = "target") -> None:
        batch = self.load_batch(batch_id)
        message = str(batch["last_error"] or "jeb batch failed")
        if self.notify_batch_issue(batch, message=message, component=component):
            self.set_batch_fields(batch_id, state="failed_notified")

    def notify_cleanup_failed(self, batch_id: str, message: str) -> None:
        batch = self.load_batch(batch_id)
        self.notify_batch_issue(batch, message=message, component="cleanup")

    def notify_batch_issue(
        self,
        batch: Mapping[str, Any] | sqlite3.Row,
        *,
        message: str,
        component: str,
    ) -> bool:
        batch_payload = dict(batch)
        fingerprint = hashlib.sha256(
            f"{batch_payload['id']}:{component}:{message}".encode()
        ).hexdigest()[:24]
        if batch_payload.get(
            "notified_error_fingerprint"
        ) == fingerprint and not self.notification_reminder_due(batch_payload):
            return True
        if not self.notifier.critical_batch_issue(
            batch=batch_payload,
            message=message,
            component=component,
            notify=self.collection_by_id(str(batch_payload["collection_id"])).notify,
        ):
            return False
        self.set_batch_fields(
            str(batch_payload["id"]),
            notified_error_fingerprint=fingerprint,
            notified_error_at=iso(),
        )
        return True

    def notification_reminder_due(self, batch: Mapping[str, Any]) -> bool:
        last_sent = batch.get("notified_error_at")
        if not last_sent:
            return True
        try:
            sent_at = datetime.fromisoformat(str(last_sent).replace("Z", "+00:00"))
        except ValueError:
            return True
        return operator_reminder_due(
            last_sent_at=sent_at,
            current=now(),
            interval=self.config.notify.reminder_interval_seconds,
            reminder_time=self.config.notify.reminder_time,
            reminder_timezone=self.config.notify.reminder_timezone,
        )


class MunchyTargetRunner:
    def advance(self, collector: Collector, batch_id: str) -> None:
        batch = collector.load_batch(batch_id)
        if batch["state"] == "target_complete":
            return
        target = collector.target_by_name(str(batch["target_name"]))
        client = MunchyRunnerClient(target.url)
        request = munchy_upload_request(collector, batch_id, target)

        state = str(batch["state"])
        if state == "preflighted":
            client.create_or_get_input_upload(request)
            collector.set_batch_state(batch_id, "munchy_input_registered")
            state = "munchy_input_registered"
        if state == "munchy_input_registered":
            client.create_job(request)
            collector.set_batch_state(batch_id, "munchy_job_submitted")
            state = "munchy_job_submitted"
        if state in {"munchy_job_submitted", "munchy_uploading"}:
            collector.set_batch_state(batch_id, "munchy_uploading")
            try:
                client.upload_files(request)
            except RunnerJobTerminalDuringUpload as exc:
                if job_finished_cleanly(exc.job):
                    collector.set_batch_state(batch_id, "target_complete")
                    return
                raise UnrecoverableJebError(str(exc)) from exc
            collector.set_batch_state(batch_id, "munchy_uploaded")
            state = "munchy_uploaded"
        if state == "munchy_uploaded":
            job = client.wait_for_job(
                str(batch["job_id"]),
                wait_for_safe_delete=target.wait_for_safe_delete,
            )
            if not job_finished_cleanly(job):
                raise UnrecoverableJebError(f"munchy job did not finish cleanly: {job}")
            collector.set_batch_state(batch_id, "target_complete")


def munchy_upload_request(
    collector: Collector,
    batch_id: str,
    target: TargetConfig,
) -> RunnerUploadRequest:
    batch = collector.load_batch(batch_id)
    collection = collector.collection_by_id(str(batch["collection_id"]))
    rows = collector.batch_files(batch_id)
    files = tuple(
        RunnerInputFile(
            source=Path(str(row["staging_path"])),
            rel_path=str(row["target_path"]),
            bytes=int(row["bytes"]),
            sha256=str(row["sha256"]),
            filesystem_metadata=collect_filesystem_metadata(filesystem_metadata_source(row)),
        )
        for row in rows
    )
    munchy_job_defaults = collection_munchy_job_defaults(collector.config, collection)
    groups = munchy_groups_from_defaults(munchy_job_defaults)
    profile_routing = mapping(munchy_job_defaults.get("profile_routing"))
    job_payload = {
        "job_id": str(batch["job_id"]),
        "input_upload_id": str(batch["input_upload_id"]),
        "collection_slug": str(batch["collection_slug"]),
        "collection_timestamp": str(batch["collection_timestamp"]),
        "workflow_mode": munchy_job_defaults.get(
            "workflow_mode",
            "collection_archive",
        ),
        "archive_mode": munchy_job_defaults.get("archive_mode", "av1_nvenc"),
        "tasks": list(munchy_job_defaults.get("tasks") or ["archive_video"]),
        "groups": groups,
        "collection_archive": dict(
            munchy_job_defaults.get("collection_archive")
            or {"destination": "riverhog"}
        ),
        "notify": dict(collection.notify),
        "cleanup_local_on_success": bool(
            munchy_job_defaults.get("cleanup_local_on_success", False)
        ),
    }
    if profile_routing:
        job_payload["profile_routing"] = dict(profile_routing)
    storage_hint = {
        "workflow_mode": job_payload["workflow_mode"],
        "collection_archive_destination": str(
            job_payload["collection_archive"].get("destination") or "riverhog"
        ),
        "archive_mode": job_payload["archive_mode"],
        "tasks": job_payload["tasks"],
        "structured_routing": bool(job_payload.get("profile_routing")),
        "groups": {
            name: {
                "archive_mode": munchy_archive_mode(str(group.get("archive_mode") or "av1_nvenc")),
                "tasks": list(group.get("tasks") or []),
            }
            for name, group in groups.items()
        },
    }
    for name, group in groups.items():
        eager_pipeline_batches = group.get("eager_pipeline_batches")
        if eager_pipeline_batches is not None:
            storage_hint["groups"][name]["eager_pipeline_batches"] = eager_pipeline_batches
    return RunnerUploadRequest(
        upload_id=str(batch["input_upload_id"]),
        job_id=str(batch["job_id"]),
        files=files,
        storage_hint=storage_hint,
        job_payload=job_payload,
        upload_workers=target.upload_workers,
        upload_chunk_mib=max(1, target.upload_chunk_bytes // (1024 * 1024)),
    )


def filesystem_metadata_source(row: sqlite3.Row) -> Path:
    source = Path(str(row["source_path"]))
    if source.exists():
        return source
    staging = Path(str(row["staging_path"]))
    if staging.exists():
        return staging
    raise UnrecoverableJebError(f"source and staging file are both missing: {source} -> {staging}")


def run_safe_remux(*, ffmpeg_path: str, source: Path, dest: Path) -> None:
    command = [
        ffmpeg_path,
        "-hide_banner",
        "-loglevel",
        "warning",
        "-y",
        "-i",
        str(source),
        "-map",
        "0",
        "-c",
        "copy",
        "-copy_unknown",
    ]
    if dest.suffix.lower() in MP4_LIKE_EXTENSIONS:
        command.extend(["-movflags", "+faststart"])
    command.append(str(dest))
    try:
        completed = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError as exc:
        raise UnrecoverableJebError(f"{ffmpeg_path} was not found for safe remux repair") from exc
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "no ffmpeg details").strip()
        raise PreflightJebError(f"ffmpeg safe remux failed: {detail}")
    if not dest.exists() or dest.stat().st_size <= 0:
        raise PreflightJebError("ffmpeg safe remux produced no output")


def unique_corrupt_path(path: Path) -> Path:
    if not path.exists():
        return path
    for index in range(1, 10_000):
        candidate = path.with_name(f"{path.stem}.{index}{path.suffix}")
        if not candidate.exists():
            return candidate
    raise UnrecoverableJebError(f"could not choose unique corrupt path for {path}")


def format_media_preflight_error(report: MediaPreflightReport) -> str:
    failed = report.failed_results
    message = (
        f"media preflight failed for {len(failed)}/{len(report.results)} file(s); no upload started"
    )
    if not failed:
        return message
    first = failed[0]
    issue = first.issues[0] if first.issues else None
    detail = (
        f"{issue.code}: {issue.message}"
        if issue is not None
        else "preflight failed without details"
    )
    return f"{message}: {first.file.label}: {detail}"


def retry_forever(collector: Collector, label: str, action: Callable[[], Any]) -> Any:
    delay = TRANSIENT_RETRY_INITIAL_SECONDS
    while True:
        try:
            return action()
        except Exception as exc:
            if not is_transient_error(exc):
                raise
            LOG.warning("%s hit transient issue; retrying in %.1fs: %s", label, delay, exc)
            collector.sleep(delay)
            delay = min(delay * 2.0, TRANSIENT_RETRY_MAX_SECONDS)


def is_transient_error(exc: BaseException) -> bool:
    if isinstance(exc, TransientJebError):
        return True
    if isinstance(exc, ServiceUnavailable):
        return True
    if isinstance(exc, httpx.TransportError):
        return True
    if munchy_is_transient_upload_error(exc):
        return True
    if isinstance(exc, Conflict):
        return False
    return False


class RoutingProbeError(JebError):
    """Raised when Jeb cannot inspect media metadata for Munchy routing."""


class RoutingFactsError(JebError):
    """Raised when Jeb cannot inspect ExifTool metadata for Munchy routing."""


def stable_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def ffprobe_for_routing_preflight(path: Path) -> dict[str, Any]:
    try:
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
    except FileNotFoundError as exc:
        raise RoutingProbeError("ffprobe was not found") from exc
    except subprocess.TimeoutExpired as exc:
        raise RoutingProbeError("ffprobe timed out") from exc
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "ffprobe failed")[-1000:]
        raise RoutingProbeError(detail.strip())
    try:
        payload = json.loads(proc.stdout or "{}")
    except json.JSONDecodeError as exc:
        raise RoutingProbeError("ffprobe returned invalid JSON") from exc
    if not isinstance(payload, dict):
        raise RoutingProbeError("ffprobe returned non-object JSON")
    return payload


def exiftool_for_routing_preflight(path: Path, *, tags: Sequence[str]) -> dict[str, Any]:
    try:
        proc = subprocess.run(
            [
                "exiftool",
                "-j",
                "-a",
                "-G1:4",
                "-s",
                "-ee",
                *[f"-{tag}" for tag in tags],
                str(path),
            ],
            text=True,
            capture_output=True,
            check=False,
            timeout=120,
        )
    except FileNotFoundError as exc:
        raise RoutingFactsError("exiftool was not found") from exc
    except subprocess.TimeoutExpired as exc:
        raise RoutingFactsError("exiftool timed out") from exc
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "exiftool failed")[-1000:]
        raise RoutingFactsError(detail.strip())
    try:
        payload = json.loads(proc.stdout or "[]")
    except json.JSONDecodeError as exc:
        raise RoutingFactsError("exiftool returned invalid JSON") from exc
    if not isinstance(payload, list) or not payload or not isinstance(payload[0], dict):
        raise RoutingFactsError("exiftool returned no metadata object")
    return cast(dict[str, Any], payload[0])


DEFAULT_JEB_INCLUDE_EXTENSIONS = (".mp4", ".mov", ".mkv", ".webm", ".xml", ".json", ".txt")


def account_env_name(account: str) -> str:
    return re.sub(r"[^A-Z0-9]", "_", account.upper())


def env_bool(env: Mapping[str, str], name: str, default: bool = False) -> bool:
    value = env.get(name)
    if value is None or not value.strip():
        return default
    text = value.strip().lower()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be boolean")


def env_csv(env: Mapping[str, str], name: str, default: Sequence[str] = ()) -> tuple[str, ...]:
    value = env.get(name)
    if value is None or not value.strip():
        return tuple(default)
    return tuple(item.strip() for item in value.split(",") if item.strip())


def env_value_from(env: Mapping[str, str], name: str, default: str | None = None) -> str | None:
    value = env.get(name)
    if value is not None and value.strip():
        return value.strip()
    return default


def required_env(env: Mapping[str, str], name: str) -> str:
    value = env_value_from(env, name)
    if value is None:
        raise ValueError(f"{name} is required")
    return value


def env_json_mapping(env: Mapping[str, str], name: str) -> dict[str, Any]:
    value = env_value_from(env, name)
    if value is None:
        return {}
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{name} must be JSON") from exc
    if not isinstance(parsed, Mapping):
        raise ValueError(f"{name} must be a JSON object")
    return dict(parsed)


def env_int(env: Mapping[str, str], name: str, default: int) -> int:
    value = env_value_from(env, name)
    if value is None:
        return default
    try:
        return int(value)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc


def parse_cadence(value: str | None, *, env_name: str) -> Cadence:
    cadence = (value or "weekly").strip().lower()
    if cadence not in JEB_CADENCES:
        raise ValueError(
            f"{env_name} must be one of: " + ", ".join(sorted(JEB_CADENCES))
        )
    return cast(Cadence, cadence)


def source_from_account(
    account: str,
    env: Mapping[str, str],
    *,
    landing_dir: Path,
    stable_seconds: int,
    include_extensions: frozenset[str],
) -> SourceConfig:
    account_env = account_env_name(account)
    upload_prefix = normalize_posix(
        env_value_from(env, f"JEB_ACCOUNT_{account_env}_UPLOAD_PREFIX", account) or account
    )
    return SourceConfig(
        id=account,
        enabled=True,
        path=landing_dir / account,
        upload_prefix=upload_prefix,
        stable_seconds=stable_seconds,
        include_extensions=include_extensions,
    )


def collection_munchy_job_defaults(
    config: JebConfig,
    collection: CollectionConfig,
) -> dict[str, Any]:
    defaults = dict(config.munchy_job_defaults)
    defaults.update(dict(collection.munchy_job_defaults))
    return defaults


def munchy_groups_from_defaults(defaults: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    groups = defaults.get("groups")
    if groups is None:
        return {}
    if not isinstance(groups, Mapping):
        raise ValueError("munchy job groups must be a JSON object")
    out: dict[str, dict[str, Any]] = {}
    for name, group in groups.items():
        if not isinstance(group, Mapping):
            raise ValueError(f"munchy job group {name} must be a JSON object")
        out[str(name)] = dict(group)
    return out


def config_from_env(env: Mapping[str, str] | None = None) -> JebConfig:
    values = os.environ if env is None else env
    landing_dir = Path(
        os.path.expandvars(env_value_from(values, "JEB_LANDING_DIR", "/landing") or "/landing")
    )
    state_dir = Path(
        os.path.expandvars(env_value_from(values, "JEB_STATE_DIR", "/state") or "/state")
    )
    accounts = env_csv(values, "JEB_ACCOUNTS")
    if not accounts:
        raise ValueError("JEB_ACCOUNTS is required")
    duplicates = sorted({account for account in accounts if accounts.count(account) > 1})
    if duplicates:
        raise ValueError("JEB_ACCOUNTS has duplicate account(s): " + ", ".join(duplicates))
    for account in accounts:
        if not SAFE_NAME.fullmatch(account):
            raise ValueError(f"JEB account must be a safe slug: {account!r}")

    preflight_repair = env_value_from(values, "JEB_PREFLIGHT_REPAIR", "safe_remux") or "safe_remux"
    if preflight_repair not in {"off", "safe_remux"}:
        raise ValueError("JEB_PREFLIGHT_REPAIR must be off or safe_remux")
    preflight_repair_original = (
        env_value_from(values, "JEB_PREFLIGHT_REPAIR_ORIGINAL", "keep_corrupt")
        or "keep_corrupt"
    )
    if preflight_repair_original not in {"keep_corrupt", "delete"}:
        raise ValueError("JEB_PREFLIGHT_REPAIR_ORIGINAL must be keep_corrupt or delete")
    collector = CollectorSettings(
        interval_seconds=parse_duration(env_value_from(values, "JEB_INTERVAL"), 300),
        state_db=Path(
            os.path.expandvars(
                env_value_from(values, "JEB_STATE_DB", str(state_dir / "jeb.sqlite3"))
                or str(state_dir / "jeb.sqlite3")
            )
        ),
        batch_dir=Path(
            os.path.expandvars(
                env_value_from(
                    values,
                    "JEB_BATCH_DIR",
                    str(landing_dir / ".jeb-batches"),
                )
                or str(landing_dir / ".jeb-batches")
            )
        ),
        preflight_repair=cast(Literal["off", "safe_remux"], preflight_repair),
        preflight_repair_original=cast(
            Literal["keep_corrupt", "delete"], preflight_repair_original
        ),
        preflight_repair_corrupt_dir=Path(
            os.path.expandvars(
                env_value_from(
                    values,
                    "JEB_PREFLIGHT_REPAIR_CORRUPT_DIR",
                    str(landing_dir / "_corrupt"),
                )
                or str(landing_dir / "_corrupt")
            )
        ),
        preflight_repair_ffmpeg=env_value_from(values, "JEB_PREFLIGHT_REPAIR_FFMPEG", "ffmpeg")
        or "ffmpeg",
    )

    notify_url = values.get("RIVERHOG_OPERATOR_WEBHOOK_URL", "").strip()
    reminder_time = normalize_reminder_time(
        env_value_from(values, "RIVERHOG_OPERATOR_WEBHOOK_REMINDER_TIME")
    )
    reminder_timezone = (
        env_value_from(values, "RIVERHOG_OPERATOR_WEBHOOK_REMINDER_TIMEZONE", "UTC") or "UTC"
    )
    reminder_zone(reminder_timezone)
    notify = NotifySettings(
        enabled=env_bool(values, "JEB_NOTIFY_ENABLED", False),
        url=notify_url or "",
        base_url=env_value_from(values, "JEB_NOTIFY_BASE_URL", "") or "",
        webhook_urls=parse_notify_webhook_map(values),
        recipients=env_csv(values, "JEB_NOTIFY_RECIPIENTS"),
        timeout_seconds=float(parse_duration(env_value_from(values, "JEB_NOTIFY_TIMEOUT"), 10)),
        reminder_interval_seconds=parse_duration(
            env_value_from(values, "RIVERHOG_OPERATOR_WEBHOOK_REMINDER_INTERVAL", "24h"),
            86_400,
        ),
        reminder_time=reminder_time,
        reminder_timezone=reminder_timezone,
    )
    if notify.enabled and not (notify.url or notify.webhook_urls):
        raise ValueError(
            "RIVERHOG_OPERATOR_WEBHOOK_URL or RIVERHOG_NOTIFY_WEBHOOKS is required "
            "when JEB_NOTIFY_ENABLED=true"
        )

    target = TargetConfig(
        name="munchy",
        url=required_env(values, "JEB_MUNCHY_URL").rstrip("/"),
        upload_workers=max(1, env_int(values, "JEB_MUNCHY_UPLOAD_WORKERS", 4)),
        upload_chunk_bytes=max(1, env_int(values, "JEB_MUNCHY_UPLOAD_CHUNK_MIB", 64))
        * 1024
        * 1024,
        wait_for_safe_delete=env_bool(values, "JEB_MUNCHY_WAIT_FOR_SAFE_DELETE", True),
    )
    if (
        env_value_from(values, "JEB_CLEANUP", "never") == "after_target_success"
        and not target.wait_for_safe_delete
    ):
        raise ValueError("JEB_CLEANUP=after_target_success requires Munchy safe-delete waiting")
    include_extensions = frozenset(
        item.lower()
        for item in env_csv(values, "JEB_INCLUDE_EXTENSIONS", DEFAULT_JEB_INCLUDE_EXTENSIONS)
    )
    stable_seconds = parse_duration(env_value_from(values, "JEB_STABLE_AGE"), 600)
    sources = tuple(
        source_from_account(
            account,
            values,
            landing_dir=landing_dir,
            stable_seconds=stable_seconds,
            include_extensions=include_extensions,
        )
        for account in accounts
    )
    collections = tuple(collection_from_account(account, values) for account in accounts)
    tasks = list(env_csv(values, "JEB_ARCHIVE_TASKS", ("archive_video",)))
    notify_defaults: dict[str, Any] = {"enabled": notify.enabled}
    if notify.recipients:
        notify_defaults["recipients"] = list(notify.recipients)
    munchy_job_defaults = {
        "workflow_mode": "collection_archive",
        "archive_mode": env_value_from(values, "JEB_ARCHIVE_MODE", "av1_nvenc") or "av1_nvenc",
        "tasks": tasks,
        "collection_archive": {
            "destination": "riverhog",
            "riverhog": {
                "wait": env_value_from(values, "JEB_RIVERHOG_WAIT", "finalized")
                or "finalized"
            },
        },
        "notify": notify_defaults,
        "cleanup_local_on_success": env_bool(values, "JEB_CLEANUP_LOCAL_ON_SUCCESS", False),
    }
    munchy_job_defaults.update(env_json_mapping(values, "JEB_MUNCHY_JOB_JSON"))
    return JebConfig(
        collector=collector,
        notify=notify,
        targets={"munchy": target},
        sources=sources,
        collections=collections,
        munchy_job_defaults=munchy_job_defaults,
    )


def collection_from_account(account: str, env: Mapping[str, str]) -> CollectionConfig:
    cleanup = env_value_from(env, "JEB_CLEANUP", "never") or "never"
    if cleanup not in {"never", "after_target_success"}:
        raise ValueError("JEB_CLEANUP must be never or after_target_success")
    default_cadence = parse_cadence(
        env_value_from(env, "JEB_CADENCE", "weekly"),
        env_name="JEB_CADENCE",
    )
    account_cadence_env = f"JEB_ACCOUNT_{account_env_name(account)}_CADENCE"
    account_munchy_job_env = f"JEB_ACCOUNT_{account_env_name(account)}_MUNCHY_JOB_JSON"
    account_notify_enabled_env = f"JEB_ACCOUNT_{account_env_name(account)}_NOTIFY_ENABLED"
    account_notify_recipients_env = f"JEB_ACCOUNT_{account_env_name(account)}_NOTIFY_RECIPIENTS"
    notify_enabled = env_bool(
        env,
        account_notify_enabled_env,
        env_bool(env, "JEB_NOTIFY_ENABLED", False),
    )
    notify_recipients = env_csv(
        env,
        account_notify_recipients_env,
        env_csv(env, "JEB_NOTIFY_RECIPIENTS"),
    )
    notify: dict[str, Any] = {"enabled": notify_enabled}
    if notify_recipients:
        notify["recipients"] = list(notify_recipients)
    cadence = parse_cadence(
        env_value_from(env, account_cadence_env, default_cadence),
        env_name=account_cadence_env,
    )
    hour = env_int(env, "JEB_HOUR", 0)
    minute = env_int(env, "JEB_MINUTE", 0)
    if not 0 <= hour <= 23:
        raise ValueError("JEB_HOUR must be 0..23")
    if not 0 <= minute <= 59:
        raise ValueError("JEB_MINUTE must be 0..59")
    return CollectionConfig(
        id=account,
        enabled=True,
        collection_slug=account,
        target="munchy",
        notify=notify,
        threshold_bytes=parse_size(env_value_from(env, "JEB_THRESHOLD", "0B")),
        cleanup=cast(Literal["never", "after_target_success"], cleanup),
        source_ids=(account,),
        cadence=cadence,
        weekday=parse_weekday(env_value_from(env, "JEB_WEEKDAY", "monday")),
        hour=hour,
        minute=minute,
        munchy_job_defaults=env_json_mapping(env, account_munchy_job_env),
    )


def parse_weekday(value: Any) -> int:
    if isinstance(value, int):
        if 0 <= value <= 6:
            return value
        raise ValueError("weekday must be 0..6")
    text = str(value).strip().lower()
    names = {
        "monday": 0,
        "mon": 0,
        "tuesday": 1,
        "tue": 1,
        "wednesday": 2,
        "wed": 2,
        "thursday": 3,
        "thu": 3,
        "friday": 4,
        "fri": 4,
        "saturday": 5,
        "sat": 5,
        "sunday": 6,
        "sun": 6,
    }
    if text in names:
        return names[text]
    return parse_weekday(int(text))


def mapping(value: Any) -> Mapping[str, Any]:
    if value is None:
        return {}
    if isinstance(value, Mapping):
        return value
    raise ValueError(f"expected table/object, got {type(value).__name__}")


def sequence(value: Any) -> Sequence[Any]:
    if value is None:
        return ()
    if isinstance(value, list | tuple):
        return value
    raise ValueError(f"expected list, got {type(value).__name__}")


def optional_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None
