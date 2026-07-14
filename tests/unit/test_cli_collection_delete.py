from __future__ import annotations

import json

from typer.testing import CliRunner

import riverhog_cli.main
from riverhog_cli.main import app

runner = CliRunner()


def _plan() -> dict[str, object]:
    return {
        "status": "ready",
        "collection_id": "docs",
        "warning": "DANGER: These encrypted objects are the sole durable copies.",
        "expires_at": "2026-07-14T22:00:00Z",
        "challenge": "delete-1-" + "a" * 64,
        "files": [{"path": "readme.txt", "bytes": 12, "hot": True}],
        "file_count": 1,
        "bytes": 12,
        "hot_objects": [{"path": "readme.txt", "bytes": 12}],
        "hot_files": 1,
        "hot_bytes": 12,
        "archive_objects": [
            {"kind": "archive", "object_path": "archive.tar.age", "stored_bytes": 20},
            {"kind": "manifest", "object_path": "manifest.yml.age", "stored_bytes": 5},
            {"kind": "proof", "object_path": "manifest.yml.ots.age", "stored_bytes": 3},
        ],
        "remote_storage_bytes": 28,
        "upload_files": [],
        "archive_restores": [],
        "metadata_rows": {"collections": 1},
        "blockers": [],
        "billing_note": "Provider billing may lag.",
    }


def test_collection_delete_dry_run_emits_warning_and_challenge(monkeypatch) -> None:
    class FakeClient:
        def plan_collection_deletion(self, collection_id: str) -> dict[str, object]:
            assert collection_id == "docs"
            return _plan()

    monkeypatch.setattr(riverhog_cli.main, "client", FakeClient)

    result = runner.invoke(app, ["collection", "delete", "docs", "--dry-run"])

    assert result.exit_code == 0
    assert "sole durable copies" in result.stdout
    assert "confirmation challenge: delete-1-" in result.stdout
    assert "archive objects: 3" in result.stdout


def test_collection_delete_interactive_requires_exact_id_after_warning(monkeypatch) -> None:
    calls: list[tuple[str, str]] = []

    class FakeClient:
        def plan_collection_deletion(self, collection_id: str) -> dict[str, object]:
            assert collection_id == "docs"
            return _plan()

        def delete_collection(
            self, collection_id: str, *, challenge: str
        ) -> dict[str, object]:
            calls.append((collection_id, challenge))
            return {
                "status": "deleted",
                "collection_id": collection_id,
                "files": 1,
                "bytes": 12,
                "remote_storage_bytes": 28,
            }

    monkeypatch.setattr(riverhog_cli.main, "client", FakeClient)

    result = runner.invoke(app, ["collection", "delete", "docs"], input="docs\n")

    assert result.exit_code == 0
    assert result.stdout.index("sole durable copies") < result.stdout.index(
        "Type the complete collection id"
    )
    assert calls == [("docs", "delete-1-" + "a" * 64)]
    assert "collection deletion: deleted" in result.stdout


def test_collection_delete_interactive_mismatch_stops_before_execution(monkeypatch) -> None:
    class FakeClient:
        def plan_collection_deletion(self, collection_id: str) -> dict[str, object]:
            return _plan()

        def delete_collection(self, collection_id: str, *, challenge: str) -> dict[str, object]:
            raise AssertionError((collection_id, challenge))

    monkeypatch.setattr(riverhog_cli.main, "client", FakeClient)

    result = runner.invoke(app, ["collection", "delete", "docs"], input="other\n")

    assert result.exit_code == 1
    assert "nothing was deleted" in result.output


def test_collection_delete_noninteractive_uses_prior_challenge(monkeypatch) -> None:
    challenge = "delete-1-" + "b" * 64

    class FakeClient:
        def plan_collection_deletion(self, collection_id: str) -> dict[str, object]:
            raise AssertionError(collection_id)

        def delete_collection(
            self, collection_id: str, *, challenge: str
        ) -> dict[str, object]:
            assert challenge == "delete-1-" + "b" * 64
            return {
                "status": "deleted",
                "collection_id": collection_id,
                "files": 1,
                "bytes": 12,
                "remote_storage_bytes": 28,
            }

    monkeypatch.setattr(riverhog_cli.main, "client", FakeClient)

    result = runner.invoke(
        app,
        ["collection", "delete", "docs", "--confirm", challenge, "--json"],
    )

    assert result.exit_code == 0
    assert json.loads(result.stdout)["status"] == "deleted"
