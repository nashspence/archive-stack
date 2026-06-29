from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_runner_compose_uses_collection_archive_target_scratch_name() -> None:
    compose = (REPO_ROOT / "services" / "munchy-runner" / "docker-compose.yaml").read_text(
        encoding="utf-8"
    )

    assert "MUNCHY_RUNNER_COLLECTION_ARCHIVE_TARGET_SCRATCH_EXTRA_MULTIPLIER" in compose
    assert "MUNCHY_RUNNER_COLLECTION_PREVIEW_SCRATCH_EXTRA_MULTIPLIER" not in compose
