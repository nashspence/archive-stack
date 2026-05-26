from __future__ import annotations

from typer.testing import CliRunner

from riverhog_cli import main as riverhog_main

runner = CliRunner()


def test_iso_candidates_lists_ready_plan_candidates(monkeypatch) -> None:
    calls: list[dict[str, object]] = []

    class FakeApi:
        def get_plan(
            self,
            *,
            page: int,
            per_page: int,
            sort: str,
            order: str,
            iso_ready: bool,
        ) -> dict[str, object]:
            calls.append(
                {
                    "page": page,
                    "per_page": per_page,
                    "sort": sort,
                    "order": order,
                    "iso_ready": iso_ready,
                }
            )
            return {
                "page": 1,
                "pages": 1,
                "per_page": 25,
                "total": 1,
                "sort": "fill",
                "order": "desc",
                "ready": True,
                "target_bytes": 50_000_000_000,
                "min_fill_bytes": 48_000_000_000,
                "unplanned_bytes": 0,
                "candidates": [
                    {
                        "candidate_id": "candidate-abc",
                        "bytes": 49_800_000_000,
                        "fill": 0.996,
                        "files": 440,
                        "collections": 1,
                        "collection_ids": ["2025/collection"],
                        "iso_ready": True,
                    }
                ],
            }

    monkeypatch.setattr(riverhog_main, "client", lambda: FakeApi())

    result = runner.invoke(riverhog_main.app, ["iso", "candidates"])

    assert result.exit_code == 0
    assert calls == [
        {
            "page": 1,
            "per_page": 25,
            "sort": "fill",
            "order": "desc",
            "iso_ready": True,
        }
    ]
    assert "candidate-abc" in result.stdout
    assert "iso_ready: True" in result.stdout


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
