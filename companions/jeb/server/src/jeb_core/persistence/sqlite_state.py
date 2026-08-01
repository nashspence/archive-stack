from __future__ import annotations

import hashlib
import sqlite3
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any, Literal, cast

from jeb_protocol import ATTEMPT_LIST_SORT_FIELDS, ATTEMPT_RESOLVED_STATES, MAX_LIST_PAGE_SIZE

import jeb_core.domain.models as domain_models
from jeb_core.domain.models import (
    EligibleFile,
    event_timestamp,
    run_id_for,
    stable_json,
)
from jeb_core.domain.sources import SourceConfig
from jeb_core.persistence.sql import like_literal


class SQLiteJebStore:
    def __init__(self, config: domain_models.JebConfig) -> None:
        self.config = config

    def initialize(self) -> None:
        self.config.service.batch_dir.mkdir(parents=True, exist_ok=True)
        with self.connect() as conn:
            self.create_batch_schema(conn)
            self.ensure_target_preflight_schema(conn)
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

    def connect(self) -> sqlite3.Connection:
        self.config.service.state_db.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.config.service.state_db, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=30000")
        return conn

    def create_batch_schema(self, conn: sqlite3.Connection) -> None:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS batches (
                id TEXT PRIMARY KEY,
                source_id TEXT NOT NULL,
                target_name TEXT NOT NULL,
                run_id TEXT NOT NULL,
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
            "CREATE INDEX IF NOT EXISTS idx_jeb_batches_source_period ON batches(source_id, run_id)"
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

    def unresolved_attempts(self) -> list[sqlite3.Row]:
        resolved = tuple(sorted(ATTEMPT_RESOLVED_STATES))
        placeholders = ", ".join("?" for _ in resolved)
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
                    b.run_id,
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
                resolved,
            ).fetchall()

    def unresolved_attempt_ids(self) -> list[str]:
        return [str(row["id"]) for row in self.unresolved_attempts()]

    def list_attempts(
        self,
        *,
        page: int = 1,
        per_page: int = 25,
        sort: str = "updated_at",
        order: str = "desc",
        query: str | None = None,
        resolution: Literal["unresolved", "resolved", "all"] = "unresolved",
        state: str | None = None,
        states: Sequence[str] | None = None,
        source: str | None = None,
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
        if resolution not in {"unresolved", "resolved", "all"}:
            raise ValueError("resolution must be unresolved, resolved, or all")
        if state is not None and states is not None:
            raise ValueError("state and states are mutually exclusive")

        clauses: list[str] = []
        values: list[object] = []
        if resolution != "all":
            resolved_placeholders = ", ".join("?" for _ in ATTEMPT_RESOLVED_STATES)
            resolved_values = tuple(sorted(ATTEMPT_RESOLVED_STATES))
            if resolution == "resolved":
                clauses.append(f"a.state IN ({resolved_placeholders})")
            else:
                clauses.append(f"a.state NOT IN ({resolved_placeholders})")
            values.extend(resolved_values)
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
        if target:
            clauses.append("b.target_name = ?")
            values.append(target)
        if query:
            like = f"%{like_literal(query)}%"
            clauses.append(
                """
                (
                    a.id LIKE ? ESCAPE '\\'
                    OR a.batch_id LIKE ? ESCAPE '\\'
                    OR a.state LIKE ? ESCAPE '\\'
                    OR a.target_submission_id LIKE ? ESCAPE '\\'
                    OR b.source_id LIKE ? ESCAPE '\\'
                    OR b.target_name LIKE ? ESCAPE '\\'
                    OR b.run_id LIKE ? ESCAPE '\\'
                    OR a.last_error LIKE ? ESCAPE '\\'
                )
                """
            )
            values.extend((like,) * 8)

        where = "WHERE " + " AND ".join(clauses) if clauses else ""
        attempts_sql = f"""
            SELECT
                a.id,
                a.batch_id,
                a.attempt_number,
                a.state,
                b.source_id,
                b.target_name,
                b.run_id,
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
            "run_id": "b.run_id",
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
            "resolution": resolution,
            "query": query,
            "filters": {
                "source": source,
                "state": state,
                "states": list(states) if states is not None else None,
                "target": target,
            },
            "attempts": attempts,
        }

    def get_attempt(self, attempt_id: str) -> dict[str, Any]:
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
                    b.run_id,
                    b.cleanup,
                    b.manifest_digest,
                    a.target_submission_id,
                    a.created_at,
                    a.updated_at,
                    a.last_error,
                    a.emitted_error_at,
                    b.file_count,
                    b.total_bytes,
                    COALESCE(
                        SUM(CASE WHEN af.staged_at IS NOT NULL THEN 1 ELSE 0 END),
                        0
                    ) AS staged_file_count
                FROM batch_attempts AS a
                JOIN batches AS b ON b.id = a.batch_id
                LEFT JOIN attempt_files AS af ON af.attempt_id = a.id
                WHERE a.id = ?
                GROUP BY
                    a.id,
                    a.batch_id,
                    a.attempt_number,
                    a.state,
                    b.source_id,
                    b.target_name,
                    b.run_id,
                    b.cleanup,
                    b.manifest_digest,
                    a.target_submission_id,
                    a.created_at,
                    a.updated_at,
                    a.last_error,
                    a.emitted_error_at,
                    b.file_count,
                    b.total_bytes
                """,
                (attempt_id,),
            ).fetchone()
        if row is None:
            raise KeyError(attempt_id)
        return self._attempt_summary(row)

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

    def _attempt_summary(self, row: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "attempt_id": str(row["id"]),
            "batch_id": str(row["batch_id"]),
            "attempt_number": int(row["attempt_number"]),
            "state": str(row["state"]),
            "source_id": str(row["source_id"]),
            "target_name": str(row["target_name"]),
            "run_id": str(row["run_id"]),
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
            "target_name": str(row["target_name"]),
            "file_count": int(row["file_count"]),
            "total_bytes": int(row["total_bytes"]),
            "first_seen_at": str(row["first_seen_at"]),
            "last_seen_at": str(row["last_seen_at"]),
            "updated_at": str(row["updated_at"]),
            "message": str(row["message"]),
        }

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
                    b.run_id,
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
                  AND (state != 'canceled' OR ? = 'canceled')
                """,
                (state, event_timestamp(), error, attempt_id, state),
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
                f"UPDATE batch_attempts SET {', '.join(assignments)} "
                "WHERE id = ? AND state != 'canceled'",
                values,
            )

    def batch_exists_for_period(
        self,
        source_id: str,
        period: datetime,
    ) -> bool:
        run_id = run_id_for(period)
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT a.id, a.state
                FROM batches b
                JOIN batch_attempts a ON a.batch_id = b.id
                WHERE b.source_id = ? AND b.run_id = ?
                """,
                (source_id, run_id),
            ).fetchall()
        return any(str(row["state"]) != "superseded" for row in rows)

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
                    source_id, state, target_name,
                    input_paths_json, failure_json, fingerprint, message,
                    file_count, total_bytes, first_seen_at,
                    last_seen_at, updated_at, resolved_at,
                    emitted_error_fingerprint, emitted_error_at
                )
                VALUES(?, 'failed', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?)
                ON CONFLICT(source_id) DO UPDATE SET
                    state = 'failed',
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

    def latest_retryable_attempt_for_source(self, source_id: str) -> sqlite3.Row | None:
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT a.*
                FROM batch_attempts a
                JOIN batches b ON b.id = a.batch_id
                WHERE b.source_id = ?
                  AND a.state IN ('failed', 'canceled')
                ORDER BY a.created_at DESC, a.id DESC
                LIMIT 1
                """,
                (source_id,),
            ).fetchone()
        return cast(sqlite3.Row | None, row)

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
