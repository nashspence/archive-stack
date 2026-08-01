from __future__ import annotations

import fcntl
import hashlib
import logging
import os
import shutil
import sqlite3
import threading
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any

import httpx
from media_preflight import (
    MediaPreflightFile,
    MediaPreflightReport,
    MediaPreflightResult,
    run_media_preflight,
)

import jeb_core.domain.models as domain_models
import jeb_core.persistence.sqlite_state as state_store
import jeb_core.services.events as event_service
import jeb_core.services.sources as source_service
from jeb_core.domain.models import (
    PREFLIGHT_MEDIA_EXTENSIONS,
    EligibleFile,
    PreflightJebError,
    TransientJebError,
    UnrecoverableJebError,
    event_timestamp,
    format_progress_bytes,
    run_id_for,
)
from jeb_core.domain.sources import SourceConfig
from jeb_core.ports.target import TargetAdapter, TargetContext
from jeb_core.services.files import (
    file_sha256,
    format_media_preflight_error,
    hardlink_stage_file,
    normalize_posix,
    run_safe_remux,
    unique_corrupt_path,
)

LOG = logging.getLogger("jeb")


class JebAttemptService:
    def __init__(
        self,
        config: domain_models.JebConfig,
        store: state_store.SQLiteJebStore,
        events: event_service.JebEventService,
        sources: source_service.JebSourceService,
        target_adapters: Mapping[str, TargetAdapter],
        clock: Callable[[], datetime],
        operation_lock: threading.RLock,
        target_context: Callable[[], TargetContext],
    ) -> None:
        self.config = config
        self.store = store
        self.events = events
        self.sources = sources
        self.target_adapters = dict(target_adapters)
        self.current_time = clock
        self.operation_lock = operation_lock
        self.target_context = target_context
        self.batch_dir = config.service.batch_dir

    def archive_now(
        self,
        *,
        source_id: str,
        process: bool = True,
    ) -> str | None:
        with self.operation_lock:
            try:
                source = self.sources.source_by_id(source_id)
            except KeyError as exc:
                raise UnrecoverableJebError(f"source {source_id!r} is not enrolled") from exc
            if not source.enabled:
                raise UnrecoverableJebError(f"source {source_id!r} is disabled")
            failed_attempt = self.store.latest_failed_attempt_for_source(source.id)
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
                        self.store.supersede_attempt(str(failed_attempt["id"]))
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
                source = self.sources.source_by_id(source_id)
            except KeyError as exc:
                raise UnrecoverableJebError(f"source {source_id!r} is not enrolled") from exc
            if not source.enabled:
                raise UnrecoverableJebError(f"source {source_id!r} is disabled")
            target = self.sources.target_by_name(source.target)
            base_payload: dict[str, Any] = {
                "source": source.id,
                "target_name": target.name,
                "cleanup": source.cleanup,
                "cadence": source.cadence,
                "threshold_bytes": source.threshold_bytes,
                "process": process,
                "dry_run": True,
            }

            failed_attempt = self.store.latest_failed_attempt_for_source(source.id)
            if failed_attempt is not None and self.failed_attempt_target_paths_match_current_config(
                failed_attempt,
                source,
            ):
                batch_id = str(failed_attempt["batch_id"])
                rows = self.store.attempt_files(batch_id)
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

            period = self.current_time()
            eligible_files = self.sources.eligible_files(source)
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

            accepted_files, preflight = self.sources.preflight_source_target_with_summary(
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
            run_id = run_id_for(period)
            target_submission_id = f"jeb-{source.id}-{run_id.lower()}-{digest}"
            return {
                **base_payload,
                "status": "would_process" if process else "would_stage",
                "mode": "discover",
                "batch_id": batch_id,
                "attempt_id": batch_id,
                "manifest_digest": digest,
                "run_id": run_id,
                "target_submission_id": target_submission_id,
                "file_count": len(accepted_files),
                "total_bytes": total,
                "target_preflight": preflight,
            }

    def discover_source(
        self,
        source: SourceConfig,
        *,
        force: bool = False,
        allow_preflight_retry: bool = False,
    ) -> str | None:
        if not force and source.cadence == "manual":
            return None
        period = self.current_time() if force else self.sources.source_period(source)
        before = None if force else period
        if not source.enabled:
            return None
        if not allow_preflight_retry and self.store.target_preflight_failure_active(source.id):
            LOG.info(
                "source %s has an active target preflight failure; skipping until operator retry",
                source.id,
            )
            return None
        files = self.sources.eligible_files(source, before=before)
        if files:
            accepted_files = self.sources.preflight_source_target(source, files)
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
            and self.store.batch_exists_for_period(source.id, period)
        ):
            return None
        return self.create_batch(
            source,
            files,
            period=period,
            batch_id=base_batch_id,
            digest=base_digest,
        )

    def failed_attempt_target_paths_match_current_config(
        self,
        failed_attempt: sqlite3.Row,
        source: SourceConfig,
    ) -> bool:
        with self.store.connect() as conn:
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

    def create_retry_attempt(self, failed_attempt_id: str) -> str:
        failed_attempt = self.store.load_attempt(failed_attempt_id)
        batch_id = str(failed_attempt["batch_id"])
        with self.store.connect() as conn:
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
                f"{str(failed_attempt['run_id']).lower()}-"
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
                    self.config.service.batch_dir
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
        shutil.rmtree(self.config.service.batch_dir / failed_attempt_id, ignore_errors=True)
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
        run_id = run_id_for(period)
        if batch_id is None or digest is None:
            batch_id, digest = self.batch_identity(source, files, period=period)
        target = self.sources.target_by_name(source.target)
        target_submission_id = f"jeb-{source.id}-{run_id.lower()}-{digest}"
        batch_root = self.config.service.batch_dir / batch_id / "input"
        created_at = event_timestamp()
        with self.store.connect() as conn:
            exists = conn.execute("SELECT 1 FROM batches WHERE id = ?", (batch_id,)).fetchone()
            if exists:
                return batch_id
            conn.execute(
                """
                INSERT INTO batches(
                    id, source_id, target_name, run_id, cleanup,
                    manifest_digest, created_at, updated_at
                )
                VALUES(?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    batch_id,
                    source.id,
                    target.name,
                    run_id,
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
        run_id = run_id_for(period)
        manifest = "\n".join(f"{item.target_path} {item.bytes} {item.mtime_ns}" for item in files)
        digest = hashlib.sha256(manifest.encode("utf-8")).hexdigest()[:12]
        return f"{run_id}__{source.id}__{digest}", digest

    def attempt_process_lock_path(self, attempt_id: str) -> Path:
        digest = hashlib.sha256(attempt_id.encode("utf-8")).hexdigest()[:16]
        return self.config.service.state_db.parent / "locks" / f"attempt-process-{digest}.lock"

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
        adapter: TargetAdapter | None = None
        try:
            attempt = self.store.load_attempt(attempt_id)
            state = str(attempt["state"])
            if state == "failed":
                self.events.emit_failed_attempt(attempt_id)
                return
            if state in {"cleanup_pending", "cleanup_failed"}:
                self.cleanup_attempt(attempt_id)
                return
            if state == "batching":
                self.stage_attempt_files(attempt_id)
            if self.store.load_attempt(attempt_id)["state"] == "batched":
                self.ensure_hashes(attempt_id)
            if self.store.load_attempt(attempt_id)["state"] == "hashed":
                self.ensure_media_preflight(attempt_id)
            adapter = self.target_adapters[str(attempt["target_name"])]
            adapter.advance(self.target_context(), attempt_id)
            self.finish_attempt_success(attempt_id)
        except PreflightJebError as exc:
            LOG.exception("attempt %s failed media preflight", attempt_id)
            self.mark_unrecoverable(attempt_id, str(exc), component="preflight")
        except UnrecoverableJebError as exc:
            LOG.exception("attempt %s has unrecoverable error", attempt_id)
            self.mark_unrecoverable(attempt_id, str(exc), component="target")
        except TransientJebError as exc:
            LOG.warning("attempt %s hit transient issue; will retry: %s", attempt_id, exc)
            self.store.set_attempt_fields(attempt_id, last_error=str(exc))
        except Exception as exc:
            if isinstance(exc, httpx.TransportError) or (
                adapter is not None and adapter.is_transient_error(exc)
            ):
                LOG.warning("attempt %s hit transient issue; will retry: %s", attempt_id, exc)
                self.store.set_attempt_fields(attempt_id, last_error=str(exc))
                return
            LOG.exception("attempt %s failed with unrecoverable target error", attempt_id)
            self.mark_unrecoverable(attempt_id, str(exc), component="target")

    def stage_attempt_files(self, attempt_id: str) -> None:
        for row in self.store.attempt_files(attempt_id):
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
            with self.store.connect() as conn:
                conn.execute(
                    """
                    UPDATE attempt_files
                    SET staged_at = ?
                    WHERE attempt_id = ? AND target_path = ?
                    """,
                    (event_timestamp(), attempt_id, row["target_path"]),
                )
        self.store.set_attempt_state(attempt_id, "batched")

    def ensure_hashes(self, batch_id: str) -> None:
        with self.store.connect() as conn:
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
            self.store.set_attempt_state(batch_id, "hashed")
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
            with self.store.connect() as conn:
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
        self.store.set_attempt_state(batch_id, "hashed")

    def ensure_media_preflight(self, batch_id: str) -> None:
        files = self.media_preflight_files(batch_id)
        if not files:
            self.store.set_attempt_state(batch_id, "preflighted")
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
        if not report.ok and self.config.service.preflight_repair == "safe_remux":
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
        self.store.set_attempt_state(batch_id, "preflighted")

    def media_preflight_files(self, batch_id: str) -> list[MediaPreflightFile]:
        return [
            MediaPreflightFile(
                source=Path(str(row["staging_path"])),
                label=str(row["target_path"]),
                bytes=int(row["bytes"]),
            )
            for row in self.store.attempt_files(batch_id)
            if Path(str(row["target_path"])).suffix.lower() in PREFLIGHT_MEDIA_EXTENSIONS
        ]

    def repair_media_preflight_failures(
        self,
        batch_id: str,
        report: MediaPreflightReport,
    ) -> tuple[MediaPreflightReport, list[str]]:
        rows = {str(row["target_path"]): row for row in self.store.attempt_files(batch_id)}
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
            for row in self.store.attempt_files(batch_id)
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
                ffmpeg_path=self.config.service.preflight_repair_ffmpeg,
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
                if self.config.service.preflight_repair_original == "keep_corrupt":
                    corrupt_dest = unique_corrupt_path(
                        self.config.service.preflight_repair_corrupt_dir
                        / PurePosixPath(target_path)
                    )
                    corrupt_dest.parent.mkdir(parents=True, exist_ok=True)
                    source.replace(corrupt_dest)
                else:
                    source.unlink()
            elif self.config.service.preflight_repair_original == "keep_corrupt":
                corrupt_dest = unique_corrupt_path(
                    self.config.service.preflight_repair_corrupt_dir / PurePosixPath(target_path)
                )
                corrupt_dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(input_path, corrupt_dest)
            source.parent.mkdir(parents=True, exist_ok=True)
            temp.replace(source)
            os.utime(source, ns=(source_stat.st_atime_ns, source_stat.st_mtime_ns))
            hardlink_stage_file(source, staging)
            stat = source.stat()
            digest = file_sha256(staging)
            with self.store.connect() as conn:
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
            self.config.service.preflight_repair_corrupt_dir / PurePosixPath(target_path)
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
        with self.store.connect() as conn:
            conn.execute(
                "DELETE FROM attempt_files WHERE attempt_id = ? AND target_path = ?",
                (batch_id, target_path),
            )
            conn.execute(
                "DELETE FROM files WHERE batch_id = ? AND target_path = ?",
                (row["batch_id"], target_path),
            )

    def finish_attempt_success(self, attempt_id: str) -> None:
        attempt = self.store.load_attempt(attempt_id)
        state = str(attempt["state"])
        if state == "target_succeeded":
            return
        if state != "target_complete":
            return
        if attempt["cleanup"] == "after_target_success":
            self.store.set_attempt_state(attempt_id, "cleanup_pending")
            self.cleanup_attempt(attempt_id)
            return
        self.store.set_attempt_state(attempt_id, "target_succeeded")

    def cleanup_attempt(self, attempt_id: str) -> None:
        try:
            for row in self.store.attempt_files(attempt_id):
                Path(str(row["input_path"])).unlink(missing_ok=True)
            shutil.rmtree(self.config.service.batch_dir / attempt_id)
        except FileNotFoundError:
            pass
        except Exception as exc:
            message = f"failed to delete completed attempt files: {exc}"
            self.store.set_attempt_state(attempt_id, "cleanup_failed", message)
            self.events.emit_cleanup_failed(attempt_id, message)
            return
        self.store.set_attempt_state(attempt_id, "cleanup_done")

    def mark_unrecoverable(self, attempt_id: str, message: str, *, component: str) -> None:
        self.store.set_attempt_state(attempt_id, "failed", message)
        self.events.emit_failed_attempt(attempt_id, component=component)
