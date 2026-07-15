from __future__ import annotations

from typing import Any

from typer.testing import CliRunner

import riverhog_cli.main
from riverhog_cli.main import app

runner = CliRunner()


def test_collection_list_all_ids_emits_pipeable_database_results(monkeypatch) -> None:
    class FakeClient:
        def list_collections(self, **kwargs: Any) -> dict[str, object]:
            assert kwargs["all_items"] is True
            assert kwargs["q"] == "2025/"
            return {
                "page": 1,
                "per_page": 2,
                "total": 2,
                "pages": 1,
                "collections": [
                    {"id": "2025/20250101T000000Z__alpha"},
                    {"id": "2025/20250102T000000Z__beta"},
                ],
            }

    monkeypatch.setattr(riverhog_cli.main, "client", FakeClient)

    result = runner.invoke(
        app,
        ["collection", "list", "--query", "2025/", "--all", "--ids"],
    )

    assert result.exit_code == 0
    assert result.stdout == ("2025/20250101T000000Z__alpha\n2025/20250102T000000Z__beta\n")


def test_fetch_list_all_ids_uses_the_same_pipeable_shape(monkeypatch) -> None:
    class FakeClient:
        def list_fetches(self, **kwargs: Any) -> dict[str, object]:
            assert kwargs["all_items"] is True
            assert kwargs["query"] == "docs"
            return {
                "page": 1,
                "per_page": 2,
                "total": 2,
                "pages": 1,
                "fetches": [{"id": "fx-1"}, {"id": "fx-2"}],
            }

    monkeypatch.setattr(riverhog_cli.main, "client", FakeClient)

    result = runner.invoke(
        app,
        ["hot", "fetch", "list", "--query", "docs", "--all", "--ids"],
    )

    assert result.exit_code == 0
    assert result.stdout == "fx-1\nfx-2\n"
