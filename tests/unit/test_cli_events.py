from __future__ import annotations

import json

import riverhog_cli.main
from lifecycle_events import EventPage, cloud_event
from riverhog_cli.main import app
from typer.testing import CliRunner


def test_riverhog_event_list_has_human_and_json_output(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    class FakeClient:
        def list_lifecycle_events(self, *, after: str | None, limit: int) -> EventPage:
            assert after == "4"
            assert limit == 6
            return EventPage(
                events=[
                    cloud_event(
                        event_id="event-1",
                        source="urn:riverhog:riverhog",
                        type="io.riverhog.riverhog.collection.finalized",
                        subject="41",
                    )
                ],
                next_cursor="10",
                has_more=True,
            )

    monkeypatch.setattr(riverhog_cli.main, "client", lambda: FakeClient())
    runner = CliRunner()
    args = ["event", "list", "--after", "4", "--limit", "6"]

    human = runner.invoke(app, args)
    machine = runner.invoke(app, [*args, "--json"])

    assert human.exit_code == 0
    assert "io.riverhog.riverhog.collection.finalized" in human.stdout
    assert "has more: yes" in human.stdout
    assert machine.exit_code == 0
    assert json.loads(machine.stdout)["next_cursor"] == "10"
