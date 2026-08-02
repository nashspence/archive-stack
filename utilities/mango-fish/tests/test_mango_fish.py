from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
from lifecycle_events import CLOUDEVENTS_JSON_CONTENT_TYPE, cloud_event
from mango_fish.cli import main as mango_fish_main
from mango_fish.relay import MangoFish, MangoFishConfig, SourceConfig
from mango_fish.schema import upgrade_state


def test_mango_fish_state_commands_report_and_verify_the_current_revision(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("EVENT_TOKEN", "token")
    monkeypatch.setenv("HA_WEBHOOK", "https://ha.test/hook")
    config_path = tmp_path / "mango-fish.yaml"
    config_path.write_text(
        "state_path: " + str(tmp_path / "mango-fish.sqlite3") + "\n"
        "sources:\n"
        "  - name: jeb\n"
        "    events_url: https://jeb.test/v1/events\n"
        "    token_env: EVENT_TOKEN\n"
        "    webhook_url_env: HA_WEBHOOK\n",
        encoding="utf-8",
    )
    prefix = ["--config", str(config_path), "state"]

    assert mango_fish_main([*prefix, "status", "--json"]) == 0
    empty = json.loads(capsys.readouterr().out)
    assert mango_fish_main([*prefix, "upgrade", "--json"]) == 0
    upgraded = json.loads(capsys.readouterr().out)
    assert mango_fish_main([*prefix, "verify", "--json"]) == 0
    verified = json.loads(capsys.readouterr().out)

    assert empty["condition"] == "empty"
    assert upgraded["current_revision"] == "v1_0001"
    assert verified["condition"] == "current"


def test_mango_fish_advances_only_after_the_complete_page_is_delivered(
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

    config = MangoFishConfig(
        state_path=tmp_path / "mango-fish.sqlite3",
        sources=(
            SourceConfig(
                name="jeb",
                events_url="https://jeb.test/v1/events",
                token_env="EVENT_TOKEN",
                webhook_url_env="HA_WEBHOOK",
            ),
        ),
    )
    upgrade_state(config.state_path)
    relay = MangoFish(config)
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
