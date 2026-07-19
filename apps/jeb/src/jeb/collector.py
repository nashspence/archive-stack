from __future__ import annotations

import fcntl
import hashlib
import json
import logging
import os
import re
import secrets
import shutil
import sqlite3
import subprocess
import threading
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath
from typing import Any, Literal, Protocol, cast

import httpx
from lifecycle_events import (
    CloudEvent,
    SQLiteEventCursorStore,
    SQLiteLifecycleEventLog,
    caused_event,
    cloud_event,
    normalize_event_context,
)
from lifecycle_events.repeats import (
    event_repeat_due,
    event_repeat_zone,
    normalize_event_repeat_time,
)
from munchy_api_client.client import (
    MunchyRunnerClient,
    RunnerInputFile,
    RunnerJobTerminalDuringUpload,
    SubmissionUploadRequest,
    job_finished_cleanly,
)
from munchy_api_client.client import (
    is_transient_upload_error as munchy_is_transient_upload_error,
)
from munchy_api_client.filesystem_metadata import collect_filesystem_metadata
from munchy_api_client.preflight import (
    MP4_LIKE_EXTENSIONS,
    MediaPreflightFile,
    MediaPreflightReport,
    MediaPreflightResult,
    run_media_preflight,
)
from time_formats import format_utc_timestamp, parse_utc_timestamp, utc_now

from jeb.ingress import (
    JebIngressConfig,
    incomplete_tus_upload_status,
    reap_stale_incomplete_tus_uploads,
    scan_incomplete_tus_uploads,
)
from jeb.listing import MAX_LIST_PAGE_SIZE
from jeb.sources import Cadence, SourceConfig, SourceRegistry, SourceRegistryError

LOG = logging.getLogger("jeb")
SAFE_NAME = re.compile(r"^[A-Za-z0-9._-]+$")
TERMINAL_STATES = {"target_succeeded", "cleanup_done", "superseded"}
SOURCE_REMOVAL_TTL = timedelta(minutes=15)
SOURCE_REMOVAL_CHALLENGE = re.compile(r"^(remove|purge)-source-(\d+)-([0-9a-f]{64})$")
SOURCE_PURGE_WARNING = (
    "DANGER: Jeb-managed upload, landing, or staged files selected by this plan may be "
    "the only copies. Purging permanently removes them, and Jeb cannot determine whether "
    "equivalent data exists elsewhere."
)
ATTEMPT_LIST_SORT_FIELDS = frozenset(
    {
        "attempt_number",
        "bytes",
        "collection_slug",
        "collection_timestamp",
        "created_at",
        "file_count",
        "target_submission_id",
        "state",
        "target",
        "updated_at",
    }
)
TRANSIENT_RETRY_INITIAL_SECONDS = 1.0
TRANSIENT_RETRY_MAX_SECONDS = 300.0
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


def collection_identity_timestamp(value: datetime | None = None) -> str:
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


def sqlite_like_literal(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


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
    collector: CollectorSettings
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


class TargetRunner(Protocol):
    def advance(self, collector: Collector, attempt_id: str) -> None: ...

    def cancel(self, collector: Collector, attempt_id: str) -> None: ...


class Collector:
    def __init__(
        self,
        config: JebConfig,
        *,
        target_runners: Mapping[str, TargetRunner] | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.config = config
        self.sleep = sleep
        self.event_log = SQLiteLifecycleEventLog(self.connect)
        self.event_cursors = SQLiteEventCursorStore(self.connect)
        self.target_runners: dict[str, TargetRunner] = {
            "munchy": MunchyTargetRunner(),
        }
        if target_runners:
            self.target_runners.update(target_runners)
        self.source_registry = SourceRegistry(
            database=config.collector.state_db,
            landing_dir=config.ingress.landing_dir,
            ftp_projection=config.ingress.ftp_projection,
            ftp_uid=config.ingress.ftp_uid,
            ftp_gid=config.ingress.ftp_gid,
        )
        self.operation_lock = threading.RLock()

    def connect(self) -> sqlite3.Connection:
        self.config.collector.state_db.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.config.collector.state_db, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=30000")
        return conn

    def emit_issue(
        self,
        *,
        context: Mapping[str, Any],
        error: str,
        component: str,
        severity: str,
    ) -> bool:
        attempt_id = str(context.get("id") or "")
        source_id = str(context.get("source_id") or "")
        event_kind = (
            "source.preflight_failed" if component == "target_preflight" else "attempt.issue"
        )
        subject = attempt_id or source_id or None
        data = {
            "component": component,
            "error": error,
            "severity": severity,
            "source_id": source_id,
            "attempt_id": attempt_id if attempt_id and attempt_id != source_id else "",
            "state": str(context.get("state") or "failed"),
            "target": str(context.get("target_name") or context.get("target") or ""),
            "collection_slug": str(context.get("collection_slug") or ""),
            "collection_timestamp": str(context.get("collection_timestamp") or ""),
        }
        event = cloud_event(
            source=self.config.events.source,
            type=f"io.riverhog.jeb.{event_kind}",
            subject=subject,
            data=data,
        )
        self.event_log.append(event, owner="jeb")
        return True

    def init_db(self) -> None:
        self.config.collector.batch_dir.mkdir(parents=True, exist_ok=True)
        with self.connect() as conn:
            self.create_batch_schema(conn)
            self.ensure_target_preflight_schema(conn)
        self.source_registry.initialize()
        self.event_log.initialize()
        self.event_cursors.initialize()
        with self.connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS source_removals (
                    challenge TEXT PRIMARY KEY,
                    source_id TEXT NOT NULL,
                    plan_json TEXT NOT NULL,
                    status TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    completed_at TEXT
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_jeb_source_removals_source "
                "ON source_removals(source_id, started_at)"
            )

    def create_batch_schema(self, conn: sqlite3.Connection) -> None:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS batches (
                id TEXT PRIMARY KEY,
                source_id TEXT NOT NULL,
                target_name TEXT NOT NULL,
                collection_slug TEXT NOT NULL,
                collection_timestamp TEXT NOT NULL,
                cleanup TEXT NOT NULL,
                manifest_digest TEXT NOT NULL,
                file_count INTEGER NOT NULL DEFAULT 0,
                total_bytes INTEGER NOT NULL DEFAULT 0,
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
                target_submission_id TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                last_error TEXT,
                emitted_error_fingerprint TEXT,
                emitted_error_at TEXT,
                UNIQUE(batch_id, attempt_number),
                FOREIGN KEY(batch_id) REFERENCES batches(id)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS files (
                batch_id TEXT NOT NULL,
                input_path TEXT NOT NULL,
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
        self.ensure_batch_file_summary_triggers(conn)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_jeb_batches_source_period "
            "ON batches(source_id, collection_timestamp)"
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_jeb_batches_source ON batches(source_id, id)")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_jeb_batches_target ON batches(target_name, id)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_jeb_batches_file_count ON batches(file_count, id)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_jeb_batches_total_bytes ON batches(total_bytes, id)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_jeb_batch_attempts_state "
            "ON batch_attempts(state, created_at)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_jeb_batch_attempts_updated "
            "ON batch_attempts(updated_at, id)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_jeb_batch_attempts_created "
            "ON batch_attempts(created_at, id)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_jeb_batch_attempts_state_updated "
            "ON batch_attempts(state, updated_at, id)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_jeb_batch_attempts_target_submission "
            "ON batch_attempts(target_submission_id, id)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_jeb_batch_attempts_batch_state "
            "ON batch_attempts(batch_id, state)"
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_jeb_files_batch ON files(batch_id)")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_jeb_attempt_files_attempt ON attempt_files(attempt_id)"
        )

    def ensure_batch_file_summary_triggers(self, conn: sqlite3.Connection) -> None:
        conn.execute(
            """
            CREATE TRIGGER IF NOT EXISTS trg_jeb_files_summary_insert
            AFTER INSERT ON files
            BEGIN
                UPDATE batches
                SET
                    file_count = file_count + 1,
                    total_bytes = total_bytes + NEW.bytes
                WHERE id = NEW.batch_id;
            END
            """
        )
        conn.execute(
            """
            CREATE TRIGGER IF NOT EXISTS trg_jeb_files_summary_delete
            AFTER DELETE ON files
            BEGIN
                UPDATE batches
                SET
                    file_count = file_count - 1,
                    total_bytes = total_bytes - OLD.bytes
                WHERE id = OLD.batch_id;
            END
            """
        )
        conn.execute(
            """
            CREATE TRIGGER IF NOT EXISTS trg_jeb_files_summary_update_same_batch
            AFTER UPDATE OF batch_id, bytes ON files
            WHEN OLD.batch_id = NEW.batch_id
            BEGIN
                UPDATE batches
                SET total_bytes = total_bytes - OLD.bytes + NEW.bytes
                WHERE id = NEW.batch_id;
            END
            """
        )
        conn.execute(
            """
            CREATE TRIGGER IF NOT EXISTS trg_jeb_files_summary_update_moved_batch
            AFTER UPDATE OF batch_id, bytes ON files
            WHEN OLD.batch_id != NEW.batch_id
            BEGIN
                UPDATE batches
                SET
                    file_count = file_count - 1,
                    total_bytes = total_bytes - OLD.bytes
                WHERE id = OLD.batch_id;

                UPDATE batches
                SET
                    file_count = file_count + 1,
                    total_bytes = total_bytes + NEW.bytes
                WHERE id = NEW.batch_id;
            END
            """
        )

    def ensure_target_preflight_schema(self, conn: sqlite3.Connection) -> None:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS target_preflight_failures (
                source_id TEXT PRIMARY KEY,
                state TEXT NOT NULL,
                collection_slug TEXT NOT NULL,
                target_name TEXT NOT NULL,
                input_paths_json TEXT NOT NULL,
                failure_json TEXT NOT NULL,
                fingerprint TEXT NOT NULL,
                message TEXT NOT NULL,
                file_count INTEGER NOT NULL,
                total_bytes INTEGER NOT NULL,
                first_seen_at TEXT NOT NULL,
                last_seen_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                resolved_at TEXT,
                emitted_error_fingerprint TEXT,
                emitted_error_at TEXT
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_jeb_target_preflight_failures_state "
            "ON target_preflight_failures(state, updated_at)"
        )

    def run_forever(self) -> None:
        self.init_db()
        threading.Thread(
            target=self.consume_munchy_events_forever,
            name="munchy-event-loop",
            daemon=True,
        ).start()
        while True:
            self.run_once()
            self.sleep(self.config.collector.interval_seconds)

    def run_once(self) -> None:
        with self.operation_lock:
            self.init_db()
            reap = reap_stale_incomplete_tus_uploads(
                self.config.ingress,
                self.source_registry,
            )
            if reap["terminated"] or reap["already_absent"]:
                LOG.info(
                    "terminated %s stale incomplete TUS upload(s); %s already absent",
                    reap["terminated"],
                    reap["already_absent"],
                )
            if reap["failed"] or reap["scan_error"]:
                LOG.warning(
                    "incomplete TUS upload cleanup was not fully successful: "
                    "failed=%s scan_error=%s",
                    reap["failed"],
                    reap["scan_error"],
                )
            self.resolve_inactive_target_preflight_failures()
            for attempt_id in self.active_attempt_ids():
                self.process_attempt(attempt_id)
            active_sources = {str(row["source_id"]) for row in self.active_attempts()}
            for source in self.source_registry.list():
                if source.enabled and source.id not in active_sources:
                    self.discover_source(source)
            self.emit_target_preflight_failures()

    def consume_munchy_events_forever(self) -> None:
        target = self.target_by_name("munchy")
        with MunchyRunnerClient(target.url, token=target.token) as client:
            while True:
                try:
                    translated = self.consume_munchy_events_once(client)
                    if translated:
                        continue
                except Exception:
                    LOG.exception("Munchy lifecycle event consumption failed")
                self.sleep(self.config.events.upstream_poll_seconds)

    def consume_munchy_events_once(self, client: MunchyRunnerClient) -> int:
        cursor = self.event_cursors.cursor("munchy")
        page = client.list_lifecycle_events(after=cursor, limit=100)
        translated = sum(1 for event in page.events if self.translate_munchy_event(event))
        if page.next_cursor != cursor:
            self.event_cursors.advance("munchy", page.next_cursor)
        return translated

    def translate_munchy_event(self, event: CloudEvent) -> bool:
        prefix = "io.riverhog.munchy."
        if not event.type.startswith(prefix):
            return False
        job_id = str(event.data.get("job_id") or event.subject or "")
        if not job_id:
            return False
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT
                    a.id AS attempt_id,
                    a.state,
                    b.source_id,
                    b.target_name,
                    b.collection_slug,
                    b.collection_timestamp
                FROM batch_attempts a
                JOIN batches b ON b.id = a.batch_id
                WHERE a.target_submission_id = ?
                ORDER BY a.created_at DESC
                LIMIT 2
                """,
                (job_id,),
            ).fetchall()
        if not rows:
            return False
        if len(rows) > 1:
            raise RuntimeError(f"multiple Jeb attempts claim Munchy job {job_id}")
        row = rows[0]
        suffix = event.type.removeprefix(prefix)
        if suffix.startswith("job."):
            suffix = suffix.removeprefix("job.")
        details = {
            key: value
            for key, value in event.data.items()
            if key not in {"actor", "cause", "context"}
        }
        details.update(
            {
                "actor": {"app": "jeb"},
                "attempt_id": str(row["attempt_id"]),
                "source_id": str(row["source_id"]),
                "state": str(row["state"]),
                "target": str(row["target_name"]),
                "collection_slug": str(row["collection_slug"]),
                "collection_timestamp": str(row["collection_timestamp"]),
                "target_submission_id": job_id,
            }
        )
        translated = caused_event(
            cause=event,
            source=self.config.events.source,
            type=f"io.riverhog.jeb.attempt.target.{suffix}",
            subject=str(row["attempt_id"]),
            data=details,
        )
        context = normalize_event_context(event.data.get("context"))
        context_expires_at = None
        if context is not None:
            context_expires_at = event_timestamp(
                current_time() + timedelta(seconds=self.config.events.context_retention_seconds)
            )
        self.event_log.append_once(
            translated,
            owner="jeb",
            context=context,
            context_expires_at=context_expires_at,
        )
        return True

    def active_attempts(self) -> list[sqlite3.Row]:
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
                    b.source_id,
                    b.target_name,
                    b.collection_slug,
                    b.collection_timestamp,
                    b.cleanup,
                    b.manifest_digest,
                    a.target_submission_id,
                    a.created_at,
                    a.updated_at,
                    a.last_error,
                    a.emitted_error_fingerprint,
                    a.emitted_error_at
                FROM batch_attempts a
                JOIN batches b ON b.id = a.batch_id
                WHERE a.state NOT IN ({placeholders})
                ORDER BY a.created_at
                """,
                terminal,
            ).fetchall()

    def active_attempt_ids(self) -> list[str]:
        return [str(row["id"]) for row in self.active_attempts()]

    def list_attempts(
        self,
        *,
        page: int = 1,
        per_page: int = 25,
        sort: str = "updated_at",
        order: str = "desc",
        query: str | None = None,
        terminal: Literal["active", "terminal", "all"] = "active",
        state: str | None = None,
        states: Sequence[str] | None = None,
        source: str | None = None,
        collection_slug: str | None = None,
        target: str | None = None,
        all_items: bool = False,
    ) -> dict[str, Any]:
        if page < 1:
            raise ValueError("page must be >= 1")
        if not 1 <= per_page <= MAX_LIST_PAGE_SIZE:
            raise ValueError(f"per_page must be between 1 and {MAX_LIST_PAGE_SIZE}")
        if sort not in ATTEMPT_LIST_SORT_FIELDS:
            raise ValueError("sort must be one of: " + ", ".join(sorted(ATTEMPT_LIST_SORT_FIELDS)))
        if order not in {"asc", "desc"}:
            raise ValueError("order must be asc or desc")
        if terminal not in {"active", "terminal", "all"}:
            raise ValueError("terminal must be active, terminal, or all")
        if state is not None and states is not None:
            raise ValueError("state and states are mutually exclusive")

        clauses: list[str] = []
        values: list[object] = []
        if terminal != "all":
            terminal_placeholders = ", ".join("?" for _ in TERMINAL_STATES)
            terminal_values = tuple(sorted(TERMINAL_STATES))
            if terminal == "terminal":
                clauses.append(f"a.state IN ({terminal_placeholders})")
            else:
                clauses.append(f"a.state NOT IN ({terminal_placeholders})")
            values.extend(terminal_values)
        if state:
            clauses.append("a.state = ?")
            values.append(state)
        if states:
            states_tuple = tuple(str(item) for item in states)
            placeholders = ", ".join("?" for _ in states_tuple)
            clauses.append(f"a.state IN ({placeholders})")
            values.extend(states_tuple)
        if source:
            clauses.append("b.source_id = ?")
            values.append(source)
        if collection_slug:
            clauses.append("b.collection_slug = ?")
            values.append(collection_slug)
        if target:
            clauses.append("b.target_name = ?")
            values.append(target)
        if query:
            like = f"%{sqlite_like_literal(query)}%"
            clauses.append(
                """
                (
                    a.id LIKE ? ESCAPE '\\'
                    OR a.batch_id LIKE ? ESCAPE '\\'
                    OR a.state LIKE ? ESCAPE '\\'
                    OR a.target_submission_id LIKE ? ESCAPE '\\'
                    OR b.source_id LIKE ? ESCAPE '\\'
                    OR b.collection_slug LIKE ? ESCAPE '\\'
                    OR b.target_name LIKE ? ESCAPE '\\'
                    OR b.collection_timestamp LIKE ? ESCAPE '\\'
                    OR a.last_error LIKE ? ESCAPE '\\'
                )
                """
            )
            values.extend((like,) * 9)

        where = "WHERE " + " AND ".join(clauses) if clauses else ""
        attempts_sql = f"""
            SELECT
                a.id,
                a.batch_id,
                a.attempt_number,
                a.state,
                b.source_id,
                b.target_name,
                b.collection_slug,
                b.collection_timestamp,
                b.cleanup,
                b.manifest_digest,
                a.target_submission_id,
                a.created_at,
                a.updated_at,
                a.last_error,
                a.emitted_error_at,
                b.file_count,
                b.total_bytes
            FROM batch_attempts a
            JOIN batches b ON b.id = a.batch_id
            {where}
        """
        sort_sql = {
            "attempt_number": "a.attempt_number",
            "bytes": "b.total_bytes",
            "collection_slug": "b.collection_slug",
            "collection_timestamp": "b.collection_timestamp",
            "created_at": "a.created_at",
            "file_count": "b.file_count",
            "target_submission_id": "a.target_submission_id",
            "state": "a.state",
            "target": "b.target_name",
            "updated_at": "a.updated_at",
        }[sort]
        order_sql = order.upper()
        offset = (page - 1) * per_page
        with self.connect() as conn:
            total_row = conn.execute(
                f"SELECT COUNT(*) AS total FROM ({attempts_sql}) batch_page",
                values,
            ).fetchone()
            total = int(total_row["total"] if total_row is not None else 0)
            selected_sql = f"""
                {attempts_sql}
                ORDER BY {sort_sql} {order_sql}, a.id {order_sql}
            """
            selected_rows = (
                conn.execute(selected_sql, values).fetchall()
                if all_items
                else conn.execute(
                    f"{selected_sql} LIMIT ? OFFSET ?",
                    [*values, per_page, offset],
                ).fetchall()
            )
            rows = [dict(row) for row in selected_rows]
            staged_counts = self._staged_file_counts_by_attempt(
                conn,
                [str(row["id"]) for row in rows],
            )
            for row in rows:
                row["staged_file_count"] = staged_counts.get(str(row["id"]), 0)
        attempts = [self._attempt_summary(row) for row in rows]
        result_page = 1 if all_items else page
        result_per_page = total if all_items else per_page
        result_pages = (1 if total else 0) if all_items else (total + per_page - 1) // per_page
        return {
            "page": result_page,
            "per_page": result_per_page,
            "total": total,
            "pages": result_pages,
            "sort": sort,
            "order": order,
            "terminal": terminal,
            "query": query,
            "filters": {
                "source": source,
                "collection_slug": collection_slug,
                "state": state,
                "states": list(states) if states is not None else None,
                "target": target,
            },
            "attempts": attempts,
        }

    def _staged_file_counts_by_attempt(
        self,
        conn: sqlite3.Connection,
        attempt_ids: Sequence[str],
    ) -> dict[str, int]:
        if not attempt_ids:
            return {}
        placeholders = ", ".join("?" for _ in attempt_ids)
        rows = conn.execute(
            f"""
            SELECT
                attempt_id,
                COALESCE(
                    SUM(CASE WHEN staged_at IS NOT NULL THEN 1 ELSE 0 END),
                    0
                ) AS staged_file_count
            FROM attempt_files
            WHERE attempt_id IN ({placeholders})
            GROUP BY attempt_id
            """,
            tuple(attempt_ids),
        ).fetchall()
        return {str(row["attempt_id"]): int(row["staged_file_count"]) for row in rows}

    def status_summary(self, *, include_backlog: bool = True) -> dict[str, Any]:
        state_counts = self.batch_state_counts()
        total_batches = sum(state_counts.values())
        terminal_count = sum(
            count for state, count in state_counts.items() if state in TERMINAL_STATES
        )
        active_preflight_failures = [
            self._target_preflight_failure_summary(row)
            for row in self.target_preflight_failures(state="failed")
        ]
        return {
            "sources": self.source_statuses(include_backlog=include_backlog),
            "batches": {
                "total": total_batches,
                "active": total_batches - terminal_count,
                "terminal": terminal_count,
                "states": state_counts,
            },
            "active_attempts": self.list_attempts(
                terminal="active",
                sort="updated_at",
                order="desc",
                page=1,
                per_page=10,
            ),
            "recent_failures": self.list_attempts(
                terminal="all",
                states=("failed", "cleanup_failed"),
                sort="updated_at",
                order="desc",
                page=1,
                per_page=5,
            ),
            "target_preflight_failures": {
                "total": len(active_preflight_failures),
                "failures": active_preflight_failures,
            },
            "incomplete_tus_uploads": incomplete_tus_upload_status(
                self.config.ingress,
                self.source_registry,
            ),
        }

    def batch_state_counts(self) -> dict[str, int]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT state, COUNT(*) AS count
                FROM batch_attempts
                GROUP BY state
                ORDER BY state
                """
            ).fetchall()
        return {str(row["state"]): int(row["count"]) for row in rows}

    def source_statuses(self, *, include_backlog: bool = True) -> list[dict[str, Any]]:
        failed_preflight_source_ids = self.failed_target_preflight_source_ids()
        statuses: list[dict[str, Any]] = []
        for source in self.source_registry.list():
            payload: dict[str, Any] = {
                "id": source.id,
                "enabled": source.enabled,
                "path": str(source.path),
                "path_exists": source.path.exists(),
                "stable_seconds": source.stable_seconds,
                "include_extensions": sorted(source.include_extensions),
                "collection_slug": source.collection_slug,
                "target": source.target,
                "cleanup": source.cleanup,
                "cadence": source.cadence,
                "threshold_bytes": source.threshold_bytes,
                "target_preflight_failed": source.id in failed_preflight_source_ids,
            }
            if include_backlog:
                try:
                    eligible = self.eligible_files(source)
                except Exception as exc:
                    payload["eligible_error"] = str(exc)
                else:
                    payload["eligible_files"] = len(eligible)
                    payload["eligible_bytes"] = sum(item.bytes for item in eligible)
            statuses.append(payload)
        return statuses

    def _attempt_summary(self, row: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "attempt_id": str(row["id"]),
            "batch_id": str(row["batch_id"]),
            "attempt_number": int(row["attempt_number"]),
            "state": str(row["state"]),
            "source_id": str(row["source_id"]),
            "target_name": str(row["target_name"]),
            "collection_slug": str(row["collection_slug"]),
            "collection_timestamp": str(row["collection_timestamp"]),
            "cleanup": str(row["cleanup"]),
            "manifest_digest": str(row["manifest_digest"]),
            "target_submission_id": row["target_submission_id"],
            "created_at": str(row["created_at"]),
            "updated_at": str(row["updated_at"]),
            "last_error": row["last_error"],
            "emitted_error_at": row["emitted_error_at"],
            "file_count": int(row["file_count"]),
            "total_bytes": int(row["total_bytes"]),
            "staged_file_count": int(row["staged_file_count"]),
        }

    def _target_preflight_failure_summary(self, row: sqlite3.Row) -> dict[str, Any]:
        return {
            "source_id": str(row["source_id"]),
            "state": str(row["state"]),
            "collection_slug": str(row["collection_slug"]),
            "target_name": str(row["target_name"]),
            "file_count": int(row["file_count"]),
            "total_bytes": int(row["total_bytes"]),
            "first_seen_at": str(row["first_seen_at"]),
            "last_seen_at": str(row["last_seen_at"]),
            "updated_at": str(row["updated_at"]),
            "message": str(row["message"]),
        }

    def source_by_id(self, source_id: str) -> SourceConfig:
        try:
            return self.source_registry.get(source_id)
        except SourceRegistryError as exc:
            raise KeyError(source_id) from exc

    def add_source(
        self,
        source_id: str,
        *,
        adapters: Sequence[str],
        template: str,
        credential: str | None = None,
        enabled: bool = True,
        stable_seconds: int = 600,
        include_extensions: Sequence[str] = (),
        collection_slug: str | None = None,
        target: str = "munchy",
        threshold_bytes: int = 0,
        cleanup: Literal["never", "after_target_success"] = "after_target_success",
        cadence: Literal["weekly", "monthly", "seasonal", "manual"] = "weekly",
        weekday: int = 0,
        hour: int = 3,
        minute: int = 0,
    ) -> tuple[SourceConfig, str | None]:
        self.init_db()
        self._validate_source_target(target=target, cleanup=cleanup)
        kwargs: dict[str, Any] = {
            "adapters": adapters,
            "template": template,
            "credential": credential,
            "enabled": enabled,
            "stable_seconds": stable_seconds,
            "collection_slug": collection_slug,
            "target": target,
            "threshold_bytes": threshold_bytes,
            "cleanup": cleanup,
            "cadence": cadence,
            "weekday": weekday,
            "hour": hour,
            "minute": minute,
        }
        if include_extensions:
            kwargs["include_extensions"] = include_extensions
        return self.source_registry.add(source_id, **kwargs)

    def update_source(self, source_id: str, changes: Mapping[str, Any]) -> SourceConfig:
        self.init_db()
        current = self.source_registry.get(source_id)
        target = str(changes.get("target", current.target))
        cleanup = str(changes.get("cleanup", current.cleanup))
        if cleanup not in {"never", "after_target_success"}:
            raise SourceRegistryError("cleanup must be never or after_target_success")
        self._validate_source_target(
            target=target,
            cleanup=cast(Literal["never", "after_target_success"], cleanup),
        )
        return self.source_registry.update(source_id, changes)

    def _validate_source_target(
        self,
        *,
        target: str,
        cleanup: Literal["never", "after_target_success"],
    ) -> None:
        configured = self.target_by_name(target)
        if cleanup == "after_target_success" and not configured.wait_for_safe_delete:
            raise SourceRegistryError(
                "cleanup=after_target_success requires target safe-delete waiting"
            )

    def source_removal_plan(
        self,
        source_id: str,
        *,
        purge: bool,
        expires_at: datetime | None = None,
    ) -> dict[str, Any]:
        self.init_db()
        source = self.source_registry.get(source_id)
        expires = (expires_at or (utc_now() + SOURCE_REMOVAL_TTL)).replace(microsecond=0)
        landing_files = filesystem_listing(source.path)
        with self.connect() as conn:
            batch_row = conn.execute(
                """
                SELECT COUNT(*) AS count, COALESCE(SUM(total_bytes), 0) AS bytes
                FROM batches WHERE source_id = ?
                """,
                (source.id,),
            ).fetchone()
            attempt_rows = conn.execute(
                """
                SELECT a.id, a.state, a.target_submission_id, b.target_name
                FROM batch_attempts a
                JOIN batches b ON b.id = a.batch_id
                WHERE b.source_id = ?
                ORDER BY a.id
                """,
                (source.id,),
            ).fetchall()
            staged_rows = conn.execute(
                """
                SELECT DISTINCT af.staging_path
                FROM attempt_files af
                JOIN batch_attempts a ON a.id = af.attempt_id
                JOIN batches b ON b.id = a.batch_id
                WHERE b.source_id = ?
                ORDER BY af.staging_path
                """,
                (source.id,),
            ).fetchall()
        active_attempts = [
            {
                "id": str(row["id"]),
                "state": str(row["state"]),
                "target": str(row["target_name"]),
            }
            for row in attempt_rows
            if str(row["state"]) not in TERMINAL_STATES
        ]
        staged_files = filesystem_listing(*(Path(str(row["staging_path"])) for row in staged_rows))
        tus_scan = scan_incomplete_tus_uploads(
            self.config.ingress,
            self.source_registry,
        )
        tus_uploads = [
            {"id": upload.upload_id, "bytes": upload.bytes}
            for upload in tus_scan.uploads
            if upload.source_id == source.id
        ]
        tus_bytes = sum(
            upload.bytes for upload in tus_scan.uploads if upload.source_id == source.id
        )
        managed_file_count = (
            landing_files["file_count"] + staged_files["file_count"] + len(tus_uploads)
        )
        managed_bytes = landing_files["bytes"] + staged_files["bytes"] + tus_bytes
        blockers: list[str] = []
        if not purge:
            if managed_file_count:
                blockers.append(
                    f"source has {managed_file_count} Jeb-managed file(s); request a purge plan"
                )
            if active_attempts:
                blockers.append(
                    f"source has {len(active_attempts)} active delivery attempt(s); "
                    "request a purge plan"
                )
        elif active_attempts:
            unsupported = sorted(
                {
                    attempt["target"]
                    for attempt in active_attempts
                    if not callable(
                        getattr(self.target_runners.get(str(attempt["target"])), "cancel", None)
                    )
                }
            )
            if unsupported:
                blockers.append(
                    "active delivery cancellation is unsupported for target(s): "
                    + ", ".join(unsupported)
                )
        plan: dict[str, Any] = {
            "status": "blocked" if blockers else "ready",
            "source": source.id,
            "purge": purge,
            "warning": SOURCE_PURGE_WARNING if purge else None,
            "expires_at": format_utc_timestamp(expires),
            "landing_root": str(source.path.resolve()),
            "landing_files": landing_files,
            "staged_files": staged_files,
            "incomplete_uploads": tus_uploads,
            "active_attempts": active_attempts,
            "batches": int(batch_row["count"] if batch_row is not None else 0),
            "batch_bytes": int(batch_row["bytes"] if batch_row is not None else 0),
            "managed_file_count": managed_file_count,
            "managed_bytes": managed_bytes,
            "blockers": blockers,
        }
        plan["challenge"] = None if blockers else source_removal_challenge(plan, expires)
        return plan

    def remove_source(
        self,
        source_id: str,
        *,
        challenge: str,
    ) -> dict[str, Any]:
        supplied = challenge.strip()
        if not supplied:
            raise SourceRegistryError("source removal challenge is required")
        self.init_db()
        with self.connect() as conn:
            active = conn.execute(
                """
                SELECT * FROM source_removals
                WHERE source_id = ? AND status = 'removing'
                ORDER BY started_at DESC LIMIT 1
                """,
                (source_id,),
            ).fetchone()
            if active is None:
                active = conn.execute(
                    "SELECT * FROM source_removals WHERE challenge = ?",
                    (supplied,),
                ).fetchone()
        if active is not None:
            if not secrets.compare_digest(str(active["challenge"]), supplied):
                raise UnrecoverableJebError(
                    "source removal challenge does not match active removal"
                )
            if str(active["status"]) == "complete":
                return cast(dict[str, Any], json.loads(str(active["plan_json"])))
            plan = cast(dict[str, Any], json.loads(str(active["plan_json"])))
        else:
            expires = source_removal_expiry(supplied)
            if utc_now() > expires:
                raise UnrecoverableJebError("source removal plan has expired; request a new plan")
            plan = self.source_removal_plan(
                source_id,
                purge=source_removal_is_purge(supplied),
                expires_at=expires,
            )
            expected = str(plan.get("challenge") or "")
            if not secrets.compare_digest(expected, supplied):
                raise UnrecoverableJebError("source removal plan changed; request a new plan")
            blockers = [str(item) for item in plan["blockers"]]
            if blockers:
                raise UnrecoverableJebError("source removal is blocked: " + "; ".join(blockers))
            with self.connect() as conn:
                conn.execute(
                    """
                    INSERT INTO source_removals(
                        source_id, challenge, plan_json, status, started_at
                    ) VALUES(?, ?, ?, 'removing', ?)
                    """,
                    (
                        source_id,
                        supplied,
                        stable_json(plan),
                        event_timestamp(),
                    ),
                )
        self._apply_source_removal(plan)
        result = {
            "status": "removed",
            "source": source_id,
            "purged": bool(plan["purge"]),
            "files": int(plan["managed_file_count"]),
            "bytes": int(plan["managed_bytes"]),
        }
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE source_removals
                SET status = 'complete', plan_json = ?, completed_at = ?
                WHERE source_id = ? AND challenge = ?
                """,
                (stable_json(result), event_timestamp(), source_id, supplied),
            )
        return result

    def _apply_source_removal(self, plan: Mapping[str, Any]) -> None:
        source_id = str(plan["source"])
        if bool(plan["purge"]):
            for attempt in cast(list[dict[str, Any]], plan["active_attempts"]):
                try:
                    self.load_attempt(str(attempt["id"]))
                except KeyError:
                    continue
                runner = self.target_runners[str(attempt["target"])]
                runner.cancel(self, str(attempt["id"]))
            for upload in cast(list[dict[str, Any]], plan["incomplete_uploads"]):
                terminate_tus_upload(self.config.ingress, str(upload["id"]))
            shutil.rmtree(Path(str(plan["landing_root"])), ignore_errors=True)
        with self.connect() as conn:
            attempt_rows = conn.execute(
                """
                SELECT a.id
                FROM batch_attempts a JOIN batches b ON b.id = a.batch_id
                WHERE b.source_id = ?
                """,
                (source_id,),
            ).fetchall()
            batch_rows = conn.execute(
                "SELECT id FROM batches WHERE source_id = ?",
                (source_id,),
            ).fetchall()
        for row in attempt_rows:
            shutil.rmtree(
                self.config.collector.batch_dir / str(row["id"]),
                ignore_errors=True,
            )
        for row in batch_rows:
            shutil.rmtree(
                self.config.collector.batch_dir / str(row["id"]),
                ignore_errors=True,
            )
        with self.connect() as conn:
            conn.execute(
                """
                DELETE FROM attempt_files WHERE attempt_id IN (
                    SELECT a.id FROM batch_attempts a
                    JOIN batches b ON b.id = a.batch_id
                    WHERE b.source_id = ?
                )
                """,
                (source_id,),
            )
            conn.execute(
                """
                DELETE FROM batch_attempts WHERE batch_id IN (
                    SELECT id FROM batches WHERE source_id = ?
                )
                """,
                (source_id,),
            )
            conn.execute(
                "DELETE FROM files WHERE batch_id IN (SELECT id FROM batches WHERE source_id = ?)",
                (source_id,),
            )
            conn.execute("DELETE FROM batches WHERE source_id = ?", (source_id,))
            conn.execute("DELETE FROM target_preflight_failures WHERE source_id = ?", (source_id,))
        try:
            self.source_registry.get(source_id)
        except SourceRegistryError:
            pass
        else:
            self.source_registry.delete(source_id)

    def target_by_name(self, target_name: str) -> TargetConfig:
        try:
            return self.config.targets[target_name]
        except KeyError as exc:
            raise UnrecoverableJebError(f"unknown target {target_name!r}") from exc

    def load_attempt(self, attempt_id: str) -> sqlite3.Row:
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT
                    a.id,
                    a.batch_id,
                    a.attempt_number,
                    a.state,
                    b.source_id,
                    b.target_name,
                    b.collection_slug,
                    b.collection_timestamp,
                    b.cleanup,
                    b.manifest_digest,
                    a.target_submission_id,
                    a.created_at,
                    a.updated_at,
                    a.last_error,
                    a.emitted_error_fingerprint,
                    a.emitted_error_at
                FROM batch_attempts a
                JOIN batches b ON b.id = a.batch_id
                WHERE a.id = ?
                """,
                (attempt_id,),
            ).fetchone()
        if row is None:
            raise KeyError(attempt_id)
        return cast(sqlite3.Row, row)

    def attempt_files(self, attempt_id: str) -> list[sqlite3.Row]:
        with self.connect() as conn:
            return conn.execute(
                """
                SELECT
                    f.batch_id,
                    af.attempt_id,
                    f.input_path,
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
                (attempt_id,),
            ).fetchall()

    def set_attempt_state(self, attempt_id: str, state: str, error: str | None = None) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE batch_attempts
                SET state = ?, updated_at = ?, last_error = ?
                WHERE id = ?
                """,
                (state, event_timestamp(), error, attempt_id),
            )

    def set_attempt_fields(self, attempt_id: str, **fields: object) -> None:
        if not fields:
            return
        allowed = {
            "state",
            "target_submission_id",
            "last_error",
            "emitted_error_fingerprint",
            "emitted_error_at",
        }
        unknown = sorted(set(fields) - allowed)
        if unknown:
            raise ValueError(f"unsupported attempt field(s): {', '.join(unknown)}")
        assignments = [f"{name} = ?" for name in fields]
        values = list(fields.values())
        assignments.append("updated_at = ?")
        values.append(event_timestamp())
        values.append(attempt_id)
        with self.connect() as conn:
            conn.execute(
                f"UPDATE batch_attempts SET {', '.join(assignments)} WHERE id = ?",
                values,
            )

    def discover_source(
        self,
        source: SourceConfig,
        *,
        force: bool = False,
        allow_preflight_retry: bool = False,
    ) -> str | None:
        if not force and source.cadence == "manual":
            return None
        period = current_time() if force else self.source_period(source)
        before = None if force else period
        if not source.enabled:
            return None
        if not allow_preflight_retry and self.target_preflight_failure_active(source.id):
            LOG.info(
                "source %s has an active target preflight failure; skipping until operator retry",
                source.id,
            )
            return None
        files = self.eligible_files(source, before=before)
        if files:
            accepted_files = self.preflight_source_target(source, files)
            if accepted_files is None:
                return None
            files = accepted_files
        if not files:
            return None
        target_paths = [item.target_path for item in files]
        if len(target_paths) != len(set(target_paths)):
            duplicates = sorted(path for path in set(target_paths) if target_paths.count(path) > 1)
            raise UnrecoverableJebError(
                f"source {source.id} has duplicate upload path(s): " + ", ".join(duplicates[:5])
            )
        total = sum(item.bytes for item in files)
        if total < source.threshold_bytes:
            LOG.info(
                "source %s below threshold: %.2fGB eligible",
                source.id,
                total / 1_000_000_000,
            )
            return None
        base_batch_id, base_digest = self.batch_identity(source, files, period=period)
        if (
            not force
            and source.cadence != "manual"
            and self.batch_exists_for_period(
                source.id,
                period,
            )
        ):
            return None
        return self.create_batch(
            source,
            files,
            period=period,
            batch_id=base_batch_id,
            digest=base_digest,
        )

    def source_period(self, source: SourceConfig) -> datetime:
        current = current_time()
        if source.cadence == "manual":
            return current
        if source.cadence == "weekly":
            return self.last_weekly_boundary(source, current)
        period_start = self.current_period_start(source.cadence, current)
        candidate = self.first_weekly_boundary_on_or_after(source, period_start)
        if candidate > current:
            period_start = self.previous_period_start(source.cadence, period_start)
            candidate = self.first_weekly_boundary_on_or_after(source, period_start)
        return candidate.astimezone(UTC)

    def last_weekly_boundary(
        self,
        source: SourceConfig,
        current: datetime,
    ) -> datetime:
        days_since = (current.weekday() - source.weekday) % 7
        candidate = current.replace(
            hour=source.hour,
            minute=source.minute,
            second=0,
            microsecond=0,
        ) - timedelta(days=days_since)
        if candidate > current:
            candidate -= timedelta(days=7)
        return candidate.astimezone(UTC)

    def first_weekly_boundary_on_or_after(
        self,
        source: SourceConfig,
        period_start: datetime,
    ) -> datetime:
        candidate = period_start.replace(
            hour=source.hour,
            minute=source.minute,
            second=0,
            microsecond=0,
        )
        days_until = (source.weekday - candidate.weekday()) % 7
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
        source_id: str,
        period: datetime,
    ) -> bool:
        timestamp = collection_identity_timestamp(period)
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT a.id, a.state
                FROM batches b
                JOIN batch_attempts a ON a.batch_id = b.id
                WHERE b.source_id = ? AND b.collection_timestamp = ?
                """,
                (source_id, timestamp),
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
            target_path = normalize_posix(PurePosixPath(source.id, *rel.parts))
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

    def preflight_source_target(
        self,
        source: SourceConfig,
        files: Sequence[EligibleFile],
    ) -> list[EligibleFile] | None:
        accepted, _summary = self.preflight_source_target_with_summary(
            source,
            files,
            record_failures=True,
        )
        return accepted

    def preflight_source_target_with_summary(
        self,
        source: SourceConfig,
        files: Sequence[EligibleFile],
        *,
        record_failures: bool,
    ) -> tuple[list[EligibleFile] | None, dict[str, Any]]:
        if not files:
            return [], {
                "ok": True,
                "status": "no_files",
                "file_count": 0,
                "template": source.template,
            }
        target = self.target_by_name(source.target)
        request = SubmissionUploadRequest(
            submission_id=f"preflight-{source.id}",
            template=source.template,
            files=tuple(
                RunnerInputFile(
                    source=item.path,
                    rel_path=item.target_path,
                    bytes=item.bytes,
                    sha256="",
                )
                for item in files
            ),
            collection_slug=source.collection_slug,
            collection_timestamp=collection_identity_timestamp(),
        )
        client = MunchyRunnerClient(target.url, token=target.token)
        try:
            try:
                result = client.preflight_submission(request)
            except Exception as exc:
                if is_transient_error(exc):
                    LOG.warning(
                        "source %s target preflight hit a transient issue; will retry later: %s",
                        source.id,
                        exc,
                    )
                    return None, {
                        "ok": False,
                        "status": "transient_error",
                        "file_count": len(files),
                        "template": source.template,
                        "error": str(exc),
                    }
                if record_failures:
                    self.record_target_preflight_failure(
                        source=source,
                        files=files,
                        error=exc,
                    )
                    self.emit_target_preflight_failures(source_id=source.id)
                return None, {
                    "ok": False,
                    "status": "rejected",
                    "file_count": len(files),
                    "template": source.template,
                    "error": str(exc),
                }
            accepted = bool(result.get("accepted"))
            summary = {
                "ok": accepted,
                "status": "accepted" if accepted else "rejected",
                "file_count": len(files),
                "template": source.template,
                "result": result,
            }
            if accepted:
                if record_failures:
                    self.clear_target_preflight_failure(source.id)
                return list(files), summary
            error = UnrecoverableJebError("target rejected submission preflight")
            if record_failures:
                self.record_target_preflight_failure(
                    source=source,
                    files=files,
                    error=error,
                )
                self.emit_target_preflight_failures(source_id=source.id)
            return None, summary
        finally:
            client.close()

    def record_target_preflight_failure(
        self,
        *,
        source: SourceConfig,
        files: Sequence[EligibleFile],
        error: BaseException,
    ) -> None:
        error_text = str(error)
        status = getattr(error, "status", None)
        failure_payload = {
            "ok": False,
            "error": error_text,
            "error_type": error.__class__.__name__,
            "status": status,
        }
        fingerprint_payload = {
            "source_id": source.id,
            "template": source.template,
            "error": error_text[:500],
            "error_type": error.__class__.__name__,
            "status": status,
        }
        self.store_target_preflight_failure(
            source=source,
            files=files,
            failure_payload=failure_payload,
            fingerprint_payload=fingerprint_payload,
            message=target_preflight_error(source_id=source.id, error=error),
        )

    def store_target_preflight_failure(
        self,
        *,
        source: SourceConfig,
        files: Sequence[EligibleFile],
        failure_payload: Mapping[str, Any],
        fingerprint_payload: Mapping[str, Any],
        message: str,
    ) -> None:
        now_text = event_timestamp()
        fingerprint = hashlib.sha256(stable_json(fingerprint_payload).encode()).hexdigest()[:24]
        input_paths = [item.target_path for item in files[:20]]
        with self.connect() as conn:
            existing = conn.execute(
                """
                SELECT first_seen_at, emitted_error_fingerprint, emitted_error_at
                FROM target_preflight_failures
                WHERE source_id = ?
                """,
                (source.id,),
            ).fetchone()
            first_seen_at = str(existing["first_seen_at"]) if existing is not None else now_text
            conn.execute(
                """
                INSERT INTO target_preflight_failures(
                    source_id, state, collection_slug, target_name,
                    input_paths_json, failure_json, fingerprint, message,
                    file_count, total_bytes, first_seen_at,
                    last_seen_at, updated_at, resolved_at,
                    emitted_error_fingerprint, emitted_error_at
                )
                VALUES(?, 'failed', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?)
                ON CONFLICT(source_id) DO UPDATE SET
                    state = 'failed',
                    collection_slug = excluded.collection_slug,
                    target_name = excluded.target_name,
                    input_paths_json = excluded.input_paths_json,
                    failure_json = excluded.failure_json,
                    fingerprint = excluded.fingerprint,
                    message = excluded.message,
                    file_count = excluded.file_count,
                    total_bytes = excluded.total_bytes,
                    last_seen_at = excluded.last_seen_at,
                    updated_at = excluded.updated_at,
                    resolved_at = NULL
                """,
                (
                    source.id,
                    source.collection_slug,
                    source.target,
                    stable_json(input_paths),
                    stable_json(failure_payload),
                    fingerprint,
                    message,
                    len(files),
                    sum(item.bytes for item in files),
                    first_seen_at,
                    now_text,
                    now_text,
                    (
                        str(existing["emitted_error_fingerprint"])
                        if existing is not None
                        and existing["emitted_error_fingerprint"] is not None
                        else None
                    ),
                    (
                        str(existing["emitted_error_at"])
                        if existing is not None and existing["emitted_error_at"] is not None
                        else None
                    ),
                ),
            )

    def clear_target_preflight_failure(self, source_id: str) -> None:
        now_text = event_timestamp()
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE target_preflight_failures
                SET state = 'resolved', resolved_at = ?, updated_at = ?
                WHERE source_id = ? AND state = 'failed'
                """,
                (now_text, now_text, source_id),
            )

    def target_preflight_failure_active(self, source_id: str) -> bool:
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT 1 FROM target_preflight_failures
                WHERE source_id = ? AND state = 'failed'
                """,
                (source_id,),
            ).fetchone()
        return row is not None

    def failed_target_preflight_source_ids(self) -> set[str]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT source_id FROM target_preflight_failures
                WHERE state = 'failed'
                """
            ).fetchall()
        return {str(row["source_id"]) for row in rows}

    def active_target_preflight_source_ids(self) -> set[str]:
        return {source.id for source in self.source_registry.list() if source.enabled}

    def resolve_inactive_target_preflight_failures(self) -> int:
        active_source_ids = sorted(self.active_target_preflight_source_ids())
        now_text = event_timestamp()
        with self.connect() as conn:
            if active_source_ids:
                placeholders = ", ".join("?" for _ in active_source_ids)
                cursor = conn.execute(
                    f"""
                    UPDATE target_preflight_failures
                    SET state = 'resolved', resolved_at = ?, updated_at = ?
                    WHERE state = 'failed' AND source_id NOT IN ({placeholders})
                    """,
                    (now_text, now_text, *active_source_ids),
                )
            else:
                cursor = conn.execute(
                    """
                    UPDATE target_preflight_failures
                    SET state = 'resolved', resolved_at = ?, updated_at = ?
                    WHERE state = 'failed'
                    """,
                    (now_text, now_text),
                )
        resolved = cursor.rowcount if cursor.rowcount is not None else 0
        if resolved:
            LOG.info("resolved %s inactive target preflight failure(s)", resolved)
        return resolved

    def target_preflight_failures(
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
                SELECT * FROM target_preflight_failures
                {where}
                ORDER BY state, source_id, updated_at DESC
                """,
                values,
            ).fetchall()

    def emit_target_preflight_failures(self, source_id: str | None = None) -> None:
        self.resolve_inactive_target_preflight_failures()
        for row in self.target_preflight_failures(source_id=source_id, state="failed"):
            self.emit_target_preflight_failure(row)

    def emit_target_preflight_failure(self, row: sqlite3.Row) -> bool:
        row_payload = dict(row)
        fingerprint = str(row_payload["fingerprint"])
        if row_payload.get(
            "emitted_error_fingerprint"
        ) == fingerprint and not self.event_repeat_due(row_payload):
            return True
        source_id = str(row_payload["source_id"])
        context = {
            "id": source_id,
            "source_id": source_id,
            "target_name": str(row_payload["target_name"]),
            "collection_slug": str(row_payload["collection_slug"]),
            "collection_timestamp": collection_identity_timestamp(),
            "state": "failed",
        }
        if not self.emit_issue(
            context=context,
            error=str(row_payload["message"]),
            component="target_preflight",
            severity="warning",
        ):
            return False
        now_text = event_timestamp()
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE target_preflight_failures
                SET emitted_error_fingerprint = ?, emitted_error_at = ?, updated_at = ?
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
        with self.operation_lock:
            try:
                source = self.source_by_id(source_id)
            except KeyError as exc:
                raise UnrecoverableJebError(f"source {source_id!r} is not enrolled") from exc
            if not source.enabled:
                raise UnrecoverableJebError(f"source {source_id!r} is disabled")
            failed_attempt = self.latest_failed_attempt_for_source(source.id)
            attempt_id: str | None
            if failed_attempt is not None:
                if self.failed_attempt_target_paths_match_current_config(
                    failed_attempt,
                    source,
                ):
                    attempt_id = self.create_retry_attempt(str(failed_attempt["id"]))
                else:
                    LOG.info(
                        "failed attempt %s target paths no longer match current source "
                        "config; rediscovering source %s",
                        failed_attempt["id"],
                        source.id,
                    )
                    attempt_id = self.discover_source(
                        source,
                        force=True,
                        allow_preflight_retry=True,
                    )
                    if attempt_id is not None:
                        self.supersede_attempt(str(failed_attempt["id"]))
            else:
                attempt_id = self.discover_source(
                    source,
                    force=True,
                    allow_preflight_retry=True,
                )
            if attempt_id is not None and process:
                self.process_attempt(attempt_id)
            return attempt_id

    def archive_plan(
        self,
        *,
        source_id: str,
        process: bool = True,
    ) -> dict[str, Any]:
        with self.operation_lock:
            try:
                source = self.source_by_id(source_id)
            except KeyError as exc:
                raise UnrecoverableJebError(f"source {source_id!r} is not enrolled") from exc
            if not source.enabled:
                raise UnrecoverableJebError(f"source {source_id!r} is disabled")
            target = self.target_by_name(source.target)
            base_payload: dict[str, Any] = {
                "source": source.id,
                "collection_slug": source.collection_slug,
                "target_name": target.name,
                "cleanup": source.cleanup,
                "cadence": source.cadence,
                "threshold_bytes": source.threshold_bytes,
                "process": process,
                "dry_run": True,
            }

            failed_attempt = self.latest_failed_attempt_for_source(source.id)
            if failed_attempt is not None and self.failed_attempt_target_paths_match_current_config(
                failed_attempt,
                source,
            ):
                batch_id = str(failed_attempt["batch_id"])
                rows = self.attempt_files(batch_id)
                return {
                    **base_payload,
                    "status": "would_retry_process" if process else "would_retry_stage",
                    "mode": "retry_failed_attempt",
                    "failed_attempt_id": str(failed_attempt["id"]),
                    "batch_id": batch_id,
                    "attempt_id": str(failed_attempt["id"]),
                    "file_count": len(rows),
                    "total_bytes": sum(int(row["bytes"] or 0) for row in rows),
                    "target_preflight": {
                        "ok": True,
                        "status": "not_rerun_for_retry",
                    },
                }

            period = current_time()
            eligible_files = self.eligible_files(source)
            if not eligible_files:
                return {
                    **base_payload,
                    "status": "no_eligible_files",
                    "mode": "discover",
                    "file_count": 0,
                    "total_bytes": 0,
                    "target_preflight": {
                        "ok": True,
                        "status": "not_needed",
                    },
                }

            accepted_files, preflight = self.preflight_source_target_with_summary(
                source,
                eligible_files,
                record_failures=False,
            )
            if accepted_files is None:
                return {
                    **base_payload,
                    "status": "target_preflight_failed",
                    "mode": "discover",
                    "file_count": len(eligible_files),
                    "total_bytes": sum(item.bytes for item in eligible_files),
                    "target_preflight": preflight,
                }

            target_paths = [item.target_path for item in accepted_files]
            duplicates = sorted(path for path in set(target_paths) if target_paths.count(path) > 1)
            if duplicates:
                raise UnrecoverableJebError(
                    f"source {source.id} has duplicate upload path(s): " + ", ".join(duplicates[:5])
                )
            total = sum(item.bytes for item in accepted_files)
            if total < source.threshold_bytes:
                return {
                    **base_payload,
                    "status": "below_threshold",
                    "mode": "discover",
                    "file_count": len(accepted_files),
                    "total_bytes": total,
                    "target_preflight": preflight,
                }

            batch_id, digest = self.batch_identity(source, accepted_files, period=period)
            collection_timestamp = collection_identity_timestamp(period)
            target_submission_id = f"jeb-{source.id}-{collection_timestamp.lower()}-{digest}"
            return {
                **base_payload,
                "status": "would_process" if process else "would_stage",
                "mode": "discover",
                "batch_id": batch_id,
                "attempt_id": batch_id,
                "manifest_digest": digest,
                "collection_timestamp": collection_timestamp,
                "target_submission_id": target_submission_id,
                "file_count": len(accepted_files),
                "total_bytes": total,
                "target_preflight": preflight,
            }

    def latest_failed_attempt_for_source(self, source_id: str) -> sqlite3.Row | None:
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT a.*
                FROM batch_attempts a
                JOIN batches b ON b.id = a.batch_id
                WHERE b.source_id = ?
                  AND a.state = 'failed'
                ORDER BY a.created_at DESC, a.id DESC
                LIMIT 1
                """,
                (source_id,),
            ).fetchone()
        return cast(sqlite3.Row | None, row)

    def failed_attempt_target_paths_match_current_config(
        self,
        failed_attempt: sqlite3.Row,
        source: SourceConfig,
    ) -> bool:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT input_path, target_path FROM files WHERE batch_id = ?",
                (failed_attempt["batch_id"],),
            ).fetchall()
        current_target_paths: list[str] = []
        for row in rows:
            current_target_path = self.current_target_path_for_input_path(
                Path(str(row["input_path"])),
                source,
            )
            if current_target_path is None:
                return False
            current_target_paths.append(current_target_path)
        if len(current_target_paths) != len(set(current_target_paths)):
            return False
        stored_target_paths = [str(row["target_path"]) for row in rows]
        return sorted(stored_target_paths) == sorted(current_target_paths)

    def current_target_path_for_input_path(
        self,
        input_path: Path,
        source: SourceConfig,
    ) -> str | None:
        try:
            rel = input_path.relative_to(source.path)
        except ValueError:
            return None
        return normalize_posix(PurePosixPath(source.id, *rel.parts))

    def supersede_attempt(self, attempt_id: str) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE batch_attempts
                SET state = 'superseded', updated_at = ?
                WHERE id = ?
                """,
                (event_timestamp(), attempt_id),
            )

    def create_retry_attempt(self, failed_attempt_id: str) -> str:
        failed_attempt = self.load_attempt(failed_attempt_id)
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
            target_submission_id = (
                f"jeb-{failed_attempt['source_id']}-"
                f"{str(failed_attempt['collection_timestamp']).lower()}-"
                f"{failed_attempt['manifest_digest']}{suffix}"
            )
            created_at = event_timestamp()
            conn.execute(
                """
                INSERT INTO batch_attempts(
                    id, batch_id, attempt_number, state, target_submission_id,
                    created_at, updated_at
                )
                VALUES(?, ?, ?, 'batching', ?, ?, ?)
                """,
                (
                    attempt_id,
                    batch_id,
                    attempt_number,
                    target_submission_id,
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
        source: SourceConfig,
        files: Sequence[EligibleFile],
        *,
        period: datetime,
        batch_id: str | None = None,
        digest: str | None = None,
    ) -> str:
        collection_timestamp = collection_identity_timestamp(period)
        if batch_id is None or digest is None:
            batch_id, digest = self.batch_identity(source, files, period=period)
        target = self.target_by_name(source.target)
        target_submission_id = f"jeb-{source.id}-{collection_timestamp.lower()}-{digest}"
        batch_root = self.config.collector.batch_dir / batch_id / "input"
        created_at = event_timestamp()
        with self.connect() as conn:
            exists = conn.execute("SELECT 1 FROM batches WHERE id = ?", (batch_id,)).fetchone()
            if exists:
                return batch_id
            conn.execute(
                """
                INSERT INTO batches(
                    id, source_id, target_name, collection_slug,
                    collection_timestamp, cleanup, manifest_digest, created_at, updated_at
                )
                VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    batch_id,
                    source.id,
                    target.name,
                    source.collection_slug,
                    collection_timestamp,
                    source.cleanup,
                    digest,
                    created_at,
                    created_at,
                ),
            )
            conn.execute(
                """
                INSERT INTO batch_attempts(
                    id, batch_id, attempt_number, state, target_submission_id,
                    created_at, updated_at
                )
                VALUES(?, ?, 1, 'batching', ?, ?, ?)
                """,
                (
                    batch_id,
                    batch_id,
                    target_submission_id,
                    created_at,
                    created_at,
                ),
            )
            for item in files:
                staging = batch_root / PurePosixPath(item.target_path)
                conn.execute(
                    """
                    INSERT INTO files(batch_id, input_path, target_path, bytes, mtime_ns, sha256)
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
            "created batch %s for source %s with %d files",
            batch_id,
            source.id,
            len(files),
        )
        return batch_id

    def batch_identity(
        self,
        source: SourceConfig,
        files: Sequence[EligibleFile],
        *,
        period: datetime,
    ) -> tuple[str, str]:
        collection_timestamp = collection_identity_timestamp(period)
        manifest = "\n".join(f"{item.target_path} {item.bytes} {item.mtime_ns}" for item in files)
        digest = hashlib.sha256(manifest.encode("utf-8")).hexdigest()[:12]
        return f"{collection_timestamp}__{source.id}__{digest}", digest

    def attempt_process_lock_path(self, attempt_id: str) -> Path:
        digest = hashlib.sha256(attempt_id.encode("utf-8")).hexdigest()[:16]
        return self.config.collector.state_db.parent / "locks" / f"attempt-process-{digest}.lock"

    @contextmanager
    def attempt_process_lock(self, attempt_id: str) -> Iterator[bool]:
        lock_path = self.attempt_process_lock_path(attempt_id)
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with lock_path.open("a+") as lock_file:
            try:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                LOG.info(
                    "attempt %s is already being processed; skipping concurrent run", attempt_id
                )
                yield False
                return
            try:
                lock_file.seek(0)
                lock_file.truncate()
                lock_file.write(
                    f"pid={os.getpid()}\nattempt_id={attempt_id}\nacquired_at={event_timestamp()}\n"
                )
                lock_file.flush()
                yield True
            finally:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

    def process_attempt(self, attempt_id: str) -> None:
        with self.attempt_process_lock(attempt_id) as acquired:
            if not acquired:
                return
            self._process_attempt_locked(attempt_id)

    def _process_attempt_locked(self, attempt_id: str) -> None:
        try:
            attempt = self.load_attempt(attempt_id)
            state = str(attempt["state"])
            if state == "failed":
                self.emit_failed_attempt(attempt_id)
                return
            if state in {"cleanup_pending", "cleanup_failed"}:
                self.cleanup_attempt(attempt_id)
                return
            if state == "batching":
                self.stage_attempt_files(attempt_id)
            if self.load_attempt(attempt_id)["state"] == "batched":
                self.ensure_hashes(attempt_id)
            if self.load_attempt(attempt_id)["state"] == "hashed":
                self.ensure_media_preflight(attempt_id)
            runner = self.target_runners["munchy"]
            runner.advance(self, attempt_id)
            self.finish_attempt_success(attempt_id)
        except PreflightJebError as exc:
            LOG.exception("attempt %s failed media preflight", attempt_id)
            self.mark_unrecoverable(attempt_id, str(exc), component="preflight")
        except UnrecoverableJebError as exc:
            LOG.exception("attempt %s has unrecoverable error", attempt_id)
            self.mark_unrecoverable(attempt_id, str(exc), component="target")
        except TransientJebError as exc:
            LOG.warning("attempt %s hit transient issue; will retry: %s", attempt_id, exc)
            self.set_attempt_fields(attempt_id, last_error=str(exc))
        except Exception as exc:
            if is_transient_error(exc):
                LOG.warning("attempt %s hit transient issue; will retry: %s", attempt_id, exc)
                self.set_attempt_fields(attempt_id, last_error=str(exc))
                return
            LOG.exception("attempt %s failed with unrecoverable target error", attempt_id)
            self.mark_unrecoverable(attempt_id, str(exc), component="target")

    def stage_attempt_files(self, attempt_id: str) -> None:
        for row in self.attempt_files(attempt_id):
            if row["staged_at"]:
                continue
            source = Path(str(row["input_path"]))
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
                    (event_timestamp(), attempt_id, row["target_path"]),
                )
        self.set_attempt_state(attempt_id, "batched")

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
            self.set_attempt_state(batch_id, "hashed")
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
        self.set_attempt_state(batch_id, "hashed")

    def ensure_media_preflight(self, batch_id: str) -> None:
        files = self.media_preflight_files(batch_id)
        if not files:
            self.set_attempt_state(batch_id, "preflighted")
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
        self.set_attempt_state(batch_id, "preflighted")

    def media_preflight_files(self, batch_id: str) -> list[MediaPreflightFile]:
        return [
            MediaPreflightFile(
                source=Path(str(row["staging_path"])),
                label=str(row["target_path"]),
                bytes=int(row["bytes"]),
            )
            for row in self.attempt_files(batch_id)
            if Path(str(row["target_path"])).suffix.lower() in PREFLIGHT_MEDIA_EXTENSIONS
        ]

    def repair_media_preflight_failures(
        self,
        batch_id: str,
        report: MediaPreflightReport,
    ) -> tuple[MediaPreflightReport, list[str]]:
        rows = {str(row["target_path"]): row for row in self.attempt_files(batch_id)}
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
            for row in self.attempt_files(batch_id)
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
        source = Path(str(row["input_path"]))
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
                    (event_timestamp(), batch_id, target_path),
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
        source = Path(str(row["input_path"]))
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

    def finish_attempt_success(self, attempt_id: str) -> None:
        attempt = self.load_attempt(attempt_id)
        state = str(attempt["state"])
        if state == "target_succeeded":
            return
        if state != "target_complete":
            return
        if attempt["cleanup"] == "after_target_success":
            self.set_attempt_state(attempt_id, "cleanup_pending")
            self.cleanup_attempt(attempt_id)
            return
        self.set_attempt_state(attempt_id, "target_succeeded")

    def cleanup_attempt(self, attempt_id: str) -> None:
        try:
            for row in self.attempt_files(attempt_id):
                Path(str(row["input_path"])).unlink(missing_ok=True)
            shutil.rmtree(self.config.collector.batch_dir / attempt_id)
        except FileNotFoundError:
            pass
        except Exception as exc:
            message = f"failed to delete completed attempt files: {exc}"
            self.set_attempt_state(attempt_id, "cleanup_failed", message)
            self.emit_cleanup_failed(attempt_id, message)
            return
        self.set_attempt_state(attempt_id, "cleanup_done")

    def mark_unrecoverable(self, attempt_id: str, message: str, *, component: str) -> None:
        self.set_attempt_state(attempt_id, "failed", message)
        self.emit_failed_attempt(attempt_id, component=component)

    def emit_failed_attempt(self, attempt_id: str, *, component: str = "target") -> None:
        attempt = self.load_attempt(attempt_id)
        message = str(attempt["last_error"] or "Jeb attempt failed")
        self.emit_attempt_issue(attempt, message=message, component=component)

    def emit_cleanup_failed(self, attempt_id: str, message: str) -> None:
        attempt = self.load_attempt(attempt_id)
        self.emit_attempt_issue(attempt, message=message, component="cleanup")

    def emit_attempt_issue(
        self,
        attempt: Mapping[str, Any] | sqlite3.Row,
        *,
        message: str,
        component: str,
    ) -> bool:
        attempt_payload = dict(attempt)
        fingerprint = hashlib.sha256(
            f"{attempt_payload['id']}:{component}:{message}".encode()
        ).hexdigest()[:24]
        if attempt_payload.get(
            "emitted_error_fingerprint"
        ) == fingerprint and not self.event_repeat_due(attempt_payload):
            return True
        if not self.emit_issue(
            context=attempt_payload,
            error=message,
            component=component,
            severity="critical",
        ):
            return False
        self.set_attempt_fields(
            str(attempt_payload["id"]),
            emitted_error_fingerprint=fingerprint,
            emitted_error_at=event_timestamp(),
        )
        return True

    def event_repeat_due(self, batch: Mapping[str, Any]) -> bool:
        last_sent = batch.get("emitted_error_at")
        if not last_sent:
            return True
        try:
            sent_at = parse_utc_timestamp(str(last_sent))
        except ValueError:
            return True
        return event_repeat_due(
            last_emitted_at=sent_at,
            current=current_time(),
            interval=self.config.events.repeat_interval_seconds,
            repeat_time=self.config.events.repeat_time,
            repeat_timezone=self.config.events.repeat_timezone,
        )


class MunchyTargetRunner:
    _REMOTE_STATES = {
        "target_submitted",
        "target_uploading",
        "target_uploaded",
        "target_complete",
        "cleanup_pending",
        "cleanup_failed",
    }

    def advance(self, collector: Collector, attempt_id: str) -> None:
        attempt = collector.load_attempt(attempt_id)
        if attempt["state"] == "target_complete":
            return
        target = collector.target_by_name(str(attempt["target_name"]))
        client = MunchyRunnerClient(target.url, token=target.token)
        try:
            request = munchy_submission_request(collector, attempt_id, target)

            state = str(attempt["state"])
            if state == "preflighted":
                client.create_submission(request)
                collector.set_attempt_state(attempt_id, "target_submitted")
                state = "target_submitted"
            if state in {"target_submitted", "target_uploading"}:
                collector.set_attempt_state(attempt_id, "target_uploading")
                try:
                    client.upload_files(request)
                except RunnerJobTerminalDuringUpload as exc:
                    if job_finished_cleanly(exc.job):
                        collector.set_attempt_state(attempt_id, "target_complete")
                        return
                    raise UnrecoverableJebError(str(exc)) from exc
                collector.set_attempt_state(attempt_id, "target_uploaded")
                state = "target_uploaded"
            if state == "target_uploaded":
                submission = client.wait_for_submission(
                    request.submission_id,
                    wait_for_safe_delete=target.wait_for_safe_delete,
                )
                job = submission.get("job")
                if not isinstance(job, dict):
                    raise UnrecoverableJebError(
                        f"Munchy submission returned invalid job state: {submission}"
                    )
                if not job_finished_cleanly(job):
                    raise UnrecoverableJebError(f"Munchy submission did not finish cleanly: {job}")
                collector.set_attempt_state(attempt_id, "target_complete")
        finally:
            client.close()

    def cancel(self, collector: Collector, attempt_id: str) -> None:
        attempt = collector.load_attempt(attempt_id)
        if str(attempt["state"]) not in self._REMOTE_STATES:
            return
        submission_id = str(attempt["target_submission_id"] or "")
        if not submission_id:
            raise UnrecoverableJebError(
                f"active target delivery has no cancellation identity: {attempt_id}"
            )
        target = collector.target_by_name(str(attempt["target_name"]))
        client = MunchyRunnerClient(target.url, token=target.token)
        try:
            client.cancel_submission(submission_id)
        finally:
            client.close()


def munchy_submission_request(
    collector: Collector,
    attempt_id: str,
    target: TargetConfig,
) -> SubmissionUploadRequest:
    attempt = collector.load_attempt(attempt_id)
    source = collector.source_by_id(str(attempt["source_id"]))
    rows = collector.attempt_files(attempt_id)
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
    return SubmissionUploadRequest(
        submission_id=str(attempt["target_submission_id"]),
        template=source.template,
        files=files,
        collection_slug=str(attempt["collection_slug"]),
        collection_timestamp=str(attempt["collection_timestamp"]),
        event_context={
            "initiator": {
                "app": "jeb",
                "attempt_id": attempt_id,
            }
        },
        upload_workers=target.upload_workers,
        upload_chunk_mib=max(1, target.upload_chunk_bytes // (1024 * 1024)),
    )


def filesystem_metadata_source(row: sqlite3.Row) -> Path:
    source = Path(str(row["input_path"]))
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
    if isinstance(exc, httpx.TransportError):
        return True
    if munchy_is_transient_upload_error(exc):
        return True
    return False


def stable_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def filesystem_listing(*roots: Path) -> dict[str, Any]:
    files: list[dict[str, Any]] = []
    seen: set[str] = set()
    for root in roots:
        candidates: Iterator[Path]
        if root.is_dir():
            candidates = (path for path in sorted(root.rglob("*")) if path.is_file())
        elif root.is_file():
            candidates = iter((root,))
        else:
            continue
        for path in candidates:
            normalized = str(path.resolve())
            if normalized in seen:
                continue
            seen.add(normalized)
            stat = path.stat()
            files.append(
                {
                    "path": normalized,
                    "bytes": stat.st_size,
                    "mtime_ns": stat.st_mtime_ns,
                }
            )
    return {
        "file_count": len(files),
        "bytes": sum(int(item["bytes"]) for item in files),
        "files": files,
    }


def source_removal_challenge(plan: Mapping[str, Any], expires_at: datetime) -> str:
    payload = stable_json(plan).encode("utf-8")
    action = "purge" if bool(plan["purge"]) else "remove"
    return f"{action}-source-{int(expires_at.timestamp())}-{hashlib.sha256(payload).hexdigest()}"


def source_removal_expiry(challenge: str) -> datetime:
    match = SOURCE_REMOVAL_CHALLENGE.fullmatch(challenge)
    if match is None:
        raise SourceRegistryError("invalid source removal challenge")
    return datetime.fromtimestamp(int(match.group(2)), tz=UTC)


def source_removal_is_purge(challenge: str) -> bool:
    match = SOURCE_REMOVAL_CHALLENGE.fullmatch(challenge)
    if match is None:
        raise SourceRegistryError("invalid source removal challenge")
    return match.group(1) == "purge"


def terminate_tus_upload(config: JebIngressConfig, upload_id: str) -> None:
    url = config.tusd_base_url.rstrip("/") + "/" + upload_id
    try:
        response = httpx.delete(
            url,
            headers={"Tus-Resumable": "1.0.0"},
            timeout=10.0,
        )
        if response.status_code != 404:
            response.raise_for_status()
    except httpx.HTTPError as exc:
        raise UnrecoverableJebError(f"could not terminate incomplete upload {upload_id}") from exc
    (config.tus_staging_dir / upload_id).unlink(missing_ok=True)
    (config.tus_staging_dir / f"{upload_id}.info").unlink(missing_ok=True)


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


def env_int(env: Mapping[str, str], name: str, default: int) -> int:
    value = env_value_from(env, name)
    if value is None:
        return default
    try:
        return int(value)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc


def config_from_env(env: Mapping[str, str] | None = None) -> JebConfig:
    values = os.environ if env is None else env
    landing_dir = Path(
        os.path.expandvars(env_value_from(values, "JEB_LANDING_DIR", "/landing") or "/landing")
    )
    state_dir = Path(
        os.path.expandvars(env_value_from(values, "JEB_STATE_DIR", "/state") or "/state")
    )
    tus_incomplete_max_age_seconds = parse_duration(
        env_value_from(values, "JEB_TUS_INCOMPLETE_MAX_AGE", "14d")
    )
    if tus_incomplete_max_age_seconds < 1:
        raise ValueError("JEB_TUS_INCOMPLETE_MAX_AGE must be positive")
    ingress = JebIngressConfig(
        landing_dir=landing_dir,
        tus_staging_dir=Path(
            os.path.expandvars(
                env_value_from(
                    values,
                    "JEB_TUS_STAGING_DIR",
                    str(landing_dir / ".ingress" / "tus"),
                )
                or str(landing_dir / ".ingress" / "tus")
            )
        ),
        tusd_base_url=(
            env_value_from(
                values,
                "JEB_TUSD_BASE_URL",
                "http://jeb-tusd:1080/files/",
            )
            or "http://jeb-tusd:1080/files/"
        ).rstrip("/")
        + "/",
        tus_incomplete_max_age_seconds=tus_incomplete_max_age_seconds,
        ftp_projection=Path(
            os.path.expandvars(
                env_value_from(
                    values,
                    "JEB_FTP_PROJECTION",
                    str(state_dir / "ingress" / "ftp" / "passwd"),
                )
                or str(state_dir / "ingress" / "ftp" / "passwd")
            )
        ),
        ftp_uid=env_int(values, "JEB_FTP_UID", 1000),
        ftp_gid=env_int(values, "JEB_FTP_GID", 1000),
    )

    preflight_repair = env_value_from(values, "JEB_PREFLIGHT_REPAIR", "safe_remux") or "safe_remux"
    if preflight_repair not in {"off", "safe_remux"}:
        raise ValueError("JEB_PREFLIGHT_REPAIR must be off or safe_remux")
    preflight_repair_original = (
        env_value_from(values, "JEB_PREFLIGHT_REPAIR_ORIGINAL", "keep_corrupt") or "keep_corrupt"
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

    repeat_time = normalize_event_repeat_time(env_value_from(values, "JEB_EVENT_REPEAT_TIME"))
    repeat_timezone = env_value_from(values, "JEB_EVENT_REPEAT_TIMEZONE", "UTC") or "UTC"
    event_repeat_zone(repeat_timezone)
    events = LifecycleEventSettings(
        source=env_value_from(values, "JEB_EVENT_SOURCE", "urn:jeb") or "urn:jeb",
        upstream_poll_seconds=max(
            1.0,
            float(env_value_from(values, "JEB_UPSTREAM_EVENT_POLL_SECONDS", "5") or "5"),
        ),
        context_retention_seconds=parse_duration(
            env_value_from(values, "JEB_EVENT_CONTEXT_RETENTION", "30d"),
            30 * 86_400,
        ),
        repeat_interval_seconds=parse_duration(
            env_value_from(values, "JEB_EVENT_REPEAT_INTERVAL", "24h"),
            86_400,
        ),
        repeat_time=repeat_time,
        repeat_timezone=repeat_timezone,
    )

    target = TargetConfig(
        name="munchy",
        url=(
            env_value_from(values, "JEB_MUNCHY_URL", "http://munchy-runner:8080")
            or "http://munchy-runner:8080"
        ).rstrip("/"),
        token=env_value_from(values, "JEB_MUNCHY_TOKEN", "") or "",
        upload_workers=max(1, env_int(values, "JEB_MUNCHY_UPLOAD_WORKERS", 4)),
        upload_chunk_bytes=max(1, env_int(values, "JEB_MUNCHY_UPLOAD_CHUNK_MIB", 64)) * 1024 * 1024,
        wait_for_safe_delete=env_bool(values, "JEB_MUNCHY_WAIT_FOR_SAFE_DELETE", True),
    )
    return JebConfig(
        collector=collector,
        ingress=ingress,
        events=events,
        targets={"munchy": target},
    )


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
