from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import riverhog_cli.main
from riverhog_cli.main import app
from typer.testing import CliRunner

RUNNER = CliRunner()
JOURNAL_ID = "urn:uuid:00000000-0000-4000-8000-000000000042"
JOURNAL = b'\x1e{"exact":"journal"}\n'


def test_provenance_list_show_trace_export_and_verify_share_one_cli_surface(
    tmp_path: Path,
    monkeypatch,
) -> None:
    calls: list[tuple[str, object]] = []
    binding = {
        "status": "captured",
        "journal_id": JOURNAL_ID,
        "current_state_id": "urn:uuid:00000000-0000-4000-8000-000000000043",
    }
    shown = {
        "collection_id": 41,
        "path": "media/movie.mov",
        "bytes": 7,
        "sha256": "a" * 64,
        "provenance": binding,
        "journal": {"journal_id": JOURNAL_ID, "entries": 2},
    }

    class FakeClient:
        def list_collection_provenance(self, collection_id: int, **kwargs: Any) -> dict[str, Any]:
            calls.append(("list", (collection_id, kwargs)))
            return {"files": [shown], "page": 1, "pages": 1, "total": 1}

        def get_collection_file_provenance(self, collection_id: int, path: str) -> dict[str, Any]:
            calls.append(("show", (collection_id, path)))
            return shown

        def trace_collection_file_provenance(
            self,
            collection_id: int,
            path: str,
            **kwargs: Any,
        ) -> dict[str, Any]:
            calls.append(("trace", (collection_id, path, kwargs)))
            return {
                **shown,
                "page": kwargs["page"],
                "per_page": kwargs["per_page"],
                "total": 1,
                "pages": 1,
                "items": [{"kind": "journal", "journal": shown["journal"]}],
            }

        def download_collection_provenance_journal(
            self,
            collection_id: int,
            journal_id: str,
            *,
            output: Path,
        ) -> tuple[int, str]:
            calls.append(("export", (collection_id, journal_id)))
            output.write_bytes(JOURNAL)
            return len(JOURNAL), hashlib.sha256(JOURNAL).hexdigest()

        def request_collection_provenance_verification(self, collection_id: int) -> dict[str, Any]:
            calls.append(("verify", collection_id))
            return {
                "collection_id": collection_id,
                "state": "succeeded",
                "requested_at": "2026-08-29T00:00:00.000000Z",
                "started_at": "2026-08-29T00:00:00.000000Z",
                "finished_at": "2026-08-29T00:00:01.000000Z",
                "attempts": 1,
                "failure": None,
                "result": {
                    "collection_id": collection_id,
                    "valid": True,
                    "provenance_mode": "captured",
                    "provenance_identity": "b" * 64,
                    "files": 1,
                    "journals": 1,
                    "entities": 4,
                },
            }

    monkeypatch.setattr(riverhog_cli.main, "client", FakeClient)
    output = tmp_path / "movie.json-seq"
    json_output = tmp_path / "movie-json.json-seq"

    listed = RUNNER.invoke(
        app,
        [
            "collection",
            "provenance",
            "list",
            "41",
            "--query",
            "movie",
            "--status",
            "captured",
            "--json",
        ],
    )
    shown_result = RUNNER.invoke(
        app,
        ["collection", "provenance", "show", "41", "media/movie.mov", "--json"],
    )
    traced = RUNNER.invoke(
        app,
        ["collection", "provenance", "trace", "41", "media/movie.mov", "--json"],
    )
    exported = RUNNER.invoke(
        app,
        [
            "collection",
            "provenance",
            "export",
            "41",
            JOURNAL_ID,
            "--output",
            str(output),
        ],
    )
    exported_json = RUNNER.invoke(
        app,
        [
            "collection",
            "provenance",
            "export",
            "41",
            JOURNAL_ID,
            "--output",
            str(json_output),
            "--json",
        ],
    )
    verified = RUNNER.invoke(
        app,
        ["collection", "provenance", "verify", "41", "--json"],
    )

    assert listed.exit_code == 0
    assert json.loads(listed.stdout)["files"] == [shown]
    assert shown_result.exit_code == 0
    assert json.loads(shown_result.stdout) == shown
    assert traced.exit_code == 0
    assert json.loads(traced.stdout)["items"] == [{"kind": "journal", "journal": shown["journal"]}]
    assert exported.exit_code == 0
    assert output.read_bytes() == JOURNAL
    assert exported_json.exit_code == 0
    assert json_output.read_bytes() == JOURNAL
    assert json.loads(exported_json.stdout) == {
        "collection_id": 41,
        "journal_id": JOURNAL_ID,
        "output": str(json_output.resolve()),
        "bytes": len(JOURNAL),
        "sha256": hashlib.sha256(JOURNAL).hexdigest(),
    }
    assert verified.exit_code == 0
    assert json.loads(verified.stdout)["result"]["valid"] is True
    assert calls == [
        (
            "list",
            (
                41,
                {
                    "page": 1,
                    "per_page": 25,
                    "q": "movie",
                    "status": "captured",
                    "sort": "path",
                    "order": "asc",
                },
            ),
        ),
        ("show", (41, "media/movie.mov")),
        ("trace", (41, "media/movie.mov", {"page": 1, "per_page": 25})),
        ("export", (41, JOURNAL_ID)),
        ("export", (41, JOURNAL_ID)),
        ("verify", 41),
    ]
    shown_human = RUNNER.invoke(app, ["collection", "provenance", "show", "41", "media/movie.mov"])
    traced_human = RUNNER.invoke(
        app, ["collection", "provenance", "trace", "41", "media/movie.mov"]
    )
    verified_human = RUNNER.invoke(app, ["collection", "provenance", "verify", "41"])
    assert shown_human.exit_code == traced_human.exit_code == verified_human.exit_code == 0
    assert "media/movie.mov" in shown_human.stdout
    assert "media/movie.mov" in traced_human.stdout
    assert "trace items" in traced_human.stdout
    assert "valid" in verified_human.stdout


def test_provenance_list_selectors_match_other_file_list_commands(monkeypatch) -> None:
    class FakeClient:
        def list_collection_provenance(self, collection_id: int, **kwargs: Any) -> dict[str, Any]:
            assert collection_id == 41
            return {
                "files": [
                    {"collection_id": 41, "path": "one.mov"},
                    {"collection_id": 41, "path": "nested/two.mov"},
                ],
                "page": 1,
                "pages": 1,
                "total": 2,
            }

    monkeypatch.setattr(riverhog_cli.main, "client", FakeClient)

    result = RUNNER.invoke(
        app,
        ["collection", "provenance", "list", "41", "--selectors"],
    )

    assert result.exit_code == 0
    assert result.stdout == "41::one.mov\n41::nested/two.mov\n"
