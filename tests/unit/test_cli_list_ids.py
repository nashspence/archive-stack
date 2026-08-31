from __future__ import annotations

import json
from typing import Any

import riverhog_cli.main
from riverhog_cli.main import app
from typer.testing import CliRunner

runner = CliRunner()


def test_collection_list_ids_emits_one_pipeable_bounded_page(monkeypatch) -> None:
    class FakeClient:
        def list_collections(self, **kwargs: Any) -> dict[str, object]:
            assert kwargs["q"] == "camera"
            assert kwargs["tag"] == "photos"
            assert kwargs["encryption_format"] == "age-v1-scrypt"
            assert kwargs["passphrase_id"] == "fixture-archive-key-v2"
            assert kwargs["page_size"] == 10
            assert kwargs["page_token"] == "next-page"
            return {
                "collections": [{"id": 41}, {"id": 42}],
                "page_size": 10,
                "next_page_token": "later-page",
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
            "--encryption-format",
            "age-v1-scrypt",
            "--passphrase-id",
            "fixture-archive-key-v2",
            "--page-token",
            "next-page",
            "--page-size",
            "10",
            "--ids",
        ],
    )

    assert result.exit_code == 0
    assert result.stdout == "41\n42\n"


def test_collection_upload_list_ids_forwards_bounded_page_and_filters(monkeypatch) -> None:
    class FakeClient:
        def list_collection_upload_sessions(self, **kwargs: Any) -> dict[str, object]:
            assert kwargs == {
                "page_size": 25,
                "page_token": None,
                "q": "camera",
                "tag": "photos",
                "state": "uploading",
                "sort": "created_at",
                "order": "desc",
            }
            return {
                "uploads": [{"collection_id": 41}, {"collection_id": 42}],
                "page_size": 25,
                "next_page_token": None,
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
            "--ids",
        ],
    )

    assert result.exit_code == 0
    assert result.stdout == "41\n42\n"


def test_find_selectors_emits_pipeable_file_identities_from_one_page(monkeypatch) -> None:
    class FakeClient:
        def search(self, query: str | None, **kwargs: Any) -> dict[str, object]:
            assert query == "invoice"
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
                ],
                "page_size": 25,
                "next_page_token": None,
            }

    monkeypatch.setattr(riverhog_cli.main, "client", FakeClient)

    result = runner.invoke(app, ["find", "-q", "invoice", "--selectors"])

    assert result.exit_code == 0
    assert result.stdout == ("41::tax/invoice.pdf\n42::tax/invoice.pdf\n")
    human = runner.invoke(app, ["find", "-q", "invoice"])
    structured = runner.invoke(app, ["find", "-q", "invoice", "--json"])
    assert human.exit_code == structured.exit_code == 0
    assert "tax/invoice.pdf" in human.stdout
    assert json.loads(structured.stdout)["files"][0]["collection_id"] == 41


def test_riverhog_closes_its_shared_api_client(monkeypatch) -> None:
    closed: list[bool] = []

    class FakeClient:
        def list_collections(self, **_kwargs: Any) -> dict[str, object]:
            return {"collections": [], "page_size": 25, "next_page_token": None}

        def close(self) -> None:
            closed.append(True)

    monkeypatch.setattr(riverhog_cli.main, "_API_CLIENT", FakeClient())

    result = runner.invoke(app, ["collection", "list"])

    assert result.exit_code == 0
    assert closed == [True]
    assert riverhog_cli.main._API_CLIENT is None
