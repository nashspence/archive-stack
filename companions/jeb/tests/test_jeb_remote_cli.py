from __future__ import annotations

import json
from typing import Any

from jeb_cli import main as jeb_cli


class FakeJebApi:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def status(self, *, include_backlog: bool = True) -> dict[str, Any]:
        self.calls.append(("status", {"include_backlog": include_backlog}))
        return {
            "sources": [],
            "batches": {"total": 0, "active": 0, "terminal": 0, "states": {}},
            "active_attempts": {"attempts": [], "total": 0},
            "recent_failures": {"attempts": [], "total": 0},
            "target_preflight_failures": {"total": 0, "failures": []},
        }

    def list_attempts(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("attempts", kwargs))
        return {
            "page": kwargs["page"],
            "pages": 0,
            "per_page": kwargs["per_page"],
            "total": 0,
            "sort": kwargs["sort"],
            "order": kwargs["order"],
            "terminal": kwargs["terminal"],
            "filters": {"source": kwargs.get("source")},
            "attempts": [],
        }

    def check_config(self) -> dict[str, Any]:
        self.calls.append(("check-config", {}))
        return {"status": "ok", "source_count": 2, "sources": ["a", "b"]}

    def run_once(self) -> dict[str, Any]:
        self.calls.append(("once", {}))
        return {"status": "started", "operation": {"id": "op-once"}}

    def archive_now(
        self,
        *,
        source: str,
        process: bool = True,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        self.calls.append(
            ("archive-now", {"source": source, "process": process, "dry_run": dry_run})
        )
        if dry_run:
            return {
                "status": "would_process" if process else "would_stage",
                "source": source,
                "source_id": source,
                "target_name": "munchy",
                "file_count": 1,
                "total_bytes": 42,
                "cleanup": "delete",
                "process": process,
                "dry_run": True,
                "batch_id": "batch-plan",
                "target_submission_id": "submission-plan",
                "target_preflight": {
                    "ok": True,
                    "status": "accepted",
                },
            }
        return {
            "status": "started",
            "source": source,
            "batch_id": "batch-1",
            "operation": None if not process else {"id": "op-archive"},
        }

    def list_sources(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("source-list", kwargs))
        return {
            "page": 1,
            "pages": 1,
            "per_page": 1,
            "total": 1,
            "sources": [
                {
                    "id": "camera",
                    "enabled": True,
                    "adapters": ["ftp"],
                    "target": "munchy",
                }
            ],
        }

    def add_source(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.calls.append(("source-add", payload))
        return {"source": payload, "credential": "generated"}

    def plan_source_removal(self, source_id: str, *, purge: bool) -> dict[str, Any]:
        self.calls.append(("source-removal-plan", {"source": source_id, "purge": purge}))
        return {
            "status": "ready",
            "source": source_id,
            "purge": purge,
            "warning": "DANGER: selected Jeb-managed files may be the only copies.",
            "managed_file_count": 2,
            "managed_bytes": 10,
            "active_attempts": [],
            "blockers": [],
            "expires_at": "2026-07-16T00:15:00Z",
            "challenge": "purge-source-challenge",
        }


def test_jeb_remote_cli_calls_api_for_status(capsys, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    fake = FakeJebApi()
    monkeypatch.setattr(jeb_cli, "client", lambda: fake)

    assert jeb_cli.main(["status", "--no-backlog", "--json"]) == 0

    assert fake.calls == [("status", {"include_backlog": False})]
    payload = json.loads(capsys.readouterr().out)
    assert payload["batches"]["total"] == 0


def test_jeb_remote_cli_calls_api_for_attempts(capsys, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    fake = FakeJebApi()
    monkeypatch.setattr(jeb_cli, "client", lambda: fake)
    monkeypatch.setenv("RIVERHOG_CLI_PLAIN", "1")

    assert jeb_cli.main(["attempt", "list", "--terminal", "all", "--source", "camera"]) == 0

    assert fake.calls == [
        (
            "attempts",
            {
                "source": "camera",
                "collection_tag": None,
                "order": "desc",
                "page": 1,
                "per_page": 25,
                "query": None,
                "sort": "updated_at",
                "state": None,
                "target": None,
                "terminal": "all",
                "all_items": False,
            },
        )
    ]
    assert "Jeb attempts: 0 (page 1/0)" in capsys.readouterr().out


def test_jeb_remote_cli_lists_ids_and_forwards_list_filters(capsys, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    fake = FakeJebApi()
    monkeypatch.setattr(jeb_cli, "client", lambda: fake)

    assert (
        jeb_cli.main(
            [
                "source",
                "list",
                "--query",
                "cam",
                "--enabled",
                "--adapter",
                "ftp",
                "--target",
                "munchy",
                "--sort",
                "updated_at",
                "--order",
                "desc",
                "--all",
                "--ids",
            ]
        )
        == 0
    )

    assert fake.calls == [
        (
            "source-list",
            {
                "page": 1,
                "per_page": 25,
                "sort": "updated_at",
                "order": "desc",
                "query": "cam",
                "enabled": True,
                "adapter": "ftp",
                "target": "munchy",
                "all_items": True,
            },
        )
    ]
    assert capsys.readouterr().out == "camera\n"


def test_jeb_remote_cli_formats_source_page(capsys, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    fake = FakeJebApi()
    monkeypatch.setattr(jeb_cli, "client", lambda: fake)

    assert jeb_cli.main(["source", "list"]) == 0

    output = capsys.readouterr().out
    assert "Jeb sources: 1 (page 1/1)" in output
    assert "- camera  state=enabled  adapters=ftp  target=munchy" in output


def test_jeb_remote_cli_attempt_ids_request_all_matches(capsys, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    fake = FakeJebApi()
    monkeypatch.setattr(jeb_cli, "client", lambda: fake)

    assert jeb_cli.main(["attempt", "list", "--terminal", "all", "--all", "--ids"]) == 0

    assert fake.calls[0][0] == "attempts"
    assert fake.calls[0][1]["all_items"] is True
    assert capsys.readouterr().out == "\n"


def test_jeb_remote_cli_calls_api_for_actions(capsys, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    fake = FakeJebApi()
    monkeypatch.setattr(jeb_cli, "client", lambda: fake)
    monkeypatch.setenv("RIVERHOG_CLI_PLAIN", "1")

    assert jeb_cli.main(["check-config"]) == 0
    assert jeb_cli.main(["once"]) == 0
    assert jeb_cli.main(["archive-now", "--source", "camera", "--no-process"]) == 0

    assert fake.calls == [
        ("check-config", {}),
        ("once", {}),
        ("archive-now", {"source": "camera", "process": False, "dry_run": False}),
    ]
    output = capsys.readouterr().out
    assert "Jeb config: ok" in output
    assert "jeb scheduler pass: started" in output
    assert "jeb archive: started" in output


def test_jeb_remote_cli_reports_started_archive_operation(capsys, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    fake = FakeJebApi()
    monkeypatch.setattr(jeb_cli, "client", lambda: fake)
    monkeypatch.setenv("RIVERHOG_CLI_PLAIN", "1")

    assert jeb_cli.main(["archive-now", "--source", "camera"]) == 0

    assert fake.calls == [("archive-now", {"source": "camera", "process": True, "dry_run": False})]
    output = capsys.readouterr().out
    assert "jeb archive" in output
    assert "jeb archive: started" in output


def test_jeb_remote_cli_renders_archive_dry_run(capsys, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    fake = FakeJebApi()
    monkeypatch.setattr(jeb_cli, "client", lambda: fake)
    monkeypatch.setenv("RIVERHOG_CLI_PLAIN", "1")

    assert jeb_cli.main(["archive-now", "--source", "camera", "--dry-run"]) == 0

    assert fake.calls == [("archive-now", {"source": "camera", "process": True, "dry_run": True})]
    output = capsys.readouterr().out
    assert "Jeb archive plan: camera" in output
    assert "eligible files: 1" in output
    assert "eligible bytes: 42" in output


def test_jeb_remote_cli_enrolls_and_plans_source_purge(capsys, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    fake = FakeJebApi()
    monkeypatch.setattr(jeb_cli, "client", lambda: fake)
    assert (
        jeb_cli.main(
            [
                "source",
                "add",
                "camera",
                "--adapter",
                "ftp",
                "--template",
                "camera-review",
            ]
        )
        == 0
    )
    capsys.readouterr()
    assert jeb_cli.main(["source", "remove", "camera", "--purge", "--dry-run"]) == 0

    assert fake.calls == [
        (
            "source-add",
            {
                "id": "camera",
                "adapters": ["ftp"],
                "template": "camera-review",
                "enabled": True,
                "stable_seconds": 600,
                "collection_tags": ["camera"],
                "target": "munchy",
                "threshold_bytes": 0,
                "cleanup": "after_target_success",
                "cadence": "weekly",
                "weekday": 0,
                "hour": 3,
                "minute": 0,
            },
        ),
        ("source-removal-plan", {"source": "camera", "purge": True}),
    ]
    output = capsys.readouterr().out
    assert "Jeb source removal plan: camera" in output
    assert "DANGER:" in output


def test_remote_cli_closes_its_shared_api_client(capsys, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    closed: list[bool] = []

    class ClosingApi(FakeJebApi):
        def close(self) -> None:
            closed.append(True)

    monkeypatch.setattr(jeb_cli, "_API_CLIENT", ClosingApi())

    assert jeb_cli.main(["status", "--json"]) == 0

    assert json.loads(capsys.readouterr().out)["sources"] == []
    assert closed == [True]
    assert jeb_cli._API_CLIENT is None
