from __future__ import annotations

import json
from typing import Any

import riverhog_cli.main
from riverhog_cli.main import app
from typer.testing import CliRunner

runner = CliRunner()


def test_collection_list_all_ids_emits_pipeable_database_results(monkeypatch) -> None:
    class FakeClient:
        def list_collections(self, **kwargs: Any) -> dict[str, object]:
            assert kwargs["all_items"] is True
            assert kwargs["q"] == "camera"
            assert kwargs["tag"] == "photos"
            return {
                "page": 1,
                "per_page": 2,
                "total": 2,
                "pages": 1,
                "collections": [
                    {"id": 41},
                    {"id": 42},
                ],
            }

    monkeypatch.setattr(riverhog_cli.main, "client", FakeClient)

    result = runner.invoke(
        app,
        [
            "collection",
            "list",
            "--query",
            "camera",
            "--tag",
            "photos",
            "--all",
            "--ids",
        ],
    )

    assert result.exit_code == 0
    assert result.stdout == "41\n42\n"


def test_collection_upload_list_all_ids_forwards_database_filters(monkeypatch) -> None:
    class FakeClient:
        def list_collection_upload_sessions(self, **kwargs: Any) -> dict[str, object]:
            assert kwargs == {
                "page": 1,
                "per_page": 25,
                "q": "camera",
                "tag": "photos",
                "state": "uploading",
                "sort": "created_at",
                "order": "desc",
                "all_items": True,
            }
            return {
                "page": 1,
                "per_page": 2,
                "total": 2,
                "pages": 1,
                "uploads": [
                    {"collection_id": 41},
                    {"collection_id": 42},
                ],
            }

    monkeypatch.setattr(riverhog_cli.main, "client", FakeClient)

    result = runner.invoke(
        app,
        [
            "collection",
            "upload",
            "list",
            "--query",
            "camera",
            "--tag",
            "photos",
            "--state",
            "uploading",
            "--all",
            "--ids",
        ],
    )

    assert result.exit_code == 0
    assert result.stdout == "41\n42\n"


def test_find_all_selectors_emits_pipeable_file_identities(monkeypatch) -> None:
    class FakeClient:
        def search(self, query: str | None, **kwargs: Any) -> dict[str, object]:
            assert query == "invoice"
            assert kwargs["all_items"] is True
            return {
                "files": [
                    {
                        "collection_id": 41,
                        "path": "tax/invoice.pdf",
                        "file_ref": "41/tax/invoice.pdf",
                    },
                    {
                        "collection_id": 42,
                        "path": "tax/invoice.pdf",
                        "file_ref": "42/tax/invoice.pdf",
                    },
                ]
            }

    monkeypatch.setattr(riverhog_cli.main, "client", FakeClient)

    result = runner.invoke(app, ["find", "-q", "invoice", "--all", "--selectors"])

    assert result.exit_code == 0
    assert result.stdout == ("41::tax/invoice.pdf\n42::tax/invoice.pdf\n")
    human = runner.invoke(app, ["find", "-q", "invoice", "--all"])
    structured = runner.invoke(app, ["find", "-q", "invoice", "--all", "--json"])
    assert human.exit_code == structured.exit_code == 0
    assert "tax/invoice.pdf" in human.stdout
    assert json.loads(structured.stdout)["files"][0]["collection_id"] == 41


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
