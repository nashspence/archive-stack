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
            "batches": {"total": 0, "active": 0, "terminal": 0, "states": {}},
            "active_attempts": {"attempts": [], "total": 0},
            "recent_failures": {"attempts": [], "total": 0},
            "routing_preflight_failures": {"total": 0, "failures": []},
        }

    def list_jeb_attempts(self, **kwargs: Any) -> dict[str, Any]:
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

    def check_jeb_config(self) -> dict[str, Any]:
        self.calls.append(("check-config", {}))
        return {"status": "ok", "source_count": 2, "sources": ["a", "b"]}

    def run_jeb_once(self) -> dict[str, Any]:
        self.calls.append(("once", {}))
        return {"status": "started", "operation": {"id": "op-once"}}

    def archive_jeb_now(
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
                "job_id": "job-plan",
                "routing_preflight": {
                    "configured": False,
                    "ok": True,
                    "status": "not_configured",
                    "unmatched_count": 0,
                    "left_count": 0,
                },
            }
        return {
            "status": "started",
            "source": source,
            "batch_id": "batch-1",
            "operation": None if not process else {"id": "op-archive"},
        }

    def list_jeb_sources(self) -> dict[str, Any]:
        self.calls.append(("source-list", {}))
        return {"sources": [{"id": "camera", "enabled": True, "adapters": ["ftp"]}]}

    def add_jeb_source(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.calls.append(("source-add", payload))
        return {"source": payload, "credential": "generated"}

    def plan_jeb_source_removal(self, source_id: str, *, purge: bool) -> dict[str, Any]:
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

    assert jeb_cli.main(["attempts", "--terminal", "all", "--source", "camera"]) == 0

    assert fake.calls == [
        (
            "attempts",
            {
                "source": "camera",
                "collection_slug": None,
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
    assert "Jeb attempts: 0 (page 1/0)" in capsys.readouterr().out


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


def test_jeb_remote_cli_enrolls_and_plans_source_purge(
    tmp_path, capsys, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    fake = FakeJebApi()
    monkeypatch.setattr(jeb_cli, "client", lambda: fake)
    policy = tmp_path / "policy.yaml"
    policy.write_text("workflow_mode: review\ntasks: [qcut_video]\n", encoding="utf-8")

    assert jeb_cli.main(
        [
            "source",
            "add",
            "camera",
            "--adapter",
            "ftp",
            "--policy",
            str(policy),
        ]
    ) == 0
    capsys.readouterr()
    assert jeb_cli.main(["source", "remove", "camera", "--purge", "--dry-run"]) == 0

    assert fake.calls == [
        (
            "source-add",
            {
                "id": "camera",
                "adapters": ["ftp"],
                "policy": {"workflow_mode": "review", "tasks": ["qcut_video"]},
                "enabled": True,
                "stable_seconds": 600,
                "collection_slug": "camera",
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
