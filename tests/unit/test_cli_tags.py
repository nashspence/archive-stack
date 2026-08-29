from __future__ import annotations

import json
from contextlib import contextmanager
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
                "inventory_identity": "etag-2",
                "tag_count": 2,
            }

        def remove_collection_tag(self, collection_id: int, tag: str) -> dict[str, Any]:
            calls.append(("remove", collection_id, tag))
            return {
                "collection_id": collection_id,
                "metadata_revision": 3,
                "inventory_identity": "etag-3",
                "tag_count": 1,
            }

    monkeypatch.setattr(riverhog_cli.main, "client", FakeClient)

    added = runner.invoke(app, ["collection", "tag", "add", "41", "reviewed", "--json"])
    removed = runner.invoke(app, ["collection", "tag", "remove", "41", "photos", "--json"])

    assert added.exit_code == 0
    assert json.loads(added.stdout)["tag_count"] == 2
    assert removed.exit_code == 0
    assert json.loads(removed.stdout)["tag_count"] == 1
    assert calls == [("add", 41, "reviewed"), ("remove", 41, "photos")]
    added_human = runner.invoke(app, ["collection", "tag", "add", "41", "reviewed"])
    removed_human = runner.invoke(app, ["collection", "tag", "remove", "41", "photos"])
    assert added_human.exit_code == removed_human.exit_code == 0
    assert "collection 41" in added_human.stdout
    assert "collection 41" in removed_human.stdout


def test_tag_and_collection_tag_reads_have_human_json_parity(monkeypatch) -> None:
    tag = {
        "id": "photos",
        "created_by_app": "operator",
        "created_by_key_id": "key-one",
        "created_at": "2026-08-13T00:00:00Z",
        "collections": 1,
    }
    collection_tags = {
        "collection_id": 41,
        "metadata_revision": 2,
        "inventory_identity": "etag-2",
        "tag_count": 1,
    }

    class FakeClient:
        def create_tag(self, value: str) -> dict[str, object]:
            assert value == "photos"
            return tag

        def get_tag(self, value: str) -> dict[str, object]:
            assert value == "photos"
            return tag

        def list_tags(self, **_kwargs: object) -> dict[str, object]:
            return {
                "page": 1,
                "pages": 1,
                "per_page": 25,
                "total": 1,
                "tags": [tag],
            }

        @contextmanager
        def stream_collection_tags(self, collection_id: int):  # type: ignore[no-untyped-def]
            assert collection_id == 41
            yield iter(({"tag": "photos"},))

        def replace_collection_tags(self, collection_id: int, tags: list[str]) -> dict[str, object]:
            assert collection_id == 41
            assert tags == ["photos"]
            return collection_tags

    monkeypatch.setattr(riverhog_cli.main, "client", FakeClient)

    cases = (
        (["tag", "create", "photos"], "photos"),
        (["tag", "show", "photos"], "photos"),
        (["tag", "list"], "photos"),
        (["collection", "tag", "list", "41"], "photos"),
        (["collection", "tag", "replace", "41", "--tag", "photos"], "collection 41"),
    )
    for arguments, identity in cases:
        human = runner.invoke(app, arguments)
        structured = runner.invoke(app, [*arguments, "--json"])
        assert human.exit_code == 0, human.output
        assert structured.exit_code == 0, structured.output
        assert identity in human.stdout
        assert str(identity).split()[-1] in json.dumps(
            json.loads(structured.stdout), sort_keys=True
        )
