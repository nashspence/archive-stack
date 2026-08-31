from __future__ import annotations

import json

import riverhog_cli.main
from riverhog_cli.main import app
from typer.testing import CliRunner

runner = CliRunner()


def test_archive_copy_selects_destination_and_optional_source(monkeypatch) -> None:
    calls: list[tuple[int, str, str | None]] = []

    class FakeClient:
        def create_or_resume_archive_copy(
            self,
            collection_id: int,
            *,
            destination_store: str,
            source_store: str | None = None,
            **_kwargs: object,
        ) -> dict[str, object]:
            calls.append((collection_id, destination_store, source_store))
            return {
                "collection_id": collection_id,
                "source_store": source_store,
                "destination_store": destination_store,
                "state": "requested",
                "requested_at": "2026-07-15T00:00:00Z",
                "ready_at": None,
                "expires_at": None,
                "failure": None,
            }

    monkeypatch.setattr(riverhog_cli.main, "client", FakeClient)

    result = runner.invoke(
        app,
        [
            "archive",
            "copy",
            "start",
            "1",
            "--from",
            "b2",
            "--to",
            "deep",
            "--json",
        ],
    )

    assert result.exit_code == 0
    assert json.loads(result.stdout)["state"] == "requested"
    human = runner.invoke(
        app,
        ["archive", "copy", "start", "1", "--from", "b2", "--to", "deep"],
    )
    assert human.exit_code == 0
    assert "deep" in human.stdout
    assert calls == [(1, "deep", "b2"), (1, "deep", "b2")]


def test_archive_copy_list_and_show_share_server_job_models(monkeypatch) -> None:
    calls: list[str] = []
    job = {
        "collection_id": 1,
        "source_store": "b2",
        "destination_store": "deep",
        "initiated_by_app": "operator",
        "initiated_by_key_id": "key",
        "state": "completed",
        "requested_at": "2026-07-15T00:00:00Z",
        "ready_at": None,
        "expires_at": None,
        "completed_at": "2026-07-15T00:01:00Z",
        "failure": None,
    }

    class FakeClient:
        def list_archive_copy_jobs(self, **_kwargs: object) -> dict[str, object]:
            calls.append("list")
            return {
                "page_size": 25,
                "page_token": None,
                "total": 1,
                "next_page_token": None,
                "sort": "requested_at",
                "order": "desc",
                "query": None,
                "filters": {},
                "copies": [job],
            }

        def get_archive_copy_job(
            self,
            collection_id: int,
            *,
            destination_store: str,
        ) -> dict[str, object]:
            assert (collection_id, destination_store) == (1, "deep")
            calls.append("show")
            return job

    monkeypatch.setattr(riverhog_cli.main, "client", FakeClient)

    human_list = runner.invoke(app, ["archive", "copy", "list"])
    listed = runner.invoke(app, ["archive", "copy", "list", "--json"])
    human_show = runner.invoke(app, ["archive", "copy", "show", "1::deep"])
    shown = runner.invoke(app, ["archive", "copy", "show", "1::deep", "--json"])

    assert human_list.exit_code == 0
    assert "b2 -> deep" in human_list.stdout
    assert listed.exit_code == 0
    assert json.loads(listed.stdout)["copies"] == [job]
    assert human_show.exit_code == 0
    assert "initiator: operator/key" in human_show.stdout
    assert shown.exit_code == 0
    assert json.loads(shown.stdout) == job
    assert calls == ["list", "list", "show", "show"]


def test_archive_copy_list_selectors_cancel_and_watch_are_actionable(monkeypatch) -> None:
    states = iter(("copying", "completed", "copying", "completed"))
    calls: list[object] = []

    def job(state: str) -> dict[str, object]:
        return {
            "collection_id": 7,
            "source_store": "b2",
            "destination_store": "deep",
            "state": state,
            "requested_at": "2026-07-15T00:00:00Z",
            "completed_at": None,
            "failure": None,
        }

    class FakeClient:
        def list_archive_copy_jobs(self, **kwargs: object) -> dict[str, object]:
            calls.append(("list", kwargs))
            return {
                "page_size": 1,
                "page_token": None,
                "total": 1,
                "next_page_token": None,
                "sort": "requested_at",
                "order": "desc",
                "query": None,
                "filters": {"state": "waiting"},
                "copies": [job("waiting")],
            }

        def cancel_archive_copy_job(
            self, collection_id: int, *, destination_store: str
        ) -> dict[str, object]:
            calls.append(("cancel", collection_id, destination_store))
            return job("canceled")

        def get_archive_copy_job(
            self, collection_id: int, *, destination_store: str
        ) -> dict[str, object]:
            calls.append(("watch", collection_id, destination_store))
            return job(next(states))

    monkeypatch.setattr(riverhog_cli.main, "client", FakeClient)
    monkeypatch.setattr(riverhog_cli.main.time, "sleep", lambda _seconds: None)

    listed = runner.invoke(
        app,
        ["archive", "copy", "list", "--state", "waiting", "--selectors"],
    )
    canceled = runner.invoke(app, ["archive", "copy", "cancel", "7::deep", "--json"])
    watched = runner.invoke(
        app,
        ["archive", "copy", "watch", "7::deep", "--interval", "0.1", "--json"],
    )

    assert listed.exit_code == 0
    assert listed.stdout == "7::deep\n"
    assert canceled.exit_code == 0
    assert json.loads(canceled.stdout)["state"] == "canceled"
    canceled_human = runner.invoke(app, ["archive", "copy", "cancel", "7::deep"])
    assert canceled_human.exit_code == 0
    assert "canceled" in canceled_human.stdout
    assert watched.exit_code == 0
    assert json.loads(watched.stdout)["state"] == "completed"
    watched_human = runner.invoke(
        app,
        ["archive", "copy", "watch", "7::deep", "--interval", "0.1"],
    )
    assert watched_human.exit_code == 0
    assert "completed" in watched_human.stdout
    assert calls[0][1]["state"] == "waiting"
