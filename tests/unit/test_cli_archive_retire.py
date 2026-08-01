from __future__ import annotations

import json

import riverhog_cli.main
from riverhog_cli.main import app
from typer.testing import CliRunner

runner = CliRunner()

_COLLECTION_ID = 1


def _plan() -> dict[str, object]:
    return {
        "status": "ready",
        "collection_id": _COLLECTION_ID,
        "store": "b2",
        "warning": "DANGER: This permanently removes one collection archive copy.",
        "expires_at": "2026-07-15T22:00:00Z",
        "challenge": "retire-copy-1-" + "a" * 64,
        "target_copy": {
            "store": "b2",
            "last_verified_at": "2026-07-15T00:00:00Z",
            "remote_storage_bytes": 28,
            "object_count": 3,
        },
        "retained_copies": [
            {
                "store": "deep",
                "last_verified_at": "2026-07-15T00:00:00Z",
                "remote_storage_bytes": 28,
            }
        ],
        "retired_retrieval_job_count": 0,
        "blockers": [],
        "verification_note": "Another store must pass remote verification.",
        "billing_note": "Provider billing may lag.",
    }


def test_archive_retire_plan_emits_data_loss_warning_and_challenge(monkeypatch) -> None:
    class FakeClient:
        def plan_archive_copy_retirement(
            self, collection_id: int, *, store: str
        ) -> dict[str, object]:
            assert (collection_id, store) == (_COLLECTION_ID, "b2")
            return _plan()

    monkeypatch.setattr(riverhog_cli.main, "client", FakeClient)

    result = runner.invoke(
        app,
        ["archive", "retire", str(_COLLECTION_ID), "--store", "b2", "--plan"],
    )

    assert result.exit_code == 0
    assert "permanently removes one collection archive copy" in result.stdout
    assert "retained copies: deep" in result.stdout
    assert "confirmation challenge: retire-copy-1-" in result.stdout


def test_archive_retire_interactive_requires_exact_collection_and_store(monkeypatch) -> None:
    calls: list[tuple[int, str, str]] = []

    class FakeClient:
        def plan_archive_copy_retirement(
            self, collection_id: int, *, store: str
        ) -> dict[str, object]:
            return _plan()

        def retire_archive_copy(
            self,
            collection_id: int,
            *,
            store: str,
            challenge: str,
        ) -> dict[str, object]:
            calls.append((collection_id, store, challenge))
            return {
                "status": "retired",
                "collection_id": collection_id,
                "store": store,
                "remote_storage_bytes": 28,
                "verified_store": "deep",
            }

    monkeypatch.setattr(riverhog_cli.main, "client", FakeClient)

    result = runner.invoke(
        app,
        ["archive", "retire", str(_COLLECTION_ID), "--store", "b2"],
        input=f"{_COLLECTION_ID}\nb2\n",
    )

    assert result.exit_code == 0
    assert calls == [(_COLLECTION_ID, "b2", "retire-copy-1-" + "a" * 64)]
    assert "verified retained store: deep" in result.stdout


def test_archive_retire_noninteractive_uses_prior_challenge(monkeypatch) -> None:
    challenge = "retire-copy-1-" + "b" * 64

    class FakeClient:
        def retire_archive_copy(
            self,
            collection_id: int,
            *,
            store: str,
            challenge: str,
        ) -> dict[str, object]:
            assert challenge == "retire-copy-1-" + "b" * 64
            return {
                "status": "retired",
                "collection_id": collection_id,
                "store": store,
                "remote_storage_bytes": 28,
                "verified_store": "deep",
            }

    monkeypatch.setattr(riverhog_cli.main, "client", FakeClient)

    result = runner.invoke(
        app,
        [
            "archive",
            "retire",
            str(_COLLECTION_ID),
            "--store",
            "b2",
            "--confirm",
            challenge,
            "--json",
        ],
    )

    assert result.exit_code == 0
    assert json.loads(result.stdout)["status"] == "retired"
