from __future__ import annotations

import json
from typing import Any

from jeb import cli as jeb_cli


class FakeJebApi:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def get_jeb_status(self, *, include_backlog: bool = True) -> dict[str, Any]:
        self.calls.append(("status", {"include_backlog": include_backlog}))
        return {
            "sources": [],
            "collections": [],
            "batches": {"total": 0, "active": 0, "terminal": 0, "states": {}},
            "active_attempts": {"batches": [], "total": 0},
            "recent_failures": {"batches": [], "total": 0},
            "routing_preflight_failures": {"total": 0, "failures": []},
        }

    def list_jeb_batches(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("batches", kwargs))
        return {
            "page": kwargs["page"],
            "pages": 0,
            "per_page": kwargs["per_page"],
            "total": 0,
            "sort": kwargs["sort"],
            "order": kwargs["order"],
            "terminal": kwargs["terminal"],
            "filters": {"account": kwargs.get("account")},
            "batches": [],
        }

    def check_jeb_config(self) -> dict[str, Any]:
        self.calls.append(("check-config", {}))
        return {"status": "ok", "source_count": 2, "sources": ["a", "b"]}

    def run_jeb_once(self) -> dict[str, Any]:
        self.calls.append(("once", {}))
        return {"status": "started", "operation": {"id": "op-once"}}

    def archive_jeb_now(self, *, account: str, process: bool = True) -> dict[str, Any]:
        self.calls.append(("archive-now", {"account": account, "process": process}))
        return {
            "status": "started",
            "account": account,
            "batch_id": "batch-1",
            "operation": None if not process else {"id": "op-archive"},
        }


def test_jeb_remote_cli_calls_api_for_status(capsys, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    fake = FakeJebApi()
    monkeypatch.setattr(jeb_cli, "client", lambda: fake)

    assert jeb_cli.main(["status", "--no-backlog", "--json"]) == 0

    assert fake.calls == [("status", {"include_backlog": False})]
    payload = json.loads(capsys.readouterr().out)
    assert payload["batches"]["total"] == 0


def test_jeb_remote_cli_calls_api_for_batches(capsys, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    fake = FakeJebApi()
    monkeypatch.setattr(jeb_cli, "client", lambda: fake)

    assert jeb_cli.main(["batches", "--terminal", "all", "--account", "camera"]) == 0

    assert fake.calls == [
        (
            "batches",
            {
                "account": "camera",
                "collection": None,
                "order": "desc",
                "page": 1,
                "per_page": 25,
                "query": None,
                "sort": "updated_at",
                "state": None,
                "target": None,
                "terminal": "all",
            },
        )
    ]
    assert "batches page 1/0" in capsys.readouterr().out


def test_jeb_remote_cli_calls_api_for_actions(capsys, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    fake = FakeJebApi()
    monkeypatch.setattr(jeb_cli, "client", lambda: fake)

    assert jeb_cli.main(["check-config"]) == 0
    assert jeb_cli.main(["once"]) == 0
    assert jeb_cli.main(["archive-now", "--account", "camera", "--no-process"]) == 0

    assert fake.calls == [
        ("check-config", {}),
        ("once", {}),
        ("archive-now", {"account": "camera", "process": False}),
    ]
    output = capsys.readouterr().out
    assert "ok: 2 sources" in output
    assert "ok: scheduler pass started: op-once" in output
    assert "archive batch staged for account camera: batch-1" in output


def test_jeb_remote_cli_reports_started_archive_operation(capsys, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    fake = FakeJebApi()
    monkeypatch.setattr(jeb_cli, "client", lambda: fake)

    assert jeb_cli.main(["archive-now", "--account", "camera"]) == 0

    assert fake.calls == [("archive-now", {"account": "camera", "process": True})]
    assert (
        "archive operation started for account camera: batch batch-1 operation op-archive"
        in capsys.readouterr().out
    )
