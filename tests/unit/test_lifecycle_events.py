from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import httpx
import pytest

from lifecycle_events import (
    CLOUDEVENTS_JSON_CONTENT_TYPE,
    SQLiteEventCursorStore,
    SQLiteLifecycleEventLog,
    caused_event,
    cloud_event,
)
from lifecycle_events.relay import LifecycleEventRelay, RelayConfig, RelaySourceConfig


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


def test_relay_advances_only_after_the_complete_page_is_delivered(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("EVENT_TOKEN", "token")
    monkeypatch.setenv("HA_WEBHOOK", "https://ha.test/hook")
    events = [
        cloud_event(source="urn:jeb", type="io.riverhog.jeb.attempt.issue", subject="a"),
        cloud_event(source="urn:jeb", type="io.riverhog.jeb.attempt.issue", subject="b"),
    ]
    deliveries: list[str] = []
    fail_second = True

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal fail_second
        if request.url.host == "jeb.test":
            return httpx.Response(
                200,
                json={
                    "events": [event.model_dump(mode="json") for event in events],
                    "next_cursor": "2",
                    "has_more": False,
                },
            )
        assert request.headers["content-type"] == CLOUDEVENTS_JSON_CONTENT_TYPE
        event_id = str(json.loads(request.content)["id"])
        deliveries.append(event_id)
        if fail_second and event_id == events[1].id:
            fail_second = False
            return httpx.Response(503)
        return httpx.Response(200)

    config = RelayConfig(
        state_path=tmp_path / "relay.sqlite3",
        sources=(
            RelaySourceConfig(
                name="jeb",
                events_url="https://jeb.test/v1/events",
                token_env="EVENT_TOKEN",
                webhook_url_env="HA_WEBHOOK",
            ),
        ),
    )
    relay = LifecycleEventRelay(config)
    client = httpx.Client(transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(httpx.HTTPStatusError):
            relay.relay_source_once(config.sources[0], client=client)
        assert relay.state.cursor("jeb") == "0"

        assert relay.relay_source_once(config.sources[0], client=client) == 2
        assert relay.state.cursor("jeb") == "2"
    finally:
        client.close()

    assert deliveries == [events[0].id, events[1].id, events[0].id, events[1].id]
