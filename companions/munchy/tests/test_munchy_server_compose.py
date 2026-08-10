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


def test_server_and_lan_gateway_share_the_tusd_signing_contract() -> None:
    compose = yaml.safe_load(
        (REPO_ROOT / "companions/munchy/server/compose.yaml").read_text(encoding="utf-8")
    )
    server_environment = compose["services"]["munchy-server"]["environment"]
    gateway_environment = compose["services"]["munchy-server-lan-gateway"]["environment"]
    nginx = (REPO_ROOT / "companions/munchy/server/config/nginx-lan-gateway.conf").read_text(
        encoding="utf-8"
    )

    signing_setting = (
        "${MUNCHY_TUSD_PUBLIC_SIGNING_SECRET:?MUNCHY_TUSD_PUBLIC_SIGNING_SECRET is required}"
    )
    assert server_environment["MUNCHY_TUSD_PUBLIC_SIGNING_SECRET"] == signing_setting
    assert gateway_environment["MUNCHY_TUSD_PUBLIC_SIGNING_SECRET"] == signing_setting
    assert server_environment["MUNCHY_TUSD_PUBLIC_URL_TTL_SECONDS"] == (
        "${MUNCHY_TUSD_PUBLIC_URL_TTL_SECONDS:-86400}"
    )
    assert nginx.count("secure_link $arg_md5,$arg_expires;") == 1
    assert (
        nginx.count(
            'secure_link_md5 "$secure_link_expires$uri ${MUNCHY_TUSD_PUBLIC_SIGNING_SECRET}";'
        )
        == 1
    )
