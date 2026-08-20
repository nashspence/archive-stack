from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from lifecycle_events import (
    EventPage,
    SQLiteEventCursorStore,
    SQLiteLifecycleEventLog,
    caused_event,
    cloud_event,
)


def test_nonempty_event_pages_require_opaque_cursor_progress() -> None:
    event = cloud_event(source="urn:riverhog", type="io.riverhog.test")
    nonadvancing = EventPage(events=[event], next_cursor="cursor-7", has_more=False)
    terminal = EventPage(events=[], next_cursor="cursor-7", has_more=False)

    with pytest.raises(ValueError, match="did not advance"):
        nonadvancing.require_progress_after("cursor-7")
    terminal.require_progress_after("cursor-7")


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
        source="urn:target",
        type="io.riverhog.target.job.archive.finalized",
        subject="job-1",
        data={"job_id": "job-1"},
    )

    first_cursor = log.append_once(translated, owner="target")
    second_cursor = log.append_once(translated, owner="target")
    cursors.advance("riverhog", "41")

    assert first_cursor == second_cursor == 1
    assert cursors.cursor("riverhog") == "41"
    page = log.page(after=None, limit=100)
    assert page.events == [translated]
    assert page.events[0].data["cause"]["id"] == upstream.id


def test_context_expiry_is_scoped_to_owner_and_subject_in_sql(tmp_path: Path) -> None:
    database = tmp_path / "events.sqlite3"

    def connect() -> sqlite3.Connection:
        return sqlite_connect(database)

    log = SQLiteLifecycleEventLog(connect)
    log.initialize()
    matching = cloud_event(
        source="urn:riverhog",
        type="io.riverhog.riverhog.collection.finalized",
        subject="collection-1",
        data={"collection_id": 1},
    )
    other_subject = cloud_event(
        source="urn:riverhog",
        type="io.riverhog.riverhog.collection.finalized",
        subject="collection-2",
        data={"collection_id": 2},
    )
    log.append(matching, owner="client", context={"workflow": "matching"})
    log.append(other_subject, owner="client", context={"workflow": "other-subject"})
    log.append(
        matching.model_copy(update={"id": "other-owner"}),
        owner="other",
        context={"workflow": "other-owner"},
    )

    assert (
        log.expire_context(
            owner="client",
            subject="collection-1",
            expires_at="2026-08-02T00:00:00.000000Z",
        )
        == 1
    )
    with sqlite_connect(database) as connection:
        rows = connection.execute(
            "SELECT owner, context_expires_at FROM lifecycle_events ORDER BY sequence"
        ).fetchall()
        indexes = {
            str(row["name"]) for row in connection.execute("PRAGMA index_list('lifecycle_events')")
        }

    assert [tuple(row) for row in rows] == [
        ("client", "2026-08-02T00:00:00.000000Z"),
        ("client", None),
        ("other", None),
    ]
    assert "lifecycle_events_owner_subject_context" in indexes
