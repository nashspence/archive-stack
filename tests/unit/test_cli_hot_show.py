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
            "target": "docs/",
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
            "copies": [],
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

    result = runner.invoke(app, ["hot", "show", "fx-done"])

    assert result.exit_code == 0
    assert "fetch: fx-done (done)" in result.stdout
    assert "pending:" in result.stdout
    assert fake_client.manifest_calls == 0


def test_hot_list_passes_page_options_and_emits_paged_json(monkeypatch) -> None:
    class FakeClient:
        def list_pins(self, *, page: int = 1, per_page: int = 25) -> dict[str, Any]:
            assert page == 2
            assert per_page == 1
            return {
                "page": page,
                "per_page": per_page,
                "total": 2,
                "pages": 2,
                "pins": [
                    {
                        "target": "docs/photos/",
                        "fetch": {
                            "id": "fx-2",
                            "state": "waiting_media",
                            "files": 4,
                            "bytes": 40,
                            "missing_bytes": 40,
                            "copy_count": 0,
                            "copies": [],
                        },
                    }
                ],
            }

    monkeypatch.setattr(riverhog_cli.main, "client", FakeClient)

    result = runner.invoke(app, ["hot", "list", "--page", "2", "--per-page", "1", "--json"])

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["page"] == 2
    assert payload["per_page"] == 1
    assert payload["total"] == 2
    assert payload["pins"][0]["fetch"]["id"] == "fx-2"
