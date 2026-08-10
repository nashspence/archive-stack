from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def isolated_provenance_user_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep repository tests from establishing a real client installation identity."""

    monkeypatch.setenv("RIVERHOG_PROVENANCE_STATE_HOME", str(tmp_path / "user-state"))
