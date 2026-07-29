from __future__ import annotations

import json

import riverhog_cli.main
from pytest import CaptureFixture
from riverhog_cli.main import app
from riverhog_cli_support.output import emit
from typer.testing import CliRunner


def test_collection_list_json_emits_the_api_response_without_a_second_model(
    monkeypatch,
) -> None:
    payload = {
        "page": 1,
        "per_page": 25,
        "total": 1,
        "pages": 1,
        "sort": "id",
        "order": "asc",
        "query": None,
        "collections": [
            {
                "id": 42,
                "created_at": "2026-07-26T18:43:00.000000Z",
                "tags": ["family", "sony-a6700"],
                "files": 2,
                "bytes": 100,
                "remote_storage_bytes": 128,
                "archive_copies": [
                    {
                        "store": "deep",
                        "state": "uploaded",
                        "storage_class": "DEEP_ARCHIVE",
                        "stored_bytes": 128,
                        "storage_prefix": "riverhog/archives/opaque",
                    }
                ],
            }
        ],
    }

    class FakeClient:
        def list_collections(self, **_kwargs: object) -> dict[str, object]:
            return payload

    monkeypatch.setattr(riverhog_cli.main, "client", FakeClient)
    result = CliRunner().invoke(app, ["collection", "list", "--json"])

    assert result.exit_code == 0
    assert json.loads(result.stdout) == payload


def test_collection_show_human_and_json_use_one_identical_api_response(monkeypatch) -> None:
    payload = {
        "id": 42,
        "created_at": "2026-07-26T18:43:00.000000Z",
        "tags": ["family"],
        "files": 1,
        "bytes": 100,
        "remote_storage_bytes": 128,
        "archive_copies": [{"store": "deep", "state": "uploaded"}],
    }
    calls: list[int] = []

    class FakeClient:
        def get_collection(self, collection_id: int) -> dict[str, object]:
            calls.append(collection_id)
            return payload

    monkeypatch.setattr(riverhog_cli.main, "client", FakeClient)
    runner = CliRunner()
    human = runner.invoke(app, ["collection", "show", "42"])
    machine = runner.invoke(app, ["collection", "show", "42", "--json"])

    assert human.exit_code == 0
    assert "remote storage: 128 B" in human.stdout
    assert machine.exit_code == 0
    assert json.loads(machine.stdout) == payload
    assert calls == [42, 42]


def test_archive_store_views_project_the_same_api_models_in_human_and_json(
    monkeypatch,
) -> None:
    store = {
        "store": "deep",
        "backend": "aws",
        "storage_class": "DEEP_ARCHIVE",
        "read_mode": "restore_required",
        "read_priority": 2,
        "write_target": False,
        "collections": 2,
        "objects": 9,
        "stored_bytes": 2048,
        "download_allowance": None,
    }
    page = {
        "page": 1,
        "per_page": 25,
        "total": 1,
        "pages": 1,
        "sort": "store",
        "order": "asc",
        "query": None,
        "stores": [store],
    }
    calls: list[str] = []

    class FakeClient:
        def list_archive_stores(self, **_kwargs: object) -> dict[str, object]:
            calls.append("list")
            return page

        def get_archive_store(self, name: str) -> dict[str, object]:
            calls.append(f"show:{name}")
            return store

    monkeypatch.setattr(riverhog_cli.main, "client", FakeClient)
    runner = CliRunner()

    human_list = runner.invoke(app, ["archive", "store", "list"])
    json_list = runner.invoke(app, ["archive", "store", "list", "--json"])
    human_show = runner.invoke(app, ["archive", "store", "show", "deep"])
    json_show = runner.invoke(app, ["archive", "store", "show", "deep", "--json"])

    assert human_list.exit_code == 0
    assert "collections=2" in human_list.stdout
    assert "read-priority=2" in human_list.stdout
    assert json.loads(json_list.stdout) == page
    assert human_show.exit_code == 0
    assert "stored: 2.0 KB" in human_show.stdout
    assert "read priority: 2" in human_show.stdout
    assert json.loads(json_show.stdout) == store
    assert calls == ["list", "list", "show:deep", "show:deep"]


def test_emit_json_is_compact_machine_output(capsys: CaptureFixture[str]) -> None:
    emit({"b": [1, 2], "a": {"c": True}}, json_mode=True)

    stdout = capsys.readouterr().out
    assert stdout == '{"a":{"c":true},"b":[1,2]}\n'
    assert json.loads(stdout) == {"a": {"c": True}, "b": [1, 2]}
