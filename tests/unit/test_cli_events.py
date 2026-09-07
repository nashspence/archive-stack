from __future__ import annotations

import json

import riverhog_cli.main
from riverhog_cli.main import app
from riverhog_protocol.lifecycle_events import RiverhogEventPage
from typer.testing import CliRunner


def test_riverhog_event_list_has_human_and_json_output(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    expected = {
        "events": [
            {
                "specversion": "1.0",
                "id": "event-1",
                "source": "urn:riverhog:riverhog",
                "type": "io.riverhog.riverhog.collection.finalized",
                "subject": "41",
                "time": "2026-08-24T00:00:00Z",
                "datacontenttype": "application/json",
                "data": {
                    "actor": {"app": "riverhog-client"},
                    "initiator": {"app": "riverhog-client"},
                    "collection_id": 41,
                    "collection_created_at": "2026-08-24T00:00:00.000000Z",
                    "files_total": 1,
                    "bytes_total": 2,
                    "archive_root_sha256": "a" * 64,
                },
            }
        ],
        "next_cursor": "10",
        "has_more": True,
    }

    class FakeClient:
        def list_lifecycle_events(self, *, after: str | None, limit: int) -> RiverhogEventPage:
            assert after == "4"
            assert limit == 6
            return RiverhogEventPage.model_validate(expected)

    monkeypatch.setattr(riverhog_cli.main, "client", lambda: FakeClient())
    runner = CliRunner()
    args = ["event", "list", "--after", "4", "--limit", "6"]

    human = runner.invoke(app, args)
    machine = runner.invoke(app, [*args, "--json"])

    assert human.exit_code == 0
    assert "io.riverhog.riverhog.collection.finalized" in human.stdout
    assert "has more: yes" in human.stdout
    assert machine.exit_code == 0
    assert json.loads(machine.stdout) == expected
