from __future__ import annotations

import sqlite3
from pathlib import Path

from lifecycle_events import (
    SQLiteEventCursorStore,
    SQLiteLifecycleEventLog,
    caused_event,
    cloud_event,
)


def sqlite_connect(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    return connection


def test_causal_events_and_sqlite_delivery_state_are_idempotent(tmp_path: Path) -> None:
    database = tmp_path / "events.sqlite3"

    def connect() -> sqlite3.Connection:
        return sqlite_connect(database)

    log = SQLiteLifecycleEventLog(connect)
    cursors = SQLiteEventCursorStore(connect)
    log.initialize()
    cursors.initialize()
    upstream = cloud_event(
        source="urn:riverhog",
        type="io.riverhog.riverhog.collection.finalized",
        subject="2026/example",
        data={"collection_id": "2026/example"},
    )
    translated = caused_event(
        cause=upstream,
        source="urn:munchy",
        type="io.riverhog.munchy.job.archive.finalized",
        subject="job-1",
        data={"job_id": "job-1"},
    )

    first_cursor = log.append_once(translated, owner="munchy")
    second_cursor = log.append_once(translated, owner="munchy")
    cursors.advance("riverhog", "41")

    assert first_cursor == second_cursor == 1
    assert cursors.cursor("riverhog") == "41"
    page = log.page(after=None, limit=100)
    assert page.events == [translated]
    assert page.events[0].data["cause"]["id"] == upstream.id
