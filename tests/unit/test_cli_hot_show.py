from __future__ import annotations

import json
from typing import Any

from typer.testing import CliRunner

import riverhog_cli.main
from riverhog_cli.main import app

runner = CliRunner()


class _DoneFetchClient:
    def __init__(self) -> None:
        self.manifest_calls = 0

    def get_fetch(self, fetch_id: str) -> dict[str, Any]:
        raise AssertionError(f"summary should only be requested in JSON mode: {fetch_id}")

    def get_fetch_status(self, fetch_id: str) -> dict[str, Any]:
        return {
            "id": fetch_id,
            "name": "Docs",
            "targets": ["docs/"],
            "state": "done",
            "files": 3,
            "bytes": 30,
            "entries_total": 3,
            "entries_pending": 0,
            "entries_partial": 0,
            "entries_byte_complete": 0,
            "entries_uploaded": 3,
            "uploaded_bytes": 30,
            "missing_bytes": 0,
            "upload_state_expires_at": None,
            "discs": [],
            "entries_limit": 25,
            "entries_returned": 0,
            "entries": [],
        }

    def get_fetch_manifest(self, fetch_id: str) -> dict[str, Any]:
        self.manifest_calls += 1
        raise AssertionError(f"manifest should not be requested for done fetch {fetch_id}")


def test_hot_show_done_fetch_does_not_request_manifest(monkeypatch) -> None:
    fake_client = _DoneFetchClient()
    monkeypatch.setattr(riverhog_cli.main, "client", lambda: fake_client)
    monkeypatch.setenv("RIVERHOG_CLI_PLAIN", "1")

    result = runner.invoke(app, ["hot", "fetch", "show", "fx-done"])

    assert result.exit_code == 0
    assert "fetch: fx-done (done)" in result.stdout
    assert "entries: none" in result.stdout
    assert "pending:" not in result.stdout
    assert fake_client.manifest_calls == 0


def test_hot_list_passes_page_options_and_emits_paged_json(monkeypatch) -> None:
    class FakeClient:
        def list_fetches(
            self,
            *,
            page: int = 1,
            per_page: int = 25,
            state: str | None = None,
            query: str | None = None,
            sort: str = "order",
            order: str = "asc",
        ) -> dict[str, Any]:
            assert page == 2
            assert per_page == 1
            assert state is None
            assert query is None
            assert sort == "order"
            assert order == "asc"
            return {
                "page": page,
                "per_page": per_page,
                "total": 2,
                "pages": 2,
                "fetches": [
                    {
                        "id": "fx-2",
                        "name": "Photos",
                        "targets": ["docs/photos/"],
                        "state": "queued_djdan",
                        "files": 4,
                        "bytes": 40,
                        "missing_bytes": 40,
                        "discs": [],
                    }
                ],
            }

    monkeypatch.setattr(riverhog_cli.main, "client", FakeClient)

    result = runner.invoke(
        app,
        ["hot", "fetch", "list", "--page", "2", "--per-page", "1", "--json"],
    )

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["page"] == 2
    assert payload["per_page"] == 1
    assert payload["total"] == 2
    assert payload["fetches"][0]["id"] == "fx-2"


def test_hot_fetch_start_dry_run_renders_plan_without_followup_status(monkeypatch) -> None:
    class FakeClient:
        def __init__(self) -> None:
            self.calls: list[tuple[str, dict[str, object]]] = []

        def start_fetch(
            self,
            fetch_id: str,
            *,
            archive: bool = False,
            dry_run: bool = False,
        ) -> dict[str, object]:
            self.calls.append((fetch_id, {"archive": archive, "dry_run": dry_run}))
            return {
                "dry_run": True,
                "status": "would_queue_archive",
                "id": fetch_id,
                "name": "Docs",
                "targets": ["docs/"],
                "state": "draft",
                "queued_state": "queued_archive",
                "archive": True,
                "will_create_archive_restore": True,
                "files": 3,
                "bytes": 30,
                "missing_bytes": 20,
                "entries_total": 0,
                "entries_pending": 0,
                "entries_partial": 0,
                "entries_byte_complete": 0,
                "entries_uploaded": 0,
                "uploaded_bytes": 0,
                "upload_state_expires_at": None,
                "discs": [],
            }

        def get_fetch_status(self, fetch_id: str) -> dict[str, object]:
            raise AssertionError(f"dry-run should not request follow-up status: {fetch_id}")

    fake = FakeClient()
    monkeypatch.setattr(riverhog_cli.main, "client", lambda: fake)
    monkeypatch.setenv("RIVERHOG_CLI_PLAIN", "1")

    result = runner.invoke(app, ["hot", "fetch", "start", "fx-1", "--archive", "--dry-run"])

    assert result.exit_code == 0
    assert fake.calls == [("fx-1", {"archive": True, "dry_run": True})]
    assert "hot fetch start dry-run" in result.stdout
    assert "status: would_queue_archive" in result.stdout
    assert "queued state: queued_archive" in result.stdout


def test_hot_evict_dry_run_passes_flag_and_renders_plan(monkeypatch) -> None:
    class FakeClient:
        def __init__(self) -> None:
            self.calls: list[tuple[list[str], bool]] = []

        def evict_hot_targets(
            self,
            targets: list[str],
            *,
            dry_run: bool = False,
        ) -> dict[str, object]:
            self.calls.append((targets, dry_run))
            return {
                "targets": targets,
                "dry_run": dry_run,
                "status": "would_evict",
                "files": 2,
                "bytes": 33,
                "evicted_files": 0,
                "evicted_bytes": 0,
                "would_evict_files": 1,
                "would_evict_bytes": 12,
            }

    fake = FakeClient()
    monkeypatch.setattr(riverhog_cli.main, "client", lambda: fake)
    monkeypatch.setenv("RIVERHOG_CLI_PLAIN", "1")

    result = runner.invoke(app, ["hot", "evict", "docs/", "--dry-run"])

    assert result.exit_code == 0
    assert fake.calls == [(["docs/"], True)]
    assert "hot evict dry-run" in result.stdout
    assert "would evict: 1 files 12 bytes" in result.stdout
