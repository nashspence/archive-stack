from __future__ import annotations

from typing import Any

import riverhog_cli.main
from riverhog_cli.main import app
from typer.testing import CliRunner

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
                    {"id": "alpha/20250101T000000Z"},
                    {"id": "beta/20250102T000000Z"},
                ],
            }

    monkeypatch.setattr(riverhog_cli.main, "client", FakeClient)

    result = runner.invoke(
        app,
        ["collection", "list", "--query", "2025/", "--all", "--ids"],
    )

    assert result.exit_code == 0
    assert result.stdout == ("alpha/20250101T000000Z\nbeta/20250102T000000Z\n")


def test_find_all_selectors_emits_pipeable_file_identities(monkeypatch) -> None:
    class FakeClient:
        def search(self, query: str | None, **kwargs: Any) -> dict[str, object]:
            assert query == "invoice"
            assert kwargs["all_items"] is True
            return {
                "files": [
                    {
                        "collection_id": "docs/20250101T000000Z",
                        "collection_path": "tax/invoice.pdf",
                    },
                    {
                        "collection_id": "docs/20250102T000000Z",
                        "collection_path": "tax/invoice.pdf",
                    },
                ]
            }

    monkeypatch.setattr(riverhog_cli.main, "client", FakeClient)

    result = runner.invoke(app, ["find", "-q", "invoice", "--all", "--selectors"])

    assert result.exit_code == 0
    assert result.stdout == (
        "docs/20250101T000000Z::tax/invoice.pdf\ndocs/20250102T000000Z::tax/invoice.pdf\n"
    )


def test_riverhog_closes_its_shared_api_client(monkeypatch) -> None:
    closed: list[bool] = []

    class FakeClient:
        def list_collections(self, **_kwargs: Any) -> dict[str, object]:
            return {"collections": [], "page": 1, "pages": 0, "total": 0}

        def close(self) -> None:
            closed.append(True)

    monkeypatch.setattr(riverhog_cli.main, "_API_CLIENT", FakeClient())

    result = runner.invoke(app, ["collection", "list"])

    assert result.exit_code == 0
    assert closed == [True]
    assert riverhog_cli.main._API_CLIENT is None
