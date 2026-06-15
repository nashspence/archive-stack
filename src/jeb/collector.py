from __future__ import annotations

import copy
import hashlib
import logging
import os
import re
import shutil
import sqlite3
import time
import tomllib
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from threading import Event, Lock
from typing import Any, Literal, Protocol, cast

import httpx

from munchy.runner_client import (
    MunchyRunnerClient,
    RunnerInputFile,
    RunnerJobTerminalDuringUpload,
    RunnerUploadRequest,
    job_finished_cleanly,
)
from munchy.runner_client import (
    is_transient_upload_error as munchy_is_transient_upload_error,
)
from riverhog_cli.client import ApiClient
from riverhog_core.domain.errors import Conflict, ServiceUnavailable
from riverhog_core.tus_upload import TusUploadLease, upload_path_to_tus
from riverhog_core.webhooks import (
    WebhookConfig,
    build_jeb_event_payload,
    post_webhook,
    utcnow,
)

LOG = logging.getLogger("jeb")
SAFE_NAME = re.compile(r"^[A-Za-z0-9._-]+$")
TERMINAL_STATES = {"target_succeeded", "cleanup_done"}
TRANSIENT_RETRY_INITIAL_SECONDS = 1.0
TRANSIENT_RETRY_MAX_SECONDS = 300.0
DEFAULT_GPU_TASKS = ("archive_video", "qcut_video")


class JebError(RuntimeError):
    """Base class for Jeb operational errors."""


class UnrecoverableJebError(JebError):
    """An operator-visible error that cannot be solved by retrying the same operation."""


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


def env_value(env_name: str | None, fallback: str | None) -> str | None:
    if env_name:
        value = os.getenv(env_name)
        if value is not None and value.strip():
            return value.strip()
    return fallback


@dataclass(frozen=True)
class CollectorSettings:
    interval_seconds: int = 300
    state_db: Path = Path("/state/jeb.sqlite3")
    batch_dir: Path = Path("/landing/.jeb-batches")


@dataclass(frozen=True)
class NotifySettings:
    enabled: bool = False
    url: str = ""
    base_url: str = ""
    recipients: tuple[str, ...] = ()
    timeout_seconds: float = 10.0
    reminder_interval_seconds: int = 86_400


@dataclass(frozen=True)
class TargetConfig:
    name: str
    type: Literal["munchy", "riverhog"]
    url: str = ""
    token: str | None = None
    upload_workers: int = 4
    upload_chunk_bytes: int = 64 * 1024 * 1024
    wait: Literal["staged", "finalized"] = "finalized"
    wait_for_safe_delete: bool = True
    ingest_source: str = "jeb"


@dataclass(frozen=True)
class SourceGroup:
    profile: str | None = None
    archive_mode: str = "av1_nvenc"
    gpu_tasks: tuple[str, ...] = DEFAULT_GPU_TASKS


@dataclass(frozen=True)
class SourceConfig:
    id: str
    enabled: bool
    path: Path
    collection_slug: str
    target: str
    threshold_bytes: int
    stable_seconds: int
    max_age_seconds: int
    root_group: str | None
    cleanup: Literal["never", "after_target_success"]
    include_extensions: frozenset[str]
    groups: Mapping[str, SourceGroup]


@dataclass(frozen=True)
class JebConfig:
    collector: CollectorSettings
    notify: NotifySettings
    targets: Mapping[str, TargetConfig]
    sources: tuple[SourceConfig, ...]
    profiles: Mapping[str, Mapping[str, Any]]
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
    ) -> bool: ...


class NullNotifier:
    def critical_batch_issue(
        self,
        *,
        batch: Mapping[str, Any],
        message: str,
        component: str,
    ) -> bool:
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
    ) -> bool:
        if not self.settings.enabled:
            return True
        config = WebhookConfig(
            url=self.settings.url,
            base_url=self.settings.base_url,
            timeout_seconds=self.settings.timeout_seconds,
        )
        recipients: Sequence[str | None] = self.settings.recipients or (None,)
        delivered_at = utcnow()
        ok = True
        for recipient in recipients:
            payload = build_jeb_event_payload(
                event="jeb.issue",
                batch=batch,
                message=message,
                severity="critical",
                delivered_at=delivered_at,
                recipient=recipient,
                details={"component": component, "error": message},
            )
            try:
                post_webhook(config=config, payload=payload)
            except Exception:
                LOG.exception("failed to deliver critical jeb webhook for batch %s", batch["id"])
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
            "riverhog": RiverhogTargetRunner(),
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
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS batches (
                    id TEXT PRIMARY KEY,
                    source_id TEXT NOT NULL,
                    state TEXT NOT NULL,
                    target_name TEXT NOT NULL,
                    target_type TEXT NOT NULL,
                    collection_slug TEXT NOT NULL,
                    collection_timestamp TEXT NOT NULL,
                    input_upload_id TEXT,
                    job_id TEXT,
                    riverhog_collection_id TEXT,
                    cleanup TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    last_error TEXT,
                    notified_error_fingerprint TEXT,
                    notified_error_at TEXT
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS files (
                    batch_id TEXT NOT NULL,
                    source_path TEXT NOT NULL,
                    staging_path TEXT NOT NULL,
                    target_path TEXT NOT NULL,
                    bytes INTEGER NOT NULL,
                    mtime_ns INTEGER NOT NULL,
                    sha256 TEXT,
                    moved_at TEXT,
                    uploaded_at TEXT,
                    PRIMARY KEY (batch_id, target_path)
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_jeb_batches_state "
                "ON batches(state, created_at)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_jeb_batches_source_state "
                "ON batches(source_id, state)"
            )

    def run_forever(self) -> None:
        self.init_db()
        while True:
            self.run_once()
            self.sleep(self.config.collector.interval_seconds)

    def run_once(self) -> None:
        self.init_db()
        for batch_id in self.active_batch_ids():
            self.process_batch(batch_id)
        active_sources = {str(row["source_id"]) for row in self.active_batches()}
        for source in self.config.sources:
            if source.enabled and source.id not in active_sources:
                self.discover_source(source)

    def active_batches(self) -> list[sqlite3.Row]:
        with self.connect() as conn:
            return conn.execute(
                "SELECT * FROM batches WHERE state NOT IN (?, ?) ORDER BY created_at",
                tuple(sorted(TERMINAL_STATES)),
            ).fetchall()

    def active_batch_ids(self) -> list[str]:
        return [str(row["id"]) for row in self.active_batches()]

    def source_by_id(self, source_id: str) -> SourceConfig:
        for source in self.config.sources:
            if source.id == source_id:
                return source
        raise KeyError(source_id)

    def target_by_name(self, target_name: str) -> TargetConfig:
        try:
            return self.config.targets[target_name]
        except KeyError as exc:
            raise UnrecoverableJebError(f"unknown target {target_name!r}") from exc

    def load_batch(self, batch_id: str) -> sqlite3.Row:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM batches WHERE id = ?", (batch_id,)).fetchone()
        if row is None:
            raise KeyError(batch_id)
        return cast(sqlite3.Row, row)

    def batch_files(self, batch_id: str) -> list[sqlite3.Row]:
        with self.connect() as conn:
            return conn.execute(
                "SELECT * FROM files WHERE batch_id = ? ORDER BY target_path",
                (batch_id,),
            ).fetchall()

    def set_batch_state(self, batch_id: str, state: str, error: str | None = None) -> None:
        with self.connect() as conn:
            conn.execute(
                "UPDATE batches SET state = ?, updated_at = ?, last_error = ? WHERE id = ?",
                (state, iso(), error, batch_id),
            )

    def set_batch_fields(self, batch_id: str, **fields: object) -> None:
        if not fields:
            return
        allowed = {
            "state",
            "input_upload_id",
            "job_id",
            "riverhog_collection_id",
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
                f"UPDATE batches SET {', '.join(assignments)} WHERE id = ?",
                values,
            )

    def discover_source(self, source: SourceConfig) -> None:
        files = self.eligible_files(source)
        if not files:
            return
        total = sum(item.bytes for item in files)
        oldest_age = max(0, int(time.time() - min(item.mtime for item in files)))
        if total < source.threshold_bytes and not (
            source.max_age_seconds and oldest_age >= source.max_age_seconds
        ):
            LOG.info(
                "source %s below threshold: %.2fGB eligible, oldest %ss",
                source.id,
                total / 1_000_000_000,
                oldest_age,
            )
            return
        self.create_batch(source, files)

    def eligible_files(self, source: SourceConfig) -> list[EligibleFile]:
        if not source.path.exists():
            return []
        cutoff = time.time() - source.stable_seconds
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
            target_path = self.target_path_for(source, rel)
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

    def target_path_for(self, source: SourceConfig, rel: Path) -> str:
        first = rel.parts[0] if rel.parts else ""
        if first in source.groups:
            return normalize_posix(PurePosixPath(*rel.parts))
        if source.root_group:
            return normalize_posix(PurePosixPath(source.root_group, *rel.parts))
        raise UnrecoverableJebError(f"source {source.id} has file outside configured groups: {rel}")

    def create_batch(self, source: SourceConfig, files: Sequence[EligibleFile]) -> None:
        first_mtime = min(item.mtime for item in files)
        collection_timestamp = datetime.fromtimestamp(first_mtime, UTC).strftime("%Y%m%dT%H%M%SZ")
        manifest = "\n".join(
            f"{item.target_path} {item.bytes} {item.mtime_ns}" for item in files
        )
        digest = hashlib.sha256(manifest.encode("utf-8")).hexdigest()[:12]
        batch_id = f"{collection_timestamp}__{source.id}__{digest}"
        target = self.target_by_name(source.target)
        input_upload_id = f"jeb-{source.id}-{collection_timestamp.lower()}-{digest}"
        job_id = f"{input_upload_id}-job"
        batch_root = self.config.collector.batch_dir / batch_id / "input"
        created_at = iso()
        with self.connect() as conn:
            exists = conn.execute("SELECT 1 FROM batches WHERE id = ?", (batch_id,)).fetchone()
            if exists:
                return
            conn.execute(
                """
                INSERT INTO batches(
                    id, source_id, state, target_name, target_type, collection_slug,
                    collection_timestamp, input_upload_id, job_id, cleanup, created_at, updated_at
                )
                VALUES(?, ?, 'batching', ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    batch_id,
                    source.id,
                    target.name,
                    target.type,
                    source.collection_slug,
                    collection_timestamp,
                    input_upload_id,
                    job_id,
                    source.cleanup,
                    created_at,
                    created_at,
                ),
            )
            for item in files:
                staging = batch_root / PurePosixPath(item.target_path)
                conn.execute(
                    """
                    INSERT INTO files(
                        batch_id, source_path, staging_path, target_path, bytes, mtime_ns, sha256
                    )
                    VALUES(?, ?, ?, ?, ?, ?, NULL)
                    """,
                    (
                        batch_id,
                        str(item.path),
                        str(staging),
                        item.target_path,
                        item.bytes,
                        item.mtime_ns,
                    ),
                )
        LOG.info("created batch %s for source %s with %d files", batch_id, source.id, len(files))

    def process_batch(self, batch_id: str) -> None:
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
            batch = self.load_batch(batch_id)
            target = self.target_by_name(str(batch["target_name"]))
            runner = self.target_runners[target.type]
            runner.advance(self, batch_id)
            self.finish_target_success(batch_id)
        except UnrecoverableJebError as exc:
            LOG.exception("batch %s has unrecoverable error", batch_id)
            self.mark_unrecoverable(batch_id, str(exc), component="target")
        except TransientJebError as exc:
            LOG.warning("batch %s hit transient issue; will retry: %s", batch_id, exc)
            self.set_batch_fields(batch_id, last_error=str(exc))

    def move_batch_files(self, batch_id: str) -> None:
        for row in self.batch_files(batch_id):
            if row["moved_at"]:
                continue
            source = Path(str(row["source_path"]))
            staging = Path(str(row["staging_path"]))
            if source.exists():
                staging.parent.mkdir(parents=True, exist_ok=True)
                if staging.exists():
                    if staging.is_dir():
                        raise UnrecoverableJebError(f"staging path is a directory: {staging}")
                    staging.unlink()
                shutil.move(str(source), str(staging))
            elif not staging.exists():
                raise UnrecoverableJebError(
                    f"source and staging file are both missing: {source} -> {staging}"
                )
            with self.connect() as conn:
                conn.execute(
                    "UPDATE files SET moved_at = ? WHERE batch_id = ? AND target_path = ?",
                    (iso(), batch_id, row["target_path"]),
                )
        self.set_batch_state(batch_id, "batched")

    def ensure_hashes(self, batch_id: str) -> None:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT target_path, staging_path FROM files WHERE batch_id = ? AND sha256 IS NULL",
                (batch_id,),
            ).fetchall()
        for row in rows:
            path = Path(str(row["staging_path"]))
            if not path.exists():
                raise UnrecoverableJebError(f"staged file disappeared before hashing: {path}")
            digest = file_sha256(path)
            with self.connect() as conn:
                conn.execute(
                    "UPDATE files SET sha256 = ? WHERE batch_id = ? AND target_path = ?",
                    (digest, batch_id, row["target_path"]),
                )
        self.set_batch_state(batch_id, "hashed")

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
        if (
            batch_payload.get("notified_error_fingerprint") == fingerprint
            and not self.notification_reminder_due(batch_payload)
        ):
            return True
        if not self.notifier.critical_batch_issue(
            batch=batch_payload,
            message=message,
            component=component,
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
        age = max(0.0, (now() - sent_at.astimezone(UTC)).total_seconds())
        return age >= self.config.notify.reminder_interval_seconds

    def mark_file_uploaded(self, batch_id: str, target_path: str) -> None:
        with self.connect() as conn:
            conn.execute(
                "UPDATE files SET uploaded_at = ? WHERE batch_id = ? AND target_path = ?",
                (iso(), batch_id, target_path),
            )


class MunchyTargetRunner:
    def advance(self, collector: Collector, batch_id: str) -> None:
        batch = collector.load_batch(batch_id)
        if batch["state"] == "target_complete":
            return
        target = collector.target_by_name(str(batch["target_name"]))
        source = collector.source_by_id(str(batch["source_id"]))
        client = MunchyRunnerClient(target.url)
        request = munchy_upload_request(collector, batch_id, source, target)

        state = str(batch["state"])
        if state == "hashed":
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


class RiverhogTargetRunner:
    def advance(self, collector: Collector, batch_id: str) -> None:
        batch = collector.load_batch(batch_id)
        if batch["state"] == "target_complete":
            return
        target = collector.target_by_name(str(batch["target_name"]))
        api = ApiClient(base_url=target.url, token=target.token)
        try:
            state = str(batch["state"])
            if state == "hashed":
                session = retry_forever(
                    collector,
                    "riverhog session setup",
                    lambda: api.create_or_resume_collection_upload_session(
                        str(batch["collection_slug"]),
                        ingest_source=target.ingest_source,
                        upload_timestamp=str(batch["collection_timestamp"]),
                    ),
                )
                collector.set_batch_fields(
                    batch_id,
                    state="riverhog_session_open",
                    riverhog_collection_id=str(session["collection_id"]),
                )
                state = "riverhog_session_open"
            batch = collector.load_batch(batch_id)
            collection_id = str(batch["riverhog_collection_id"] or "")
            if not collection_id:
                raise UnrecoverableJebError("riverhog collection id was not recorded")
            if state in {"riverhog_session_open", "riverhog_uploading"}:
                collector.set_batch_state(batch_id, "riverhog_uploading")
                self.upload_files(collector, api, target, batch_id, collection_id)
                collector.set_batch_state(batch_id, "riverhog_uploaded")
                state = "riverhog_uploaded"
            if state == "riverhog_uploaded":
                retry_forever(
                    collector,
                    "riverhog session complete",
                    lambda: api.complete_collection_upload_session(collection_id),
                )
                collector.set_batch_state(batch_id, "riverhog_completed")
                state = "riverhog_completed"
            if state == "riverhog_completed":
                final = self.wait_for_riverhog(collector, api, target, collection_id)
                if str(final.get("state") or "") == "failed":
                    raise UnrecoverableJebError(
                        f"riverhog upload failed: {final.get('latest_failure')}"
                    )
                collector.set_batch_state(batch_id, "target_complete")
        finally:
            api.close()

    def upload_files(
        self,
        collector: Collector,
        api: ApiClient,
        target: TargetConfig,
        batch_id: str,
        collection_id: str,
    ) -> None:
        files = collector.batch_files(batch_id)
        if target.upload_workers <= 1 or len(files) <= 1:
            for row in files:
                self.upload_one(collector, api, target, batch_id, collection_id, row)
            return

        stop_event = Event()
        next_lock = Lock()
        pending = iter(files)

        def worker() -> None:
            worker_api = ApiClient(base_url=target.url, token=target.token)
            try:
                while not stop_event.is_set():
                    with next_lock:
                        try:
                            row = next(pending)
                        except StopIteration:
                            return
                    self.upload_one(
                        collector,
                        worker_api,
                        target,
                        batch_id,
                        collection_id,
                        row,
                    )
            finally:
                worker_api.close()

        with ThreadPoolExecutor(max_workers=target.upload_workers) as executor:
            futures = [
                executor.submit(worker)
                for _ in range(min(target.upload_workers, len(files)))
            ]
            try:
                for future in as_completed(futures):
                    future.result()
            except Exception:
                stop_event.set()
                for future in futures:
                    future.cancel()
                raise

    def upload_one(
        self,
        collector: Collector,
        api: ApiClient,
        target: TargetConfig,
        batch_id: str,
        collection_id: str,
        row: sqlite3.Row,
    ) -> None:
        path = Path(str(row["staging_path"]))
        if not path.exists():
            raise UnrecoverableJebError(f"staged file disappeared before upload: {path}")
        file_payload = {
            "path": str(row["target_path"]),
            "bytes": int(row["bytes"]),
            "sha256": str(row["sha256"]),
        }
        while True:
            try:
                lease_payload = retry_forever(
                    collector,
                    f"riverhog file setup {row['target_path']}",
                    lambda: api.create_or_resume_registered_collection_file_upload(
                        collection_id,
                        file_payload,
                    ),
                )
                lease = TusUploadLease(
                    upload_url=str(lease_payload["upload_url"]),
                    offset=int(lease_payload.get("offset") or 0),
                    length=int(lease_payload.get("length") or row["bytes"]),
                    checksum_algorithm=str(lease_payload.get("checksum_algorithm") or "sha256"),
                )
                upload_path_to_tus(
                    client=api.tus_client(),
                    source_path=path,
                    lease=lease,
                    chunk_bytes=target.upload_chunk_bytes,
                )
                collector.mark_file_uploaded(batch_id, str(row["target_path"]))
                return
            except Exception as exc:
                if not is_transient_error(exc):
                    raise
                LOG.warning(
                    "transient riverhog upload issue for %s; retrying: %s",
                    row["target_path"],
                    exc,
                )
                collector.sleep(TRANSIENT_RETRY_INITIAL_SECONDS)

    def wait_for_riverhog(
        self,
        collector: Collector,
        api: ApiClient,
        target: TargetConfig,
        collection_id: str,
    ) -> dict[str, Any]:
        if target.wait == "staged":
            return cast(
                dict[str, Any],
                retry_forever(
                    collector,
                    "riverhog staged status",
                    lambda: api.get_collection_upload(collection_id),
                ),
            )
        while True:
            try:
                return api.get_collection(collection_id)
            except Exception as exc:
                if not is_transient_error(exc):
                    try:
                        upload = api.get_collection_upload(collection_id)
                    except Exception as upload_exc:
                        if not is_transient_error(upload_exc):
                            raise exc from upload_exc
                        raise TransientJebError(str(upload_exc)) from upload_exc
                    state = str(upload.get("state") or "")
                    if state == "failed":
                        return upload
                    LOG.info("riverhog collection %s waiting: state=%s", collection_id, state)
                collector.sleep(10.0)


def munchy_upload_request(
    collector: Collector,
    batch_id: str,
    source: SourceConfig,
    target: TargetConfig,
) -> RunnerUploadRequest:
    batch = collector.load_batch(batch_id)
    rows = collector.batch_files(batch_id)
    files = tuple(
        RunnerInputFile(
            source=Path(str(row["staging_path"])),
            rel_path=str(row["target_path"]),
            bytes=int(row["bytes"]),
            sha256=str(row["sha256"]),
        )
        for row in rows
    )
    groups = munchy_groups_payload(collector.config, source)
    job_payload = {
        "job_id": str(batch["job_id"]),
        "input_upload_id": str(batch["input_upload_id"]),
        "collection_slug": str(batch["collection_slug"]),
        "collection_timestamp": str(batch["collection_timestamp"]),
        "workflow_mode": collector.config.munchy_job_defaults.get("workflow_mode", "archive"),
        "groups": groups,
        "riverhog": dict(collector.config.munchy_job_defaults.get("riverhog") or {}),
        "review_upload": dict(collector.config.munchy_job_defaults.get("review_upload") or {}),
        "notify": dict(collector.config.munchy_job_defaults.get("notify") or {}),
        "cleanup_local_on_success": bool(
            collector.config.munchy_job_defaults.get("cleanup_local_on_success", False)
        ),
    }
    storage_hint = {
        "workflow_mode": job_payload["workflow_mode"],
        "groups": {
            name: {
                "archive_mode": str(group.get("archive_mode") or "av1_nvenc"),
                "gpu_tasks": list(group.get("gpu_tasks") or []),
            }
            for name, group in groups.items()
        },
    }
    return RunnerUploadRequest(
        upload_id=str(batch["input_upload_id"]),
        job_id=str(batch["job_id"]),
        files=files,
        storage_hint=storage_hint,
        job_payload=job_payload,
        upload_workers=target.upload_workers,
        upload_chunk_mib=max(1, target.upload_chunk_bytes // (1024 * 1024)),
    )


def munchy_groups_payload(config: JebConfig, source: SourceConfig) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for name, group in source.groups.items():
        payload: dict[str, Any] = {
            "archive_mode": group.archive_mode,
            "gpu_tasks": list(group.gpu_tasks),
        }
        if group.profile:
            profile = config.profiles.get(group.profile)
            if profile is None:
                raise UnrecoverableJebError(f"unknown Munchy profile {group.profile!r}")
            payload["encode_profile"] = copy.deepcopy(dict(profile))
        out[name] = payload
    return out


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


def load_config(path: Path) -> JebConfig:
    with path.open("rb") as handle:
        raw = tomllib.load(handle)
    return config_from_mapping(raw)


def config_from_mapping(raw: Mapping[str, Any]) -> JebConfig:
    collector_raw = mapping(raw.get("collector"))
    collector = CollectorSettings(
        interval_seconds=parse_duration(collector_raw.get("interval"), 300),
        state_db=Path(os.path.expandvars(str(collector_raw.get("state_db", "/state/jeb.sqlite3")))),
        batch_dir=Path(
            os.path.expandvars(str(collector_raw.get("batch_dir", "/landing/.jeb-batches")))
        ),
    )
    notify_raw = mapping(raw.get("notify"))
    notify_url = env_value(
        optional_str(notify_raw.get("url_env")) or "JEB_WEBHOOK_URL",
        optional_str(notify_raw.get("url")) or "",
    )
    notify = NotifySettings(
        enabled=bool(notify_raw.get("enabled", False)),
        url=notify_url or "",
        base_url=optional_str(notify_raw.get("base_url")) or "",
        recipients=tuple(str(item) for item in sequence(notify_raw.get("recipients"))),
        timeout_seconds=float(parse_duration(notify_raw.get("timeout"), 10)),
        reminder_interval_seconds=parse_duration(notify_raw.get("reminder_interval"), 86_400),
    )
    if notify.enabled and not notify.url:
        raise ValueError("notify.url or JEB_WEBHOOK_URL is required when notify.enabled=true")

    profiles = {
        str(name): dict(mapping(profile))
        for name, profile in mapping(raw.get("profiles")).items()
    }
    targets = load_targets(mapping(raw.get("targets")))
    sources = tuple(load_source(source, targets) for source in sequence(raw.get("sources")))
    if not sources:
        raise ValueError("at least one source is required")
    return JebConfig(
        collector=collector,
        notify=notify,
        targets=targets,
        sources=sources,
        profiles=profiles,
        munchy_job_defaults=mapping(raw.get("munchy_job_defaults")),
    )


def load_targets(raw_targets: Mapping[str, Any]) -> dict[str, TargetConfig]:
    if not raw_targets:
        raise ValueError("at least one target is required")
    out: dict[str, TargetConfig] = {}
    for name, raw_any in raw_targets.items():
        raw = mapping(raw_any)
        target_type = str(raw.get("type") or "").strip()
        if target_type not in {"munchy", "riverhog"}:
            raise ValueError(f"target {name} has unsupported type {target_type!r}")
        url = env_value(
            optional_str(raw.get("url_env")) or (
                "JEB_MUNCHY_URL" if target_type == "munchy" else "JEB_RIVERHOG_URL"
            ),
            optional_str(raw.get("url")) or optional_str(raw.get("base_url")),
        )
        if not url:
            raise ValueError(f"target {name} requires url")
        chunk_mib = int(raw.get("upload_chunk_mib") or 64)
        token = env_value(optional_str(raw.get("token_env")) or "RIVERHOG_TOKEN", None)
        wait = str(raw.get("wait") or "finalized")
        if wait not in {"staged", "finalized"}:
            raise ValueError(f"target {name} has invalid wait mode {wait!r}")
        out[str(name)] = TargetConfig(
            name=str(name),
            type=cast(Literal["munchy", "riverhog"], target_type),
            url=url.rstrip("/"),
            token=token if target_type == "riverhog" else None,
            upload_workers=max(1, int(raw.get("upload_workers") or 4)),
            upload_chunk_bytes=max(1, chunk_mib) * 1024 * 1024,
            wait=cast(Literal["staged", "finalized"], wait),
            wait_for_safe_delete=bool(raw.get("wait_for_safe_delete", True)),
            ingest_source=optional_str(raw.get("ingest_source")) or "jeb",
        )
    return out


def load_source(raw_any: Any, targets: Mapping[str, TargetConfig]) -> SourceConfig:
    raw = mapping(raw_any)
    source_id = str(raw["id"])
    if not SAFE_NAME.fullmatch(source_id):
        raise ValueError(f"invalid source id {source_id!r}")
    target_name = str(raw["target"])
    if target_name not in targets:
        raise ValueError(f"source {source_id} references unknown target {target_name!r}")
    groups = {
        str(name): load_source_group(group)
        for name, group in mapping(raw.get("groups")).items()
    }
    if not groups:
        raise ValueError(f"source {source_id} has no groups")
    for name in groups:
        if not SAFE_NAME.fullmatch(name):
            raise ValueError(f"source {source_id} has invalid group name {name!r}")
    root_group = optional_str(raw.get("root_group"))
    if root_group and root_group not in groups:
        raise ValueError(f"source {source_id} root_group is not configured")
    cleanup = str(raw.get("cleanup", "never"))
    if cleanup not in {"never", "after_target_success"}:
        raise ValueError(f"source {source_id} has invalid cleanup mode {cleanup!r}")
    target = targets[target_name]
    if cleanup == "after_target_success":
        if target.type == "riverhog" and target.wait != "finalized":
            raise ValueError(
                f"source {source_id} cannot cleanup after a non-finalized Riverhog target"
            )
        if target.type == "munchy" and not target.wait_for_safe_delete:
            raise ValueError(
                f"source {source_id} cannot cleanup until Munchy waits for safe delete"
            )
    return SourceConfig(
        id=source_id,
        enabled=bool(raw.get("enabled", True)),
        path=Path(os.path.expandvars(str(raw["path"]))),
        collection_slug=str(raw["collection_slug"]),
        target=target_name,
        threshold_bytes=parse_size(raw.get("threshold", "25GB")),
        stable_seconds=parse_duration(raw.get("stable_age"), 600),
        max_age_seconds=parse_duration(raw.get("max_age"), 0),
        root_group=root_group,
        cleanup=cast(Literal["never", "after_target_success"], cleanup),
        include_extensions=frozenset(
            str(item).lower() for item in sequence(raw.get("include_extensions"))
        ),
        groups=groups,
    )


def load_source_group(raw_any: Any) -> SourceGroup:
    raw = mapping(raw_any)
    tasks = raw.get("gpu_tasks")
    return SourceGroup(
        profile=optional_str(raw.get("profile")),
        archive_mode=str(raw.get("archive_mode") or "av1_nvenc"),
        gpu_tasks=(
            tuple(str(item) for item in sequence(tasks))
            if tasks is not None
            else DEFAULT_GPU_TASKS
        ),
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
