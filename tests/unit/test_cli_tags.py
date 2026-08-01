from __future__ import annotations

import json
from typing import Any

import riverhog_cli.main
from riverhog_cli.main import app
from typer.testing import CliRunner

runner = CliRunner()


def test_tag_delete_plan_exposes_actionable_bounded_dependencies(monkeypatch) -> None:
    class FakeClient:
        def plan_tag_deletion(self, tag: str) -> dict[str, object]:
            assert tag == "photos"
            return {
                "status": "blocked",
                "tag": tag,
                "warning": "Confirm external references are gone.",
                "expires_at": "2026-08-01T00:15:00.000000Z",
                "challenge": None,
                "dependencies": {
                    "collections": {"count": 2, "sample": ["41", "42"]},
                    "upload_sessions": {"count": 0, "sample": []},
                    "app_key_access": {
                        "count": 1,
                        "sample": ["reader/key-one/catalog:read"],
                    },
                    "metadata_publications": {"count": 0, "sample": []},
                },
                "blockers": [
                    "2 collection(s) still use this tag; run riverhog collection list --tag photos"
                ],
            }

    monkeypatch.setattr(riverhog_cli.main, "client", FakeClient)

    result = runner.invoke(app, ["tag", "delete", "photos", "--plan", "--json"])

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["dependencies"]["collections"]["sample"] == ["41", "42"]
    assert payload["blockers"] == [
        "2 collection(s) still use this tag; run riverhog collection list --tag photos"
    ]


def test_tag_delete_confirm_uses_prior_challenge_without_cascading(monkeypatch) -> None:
    class FakeClient:
        def delete_tag(self, tag: str, *, challenge: str) -> dict[str, object]:
            assert (tag, challenge) == ("photos", "delete-tag.challenge")
            return {"status": "deleted", "tag": tag}

    monkeypatch.setattr(riverhog_cli.main, "client", FakeClient)

    result = runner.invoke(
        app,
        ["tag", "delete", "photos", "--confirm", "delete-tag.challenge"],
    )

    assert result.exit_code == 0
    assert result.stdout == "tag deletion: deleted\ntag: photos\n"


def test_collection_tag_add_and_remove_match_json_data_model(monkeypatch) -> None:
    calls: list[tuple[str, int, str]] = []

    class FakeClient:
        def add_collection_tag(self, collection_id: int, tag: str) -> dict[str, Any]:
            calls.append(("add", collection_id, tag))
            return {
                "collection_id": collection_id,
                "metadata_revision": 2,
                "record_etag": "etag-2",
                "tags": ["photos", tag],
            }

        def remove_collection_tag(self, collection_id: int, tag: str) -> dict[str, Any]:
            calls.append(("remove", collection_id, tag))
            return {
                "collection_id": collection_id,
                "metadata_revision": 3,
                "record_etag": "etag-3",
                "tags": ["reviewed"],
            }

    monkeypatch.setattr(riverhog_cli.main, "client", FakeClient)

    added = runner.invoke(app, ["collection", "tag", "add", "41", "reviewed", "--json"])
    removed = runner.invoke(app, ["collection", "tag", "remove", "41", "photos", "--json"])

    assert added.exit_code == 0
    assert json.loads(added.stdout)["tags"] == ["photos", "reviewed"]
    assert removed.exit_code == 0
    assert json.loads(removed.stdout)["tags"] == ["reviewed"]
    assert calls == [("add", 41, "reviewed"), ("remove", 41, "photos")]
