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
    assert calls == [(1, "deep", "b2")]
