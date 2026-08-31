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
        "page_size": 25,
        "page_token": None,
        "total": 1,
        "next_page_token": None,
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
                "encryption_format": "age-v1-scrypt",
                "passphrase_id": "fixture-archive-key-v2",
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
    human = CliRunner().invoke(app, ["collection", "list"])

    assert result.exit_code == 0
    assert json.loads(result.stdout) == payload
    assert human.exit_code == 0
    assert "encryption=age-v1-scrypt:fixture-archive-key-v2" in human.stdout


def test_collection_show_human_and_json_use_one_identical_api_response(monkeypatch) -> None:
    payload = {
        "id": 42,
        "created_at": "2026-07-26T18:43:00.000000Z",
        "tags": ["family"],
        "files": 1,
        "bytes": 100,
        "remote_storage_bytes": 128,
        "encryption_format": "age-v1-scrypt",
        "passphrase_id": "fixture-archive-key-v2",
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
    assert "encryption: age-v1-scrypt:fixture-archive-key-v2" in human.stdout
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
        "page_size": 25,
        "page_token": None,
        "total": 1,
        "next_page_token": None,
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


def test_retrieval_cache_views_project_the_same_api_models_in_human_and_json(
    monkeypatch,
) -> None:
    cached = {
        "collection_id": 42,
        "source_store": "deep",
        "cache_store": "local",
        "object_id": "pack-000000000000",
        "state": "ready",
        "stored_bytes": 2048,
        "stored_sha256": "a" * 64,
        "cached_at": "2026-08-13T00:00:00.000000Z",
        "verified_at": "2026-08-13T00:00:01.000000Z",
        "protected_until": "2026-08-14T00:00:00.000000Z",
        "new_archive_expires_at": "2026-08-14T00:00:00.000000Z",
        "lease_categories": ["new_archive"],
        "retrieval_job_leases": 0,
        "tag_count": 1,
    }
    page = {
        "page_size": 25,
        "page_token": None,
        "total": 1,
        "next_page_token": None,
        "sort": "cached_at",
        "order": "desc",
        "query": None,
        "filters": {
            "tag": "photos",
            "collection_id": "42",
            "source_store": "deep",
            "cache_store": "local",
            "state": "ready",
            "protection": "protected",
            "expires_before": None,
            "expires_after": None,
        },
        "objects": [cached],
    }
    calls: list[tuple[str, object]] = []

    class FakeClient:
        def retrieval_cache_status(self) -> dict[str, object]:
            calls.append(("status", None))
            return {
                "configured": True,
                "new_archive_enabled": True,
                "objects": 1,
                "stored_bytes": 2048,
                "protected_objects": 1,
                "unleased_objects": 0,
                "stores": [
                    {
                        "cache_store": "local",
                        "priority": 1,
                        "admission_enabled": True,
                        "admission_budget_bytes": None,
                        "reserved_bytes": 0,
                        "committed_bytes": 2048,
                    }
                ],
                "policy": {
                    "new_archive_lease_seconds": 3600,
                    "retrieval_default_lease_seconds": 7200,
                    "retrieval_max_lease_seconds": 10800,
                    "pending_timeout_seconds": 14400,
                    "sweep_interval_seconds": 30,
                    "restore_poll_interval_seconds": 60,
                },
            }

        def list_retrieval_cache_objects(self, **kwargs: object) -> dict[str, object]:
            calls.append(("list", kwargs))
            return page

        def get_retrieval_cache_object(
            self,
            collection_id: int,
            source_store: str,
            object_id: str,
        ) -> dict[str, object]:
            calls.append(("show", (collection_id, source_store, object_id)))
            return cached

    monkeypatch.setattr(riverhog_cli.main, "client", FakeClient)
    runner = CliRunner()
    list_args = [
        "retrieval",
        "cache",
        "list",
        "--tag",
        "photos",
        "--collection",
        "42",
        "--source-store",
        "deep",
        "--cache-store",
        "local",
        "--state",
        "ready",
        "--protection",
        "protected",
    ]

    human_list = runner.invoke(app, list_args)
    json_list = runner.invoke(app, [*list_args, "--json"])
    human_show = runner.invoke(
        app,
        ["retrieval", "cache", "show", "42::deep::pack-000000000000"],
    )
    json_show = runner.invoke(
        app,
        ["retrieval", "cache", "show", "42::deep::pack-000000000000", "--json"],
    )
    human_status = runner.invoke(app, ["retrieval", "cache", "status"])
    json_status = runner.invoke(app, ["retrieval", "cache", "status", "--json"])

    assert human_list.exit_code == 0
    assert "state=ready" in human_list.stdout
    assert "cache=local" in human_list.stdout
    assert "protected-until=2026-08-14T00:00:00.000000Z" in human_list.stdout
    assert json.loads(json_list.stdout) == page
    assert human_show.exit_code == 0
    assert "state: ready" in human_show.stdout
    assert "cache store: local" in human_show.stdout
    assert "tags: 1" in human_show.stdout
    assert json.loads(json_show.stdout) == cached
    assert human_status.exit_code == json_status.exit_code == 0
    assert "retrieval cache" in human_status.stdout
    assert "budget=adapter-decided" in human_status.stdout
    assert json.loads(json_status.stdout)["configured"] is True
    assert calls == [
        (
            "list",
            {
                "page_size": 25,
                "page_token": None,
                "q": None,
                "tag": "photos",
                "collection_id": 42,
                "source_store": "deep",
                "cache_store": "local",
                "state": "ready",
                "protection": "protected",
                "expires_before": None,
                "expires_after": None,
                "sort": "cached_at",
                "order": "desc",
            },
        ),
        (
            "list",
            {
                "page_size": 25,
                "page_token": None,
                "q": None,
                "tag": "photos",
                "collection_id": 42,
                "source_store": "deep",
                "cache_store": "local",
                "state": "ready",
                "protection": "protected",
                "expires_before": None,
                "expires_after": None,
                "sort": "cached_at",
                "order": "desc",
            },
        ),
        ("show", (42, "deep", "pack-000000000000")),
        ("show", (42, "deep", "pack-000000000000")),
        ("status", None),
        ("status", None),
    ]


def test_emit_json_is_compact_machine_output(capsys: CaptureFixture[str]) -> None:
    emit({"b": [1, 2], "a": {"c": True}}, json_mode=True)

    stdout = capsys.readouterr().out
    assert stdout == '{"a":{"c":true},"b":[1,2]}\n'
    assert json.loads(stdout) == {"a": {"c": True}, "b": [1, 2]}
