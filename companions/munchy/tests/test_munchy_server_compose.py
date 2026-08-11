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


def test_server_signs_tusd_urls_and_gateway_uses_the_server_authorizer() -> None:
    compose = yaml.safe_load(
        (REPO_ROOT / "companions/munchy/server/compose.yaml").read_text(encoding="utf-8")
    )
    server_environment = compose["services"]["munchy-server"]["environment"]
    nginx = (REPO_ROOT / "companions/munchy/server/config/nginx-lan-gateway.conf").read_text(
        encoding="utf-8"
    )

    signing_setting = (
        "${MUNCHY_TUSD_PUBLIC_SIGNING_SECRET:?MUNCHY_TUSD_PUBLIC_SIGNING_SECRET is required}"
    )
    assert server_environment["MUNCHY_TUSD_PUBLIC_SIGNING_SECRET"] == signing_setting
    assert server_environment["MUNCHY_TUSD_PUBLIC_URL_TTL_SECONDS"] == (
        "${MUNCHY_TUSD_PUBLIC_URL_TTL_SECONDS:-86400}"
    )
    assert nginx.count("auth_request /_munchy_tusd_authorize;") == 1
    assert nginx.count("proxy_pass http://munchy_server_api/internal/tusd/authorize;") == 1
    assert nginx.count("proxy_set_header X-Munchy-Tusd-Original-Uri $request_uri;") == 1


def test_lan_gateway_keeps_parallel_transfers_observable_and_unbuffered() -> None:
    compose = yaml.safe_load(
        (REPO_ROOT / "companions/munchy/server/compose.yaml").read_text(encoding="utf-8")
    )
    gateway_environment = compose["services"]["munchy-server-lan-gateway"]["environment"]
    nginx = (REPO_ROOT / "companions/munchy/server/config/nginx-lan-gateway.conf").read_text(
        encoding="utf-8"
    )

    assert gateway_environment["MUNCHY_GATEWAY_BIND_ADDR"] == (
        "${MUNCHY_GATEWAY_BIND_ADDR:-127.0.0.1}"
    )
    assert gateway_environment["MUNCHY_GATEWAY_API_PORT"] == ("${MUNCHY_GATEWAY_API_PORT:-8092}")
    assert gateway_environment["MUNCHY_GATEWAY_TUSD_PORT"] == ("${MUNCHY_GATEWAY_TUSD_PORT:-8093}")
    assert gateway_environment["MUNCHY_GATEWAY_API_UPSTREAM_ADDR"] == (
        "${MUNCHY_GATEWAY_API_UPSTREAM_ADDR:-127.0.0.1:8092}"
    )
    assert gateway_environment["MUNCHY_GATEWAY_TUSD_UPSTREAM_ADDR"] == (
        "${MUNCHY_GATEWAY_TUSD_UPSTREAM_ADDR:-127.0.0.1:8093}"
    )
    assert (
        "MUNCHY_GATEWAY_(BIND_ADDR|API_PORT|TUSD_PORT|API_UPSTREAM_ADDR|TUSD_UPSTREAM_ADDR)"
        in (gateway_environment["NGINX_ENVSUBST_FILTER"])
    )
    assert "worker_processes auto;" in nginx
    assert "worker_connections 1024;" in nginx
    assert "access_log /dev/stdout riverhog_transfer;" in nginx
    assert '"$request_method $uri $server_protocol"' in nginx
    assert "rl=$request_length rt=$request_time urt=$upstream_response_time" in nginx
    assert "proxy_request_buffering off;" in nginx
    assert "proxy_buffering off;" in nginx
    assert "proxy_pass_request_body off;" in nginx
    assert "server ${MUNCHY_GATEWAY_API_UPSTREAM_ADDR};" in nginx
    assert "server ${MUNCHY_GATEWAY_TUSD_UPSTREAM_ADDR};" in nginx
    assert "listen ${MUNCHY_GATEWAY_BIND_ADDR}:${MUNCHY_GATEWAY_API_PORT};" in nginx
    assert "listen ${MUNCHY_GATEWAY_BIND_ADDR}:${MUNCHY_GATEWAY_TUSD_PORT};" in nginx
