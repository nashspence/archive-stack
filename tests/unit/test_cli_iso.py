from __future__ import annotations

from typer.testing import CliRunner

from riverhog_cli import main as riverhog_main

runner = CliRunner()


def test_iso_finalize_prints_finalized_image(monkeypatch) -> None:
    calls: list[str] = []

    class FakeApi:
        def finalize_image(self, candidate_id: str) -> dict[str, object]:
            calls.append(candidate_id)
            return {
                "id": "20260526T011347Z",
                "filename": "20260526T011347Z.iso",
                "finalized_at": "2026-05-26T01:13:47Z",
                "bytes": 49_814_339_584,
                "fill": 0.99628679168,
                "files": 440,
                "collections": 1,
                "collection_ids": ["2025/collection"],
                "physical_protection_state": "unprotected",
                "physical_copies_required": 2,
                "physical_copies_registered": 0,
                "physical_copies_verified": 0,
            }

    monkeypatch.setattr(riverhog_main, "client", lambda: FakeApi())

    result = runner.invoke(riverhog_main.app, ["iso", "finalize", "candidate-abc"])

    assert result.exit_code == 0
    assert calls == ["candidate-abc"]
    assert "image: 20260526T011347Z (20260526T011347Z.iso)" in result.stdout
    assert "collections: 1 [2025/collection]" in result.stdout
