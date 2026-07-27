from __future__ import annotations

import json

import riverhog_cli.main
from riverhog_cli.main import app
from typer.testing import CliRunner

runner = CliRunner()
COLLECTION_ID = 1


def _plan() -> dict[str, object]:
    return {
        "status": "ready",
        "collection_id": COLLECTION_ID,
        "warning": "DANGER: These encrypted objects are the sole durable copies.",
        "expires_at": "2026-07-14T22:00:00Z",
        "challenge": "delete-1-" + "a" * 64,
        "file_count": 1,
        "bytes": 12,
        "archive_copies": [{"store": "b2", "objects": 3, "stored_bytes": 28}],
        "archive_object_count": 3,
        "remote_storage_bytes": 28,
        "upload_file_count": 0,
        "metadata_rows": {"collections": 1},
        "blockers": [],
        "billing_note": "Provider billing may lag.",
    }


def test_collection_delete_dry_run_emits_warning_and_challenge(monkeypatch) -> None:
    class FakeClient:
        def plan_collection_deletion(self, collection_id: int) -> dict[str, object]:
            assert collection_id == COLLECTION_ID
            return _plan()

    monkeypatch.setattr(riverhog_cli.main, "client", FakeClient)

    result = runner.invoke(app, ["collection", "delete", str(COLLECTION_ID), "--dry-run"])

    assert result.exit_code == 0
    assert "sole durable copies" in result.stdout
    assert "confirmation challenge: delete-1-" in result.stdout
    assert "archive objects: 3" in result.stdout


def test_collection_delete_interactive_requires_exact_id_after_warning(monkeypatch) -> None:
    calls: list[tuple[int, str]] = []

    class FakeClient:
        def plan_collection_deletion(self, collection_id: int) -> dict[str, object]:
            assert collection_id == COLLECTION_ID
            return _plan()

        def delete_collection(self, collection_id: int, *, challenge: str) -> dict[str, object]:
            calls.append((collection_id, challenge))
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
        ["collection", "delete", str(COLLECTION_ID)],
        input=f"{COLLECTION_ID}\n",
    )

    assert result.exit_code == 0
    assert result.stdout.index("sole durable copies") < result.stdout.index(
        "Type the complete collection id"
    )
    assert calls == [(COLLECTION_ID, "delete-1-" + "a" * 64)]
    assert "collection deletion: deleted" in result.stdout


def test_collection_delete_interactive_mismatch_stops_before_execution(monkeypatch) -> None:
    class FakeClient:
        def plan_collection_deletion(self, collection_id: int) -> dict[str, object]:
            return _plan()

        def delete_collection(self, collection_id: int, *, challenge: str) -> dict[str, object]:
            raise AssertionError((collection_id, challenge))

    monkeypatch.setattr(riverhog_cli.main, "client", FakeClient)

    result = runner.invoke(app, ["collection", "delete", str(COLLECTION_ID)], input="2\n")

    assert result.exit_code == 1
    assert "nothing was deleted" in result.output


def test_collection_delete_noninteractive_uses_prior_challenge(monkeypatch) -> None:
    challenge = "delete-1-" + "b" * 64

    class FakeClient:
        def plan_collection_deletion(self, collection_id: int) -> dict[str, object]:
            raise AssertionError(collection_id)

        def delete_collection(self, collection_id: int, *, challenge: str) -> dict[str, object]:
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
        ["collection", "delete", str(COLLECTION_ID), "--confirm", challenge, "--json"],
    )

    assert result.exit_code == 0
    assert json.loads(result.stdout)["status"] == "deleted"
