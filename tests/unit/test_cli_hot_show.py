from __future__ import annotations

import json
from typing import Any

from typer.testing import CliRunner

import riverhog_cli.main
from riverhog_cli.main import app

runner = CliRunner()


def _status(fetch_id: str) -> dict[str, object]:
    return {
        "id": fetch_id,
        "name": "Docs",
        "collections": ["2025/20250102T030405Z__docs"],
        "state": "done",
        "files": 3,
        "bytes": 30,
        "hot_files": 3,
        "hot_bytes": 30,
        "missing_files": 0,
        "missing_bytes": 0,
        "next_action": {"action": "none", "reason": "all collections are hot"},
        "archive_restores": {"total": 0},
    }


def test_hot_fetch_show_renders_current_status(monkeypatch) -> None:
    class FakeClient:
        def get_fetch_status(self, fetch_id: str) -> dict[str, object]:
            return _status(fetch_id)

    monkeypatch.setattr(riverhog_cli.main, "client", FakeClient)

    result = runner.invoke(app, ["hot", "fetch", "show", "fx-1"])

    assert result.exit_code == 0
    assert "fetch fx-1" in result.stdout
    assert "hot: 3" in result.stdout


def test_hot_fetch_list_emits_paged_json(monkeypatch) -> None:
    class FakeClient:
        def list_fetches(self, **kwargs: Any) -> dict[str, object]:
            assert kwargs["page"] == 2
            return {
                "page": 2,
                "per_page": 1,
                "total": 2,
                "pages": 2,
                "fetches": [_status("fx-2")],
            }

    monkeypatch.setattr(riverhog_cli.main, "client", FakeClient)
    result = runner.invoke(
        app,
        ["hot", "fetch", "list", "--page", "2", "--per-page", "1", "--json"],
    )

    assert result.exit_code == 0
    assert json.loads(result.stdout)["fetches"][0]["id"] == "fx-2"


def test_hot_fetch_start_follows_with_status(monkeypatch) -> None:
    class FakeClient:
        def start_fetch(self, fetch_id: str) -> dict[str, object]:
            return {**_status(fetch_id), "state": "done"}

        def get_fetch_status(self, fetch_id: str) -> dict[str, object]:
            return _status(fetch_id)

    monkeypatch.setattr(riverhog_cli.main, "client", FakeClient)
    result = runner.invoke(app, ["hot", "fetch", "start", "fx-1"])

    assert result.exit_code == 0
    assert "state: done" in result.stdout


def test_hot_evict_dry_run_renders_selection(monkeypatch) -> None:
    class FakeClient:
        def evict_hot(
            self,
            collections: list[str],
            *,
            files: list[tuple[str, str]],
            dry_run: bool = False,
        ) -> dict[str, object]:
            return {
                "collections": collections,
                "files": files,
                "dry_run": dry_run,
                "status": "would_evict",
                "selected_files": 2,
                "selected_bytes": 33,
                "evicted_files": 0,
                "evicted_bytes": 0,
                "would_evict_files": 1,
                "would_evict_bytes": 12,
            }

    monkeypatch.setattr(riverhog_cli.main, "client", FakeClient)
    result = runner.invoke(
        app,
        ["hot", "evict", "2025/20250102T030405Z__docs", "--dry-run"],
    )

    assert result.exit_code == 0
    assert "hot eviction: would_evict" in result.stdout
    assert "affected: 1" in result.stdout
