from __future__ import annotations

import ast
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]


def test_server_compose_exposes_every_runtime_setting() -> None:
    compose = yaml.safe_load(
        (REPO_ROOT / "companions/munchy/server/compose.yaml").read_text(encoding="utf-8")
    )
    environment = set(compose["services"]["munchy-server"]["environment"])
    runtime_settings = set()
    for source in (REPO_ROOT / "companions/munchy/server/src").rglob("*.py"):
        tree = ast.parse(source.read_text(encoding="utf-8"))
        runtime_settings.update(
            node.args[0].value
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
            and (
                (isinstance(node.func, ast.Attribute) and node.func.attr == "getenv")
                or (isinstance(node.func, ast.Name) and node.func.id in {"env_flag", "env_list"})
            )
        )

    assert runtime_settings <= environment


def test_server_compose_upgrades_state_before_startup() -> None:
    compose = yaml.safe_load(
        (REPO_ROOT / "companions/munchy/server/compose.yaml").read_text(encoding="utf-8")
    )

    assert compose["services"]["munchy-state"]["command"] == [
        "munchy-server",
        "state",
        "upgrade",
    ]
    assert compose["services"]["munchy-server"]["depends_on"]["munchy-state"] == {
        "condition": "service_completed_successfully"
    }
