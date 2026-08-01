from __future__ import annotations

import json
from typing import Any

from jeb_cli import main as jeb_cli


def source_payload(*, enabled: bool = True) -> dict[str, Any]:
    return {
        "id": "camera",
        "enabled": enabled,
        "adapters": ["ftp"],
        "stable_seconds": 600,
        "include_extensions": ["mp4"],
        "target": "munchy",
        "target_config": {"template_id": "camera-review"},
        "threshold_bytes": 0,
        "cleanup": "after_target_success",
        "cadence": "weekly",
        "weekday": 0,
        "hour": 3,
        "minute": 0,
    }


class FakeJebApi:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def status(self, *, include_backlog: bool = True) -> dict[str, Any]:
        self.calls.append(("status", {"include_backlog": include_backlog}))
        return {
            "sources": [],
            "batches": {"total": 0, "unresolved": 0, "resolved": 0, "states": {}},
            "unresolved_attempts": {"attempts": [], "total": 0},
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
            "resolution": kwargs["resolution"],
            "filters": {"source": kwargs.get("source")},
            "attempts": [],
        }

    def get_attempt(self, attempt_id: str) -> dict[str, Any]:
        self.calls.append(("attempt-show", {"attempt": attempt_id}))
        return {
            "attempt_id": attempt_id,
            "source_id": "camera",
            "target_name": "munchy",
            "state": "failed",
            "file_count": 2,
            "staged_file_count": 1,
            "total_bytes": 42,
            "last_error": "target unavailable",
        }

    def wait_for_attempt(self, attempt_id: str, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("attempt-watch", {"attempt": attempt_id, **kwargs}))
        payload = self.get_attempt(attempt_id)
        on_update = kwargs.get("on_update")
        if callable(on_update):
            on_update(payload)
        return payload

    def cancel_attempt(self, attempt_id: str) -> dict[str, Any]:
        self.calls.append(("attempt-cancel", {"attempt": attempt_id}))
        payload = self.get_attempt(attempt_id)
        payload["state"] = "canceled"
        return payload

    def list_operations(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("operation-list", kwargs))
        return {
            "page": 1,
            "pages": 1,
            "per_page": 1,
            "total": 1,
            "sort": kwargs["sort"],
            "order": kwargs["order"],
            "query": kwargs["query"],
            "filters": {},
            "operations": [
                {
                    "id": "op-once",
                    "operation": "once",
                    "state": "succeeded",
                    "started_at": "2026-08-01T00:00:00Z",
                }
            ],
        }

    def get_operation(self, operation_id: str) -> dict[str, Any]:
        self.calls.append(("operation-show", {"operation": operation_id}))
        return {
            "id": operation_id,
            "operation": "once",
            "state": "succeeded",
            "started_at": "2026-08-01T00:00:00Z",
            "completed_at": "2026-08-01T00:00:01Z",
        }

    def wait_for_operation(self, operation_id: str, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("operation-watch", {"operation": operation_id, **kwargs}))
        return self.get_operation(operation_id)

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
            "sources": [source_payload()],
        }

    def get_source(self, source_id: str) -> dict[str, Any]:
        self.calls.append(("source-show", {"source": source_id}))
        return source_payload()

    def add_source(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.calls.append(("source-add", payload))
        return {"source": source_payload(), "credential": "generated"}

    def update_source(self, source_id: str, changes: dict[str, Any]) -> dict[str, Any]:
        self.calls.append(("source-set", {"source": source_id, "changes": changes}))
        return source_payload()

    def set_source_enabled(self, source_id: str, *, enabled: bool) -> dict[str, Any]:
        self.calls.append(("source-enabled", {"source": source_id, "enabled": enabled}))
        return source_payload(enabled=enabled)

    def rotate_source_credential(
        self,
        source_id: str,
        *,
        credential: str | None = None,
    ) -> dict[str, Any]:
        self.calls.append(("source-credential", {"source": source_id, "credential": credential}))
        return {"source": source_payload(), "credential": credential or "generated"}

    def plan_source_removal(self, source_id: str, *, purge: bool) -> dict[str, Any]:
        self.calls.append(("source-removal-plan", {"source": source_id, "purge": purge}))
        return {
            "status": "ready",
            "source": source_id,
            "purge": purge,
            "warning": "DANGER: selected Jeb-managed files may be the only copies.",
            "managed_file_count": 2,
            "managed_bytes": 10,
            "unresolved_attempts": [],
            "blockers": [],
            "expires_at": "2026-07-16T00:15:00Z",
            "challenge": "purge-source-challenge",
        }

    def remove_source(self, source_id: str, *, challenge: str) -> dict[str, Any]:
        self.calls.append(("source-remove", {"source": source_id, "challenge": challenge}))
        return {"status": "removed", "source": source_id, "purged": False, "files": 0, "bytes": 0}


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

    assert jeb_cli.main(["attempt", "list", "--resolution", "all", "--source", "camera"]) == 0

    assert fake.calls == [
        (
            "attempts",
            {
                "source": "camera",
                "order": "desc",
                "page": 1,
                "per_page": 25,
                "query": None,
                "sort": "updated_at",
                "state": None,
                "target": None,
                "resolution": "all",
                "all_items": False,
            },
        )
    ]
    assert "Jeb attempts: 0 (page 1/0)" in capsys.readouterr().out


def test_jeb_remote_cli_shows_one_actionable_attempt(capsys, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    fake = FakeJebApi()
    monkeypatch.setattr(jeb_cli, "client", lambda: fake)
    monkeypatch.setenv("RIVERHOG_CLI_PLAIN", "1")

    assert jeb_cli.main(["attempt", "show", "attempt-1"]) == 0

    assert fake.calls == [("attempt-show", {"attempt": "attempt-1"})]
    output = capsys.readouterr().out
    assert "Jeb attempt attempt-1" in output
    assert "source: camera" in output
    assert "error: target unavailable" in output


def test_jeb_remote_cli_watches_only_jeb_attempt_state(capsys, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    fake = FakeJebApi()
    monkeypatch.setattr(jeb_cli, "client", lambda: fake)

    assert jeb_cli.main(["attempt", "watch", "attempt-1", "--interval", "0.5"]) == 1

    captured = capsys.readouterr()
    assert "Jeb attempt attempt-1: failed" in captured.err
    assert "Jeb attempt attempt-1" in captured.out
    assert "target submission" not in captured.err


def test_jeb_remote_cli_watch_emits_final_json_and_success_exit(capsys, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    class SuccessfulApi(FakeJebApi):
        def wait_for_attempt(self, attempt_id: str, **kwargs: Any) -> dict[str, Any]:
            assert kwargs == {"interval": 2.0, "on_update": None}
            payload = super().get_attempt(attempt_id)
            payload["state"] = "cleanup_done"
            payload["last_error"] = None
            return payload

    fake = SuccessfulApi()
    monkeypatch.setattr(jeb_cli, "client", lambda: fake)

    assert jeb_cli.main(["attempt", "watch", "attempt-1", "--interval", "2", "--json"]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["attempt_id"] == "attempt-1"
    assert payload["state"] == "cleanup_done"


def test_jeb_remote_cli_cancels_attempt_and_inspects_operations(capsys, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    fake = FakeJebApi()
    monkeypatch.setattr(jeb_cli, "client", lambda: fake)

    assert jeb_cli.main(["attempt", "cancel", "attempt-1", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["state"] == "canceled"
    assert jeb_cli.main(["operation", "list", "--all", "--ids"]) == 0
    assert capsys.readouterr().out == "op-once\n"
    assert jeb_cli.main(["operation", "show", "op-once"]) == 0
    assert "Jeb operation op-once" in capsys.readouterr().out
    assert jeb_cli.main(["operation", "watch", "op-once", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["state"] == "succeeded"


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


def test_jeb_remote_cli_source_show_has_human_and_json_projections(capsys, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    fake = FakeJebApi()
    monkeypatch.setattr(jeb_cli, "client", lambda: fake)

    assert jeb_cli.main(["source", "show", "camera"]) == 0
    human = capsys.readouterr().out
    assert "Jeb source camera" in human
    assert 'target config: {"template_id":"camera-review"}' in human
    assert "schedule: weekly weekday=0 at=03:00" in human

    assert jeb_cli.main(["source", "show", "camera", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == source_payload()


def test_jeb_remote_cli_source_mutations_have_human_and_json_projections(
    capsys,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    fake = FakeJebApi()
    monkeypatch.setattr(jeb_cli, "client", lambda: fake)

    add_args = [
        "source",
        "add",
        "camera",
        "--adapter",
        "ftp",
        "--target-config",
        "template_id=camera-review",
    ]
    assert jeb_cli.main(add_args) == 0
    assert "credential: generated" in capsys.readouterr().out
    assert jeb_cli.main([*add_args, "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["credential"] == "generated"

    assert (
        jeb_cli.main(["source", "set", "camera", "--target-config", "template_id=camera-review"])
        == 0
    )
    assert "Jeb source camera" in capsys.readouterr().out

    assert jeb_cli.main(["source", "enable", "camera", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["enabled"] is True
    assert jeb_cli.main(["source", "disable", "camera"]) == 0
    assert "state: disabled" in capsys.readouterr().out

    assert jeb_cli.main(["source", "credential", "camera"]) == 0
    assert "credential: generated" in capsys.readouterr().out
    assert jeb_cli.main(["source", "credential", "camera", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["credential"] == "generated"

    assert jeb_cli.main(["source", "remove", "camera", "--confirm", "challenge"]) == 0
    removal = capsys.readouterr().out
    assert "removed Jeb source camera" in removal
    assert "purged: false" in removal


def test_jeb_remote_cli_attempt_ids_request_all_matches(capsys, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    fake = FakeJebApi()
    monkeypatch.setattr(jeb_cli, "client", lambda: fake)

    assert jeb_cli.main(["attempt", "list", "--resolution", "all", "--all", "--ids"]) == 0

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


def test_jeb_remote_cli_fails_rejected_archive_dry_run(capsys, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    fake = FakeJebApi()
    monkeypatch.setattr(jeb_cli, "client", lambda: fake)
    monkeypatch.setattr(
        fake,
        "archive_now",
        lambda **_kwargs: {
            "status": "target_preflight_failed",
            "source": "camera",
            "file_count": 1,
            "total_bytes": 42,
            "target_preflight": {
                "ok": False,
                "status": "rejected",
                "error": "source filesystem metadata is required",
            },
        },
    )
    monkeypatch.setenv("RIVERHOG_CLI_PLAIN", "1")

    assert jeb_cli.main(["archive-now", "--source", "camera", "--dry-run"]) == 1

    output = capsys.readouterr().out
    assert "status: target_preflight_failed" in output
    assert "error: source filesystem metadata is required" in output


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
                "--target-config",
                "template_id=camera-review",
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
                "target_config": {"template_id": "camera-review"},
                "enabled": True,
                "stable_seconds": 600,
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
