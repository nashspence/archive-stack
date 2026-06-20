from __future__ import annotations

from typing import Any

from typer.testing import CliRunner

import riverhog_cli.main
from riverhog_cli.main import app

runner = CliRunner()


class _DoneFetchClient:
    def __init__(self) -> None:
        self.manifest_calls = 0

    def get_fetch(self, fetch_id: str) -> dict[str, Any]:
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
