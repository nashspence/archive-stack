from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Callable
from contextlib import closing
from datetime import UTC, datetime

from lifecycle_events.models import CloudEvent, EventPage, normalize_event_context


def _now_text() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _cursor(value: str | None) -> int:
    raw = (value or "0").strip()
    try:
        cursor = int(raw)
    except ValueError as exc:
        raise ValueError("after must be a non-negative event cursor") from exc
    if cursor < 0:
        raise ValueError("after must be a non-negative event cursor")
    return cursor


class SQLiteLifecycleEventLog:
    def __init__(self, connect: Callable[[], sqlite3.Connection]) -> None:
        self._connect = connect

    def initialize(self) -> None:
        with closing(self._connect()) as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS lifecycle_events (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_id TEXT NOT NULL UNIQUE,
                    owner TEXT NOT NULL,
                    event_json TEXT NOT NULL,
                    context_json TEXT,
                    context_expires_at TEXT
                );
                CREATE INDEX IF NOT EXISTS lifecycle_events_owner_sequence
                ON lifecycle_events(owner, sequence);
                CREATE INDEX IF NOT EXISTS lifecycle_events_context_expiry
                ON lifecycle_events(context_expires_at)
                WHERE context_json IS NOT NULL;
                """
            )
            connection.commit()

    def append(
        self,
        event: CloudEvent,
        *,
        owner: str,
        context: dict[str, object] | None = None,
        context_expires_at: str | None = None,
    ) -> int:
        normalized_owner = owner.strip()
        if not normalized_owner:
            raise ValueError("event owner must not be blank")
        normalized_context = normalize_event_context(context)
        with closing(self._connect()) as connection:
            cursor = connection.execute(
                """
                INSERT INTO lifecycle_events(
                    event_id, owner, event_json, context_json, context_expires_at
                ) VALUES(?, ?, ?, ?, ?)
                """,
                (
                    event.id,
                    normalized_owner,
                    event.model_dump_json(exclude_none=True),
                    (
                        json.dumps(normalized_context, sort_keys=True, separators=(",", ":"))
                        if normalized_context is not None
                        else None
                    ),
                    context_expires_at,
                ),
            )
            connection.commit()
            if cursor.lastrowid is None:  # pragma: no cover - SQLite always supplies this
                raise RuntimeError("event cursor was not assigned")
            return int(cursor.lastrowid)

    def append_once(
        self,
        event: CloudEvent,
        *,
        owner: str,
        context: dict[str, object] | None = None,
        context_expires_at: str | None = None,
    ) -> int:
        normalized_owner = owner.strip()
        if not normalized_owner:
            raise ValueError("event owner must not be blank")
        normalized_context = normalize_event_context(context)
        with closing(self._connect()) as connection:
            connection.execute(
                """
                INSERT OR IGNORE INTO lifecycle_events(
                    event_id, owner, event_json, context_json, context_expires_at
                ) VALUES(?, ?, ?, ?, ?)
                """,
                (
                    event.id,
                    normalized_owner,
                    event.model_dump_json(exclude_none=True),
                    (
                        json.dumps(normalized_context, sort_keys=True, separators=(",", ":"))
                        if normalized_context is not None
                        else None
                    ),
                    context_expires_at,
                ),
            )
            row = connection.execute(
                "SELECT sequence FROM lifecycle_events WHERE event_id = ?",
                (event.id,),
            ).fetchone()
            connection.commit()
        if row is None:  # pragma: no cover - insert-or-existing guarantees a row
            raise RuntimeError("event cursor was not assigned")
        return int(row["sequence"])

    def expire_context(self, *, owner: str, subject: str, expires_at: str) -> int:
        updated = 0
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT sequence, event_json
                FROM lifecycle_events
                WHERE owner = ? AND context_json IS NOT NULL AND context_expires_at IS NULL
                """,
                (owner,),
            ).fetchall()
            for row in rows:
                event = CloudEvent.model_validate_json(str(row["event_json"]))
                if event.subject != subject:
                    continue
                connection.execute(
                    "UPDATE lifecycle_events SET context_expires_at = ? WHERE sequence = ?",
                    (expires_at, row["sequence"]),
                )
                updated += 1
            connection.commit()
        return updated

    def page(
        self,
        *,
        after: str | None,
        limit: int,
        owner: str | None = None,
    ) -> EventPage:
        cursor = _cursor(after)
        if limit < 1 or limit > 100:
            raise ValueError("limit must be between 1 and 100")
        with closing(self._connect()) as connection:
            self._purge_expired_context(connection)
            clauses = ["sequence > ?"]
            values: list[object] = [cursor]
            if owner is not None:
                clauses.append("owner = ?")
                values.append(owner)
            values.append(limit + 1)
            rows = connection.execute(
                """
                SELECT sequence, event_json, context_json
                FROM lifecycle_events
                WHERE """
                + " AND ".join(clauses)
                + " ORDER BY sequence ASC LIMIT ?",
                values,
            ).fetchall()
            connection.commit()

        has_more = len(rows) > limit
        selected = rows[:limit]
        events: list[CloudEvent] = []
        for row in selected:
            event = CloudEvent.model_validate_json(str(row["event_json"]))
            if row["context_json"] is not None:
                data = dict(event.data)
                data["context"] = json.loads(str(row["context_json"]))
                event = event.model_copy(update={"data": data})
            events.append(event)
        next_cursor = str(selected[-1]["sequence"] if selected else cursor)
        return EventPage(events=events, next_cursor=next_cursor, has_more=has_more)

    @staticmethod
    def _purge_expired_context(connection: sqlite3.Connection) -> None:
        connection.execute(
            """
            UPDATE lifecycle_events
            SET context_json = NULL, context_expires_at = NULL
            WHERE context_json IS NOT NULL AND context_expires_at <= ?
            """,
            (_now_text(),),
        )


class SQLiteEventCursorStore:
    def __init__(self, connect: Callable[[], sqlite3.Connection]) -> None:
        self._connect = connect
        self._lock = threading.Lock()

    def initialize(self) -> None:
        with closing(self._connect()) as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS lifecycle_event_cursors (
                    source TEXT PRIMARY KEY,
                    cursor TEXT NOT NULL
                )
                """
            )
            connection.commit()

    def cursor(self, source: str) -> str:
        with self._lock, closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT cursor FROM lifecycle_event_cursors WHERE source = ?",
                (source,),
            ).fetchone()
        return str(row[0]) if row is not None else "0"

    def advance(self, source: str, cursor: str) -> None:
        with self._lock, closing(self._connect()) as connection:
            connection.execute(
                """
                INSERT INTO lifecycle_event_cursors(source, cursor) VALUES(?, ?)
                ON CONFLICT(source) DO UPDATE SET cursor = excluded.cursor
                """,
                (source, cursor),
            )
            connection.commit()


__all__ = ["SQLiteEventCursorStore", "SQLiteLifecycleEventLog"]
