from __future__ import annotations

import contextlib
import json

import munchy_cli.main as munchy_main
from lifecycle_events import EventPage, cloud_event
from munchy_api_client.client import MunchyHttpError
from munchy_cli.main import app
from typer.testing import CliRunner

runner = CliRunner()


def test_munchy_help() -> None:
    result = runner.invoke(app, ["--help"])

    assert result.exit_code == 0
    assert "Munchy media ingest CLI." in result.stdout
    assert "Encode profile operations." in result.stdout
    assert "Munchy job operations." in result.stdout
    assert "Submit local files through a server-owned job template." in result.stdout
    assert "Lifecycle event inspection." in result.stdout
    assert "Job scheduler controls." in result.stdout


def test_munchy_event_list_has_human_and_json_output(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    class FakeClient:
        def __init__(self, base_url: str) -> None:
            assert base_url == "https://munchy.test"

        def list_lifecycle_events(self, *, after: str | None, limit: int) -> EventPage:
            assert after == "12"
            assert limit == 7
            return EventPage(
                events=[
                    cloud_event(
                        event_id="event-1",
                        source="urn:riverhog:munchy",
                        type="io.riverhog.munchy.job.succeeded",
                        subject="job-1",
                    )
                ],
                next_cursor="19",
                has_more=False,
            )

    monkeypatch.setattr("munchy_cli.main.MunchyClient", FakeClient)

    human = runner.invoke(
        app,
        [
            "event",
            "list",
            "--server-url",
            "https://munchy.test",
            "--after",
            "12",
            "--limit",
            "7",
        ],
    )
    machine = runner.invoke(
        app,
        [
            "event",
            "list",
            "--server-url",
            "https://munchy.test",
            "--after",
            "12",
            "--limit",
            "7",
            "--json",
        ],
    )

    assert human.exit_code == 0
    assert "io.riverhog.munchy.job.succeeded" in human.stdout
    assert machine.exit_code == 0
    assert json.loads(machine.stdout)["next_cursor"] == "19"


def test_munchy_json_mode_emits_the_public_error_document(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    class FakeClient:
        def __init__(self, _base_url: str) -> None:
            pass

        def list_lifecycle_events(self, **_kwargs: object) -> EventPage:
            raise MunchyHttpError(
                "GET",
                "https://munchy.test/v1/events",
                403,
                b'{"error":{"code":"forbidden","message":"events denied"}}',
            )

    monkeypatch.setattr("munchy_cli.main.MunchyClient", FakeClient)

    result = runner.invoke(
        app,
        ["event", "list", "--server-url", "https://munchy.test", "--json"],
    )

    assert result.exit_code == 1
    assert json.loads(result.stdout) == {"error": {"code": "forbidden", "message": "events denied"}}
    assert result.stderr == ""


def test_munchy_watch_json_emits_unsuccessful_final_document(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    class FakeClient:
        def __init__(self, _base_url: str) -> None:
            pass

        def wait_for_job(self, job_id: str, *, interval: float) -> dict[str, object]:
            assert job_id == "job-1"
            assert interval == 0.5
            return {"job_id": job_id, "state": "failed", "error": "encoder failed"}

    monkeypatch.setattr("munchy_cli.main.MunchyClient", FakeClient)

    result = runner.invoke(
        app,
        [
            "job",
            "watch",
            "job-1",
            "--server-url",
            "https://munchy.test",
            "--interval",
            "0.5",
            "--json",
        ],
    )

    assert result.exit_code == 1
    assert json.loads(result.stdout) == {
        "job_id": "job-1",
        "state": "failed",
        "error": "encoder failed",
    }
    assert result.stderr == ""


def test_munchy_watch_success_has_human_and_json_receipts(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    final = {
        "job_id": "job-1",
        "state": "succeeded",
        "phase": "done",
        "handoff": {"state": "complete", "safe_to_delete": True},
    }

    class FakeClient:
        def __init__(self, _base_url: str) -> None:
            pass

        def wait_for_job(self, job_id: str, *, interval: float) -> dict[str, object]:
            assert job_id == "job-1"
            assert interval == 0.5
            return final

    monkeypatch.setattr("munchy_cli.main.MunchyClient", FakeClient)
    base = ["job", "watch", "job-1", "--server-url", "https://munchy.test", "--interval", "0.5"]
    human = runner.invoke(app, base)
    structured = runner.invoke(app, [*base, "--json"])

    assert human.exit_code == structured.exit_code == 0
    assert "job-1" in human.stdout
    assert json.loads(structured.stdout)["job_id"] == "job-1"


def test_munchy_scheduler_controls_use_admin_api(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    calls: list[str] = []
    state = {"paused": False}

    class FakeAdminClient:
        def __init__(self, base_url: str) -> None:
            assert base_url == "https://munchy.test"

        def get_scheduler_status(self) -> dict[str, object]:
            calls.append("status")
            return {
                "paused": state["paused"],
                "active_jobs": ["job-1"],
                "scheduled_jobs": [],
                "running_job_limit": 2,
                "running_job_slots_available": 1,
                "runnable_job_count": 1,
                "runnable_jobs": ["job-2"],
            }

        def pause_scheduler(self) -> dict[str, object]:
            calls.append("pause")
            state["paused"] = True
            return {"paused": True}

        def resume_scheduler(self) -> dict[str, object]:
            calls.append("resume")
            state["paused"] = False
            return {"paused": False}

    monkeypatch.setattr("munchy_cli.main.MunchyAdminClient", FakeAdminClient)

    paused = runner.invoke(
        app,
        ["scheduler", "pause", "--server-url", "https://munchy.test"],
    )
    resumed = runner.invoke(
        app,
        ["scheduler", "resume", "--server-url", "https://munchy.test", "--json"],
    )

    assert paused.exit_code == 0
    assert "scheduler: paused" in paused.stdout
    assert resumed.exit_code == 0
    assert json.loads(resumed.stdout)["paused"] is False
    assert calls == ["pause", "status", "resume", "status"]

    status_human = runner.invoke(
        app, ["scheduler", "status", "--server-url", "https://munchy.test"]
    )
    status_json = runner.invoke(
        app, ["scheduler", "status", "--server-url", "https://munchy.test", "--json"]
    )
    pause_json = runner.invoke(
        app, ["scheduler", "pause", "--server-url", "https://munchy.test", "--json"]
    )
    resume_human = runner.invoke(
        app, ["scheduler", "resume", "--server-url", "https://munchy.test"]
    )
    assert status_human.exit_code == status_json.exit_code == 0
    assert "scheduler:" in status_human.stdout
    assert "paused" in json.loads(status_json.stdout)
    assert pause_json.exit_code == resume_human.exit_code == 0
    assert json.loads(pause_json.stdout)["paused"] is True
    assert "scheduler:" in resume_human.stdout


def test_munchy_command_help_has_summaries() -> None:
    profile = runner.invoke(app, ["profile", "--help"])
    assert profile.exit_code == 0
    assert "Validate Munchy server encode profile config." in profile.stdout
    assert "Show normalized Munchy server encode profile config." in profile.stdout
    assert "dump-json" not in profile.stdout

    job = runner.invoke(app, ["job", "--help"])
    assert job.exit_code == 0
    for summary in (
        "Dry-run the configured routed review sweep.",
        "List Munchy jobs.",
        "Show Munchy job details.",
        "Cancel a Munchy job.",
    ):
        assert summary in job.stdout
    assert "Watch a Munchy job until it is safe to delete local" in job.stdout
    assert "sources." in job.stdout

    routing = runner.invoke(app, ["routing", "--help"])
    assert routing.exit_code == 0
    assert "Explain how routing classifies local files." in routing.stdout


def test_munchy_profile_validate(tmp_path) -> None:  # type: ignore[no-untyped-def]
    profile_path = tmp_path / "profile.yaml"
    profile_path.write_text(
        """
target: munchy-av1-nvenc
name: camera
archive:
  codec: av1_nvenc
  container: webm
  quality: 52
""".strip(),
        encoding="utf-8",
    )

    result = runner.invoke(app, ["profile", "validate", str(profile_path)])

    assert result.exit_code == 0
    assert f"{profile_path}: ok (1 profile)" in result.stdout


def test_munchy_profile_validate_json(tmp_path) -> None:  # type: ignore[no-untyped-def]
    profile_path = tmp_path / "profile.yaml"
    profile_path.write_text(
        """
target: munchy-av1-nvenc
name: camera
archive:
  codec: av1_nvenc
  container: webm
  quality: 52
""".strip(),
        encoding="utf-8",
    )

    result = runner.invoke(app, ["profile", "validate", str(profile_path), "--json"])

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload == {
        "path": str(profile_path),
        "profile_count": 1,
        "profiles": [
            {
                "container": "webm",
                "name": "camera",
                "quality": 52,
                "target": "munchy-av1-nvenc",
            }
        ],
        "valid": True,
    }


def test_munchy_profile_show_json(tmp_path) -> None:  # type: ignore[no-untyped-def]
    profile_path = tmp_path / "profile.yaml"
    profile_path.write_text(
        """
schema_version: 1
target: munchy-av1-nvenc
name: camera
""".strip(),
        encoding="utf-8",
    )

    result = runner.invoke(app, ["profile", "show", str(profile_path), "--json"])

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["profiles"]["camera"]["target"] == "munchy-av1-nvenc"
    assert payload["profiles"]["camera"]["archive"]["container"] == "mkv"


def test_munchy_profile_show_accepts_job_config_profiles(tmp_path) -> None:  # type: ignore[no-untyped-def]
    config_path = tmp_path / "job.yaml"
    config_path.write_text(
        """
profiles:
  camera:
    schema_version: 1
    target: munchy-av1-nvenc
    name: camera
    archive:
      codec: av1_nvenc
      container: webm
      quality: 38
""".strip(),
        encoding="utf-8",
    )

    result = runner.invoke(app, ["profile", "show", str(config_path), "--json"])

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["profiles"]["camera"]["archive"]["container"] == "webm"


def test_munchy_job_list(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    class FakeClient:
        def __init__(self, base_url: str) -> None:
            assert base_url == "https://munchy.test"

        def list_jobs(
            self,
            *,
            page: int,
            per_page: int,
            sort: str,
            order: str,
            query: str | None,
            terminal: str,
            state: str | None,
            workflow_mode: str | None,
            handoff_destination: str | None,
            cancel_requested: bool | None,
            storage_wait: bool | None,
            all_items: bool,
        ) -> dict[str, object]:
            assert page == 2
            assert per_page == 2
            assert sort == "created_at"
            assert order == "asc"
            assert query == "camera"
            assert terminal == "all"
            assert state == "running"
            assert workflow_mode == "collection_archive"
            assert handoff_destination == "riverhog"
            assert cancel_requested is False
            assert storage_wait is True
            assert all_items is True
            return {
                "page": 2,
                "pages": 3,
                "per_page": 2,
                "total": 5,
                "sort": sort,
                "order": order,
                "query": query,
                "terminal": terminal,
                "filters": {
                    "state": state,
                    "workflow_mode": workflow_mode,
                    "handoff_destination": handoff_destination,
                    "cancel_requested": cancel_requested,
                    "storage_wait": storage_wait,
                },
                "jobs": [{"job_id": "job-1", "state": "running"}],
            }

    monkeypatch.setattr("munchy_cli.main.MunchyClient", FakeClient)

    result = runner.invoke(
        app,
        [
            "job",
            "list",
            "--server-url",
            "https://munchy.test",
            "--page",
            "2",
            "--per-page",
            "2",
            "--sort",
            "created_at",
            "--order",
            "asc",
            "--query",
            "camera",
            "--terminal",
            "all",
            "--state",
            "running",
            "--workflow",
            "collection-archive",
            "--destination",
            "riverhog",
            "--not-cancel-requested",
            "--storage-wait",
            "--all",
        ],
    )

    assert result.exit_code == 0
    assert "jobs page 2/3" in result.stdout
    assert "job-1" in result.stdout
    assert "job: running" in result.stdout


def test_munchy_job_list_all_ids_is_pipeable(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    class FakeClient:
        def __init__(self, base_url: str) -> None:
            assert base_url == "https://munchy.test"

        def list_jobs(self, **kwargs: object) -> dict[str, object]:
            assert kwargs["all_items"] is True
            return {"jobs": [{"job_id": "job-1"}, {"job_id": "job-2"}]}

    monkeypatch.setattr("munchy_cli.main.MunchyClient", FakeClient)

    result = runner.invoke(
        app,
        ["job", "list", "--server-url", "https://munchy.test", "--all", "--ids"],
    )

    assert result.exit_code == 0
    assert result.stdout == "job-1\njob-2\n"


def test_munchy_template_list_all_ids_is_pipeable(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    class FakeClient:
        def __init__(self, base_url: str) -> None:
            assert base_url == "https://munchy.test"

        def list_job_templates(self, **kwargs: object) -> dict[str, object]:
            assert kwargs["all_items"] is True
            assert kwargs["enabled"] is False
            return {
                "templates": [
                    {"template_id": "archive"},
                    {"template_id": "review"},
                ]
            }

    monkeypatch.setattr("munchy_cli.main.MunchyAdminClient", FakeClient)

    result = runner.invoke(
        app,
        [
            "template",
            "list",
            "--server-url",
            "https://munchy.test",
            "--disabled",
            "--all",
            "--ids",
        ],
    )

    assert result.exit_code == 0
    assert result.stdout == "archive\nreview\n"


def test_munchy_template_remove_has_human_and_json_receipts(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    class FakeClient:
        def __init__(self, base_url: str) -> None:
            assert base_url == "https://munchy.test"

        def get_job_template(self, template_id: str) -> dict[str, object]:
            assert template_id == "review"
            return {"template_id": template_id, "revision": 3}

        def delete_job_template(
            self,
            template_id: str,
            *,
            expected_revision: int,
        ) -> dict[str, object]:
            assert template_id == "review"
            assert expected_revision == 3
            return {"template_id": template_id, "state": "removed"}

    monkeypatch.setattr("munchy_cli.main.MunchyAdminClient", FakeClient)

    human = runner.invoke(
        app,
        ["template", "remove", "review", "--server-url", "https://munchy.test"],
    )
    structured = runner.invoke(
        app,
        [
            "template",
            "remove",
            "review",
            "--server-url",
            "https://munchy.test",
            "--json",
        ],
    )

    assert human.exit_code == 0
    assert human.stdout == "removed job template: review\n"
    assert structured.exit_code == 0
    assert json.loads(structured.stdout) == {"template_id": "review", "state": "removed"}


def test_munchy_application_list_all_ids_is_pipeable(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    class FakeClient:
        def __init__(self, base_url: str) -> None:
            assert base_url == "https://munchy.test"

        def list_apps(self, **kwargs: object) -> dict[str, object]:
            assert kwargs["all_items"] is True
            assert kwargs["active"] is True
            return {"apps": [{"name": "desktop-client"}, {"name": "jeb"}]}

    monkeypatch.setattr("munchy_cli.main.MunchyAdminClient", FakeClient)

    result = runner.invoke(
        app,
        [
            "app",
            "list",
            "--server-url",
            "https://munchy.test",
            "--active",
            "--all",
            "--ids",
        ],
    )

    assert result.exit_code == 0
    assert result.stdout == "desktop-client\njeb\n"


def test_munchy_closes_the_server_client(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    closed: list[bool] = []

    class FakeClient:
        def __init__(self, _base_url: str) -> None:
            pass

        def list_jobs(self, **_kwargs: object) -> dict[str, object]:
            return {"jobs": []}

        def close(self) -> None:
            closed.append(True)

    monkeypatch.setattr("munchy_cli.main.MunchyClient", FakeClient)

    result = runner.invoke(app, ["job", "list", "--server-url", "https://munchy.test"])

    assert result.exit_code == 0
    assert closed == [True]


def test_munchy_job_list_json(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    class FakeClient:
        def __init__(self, base_url: str) -> None:
            self.base_url = base_url

        def list_jobs(self, **_kwargs: object) -> dict[str, object]:
            return {
                "page": 1,
                "pages": 1,
                "per_page": 25,
                "total": 1,
                "sort": "updated_at",
                "order": "desc",
                "query": None,
                "terminal": "active",
                "filters": {},
                "jobs": [{"job_id": "job-1"}],
            }

    monkeypatch.setattr("munchy_cli.main.MunchyClient", FakeClient)

    result = runner.invoke(app, ["job", "list", "--server-url", "https://munchy.test", "--json"])

    assert result.exit_code == 0
    assert json.loads(result.stdout) == {
        "filters": {},
        "jobs": [{"job_id": "job-1"}],
        "order": "desc",
        "page": 1,
        "pages": 1,
        "per_page": 25,
        "query": None,
        "sort": "updated_at",
        "terminal": "active",
        "total": 1,
    }


def test_munchy_job_diagnostic_list_matches_list_conventions(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    class FakeClient:
        def __init__(self, base_url: str) -> None:
            assert base_url == "https://munchy.test"

        def list_job_diagnostics(self, **kwargs: object) -> dict[str, object]:
            assert kwargs == {
                "page": 2,
                "per_page": 3,
                "sort": "bytes",
                "order": "asc",
                "query": "encoding",
                "all_items": True,
            }
            return {
                "page": 2,
                "pages": 2,
                "per_page": 3,
                "total": 4,
                "sort": "bytes",
                "order": "asc",
                "query": "encoding",
                "diagnostics": [
                    {
                        "job_id": "job-1",
                        "created_at": "2026-08-01T00:00:00.000000Z",
                        "reason": "encoding_failed",
                        "bytes": 1024,
                        "sha256": "a" * 64,
                    }
                ],
            }

    monkeypatch.setattr("munchy_cli.main.MunchyAdminClient", FakeClient)

    result = runner.invoke(
        app,
        [
            "job",
            "diagnostic",
            "list",
            "--server-url",
            "https://munchy.test",
            "--page",
            "2",
            "--per-page",
            "3",
            "--sort",
            "bytes",
            "--order",
            "asc",
            "--query",
            "encoding",
            "--all",
        ],
    )

    assert result.exit_code == 0
    assert "job diagnostics page 2/2" in result.stdout
    assert "job-1" in result.stdout
    assert "encoding_failed" in result.stdout
    structured = runner.invoke(
        app,
        [
            "job",
            "diagnostic",
            "list",
            "--server-url",
            "https://munchy.test",
            "--page",
            "2",
            "--per-page",
            "3",
            "--sort",
            "bytes",
            "--order",
            "asc",
            "--query",
            "encoding",
            "--all",
            "--json",
        ],
    )
    assert structured.exit_code == 0
    assert json.loads(structured.stdout)["diagnostics"][0]["job_id"] == "job-1"


def test_munchy_job_diagnostic_list_ids_is_pipeable(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    class FakeClient:
        def __init__(self, _base_url: str) -> None:
            pass

        def list_job_diagnostics(self, **_kwargs: object) -> dict[str, object]:
            return {"diagnostics": [{"job_id": "job-1"}, {"job_id": "job-2"}]}

    monkeypatch.setattr("munchy_cli.main.MunchyAdminClient", FakeClient)

    result = runner.invoke(
        app,
        [
            "job",
            "diagnostic",
            "list",
            "--server-url",
            "https://munchy.test",
            "--all",
            "--ids",
        ],
    )

    assert result.exit_code == 0
    assert result.stdout == "job-1\njob-2\n"


def test_munchy_job_diagnostic_download_requires_explicit_output_and_forwards_overwrite(
    tmp_path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    class FakeClient:
        def __init__(self, _base_url: str) -> None:
            pass

        def download_job_diagnostic(self, job_id: str, **kwargs: object) -> dict[str, object]:
            assert job_id == "job-1"
            assert kwargs == {"output": tmp_path / "case.tar.gz", "overwrite": True}
            return {
                "job_id": job_id,
                "output": str(kwargs["output"]),
                "bytes": 1024,
                "sha256": "a" * 64,
            }

    monkeypatch.setattr("munchy_cli.main.MunchyAdminClient", FakeClient)

    missing = runner.invoke(
        app,
        ["job", "diagnostic", "download", "job-1", "--server-url", "https://munchy.test"],
    )
    result = runner.invoke(
        app,
        [
            "job",
            "diagnostic",
            "download",
            "job-1",
            "--output",
            str(tmp_path / "case.tar.gz"),
            "--overwrite",
            "--server-url",
            "https://munchy.test",
            "--json",
        ],
    )

    assert missing.exit_code == 2
    assert result.exit_code == 0
    assert json.loads(result.stdout)["output"] == str(tmp_path / "case.tar.gz")
    human = runner.invoke(
        app,
        [
            "job",
            "diagnostic",
            "download",
            "job-1",
            "--output",
            str(tmp_path / "case.tar.gz"),
            "--overwrite",
            "--server-url",
            "https://munchy.test",
        ],
    )
    assert human.exit_code == 0
    assert "job-1" in human.stdout


def test_munchy_removes_diagnostics_and_terminal_jobs_by_explicit_ids(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    calls: list[tuple[str, str]] = []

    class FakeClient:
        def __init__(self, _base_url: str) -> None:
            pass

        def remove_job_diagnostic(self, job_id: str) -> dict[str, object]:
            calls.append(("diagnostic", job_id))
            return {"job_id": job_id, "removed": True}

        def remove_terminal_job(self, job_id: str) -> dict[str, object]:
            calls.append(("job", job_id))
            return {"job_id": job_id, "removed": True}

    monkeypatch.setattr("munchy_cli.main.MunchyAdminClient", FakeClient)

    diagnostics = runner.invoke(
        app,
        [
            "job",
            "diagnostic",
            "remove",
            "job-1",
            "job-2",
            "--server-url",
            "https://munchy.test",
        ],
    )
    jobs = runner.invoke(
        app,
        ["job", "remove", "job-3", "job-4", "--server-url", "https://munchy.test"],
    )

    assert diagnostics.exit_code == 0
    assert jobs.exit_code == 0
    assert calls == [
        ("diagnostic", "job-1"),
        ("diagnostic", "job-2"),
        ("job", "job-3"),
        ("job", "job-4"),
    ]
    diagnostics_json = runner.invoke(
        app,
        [
            "job",
            "diagnostic",
            "remove",
            "job-1",
            "--server-url",
            "https://munchy.test",
            "--json",
        ],
    )
    jobs_json = runner.invoke(
        app,
        ["job", "remove", "job-3", "--server-url", "https://munchy.test", "--json"],
    )
    assert json.loads(diagnostics_json.stdout)["removed"][0]["job_id"] == "job-1"
    assert json.loads(jobs_json.stdout)["removed"][0]["job_id"] == "job-3"


def test_munchy_retention_is_a_plan_until_apply_is_explicit(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    calls: list[str] = []

    class FakeClient:
        def __init__(self, _base_url: str) -> None:
            pass

        def get_retention_plan(self) -> dict[str, object]:
            calls.append("plan")
            return {
                "policy": {
                    "terminal_job_retention_seconds": 90 * 86400,
                    "job_diagnostic_retention_seconds": 30 * 86400,
                },
                "terminal_jobs": {"eligible": 2, "sample_job_ids": ["job-1"]},
                "job_diagnostics": {"eligible": 1, "sample_job_ids": ["job-2"]},
            }

        def apply_retention(self) -> dict[str, object]:
            calls.append("apply")
            return {
                "policy": {},
                "removed": {"terminal_jobs": 2, "job_diagnostics": 1},
                "errors": [],
                "remaining": {
                    "terminal_jobs": {"eligible": 0},
                    "job_diagnostics": {"eligible": 0},
                },
            }

    monkeypatch.setattr("munchy_cli.main.MunchyAdminClient", FakeClient)

    plan = runner.invoke(
        app,
        ["maintenance", "retention", "--server-url", "https://munchy.test"],
    )
    apply = runner.invoke(
        app,
        ["maintenance", "retention", "--server-url", "https://munchy.test", "--apply"],
    )

    assert plan.exit_code == 0
    assert "terminal jobs eligible: 2" in plan.stdout
    assert apply.exit_code == 0
    assert "terminal jobs removed: 2" in apply.stdout
    assert calls == ["plan", "apply"]
    plan_json = runner.invoke(
        app,
        ["maintenance", "retention", "--server-url", "https://munchy.test", "--json"],
    )
    apply_json = runner.invoke(
        app,
        [
            "maintenance",
            "retention",
            "--server-url",
            "https://munchy.test",
            "--apply",
            "--json",
        ],
    )
    assert json.loads(plan_json.stdout)["terminal_jobs"]["eligible"] == 2
    assert json.loads(apply_json.stdout)["removed"]["terminal_jobs"] == 2


def test_munchy_job_list_reports_server_errors_without_traceback(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    class FakeClient:
        def __init__(self, base_url: str) -> None:
            self.base_url = base_url

        def list_jobs(self, **_kwargs: object) -> dict[str, object]:
            raise OSError("connection refused")

    monkeypatch.setattr("munchy_cli.main.MunchyClient", FakeClient)

    result = runner.invoke(app, ["job", "list", "--server-url", "https://munchy.test"])

    assert result.exit_code == 1
    assert "munchy: connection refused" in result.stderr
    assert "Traceback" not in result.output


def test_munchy_job_show(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    class FakeClient:
        def __init__(self, base_url: str) -> None:
            assert base_url == "https://munchy.test"

        def get_job(self, job_id: str, *, compact: bool = False) -> dict[str, object]:
            assert job_id == "job-1"
            assert compact is True
            return {
                "job_id": "job-1",
                "template_id": "camera-archive",
                "created_at": "2026-07-27T12:34:56Z",
                "state": "running",
                "phase": "encoding",
            }

    monkeypatch.setattr("munchy_cli.main.MunchyClient", FakeClient)

    result = runner.invoke(
        app,
        ["job", "show", "job-1", "--server-url", "https://munchy.test", "--compact"],
    )

    assert result.exit_code == 0
    assert "job-1" in result.stdout
    assert "camera-archive · 20260727T123456Z" in result.stdout
    assert "encoding" in result.stdout
    structured = runner.invoke(
        app,
        [
            "job",
            "show",
            "job-1",
            "--server-url",
            "https://munchy.test",
            "--compact",
            "--json",
        ],
    )
    assert structured.exit_code == 0
    assert json.loads(structured.stdout)["job_id"] == "job-1"


def test_munchy_job_cancel_does_not_require_confirmation(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    class FakeClient:
        def __init__(self, base_url: str) -> None:
            assert base_url == "https://munchy.test"

        def cancel_job(self, job_id: str, *, cleanup: bool = False) -> dict[str, object]:
            assert job_id == "job-1"
            assert cleanup is False
            return {"job_id": "job-1", "state": "canceled"}

    monkeypatch.setattr("munchy_cli.main.MunchyClient", FakeClient)

    result = runner.invoke(app, ["job", "cancel", "job-1", "--server-url", "https://munchy.test"])

    assert result.exit_code == 0
    assert "canceled" in result.stdout
    structured = runner.invoke(
        app,
        ["job", "cancel", "job-1", "--server-url", "https://munchy.test", "--json"],
    )
    assert structured.exit_code == 0
    assert json.loads(structured.stdout)["state"] == "canceled"


def test_munchy_job_resume_has_human_and_json_receipts(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    class FakeClient:
        def __init__(self, base_url: str) -> None:
            assert base_url == "https://munchy.test"

        def resume_job(self, job_id: str) -> dict[str, object]:
            return {"job_id": job_id, "state": "queued", "phase": "queued"}

    monkeypatch.setattr("munchy_cli.main.MunchyClient", FakeClient)

    human = runner.invoke(app, ["job", "resume", "job-1", "--server-url", "https://munchy.test"])
    structured = runner.invoke(
        app,
        ["job", "resume", "job-1", "--server-url", "https://munchy.test", "--json"],
    )

    assert human.exit_code == structured.exit_code == 0
    assert "job-1" in human.stdout
    assert json.loads(structured.stdout)["job_id"] == "job-1"


def test_munchy_admin_operations_have_executable_human_json_parity(
    monkeypatch,
    tmp_path,
) -> None:  # type: ignore[no-untyped-def]
    template_path = tmp_path / "template.yaml"
    template_path.write_text("workflow_mode: collection_archive\n", encoding="utf-8")
    monkeypatch.setattr(
        munchy_main,
        "_job_template_definition",
        lambda _path: {"workflow_mode": "collection_archive"},
    )
    monkeypatch.setenv("RIVERHOG_CLI_PLAIN", "1")

    template = {
        "template_id": "review",
        "revision": 3,
        "enabled": True,
        "digest": "sha256:template",
        "definition": {"workflow_mode": "collection_archive"},
    }

    class FakeAdminClient:
        def __init__(self, base_url: str) -> None:
            assert base_url == "https://munchy.test"

        def list_apps(self, **_kwargs: object) -> dict[str, object]:
            return {
                "page": 1,
                "pages": 1,
                "per_page": 25,
                "total": 1,
                "apps": [{"name": "jeb", "keys": 1, "active_keys": 1}],
            }

        def create_app_key(self, app_name: str, **_kwargs: object) -> dict[str, object]:
            return {
                "id": "key-1",
                "app": app_name,
                "permissions": ["jobs:submit"],
                "status": "active",
                "token": "one-time-token",
            }

        def list_app_keys(self, app_name: str, **_kwargs: object) -> dict[str, object]:
            return {
                "app": app_name,
                "page": 1,
                "pages": 1,
                "per_page": 25,
                "total": 1,
                "keys": [{"id": "key-1", "status": "active"}],
            }

        def revoke_app_key(self, app_name: str, key_id: str) -> dict[str, object]:
            return {"app": app_name, "id": key_id, "status": "revoked"}

        def list_job_templates(self, **_kwargs: object) -> dict[str, object]:
            return {
                "page": 1,
                "pages": 1,
                "per_page": 25,
                "total": 1,
                "templates": [template],
            }

        def get_job_template(self, _template_id: str) -> dict[str, object]:
            return dict(template)

        def validate_job_template(
            self, template_id: str, _definition: dict[str, object]
        ) -> dict[str, object]:
            return {"template_id": template_id, "valid": True, "digest": "sha256:template"}

        def create_job_template(
            self, _template_id: str, _definition: dict[str, object], **_kwargs: object
        ) -> dict[str, object]:
            return dict(template)

        def replace_job_template(
            self, _template_id: str, _definition: dict[str, object], **_kwargs: object
        ) -> dict[str, object]:
            return {**template, "revision": 4}

        def enable_job_template(self, _template_id: str, **_kwargs: object) -> dict[str, object]:
            return {**template, "enabled": True}

        def disable_job_template(self, _template_id: str, **_kwargs: object) -> dict[str, object]:
            return {**template, "enabled": False}

    monkeypatch.setattr(munchy_main, "MunchyAdminClient", FakeAdminClient)

    cases = (
        (["app", "list"], "jeb"),
        (["app", "key", "create", "jeb", "--permission", "jobs:submit"], "key-1"),
        (["app", "key", "list", "jeb"], "key-1"),
        (["app", "key", "revoke", "jeb", "key-1"], "key-1"),
        (["template", "list"], "review"),
        (["template", "show", "review"], "review"),
        (["template", "check", "review", str(template_path)], "review"),
        (["template", "create", "review", str(template_path)], "review"),
        (["template", "replace", "review", str(template_path)], "review"),
        (["template", "enable", "review"], "review"),
        (["template", "disable", "review"], "review"),
    )
    for arguments, identity in cases:
        base = [*arguments, "--server-url", "https://munchy.test"]
        human = runner.invoke(app, base)
        structured = runner.invoke(app, [*base, "--json"])
        assert human.exit_code == 0, human.output
        assert structured.exit_code == 0, structured.output
        assert identity in human.stdout
        assert identity in json.dumps(json.loads(structured.stdout), sort_keys=True)

    listed = runner.invoke(
        app,
        ["app", "key", "list", "jeb", "--server-url", "https://munchy.test", "--json"],
    )
    assert "one-time-token" not in listed.stdout


def test_munchy_job_cleanup_accepts_cleaned_terminal_failure(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    cleaned = {
        "job_id": "job-1",
        "state": "failed",
        "phase": "routing",
        "cleanup_completed_at": "2026-01-01T00:00:00Z",
    }

    class FakeClient:
        def __init__(self, base_url: str) -> None:
            assert base_url == "https://munchy.test"

        def cancel_job(self, job_id: str, *, cleanup: bool = False) -> dict[str, object]:
            assert job_id == "job-1"
            assert cleanup is True
            return cleaned

        def wait_for_job(self, job_id: str, *, interval: float = 10.0) -> dict[str, object]:
            assert job_id == "job-1"
            return cleaned

    monkeypatch.setattr("munchy_cli.main.MunchyClient", FakeClient)

    result = runner.invoke(
        app,
        ["job", "cancel", "job-1", "--server-url", "https://munchy.test", "--cleanup"],
    )

    assert result.exit_code == 0
    assert "cleanup complete" in result.stdout


def test_munchy_submit_uses_server_template(monkeypatch, tmp_path) -> None:  # type: ignore[no-untyped-def]
    source = tmp_path / "clip.mp4"
    source.write_bytes(b"video")
    seen: dict[str, object] = {}
    awake_reasons: list[str] = []

    @contextlib.contextmanager
    def fake_keep_awake(reason: str):  # type: ignore[no-untyped-def]
        awake_reasons.append(reason)
        yield

    class FakeClient:
        def __init__(self, base_url: str) -> None:
            assert base_url == "https://munchy.test"

        def preflight_submission(self, request):  # type: ignore[no-untyped-def]
            seen["preflight_request"] = request
            return {
                "accepted": True,
                "template_id": request.template_id,
                "template_revision": 3,
                "template_digest": "digest",
                "workflow_mode": "collection_archive",
                "content_inspection": "after_upload",
            }

        def create_submission(self, request):  # type: ignore[no-untyped-def]
            seen["upload_request"] = request
            return {
                "submission_id": request.submission_id,
                "upload": {"state": "uploading"},
                "job": {"job_id": request.submission_id, "state": "queued"},
            }

        def upload_files(self, request):  # type: ignore[no-untyped-def]
            seen["uploaded"] = request.submission_id
            return {"state": "uploaded"}

        def wait_for_job(self, job_id: str, *, interval: float = 10.0):
            assert interval == 0.5
            seen["waited_job"] = job_id
            return {
                "job_id": job_id,
                "state": "succeeded",
                "phase": "done",
                "handoff": {"state": "complete", "safe_to_delete": True},
            }

    monkeypatch.setattr("munchy_cli.main.MunchyClient", FakeClient)
    monkeypatch.setattr("munchy_cli.main.keep_system_awake", fake_keep_awake)

    result = runner.invoke(
        app,
        [
            "submit",
            str(source),
            "--template",
            "camera-archive",
            "--input",
            "route=camera-main",
            "--server-url",
            "https://munchy.test",
            "--run-id",
            "20260621T120000.123456Z",
            "--no-hash-cache",
            "--interval",
            "0.5",
            "--json",
        ],
    )

    assert result.exit_code == 0
    preflight_request = seen["preflight_request"]
    upload_request = seen["upload_request"]
    assert preflight_request.template_id == "camera-archive"
    assert preflight_request.inputs == {"route": "camera-main"}
    assert preflight_request.run_id == "20260621T120000.123456Z"
    assert [item.rel_path for item in preflight_request.files] == ["clip.mp4"]
    assert seen["uploaded"] == upload_request.submission_id
    assert seen["waited_job"] == upload_request.submission_id
    assert awake_reasons == ["munchy submit"]
    payload = json.loads(result.stdout)
    assert payload["submission_id"] == upload_request.submission_id
    assert payload["upload"]["state"] == "uploaded"
    assert payload["job"]["state"] == "succeeded"


def test_munchy_submit_dry_run_preflights_without_creating_state(
    monkeypatch,
    tmp_path,
) -> None:  # type: ignore[no-untyped-def]
    source_dir = tmp_path / "phone"
    source_dir.mkdir()
    (source_dir / "IMG_0001.MOV").write_bytes(b"video")
    (source_dir / ".DS_Store").write_bytes(b"finder")
    seen: dict[str, object] = {}

    class FakeClient:
        def __init__(self, base_url: str) -> None:
            assert base_url == "https://munchy.test"

        def preflight_submission(self, request):  # type: ignore[no-untyped-def]
            seen["preflight_request"] = request
            return {
                "accepted": True,
                "template_id": request.template_id,
                "template_revision": 2,
                "template_digest": "digest",
                "workflow_mode": "review",
                "content_inspection": "after_upload",
            }

        def create_submission(self, request):  # type: ignore[no-untyped-def]
            raise AssertionError(f"dry-run created submission {request.submission_id}")

    monkeypatch.setattr("munchy_cli.main.MunchyClient", FakeClient)

    result = runner.invoke(
        app,
        [
            "submit",
            str(source_dir),
            "--template",
            "phone-review",
            "--server-url",
            "https://munchy.test",
            "--no-hash-cache",
            "--dry-run",
            "--json",
        ],
    )

    assert result.exit_code == 0
    preflight_request = seen["preflight_request"]
    assert [item.rel_path for item in preflight_request.files] == ["IMG_0001.MOV"]
    payload = json.loads(result.stdout)
    assert payload["status"] == "would_submit"
    assert payload["template_id"] == "phone-review"
    assert payload["template_revision"] == 2
    assert payload["workflow_mode"] == "review"
    human = runner.invoke(
        app,
        [
            "submit",
            str(source_dir),
            "--template",
            "phone-review",
            "--server-url",
            "https://munchy.test",
            "--no-hash-cache",
            "--dry-run",
        ],
    )
    assert human.exit_code == 0
    assert "phone-review" in human.stdout


def test_munchy_job_plan_review_sweep_reports_routes(tmp_path) -> None:  # type: ignore[no-untyped-def]
    source_dir = tmp_path / "camera"
    source_dir.mkdir()
    (source_dir / "clip.mp4").write_bytes(b"video")
    (source_dir / "photo.jpg").write_bytes(b"photo")
    config = tmp_path / "munchy-review.yaml"
    config.write_text(
        """
job:
  workflow_mode: review
  run_id: 20260712T120000Z
  handoff:
    destination: rclone
    options:
      location: review-remote:reviews/{template_id}/{route_id}/{profile_id}/{run_id}
  review:
    sweep:
      route_ids:
        - camera-video
      quality: 24..28:4
  routing:
    routes:
      - id: camera-video
        group: video
        when:
          path:
            suffix: .mp4
      - id: camera-photo
        group: preserve
        when:
          path:
            suffix: .jpg

profiles:
  video:
    schema_version: 1
    target: munchy-av1-nvenc
    name: video
    archive:
      codec: av1_nvenc
      container: webm
      quality: 40

groups:
  video:
    profile: video
    output_mode: video
    tasks:
      - archive_video
      - qcut_video
  preserve:
    output_mode: preserve
    tasks: []
""".strip(),
        encoding="utf-8",
    )

    result = runner.invoke(
        app,
        [
            "job",
            "plan-review-sweep",
            str(source_dir),
            "--template",
            "camera-review-sweep",
            "--config",
            str(config),
            "--json",
        ],
    )

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["kind"] == "munchy.review-sweep-plan"
    assert payload["template_id"] == "camera-review-sweep"
    assert payload["ok"] is True
    assert payload["requested_route_ids"] == ["camera-video"]
    assert payload["routes_total"] == 1
    assert payload["files_total"] == 1
    assert payload["routing"]["matched_files"] == 2
    route = payload["routes"][0]
    assert route["route_id"] == "camera-video"
    assert route["tasks"] == ["qcut_video"]
    assert [variant["profile_id"] for variant in route["variants"]] == ["q24", "q28"]
    assert route["variants"][1]["location"] == (
        "review-remote:reviews/camera-review-sweep/camera-video/q28/20260712T120000Z"
    )


def test_munchy_routing_explain_reports_matches(tmp_path) -> None:  # type: ignore[no-untyped-def]
    source_dir = tmp_path / "phone"
    source_dir.mkdir()
    (source_dir / "IMG_0001.MOV").write_bytes(b"video")
    config = tmp_path / "munchy.yaml"
    config.write_text(
        """
job:
  destination_prefix: phone
  handoff:
    destination: command
  routing:
    routes:
      - id: phone-video
        group: video
        into: phone/video
        when:
          path:
            prefix: phone
            suffix: .mov

groups:
  video:
    output_mode: video
    tasks:
      - archive_video
""".strip(),
        encoding="utf-8",
    )

    result = runner.invoke(
        app,
        ["routing", "explain", str(source_dir), "--config", str(config), "--json"],
    )

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["ok"] is True
    assert payload["matched_files"] == 1
    assert payload["matches"][0]["path"] == "phone/IMG_0001.MOV"
    assert payload["matches"][0]["route_id"] == "phone-video"
    assert payload["matches"][0]["group"] == "video"
    assert payload["matches"][0]["collection_rel_path"] == "phone/video/IMG_0001.MOV"


def test_munchy_routing_explain_uses_configured_sidecar_facts_only(
    monkeypatch,
    tmp_path,
) -> None:  # type: ignore[no-untyped-def]
    source_dir = tmp_path / "camera"
    source_dir.mkdir()
    (source_dir / "C0001.MP4").write_bytes(b"video")
    (source_dir / "C0001M01.XML").write_text("<metadata />", encoding="utf-8")
    config = tmp_path / "munchy.yaml"
    config.write_text(
        """
job:
  destination_prefix: camera
  handoff:
    destination: command
  routing:
    sidecars:
      camera_xml:
        format: xml
        path: "{parent}/{stem}M01.XML"
        primary:
          path:
            suffix: .mp4
        facts:
          source: exiftool
          tags:
            - Make
            - Model
    routes:
      - id: camera-video
        group: video
        when:
          all:
            - path:
                suffix: .mp4
            - fact: sidecars.camera_xml.facts.exif.make
              equals: example imaging

groups:
  video:
    output_mode: video
    tasks:
      - archive_video
""".strip(),
        encoding="utf-8",
    )
    exiftool_calls: list[tuple[str, tuple[str, ...]]] = []

    def fake_exiftool(path, *, tags):  # type: ignore[no-untyped-def]
        exiftool_calls.append((path.name, tuple(tags)))
        assert path.name == "C0001M01.XML"
        return {
            "EXIF:Make": "Example Imaging",
            "EXIF:Model": "Synthetic Camera",
        }

    monkeypatch.setattr("munchy_api_client.local_routing.exiftool_for_routing", fake_exiftool)

    result = runner.invoke(
        app,
        ["routing", "explain", str(source_dir), "--config", str(config), "--json"],
    )

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["ok"] is True
    assert payload["matches"][0]["route_id"] == "camera-video"
    assert payload["matches"][0]["matched_facts"] == {
        "sidecars.camera_xml.facts.exif.make": "example imaging"
    }
    assert exiftool_calls == [("C0001M01.XML", ("Make", "Model"))]


def test_munchy_routing_explain_skips_expensive_tools_for_path_only_route(
    monkeypatch,
    tmp_path,
) -> None:  # type: ignore[no-untyped-def]
    source_dir = tmp_path / "camera"
    source_dir.mkdir()
    (source_dir / "leinfo.sav").write_bytes(b"state")
    config = tmp_path / "munchy.yaml"
    config.write_text(
        """
job:
  destination_prefix: camera
  handoff:
    destination: command
  routing:
    routes:
      - id: device-state
        group: state
        when:
          path:
            filename_glob: leinfo.sav
      - id: camera-video
        group: video
        when:
          all:
            - path:
                suffix: .mp4
            - fact: video.codec
              equals: hevc
            - fact: exif.make
              equals: example imaging

groups:
  state:
    output_mode: preserve
    tasks: []
  video:
    output_mode: video
    tasks:
      - archive_video
""".strip(),
        encoding="utf-8",
    )

    def fail_probe(path):  # type: ignore[no-untyped-def]
        raise AssertionError(f"unexpected ffprobe call for {path}")

    def fail_exiftool(path, *, tags):  # type: ignore[no-untyped-def]
        raise AssertionError(f"unexpected exiftool call for {path} with {tags}")

    monkeypatch.setattr("munchy_api_client.local_routing.ffprobe_for_routing", fail_probe)
    monkeypatch.setattr("munchy_api_client.local_routing.exiftool_for_routing", fail_exiftool)

    result = runner.invoke(
        app,
        ["routing", "explain", str(source_dir), "--config", str(config), "--json"],
    )

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["ok"] is True
    assert payload["matches"][0]["route_id"] == "device-state"
