from __future__ import annotations

import ast
import os
import re
from pathlib import Path

import yaml
from jeb_api.cli import api_host, api_port
from jeb_api.composition import config_from_env
from jeb_core.domain.models import parse_duration

REPO = Path(__file__).resolve().parents[3]


def test_jeb_compose_exposes_every_runtime_setting() -> None:
    compose = yaml.safe_load((REPO / "companions/jeb/server/compose.yaml").read_text())
    services = compose["services"]
    environment = set(services["jeb"]["environment"])
    runtime_settings: set[str] = set()
    for source in (REPO / "companions/jeb/server/src").rglob("*.py"):
        tree = ast.parse(source.read_text(encoding="utf-8"))
        runtime_settings.update(
            node.value
            for node in ast.walk(tree)
            if isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and re.fullmatch(r"JEB_[A-Z0-9_]+", node.value)
        )

    assert runtime_settings <= environment

    adapter = (REPO / "companions/jeb/server/adapters/ftp/run-adapter.sh").read_text(
        encoding="utf-8"
    )
    adapter_settings = set(re.findall(r"\$\{(JEB_[A-Z0-9_]+)", adapter))
    assert adapter_settings <= set(services["jeb-ftp"]["environment"])

    assert services["jeb-state"]["environment"] == {
        "JEB_STATE_DIR": services["jeb"]["environment"]["JEB_STATE_DIR"],
        "JEB_STATE_DB": services["jeb"]["environment"]["JEB_STATE_DB"],
    }
    assert (
        services["jeb-ftp"]["environment"]["JEB_FTP_PROJECTION"]
        == services["jeb"]["environment"]["JEB_FTP_PROJECTION"]
    )
    staging = services["jeb"]["environment"]["JEB_TUS_STAGING_DIR"]
    tusd_command = services["jeb-tusd"]["command"]
    assert tusd_command[tusd_command.index("-upload-dir") + 1] == staging
    assert services["jeb-ingress-init"]["environment"]["JEB_TUS_STAGING_DIR"] == staging


def test_jeb_compose_exposes_readiness_healthcheck(tmp_path: Path) -> None:
    compose = yaml.safe_load((REPO / "companions/jeb/server/compose.yaml").read_text())
    service = compose["services"]["jeb"]

    assert service["environment"]["JEB_HOST"] == "0.0.0.0"
    assert service["environment"]["JEB_PORT"] == "8081"
    assert service["environment"]["JEB_API_TOKEN"] == (
        "${JEB_API_TOKEN:-jeb-development-api-token}"
    )
    assert service["environment"]["JEB_MUNCHY_ALLOW_INSECURE_HTTP"] == (
        "${JEB_MUNCHY_ALLOW_INSECURE_HTTP:-false}"
    )
    assert service["ports"] == ["${JEB_API_BIND_ADDR:-127.0.0.1}:${JEB_API_PORT:-8081}:8081"]
    runtime = config_from_env(
        {
            "JEB_LANDING_DIR": str(tmp_path / "landing"),
            "JEB_STATE_DIR": str(tmp_path / "state"),
            "JEB_MUNCHY_URL": "http://munchy.invalid",
        }
    )
    assert service["environment"]["JEB_TUSD_BASE_URL"] == (
        "${JEB_TUSD_BASE_URL:-" + runtime.ingress.tusd_base_url + "}"
    )
    max_age_default = (
        service["environment"]["JEB_TUS_INCOMPLETE_MAX_AGE"]
        .removeprefix("${JEB_TUS_INCOMPLETE_MAX_AGE:-")
        .removesuffix("}")
    )
    assert parse_duration(max_age_default) == runtime.ingress.tus_incomplete_max_age_seconds
    assert all(
        volume.get("target", "").startswith(("/landing", "/state")) for volume in service["volumes"]
    )
    healthcheck = service["healthcheck"]
    assert healthcheck["test"][:3] == ["CMD", "python", "-c"]
    assert "/health/ready" in healthcheck["test"][3]
    assert healthcheck["interval"] == "15s"


def test_jeb_api_defaults_to_loopback_and_accepts_an_explicit_container_bind(
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.delenv("JEB_HOST", raising=False)
    monkeypatch.delenv("JEB_PORT", raising=False)

    assert api_host() == "127.0.0.1"
    assert api_port() == 8081

    monkeypatch.setenv("JEB_HOST", "0.0.0.0")
    monkeypatch.setenv("JEB_PORT", "9081")

    assert api_host() == "0.0.0.0"
    assert api_port() == 9081


def test_jeb_compose_routes_adapters_to_the_shared_landing_contract() -> None:
    compose = yaml.safe_load((REPO / "companions/jeb/server/compose.yaml").read_text())
    services = compose["services"]

    assert set(services) == {
        "jeb",
        "jeb-ftp",
        "jeb-ingress-init",
        "jeb-state",
        "jeb-tus",
        "jeb-tusd",
    }
    assert services["jeb"]["depends_on"]["jeb-state"]["condition"] == (
        "service_completed_successfully"
    )
    assert services["jeb-state"]["command"] == ["jeb-service", "state", "upgrade"]
    assert services["jeb-tusd"]["command"][-1] == "pre-create,post-finish"
    assert "/files/" in services["jeb-tusd"]["command"]
    assert services["jeb-tusd"].get("ports", []) == []
    assert services["jeb-tus"]["ports"] == [
        "${JEB_INGRESS_BIND_ADDR:-127.0.0.1}:${JEB_TUS_PORT:-1081}:1081"
    ]
    for service_name in ("jeb", "jeb-ftp", "jeb-tusd"):
        assert any(volume["target"] == "/landing" for volume in services[service_name]["volumes"])
    assert {volume["target"] for volume in services["jeb-ftp"]["volumes"]} == {
        "/landing",
        "/state",
        "/usr/local/bin/run-jeb-ftp",
    }

    adapter = REPO / "companions/jeb/server/adapters/ftp/run-adapter.sh"
    assert adapter.is_file()
    assert os.access(adapter, os.X_OK)
    adapter_source = adapter.read_text(encoding="utf-8")
    assert "JEB_FTP_PROJECTION" in adapter_source
    assert "pure-pw mkdb" in adapter_source
    assert "sha256sum" in adapter_source


def test_jeb_service_image_runs_service_entrypoint() -> None:
    dockerfile = (REPO / "companions/jeb/server/Dockerfile").read_text()

    assert 'CMD ["jeb-service", "run"]' in dockerfile


def test_jeb_tus_proxy_streams_bounded_upload_chunks() -> None:
    config = (REPO / "companions/jeb/server/adapters/tus/nginx.conf").read_text(encoding="utf-8")
    assert "location ^~ /files/" in config

    assert config.count("client_max_body_size 128m;") == 2
    for directive in (
        "client_body_timeout 75s;",
        "client_body_buffer_size 16m;",
        "send_timeout 75s;",
        "proxy_buffering off;",
        "proxy_request_buffering off;",
        "proxy_connect_timeout 240s;",
        "proxy_http_version 1.1;",
        "proxy_read_timeout 240s;",
        "proxy_send_timeout 240s;",
    ):
        assert directive in config
