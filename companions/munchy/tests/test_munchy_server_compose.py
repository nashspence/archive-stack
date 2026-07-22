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
    tree = ast.parse(
        (REPO_ROOT / "companions/munchy/server/src/munchy_server/main.py").read_text(
            encoding="utf-8"
        )
    )
    runtime_settings = {
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
    }

    assert runtime_settings <= environment
