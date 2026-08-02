from __future__ import annotations

from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize(
    ("compose_path", "upgrade_service", "runtime_service", "command"),
    (
        (
            "riverhog/server/compose.yaml",
            "state",
            "app",
            ["state", "upgrade"],
        ),
        (
            "companions/munchy/server/compose.yaml",
            "munchy-state",
            "munchy-server",
            ["munchy-server", "state", "upgrade"],
        ),
        (
            "companions/jeb/server/compose.yaml",
            "jeb-state",
            "jeb",
            ["jeb-service", "state", "upgrade"],
        ),
    ),
)
def test_compose_runs_explicit_state_upgrade_before_service_start(
    compose_path: str,
    upgrade_service: str,
    runtime_service: str,
    command: list[str],
) -> None:
    compose = yaml.safe_load((REPO_ROOT / compose_path).read_text(encoding="utf-8"))
    services = compose["services"]

    assert services[upgrade_service]["command"] == command
    assert services[upgrade_service]["restart"] == "no"
    assert services[runtime_service]["depends_on"][upgrade_service] == {
        "condition": "service_completed_successfully"
    }
