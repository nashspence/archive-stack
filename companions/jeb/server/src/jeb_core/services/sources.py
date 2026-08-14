from __future__ import annotations

import json
import logging
import os
import secrets
import shutil
import stat
import time
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath
from typing import Any, Literal, cast

from jeb_protocol import ATTEMPT_RESOLVED_STATES
from time_formats import format_utc_timestamp, utc_now

import jeb_core.domain.models as domain_models
import jeb_core.persistence.sqlite_state as state_store
import jeb_core.services.events as event_service
from jeb_core.domain.models import (
    SOURCE_PURGE_WARNING,
    SOURCE_REMOVAL_TTL,
    EligibleFile,
    TargetConfig,
    UnrecoverableJebError,
    event_timestamp,
    stable_json,
    target_preflight_error,
)
from jeb_core.domain.sources import Cadence, SourceConfig, SourceRegistryError
from jeb_core.persistence.source_registry import SourceRegistry
from jeb_core.ports.target import TargetAdapter, TargetContext
from jeb_core.services.files import (
    filesystem_listing,
    normalize_posix,
    source_removal_challenge,
    source_removal_expiry,
    source_removal_is_purge,
)

LOG = logging.getLogger("jeb")


class JebSourceService:
    def __init__(
        self,
        config: domain_models.JebConfig,
        store: state_store.SQLiteJebStore,
        events: event_service.JebEventService,
        source_registry: SourceRegistry,
        target_adapters: Mapping[str, TargetAdapter],
        clock: Callable[[], datetime],
        target_context: Callable[[], TargetContext],
        initialize: Callable[[], None],
        cancel_ingress_source: Callable[[str], None],
    ) -> None:
        self.config = config
        self.store = store
        self.events = events
        self.source_registry = source_registry
        self.target_adapters = dict(target_adapters)
        self.current_time = clock
        self.target_context = target_context
        self.initialize = initialize
        self.cancel_ingress_source = cancel_ingress_source
        self._preflight_failure_cursor: str | None = None

    def source_statuses(self, *, include_backlog: bool = True) -> list[dict[str, Any]]:
        failed_preflight_source_ids = self.store.failed_target_preflight_source_ids()
        statuses: list[dict[str, Any]] = []
        for source in self.source_registry.list():
            payload: dict[str, Any] = {
                "id": source.id,
                "enabled": source.enabled,
                "path": str(source.path),
                "path_exists": source.path.exists(),
                "stable_seconds": source.stable_seconds,
                "include_extensions": sorted(source.include_extensions),
                "target": source.target,
                "target_config": source.target_config,
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
        target_config: Mapping[str, Any],
        credential: str | None = None,
        enabled: bool = True,
        stable_seconds: int = 600,
        include_extensions: Sequence[str] = (),
        target: str = "munchy",
        threshold_bytes: int = 0,
        cleanup: Literal["never", "after_target_success"] = "after_target_success",
        cadence: Literal["weekly", "monthly", "seasonal", "manual"] = "weekly",
        weekday: int = 0,
        hour: int = 3,
        minute: int = 0,
    ) -> tuple[SourceConfig, str | None]:
        self.initialize()
        normalized_target_config = self._validate_source_target(
            target=target,
            target_config=target_config,
            cleanup=cleanup,
        )
        kwargs: dict[str, Any] = {
            "adapters": adapters,
            "target_config": normalized_target_config,
            "credential": credential,
            "enabled": enabled,
            "stable_seconds": stable_seconds,
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
        self.initialize()
        current = self.source_registry.get(source_id)
        target = str(changes.get("target", current.target))
        target_config = changes.get("target_config", current.target_config)
        if not isinstance(target_config, Mapping):
            raise SourceRegistryError("target_config must be an object")
        cleanup = str(changes.get("cleanup", current.cleanup))
        if cleanup not in {"never", "after_target_success"}:
            raise SourceRegistryError("cleanup must be never or after_target_success")
        normalized_target_config = self._validate_source_target(
            target=target,
            target_config=target_config,
            cleanup=cast(Literal["never", "after_target_success"], cleanup),
        )
        normalized_changes = dict(changes)
        normalized_changes["target_config"] = normalized_target_config
        return self.source_registry.update(source_id, normalized_changes)

    def _validate_source_target(
        self,
        *,
        target: str,
        target_config: Mapping[str, Any],
        cleanup: Literal["never", "after_target_success"],
    ) -> dict[str, Any]:
        configured = self.target_by_name(target)
        try:
            adapter = self.target_adapters[target]
        except KeyError as exc:
            raise SourceRegistryError(f"target has no source-option contract: {target}") from exc
        normalized = adapter.normalize_source_config(target_config)
        if cleanup == "after_target_success" and not configured.wait_for_safe_delete:
            raise SourceRegistryError(
                "cleanup=after_target_success requires target safe-delete waiting"
            )
        return normalized

    def source_removal_plan(
        self,
        source_id: str,
        *,
        purge: bool,
        expires_at: datetime | None = None,
    ) -> dict[str, Any]:
        self.initialize()
        source = self.source_registry.get(source_id)
        expires = (expires_at or (utc_now() + SOURCE_REMOVAL_TTL)).replace(microsecond=0)
        landing_files = filesystem_listing(source.path)
        with self.store.transaction() as conn:
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
            custody_rows = conn.execute(
                """
                SELECT DISTINCT f.custody_path
                FROM files f
                JOIN batches b ON b.id = f.batch_id
                WHERE b.source_id = ?
                ORDER BY f.custody_path
                """,
                (source.id,),
            ).fetchall()
        unresolved_attempts = [
            {
                "id": str(row["id"]),
                "state": str(row["state"]),
                "target": str(row["target_name"]),
            }
            for row in attempt_rows
            if str(row["state"]) not in ATTEMPT_RESOLVED_STATES
        ]
        custody_files = filesystem_listing(
            *(Path(str(row["custody_path"])) for row in custody_rows)
        )
        ingress_publications = []
        tus_bytes = 0
        for publication in self.store.pending_ingress_publications(source_id=source.id):
            staged_path = self.config.ingress.tus_staging_dir / str(publication["upload_id"])
            try:
                staged_bytes = staged_path.stat().st_size
            except FileNotFoundError:
                staged_bytes = 0
            ingress_publications.append(
                {"id": str(publication["upload_id"]), "bytes": staged_bytes}
            )
            tus_bytes += staged_bytes
        managed_file_count = (
            landing_files["file_count"] + custody_files["file_count"] + len(ingress_publications)
        )
        managed_bytes = landing_files["bytes"] + custody_files["bytes"] + tus_bytes
        blockers: list[str] = []
        if not purge:
            if managed_file_count:
                blockers.append(
                    f"source has {managed_file_count} Jeb-managed file(s); request a purge plan"
                )
            if unresolved_attempts:
                blockers.append(
                    f"source has {len(unresolved_attempts)} unresolved delivery attempt(s); "
                    "request a purge plan"
                )
        elif unresolved_attempts:
            unsupported = sorted(
                {
                    attempt["target"]
                    for attempt in unresolved_attempts
                    if not callable(
                        getattr(self.target_adapters.get(str(attempt["target"])), "cancel", None)
                    )
                }
            )
            if unsupported:
                blockers.append(
                    "unresolved delivery cancellation is unsupported for target(s): "
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
            "custody_files": custody_files,
            "ingress_publications": ingress_publications,
            "unresolved_attempts": unresolved_attempts,
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
        self.initialize()
        with self.store.transaction() as conn:
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
            with self.store.transaction() as conn:
                conn.execute(
                    """
                    INSERT INTO source_removals(
                        source_id, challenge, plan_json, status, phase, started_at
                    ) VALUES(?, ?, ?, 'removing', 'quiesce', ?)
                    """,
                    (
                        source_id,
                        supplied,
                        stable_json(plan),
                        event_timestamp(),
                    ),
                )
            self.source_registry.write_ftp_projection()
        try:
            self._apply_source_removal(plan, challenge=supplied)
        except Exception as exc:
            diagnostic = f"{exc.__class__.__name__}: {exc}"[:400]
            with self.store.transaction() as conn:
                conn.execute(
                    "UPDATE source_removals SET last_error = ? WHERE challenge = ?",
                    (diagnostic, supplied),
                )
            raise
        result = {
            "status": "removed",
            "source": source_id,
            "purged": bool(plan["purge"]),
            "files": int(plan["managed_file_count"]),
            "bytes": int(plan["managed_bytes"]),
        }
        with self.store.transaction() as conn:
            conn.execute(
                """
                UPDATE source_removals
                SET status = 'complete', phase = 'complete', last_error = NULL,
                    plan_json = ?, completed_at = ?
                WHERE source_id = ? AND challenge = ?
                """,
                (stable_json(result), event_timestamp(), source_id, supplied),
            )
        return result

    def _apply_source_removal(self, plan: Mapping[str, Any], *, challenge: str) -> None:
        source_id = str(plan["source"])
        phases = (
            "quiesce",
            "cancel_attempts",
            "cancel_ingress",
            "delete_landing",
            "delete_custody",
            "delete_state",
            "delete_source",
            "verify",
        )
        with self.store.transaction() as conn:
            row = conn.execute(
                "SELECT phase FROM source_removals WHERE challenge = ?",
                (challenge,),
            ).fetchone()
        if row is None:
            raise UnrecoverableJebError("source removal state disappeared")
        phase = str(row["phase"])
        try:
            start = phases.index(phase)
        except ValueError as exc:
            raise UnrecoverableJebError("source removal phase is invalid") from exc
        for index in range(start, len(phases)):
            current = phases[index]
            if current == "quiesce":
                self.source_registry.write_ftp_projection()
            elif current == "cancel_attempts" and bool(plan["purge"]):
                with self.store.transaction() as conn:
                    attempts = conn.execute(
                        """
                        SELECT a.id, a.state, b.target_name AS target
                        FROM batch_attempts a JOIN batches b ON b.id = a.batch_id
                        WHERE b.source_id = ?
                        ORDER BY a.id
                        """,
                        (source_id,),
                    ).fetchall()
                for attempt in attempts:
                    if str(attempt["state"]) in ATTEMPT_RESOLVED_STATES:
                        continue
                    adapter = self.target_adapters[str(attempt["target"])]
                    adapter.cancel(self.target_context(), str(attempt["id"]))
            elif current == "cancel_ingress" and bool(plan["purge"]):
                self.cancel_ingress_source(source_id)
            elif current == "delete_landing":
                landing_root = self.source_registry.source_roots.root(source_id)
                if landing_root.exists():
                    if bool(plan["purge"]):
                        shutil.rmtree(landing_root)
                    else:
                        landing_root.rmdir()
                if landing_root.exists():
                    raise UnrecoverableJebError("source landing root remains after removal")
            elif current == "delete_custody":
                with self.store.transaction() as conn:
                    batch_rows = conn.execute(
                        "SELECT id FROM batches WHERE source_id = ? ORDER BY id",
                        (source_id,),
                    ).fetchall()
                for batch in batch_rows:
                    batch_root = self.config.service.batch_dir / str(batch["id"])
                    if batch_root.exists():
                        shutil.rmtree(batch_root)
                    if batch_root.exists():
                        raise UnrecoverableJebError("batch custody remains after removal")
            elif current == "delete_state":
                self._delete_source_state(source_id)
            elif current == "delete_source":
                try:
                    self.source_registry.get(source_id)
                except SourceRegistryError:
                    pass
                else:
                    self.source_registry.delete(source_id)
            elif current == "verify":
                self._verify_source_absent(source_id)
            next_phase = phases[index + 1] if index + 1 < len(phases) else "verify"
            with self.store.transaction() as conn:
                conn.execute(
                    "UPDATE source_removals SET phase = ?, last_error = NULL WHERE challenge = ?",
                    (next_phase, challenge),
                )

    def _delete_source_state(self, source_id: str) -> None:
        with self.store.transaction() as conn:
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

    def _verify_source_absent(self, source_id: str) -> None:
        source_root = self.source_registry.source_roots.root(source_id)
        with self.store.transaction() as conn:
            counts = {
                "batches": int(
                    conn.execute(
                        "SELECT COUNT(*) FROM batches WHERE source_id = ?", (source_id,)
                    ).fetchone()[0]
                ),
                "attempts": int(
                    conn.execute(
                        """
                        SELECT COUNT(*) FROM batch_attempts a
                        JOIN batches b ON b.id = a.batch_id WHERE b.source_id = ?
                        """,
                        (source_id,),
                    ).fetchone()[0]
                ),
                "pending_ingress": int(
                    conn.execute(
                        "SELECT COUNT(*) FROM ingress_publications "
                        "WHERE source_id = ? AND status = 'pending'",
                        (source_id,),
                    ).fetchone()[0]
                ),
            }
        try:
            self.source_registry.get(source_id)
        except SourceRegistryError:
            enrolled = False
        else:
            enrolled = True
        if source_root.exists() or enrolled or any(counts.values()):
            raise UnrecoverableJebError("source removal verification found retained state")

    def target_by_name(self, target_name: str) -> TargetConfig:
        try:
            return self.config.targets[target_name]
        except KeyError as exc:
            raise UnrecoverableJebError(f"unknown target {target_name!r}") from exc

    def source_period(self, source: SourceConfig) -> datetime:
        current = self.current_time()
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
        pending_publications = self.store.pending_ingress_paths(source.id)
        for path in sorted(source.path.rglob("*")):
            try:
                observed = os.lstat(path)
            except FileNotFoundError:
                continue
            if not stat.S_ISREG(observed.st_mode):
                continue
            rel = path.relative_to(source.path)
            if rel.as_posix() in pending_publications:
                continue
            if any(part.startswith(".") for part in rel.parts):
                continue
            if source.include_extensions and path.suffix.lower() not in source.include_extensions:
                continue
            if observed.st_mtime > cutoff:
                continue
            if before_ts is not None and observed.st_mtime >= before_ts:
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
                    bytes=observed.st_size,
                    mtime=observed.st_mtime,
                    mtime_ns=observed.st_mtime_ns,
                    device=observed.st_dev,
                    inode=observed.st_ino,
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
        try:
            adapter = self.target_adapters[source.target]
        except KeyError as exc:
            raise UnrecoverableJebError(
                f"target has no preflight contract: {source.target}"
            ) from exc
        return adapter.preflight(
            self.target_context(),
            source,
            files,
            record_failures=record_failures,
        )

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
            "target_config": source.target_config,
            "error": error_text[:500],
            "error_type": error.__class__.__name__,
            "status": status,
        }
        self.store.store_target_preflight_failure(
            source=source,
            files=files,
            failure_payload=failure_payload,
            fingerprint_payload=fingerprint_payload,
            message=target_preflight_error(source_id=source.id, error=error),
        )

    def resolve_inactive_target_preflight_failures(self) -> int:
        now_text = event_timestamp()
        with self.store.transaction() as conn:
            cursor = conn.execute(
                """
                UPDATE target_preflight_failures
                SET state = 'resolved', resolved_at = ?, updated_at = ?
                WHERE state = 'failed'
                  AND NOT EXISTS (
                      SELECT 1 FROM sources
                      WHERE sources.id = target_preflight_failures.source_id
                        AND sources.enabled = 1
                  )
                """,
                (now_text, now_text),
            )
        resolved = cursor.rowcount if cursor.rowcount is not None else 0
        if resolved:
            LOG.info("resolved %s inactive target preflight failure(s)", resolved)
        return resolved

    def emit_target_preflight_failures(self, source_id: str | None = None) -> None:
        self.resolve_inactive_target_preflight_failures()
        if source_id is not None:
            rows = self.store.target_preflight_failures(
                source_id=source_id,
                state="failed",
            )
        else:
            rows = self.store.target_preflight_failures(
                after_source_id=self._preflight_failure_cursor,
                state="failed",
            )
            if not rows and self._preflight_failure_cursor is not None:
                rows = self.store.target_preflight_failures(state="failed")
            self._preflight_failure_cursor = str(rows[-1]["source_id"]) if rows else None
        for row in rows:
            self.events.emit_target_preflight_failure(row)
