from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import httpx
import mango_fish.relay as relay_module
import pytest
from lifecycle_events import CLOUDEVENTS_JSON_CONTENT_TYPE, cloud_event
from mango_fish.cli import main as mango_fish_main
from mango_fish.relay import (
    CursorState,
    MangoFish,
    MangoFishConfig,
    SourceConfig,
    SourceRelayError,
    load_config,
)
from mango_fish.schema import upgrade_state


def _write_config(
    config_path: Path,
    *,
    state_path: str,
    sources: tuple[tuple[str, str, str, str], ...],
) -> None:
    lines = [f"state_path: {state_path}", "sources:"]
    for name, events_url, token_env, webhook_url_env in sources:
        lines.extend(
            (
                f"  - name: {name}",
                f"    events_url: {events_url}",
                f"    token_env: {token_env}",
                f"    webhook_url_env: {webhook_url_env}",
            )
        )
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _use_mock_transport(
    monkeypatch: pytest.MonkeyPatch,
    handler: Any,
) -> None:
    client_type = httpx.Client

    def client_factory(*args: Any, **kwargs: Any) -> httpx.Client:
        kwargs["transport"] = httpx.MockTransport(handler)
        return client_type(*args, **kwargs)

    monkeypatch.setattr(relay_module.httpx, "Client", client_factory)


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
        "  - name: stove0\n"
        "    events_url: https://stove0.test/v1/events\n"
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


def test_relative_state_path_is_resolved_from_config_for_every_command(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("EVENT_TOKEN", "token")
    monkeypatch.setenv("HA_WEBHOOK", "https://ha.test/hook")
    config_path = tmp_path / "configuration" / "mango-fish.yaml"
    _write_config(
        config_path,
        state_path="state/mango-fish.sqlite3",
        sources=(("stove0", "https://stove0.test/v1/events", "EVENT_TOKEN", "HA_WEBHOOK"),),
    )
    expected_state = config_path.parent / "state" / "mango-fish.sqlite3"

    config = load_config(config_path)
    assert config.state_path == expected_state
    assert mango_fish_main(["--config", str(config_path), "--check"]) == 0
    assert json.loads(capsys.readouterr().out)["state_path"] == str(expected_state)

    prefix = ["--config", str(config_path), "state"]
    assert mango_fish_main([*prefix, "status", "--json"]) == 0
    capsys.readouterr()
    assert mango_fish_main([*prefix, "upgrade", "--json"]) == 0
    capsys.readouterr()
    assert mango_fish_main([*prefix, "verify", "--json"]) == 0
    capsys.readouterr()

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["after"] == "0"
        return httpx.Response(200, json={"events": [], "next_cursor": "0", "has_more": False})

    _use_mock_transport(monkeypatch, handler)
    assert mango_fish_main(["--config", str(config_path), "--once"]) == 0


def test_one_shot_partial_failure_is_visible_and_does_not_block_other_sources(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "mango-fish.yaml"
    state_path = tmp_path / "mango-fish.sqlite3"
    _write_config(
        config_path,
        state_path=str(state_path),
        sources=(
            ("alpha", "https://alpha.test/v1/events", "ALPHA_TOKEN", "ALPHA_WEBHOOK"),
            ("beta", "https://beta.test/v1/events", "BETA_TOKEN", "BETA_WEBHOOK"),
        ),
    )
    monkeypatch.setenv("ALPHA_TOKEN", "alpha-token")
    monkeypatch.setenv(
        "ALPHA_WEBHOOK", "https://alpha-hook.test/hooks/super-secret?token=credential"
    )
    monkeypatch.setenv("BETA_TOKEN", "beta-token")
    monkeypatch.setenv("BETA_WEBHOOK", "https://beta-hook.test/hook")
    upgrade_state(state_path)
    event = cloud_event(source="urn:test", type="io.riverhog.test")
    fetched: list[str] = []
    delivered: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            fetched.append(str(request.url.host))
            return httpx.Response(
                200,
                json={
                    "events": [event.model_dump(mode="json")],
                    "next_cursor": "1",
                    "has_more": False,
                },
            )
        delivered.append(str(request.url.host))
        return httpx.Response(503 if request.url.host == "alpha-hook.test" else 204)

    _use_mock_transport(monkeypatch, handler)
    caplog.set_level(logging.INFO)
    caplog.set_level(logging.ERROR, logger=relay_module.__name__)

    assert mango_fish_main(["--config", str(config_path), "--once"]) == 1
    state = CursorState(state_path)
    assert fetched == ["alpha.test", "beta.test"]
    assert delivered == ["alpha-hook.test", "beta-hook.test"]
    assert state.cursor("alpha") == "0"
    assert state.cursor("beta") == "1"
    log_text = caplog.text
    assert "source=alpha phase=delivery error=HTTPStatusError status=503" in log_text
    assert "super-secret" not in log_text
    assert "credential" not in log_text
    assert not any(record.name == "httpx" for record in caplog.records)


def test_one_shot_complete_failure_attempts_every_source(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "mango-fish.yaml"
    state_path = tmp_path / "mango-fish.sqlite3"
    _write_config(
        config_path,
        state_path=str(state_path),
        sources=(
            ("alpha", "https://alpha.test/v1/events", "ALPHA_TOKEN", "ALPHA_WEBHOOK"),
            ("beta", "https://beta.test/v1/events", "BETA_TOKEN", "BETA_WEBHOOK"),
        ),
    )
    for name in ("ALPHA", "BETA"):
        monkeypatch.setenv(f"{name}_TOKEN", "token")
        monkeypatch.setenv(f"{name}_WEBHOOK", "https://hook.test/path")
    upgrade_state(state_path)
    fetched: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        fetched.append(str(request.url.host))
        return httpx.Response(503)

    _use_mock_transport(monkeypatch, handler)

    assert mango_fish_main(["--config", str(config_path), "--once"]) == 1
    assert fetched == ["alpha.test", "beta.test"]
    state = CursorState(state_path)
    assert state.cursor("alpha") == "0"
    assert state.cursor("beta") == "0"


def test_one_shot_empty_pages_succeed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "mango-fish.yaml"
    state_path = tmp_path / "mango-fish.sqlite3"
    _write_config(
        config_path,
        state_path=str(state_path),
        sources=(("stove0", "https://stove0.test/v1/events", "EVENT_TOKEN", "HA_WEBHOOK"),),
    )
    monkeypatch.setenv("EVENT_TOKEN", "token")
    monkeypatch.setenv("HA_WEBHOOK", "https://ha.test/hook")
    upgrade_state(state_path)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"events": [], "next_cursor": "0", "has_more": False})

    _use_mock_transport(monkeypatch, handler)
    assert mango_fish_main(["--config", str(config_path), "--once"]) == 0


def test_nonadvancing_page_is_rejected_before_webhook_delivery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("EVENT_TOKEN", "token")
    monkeypatch.setenv("HA_WEBHOOK", "https://ha.test/hook")
    event = cloud_event(source="urn:stove0", type="io.riverhog.stove0.attempt.issue")
    config = MangoFishConfig(
        state_path=tmp_path / "mango-fish.sqlite3",
        sources=(
            SourceConfig(
                name="stove0",
                events_url="https://stove0.test/v1/events",
                token_env="EVENT_TOKEN",
                webhook_url_env="HA_WEBHOOK",
            ),
        ),
    )
    upgrade_state(config.state_path)

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        return httpx.Response(
            200,
            json={
                "events": [event.model_dump(mode="json")],
                "next_cursor": "0",
                "has_more": False,
            },
        )

    relay = MangoFish(config)
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(SourceRelayError, match="fetch failed") as caught:
            relay.relay_source_once(config.sources[0], client=client)

    assert caught.value.error_type == "ValueError"
    assert relay.state.cursor("stove0") == "0"


def test_mango_fish_advances_only_after_the_complete_page_is_delivered(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("EVENT_TOKEN", "token")
    monkeypatch.setenv("HA_WEBHOOK", "https://ha.test/hook")
    events = [
        cloud_event(source="urn:stove0", type="io.riverhog.stove0.attempt.issue", subject="a"),
        cloud_event(source="urn:stove0", type="io.riverhog.stove0.attempt.issue", subject="b"),
    ]
    deliveries: list[str] = []
    fail_second = True

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal fail_second
        if request.url.host == "stove0.test":
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
                name="stove0",
                events_url="https://stove0.test/v1/events",
                token_env="EVENT_TOKEN",
                webhook_url_env="HA_WEBHOOK",
            ),
        ),
    )
    upgrade_state(config.state_path)
    relay = MangoFish(config)
    client = httpx.Client(transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(SourceRelayError, match="delivery failed"):
            relay.relay_source_once(config.sources[0], client=client)
        assert relay.state.cursor("stove0") == "0"

        assert relay.relay_source_once(config.sources[0], client=client) == 2
        assert relay.state.cursor("stove0") == "2"
    finally:
        client.close()

    assert deliveries == [events[0].id, events[1].id, events[0].id, events[1].id]
