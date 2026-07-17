from __future__ import annotations

import os
from pathlib import Path

import yaml

from jeb.collector import config_from_env, parse_duration

REPO = Path(__file__).resolve().parents[2]


def test_jeb_compose_exposes_readiness_healthcheck(tmp_path: Path) -> None:
    compose = yaml.safe_load((REPO / "services" / "jeb" / "compose.yaml").read_text())
    service = compose["services"]["jeb"]

    assert service["environment"]["JEB_HEALTH_HOST"] == "0.0.0.0"
    assert service["environment"]["JEB_HEALTH_PORT"] == "8081"
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
    max_age_default = service["environment"]["JEB_TUS_INCOMPLETE_MAX_AGE"].removeprefix(
        "${JEB_TUS_INCOMPLETE_MAX_AGE:-"
    ).removesuffix("}")
    assert parse_duration(max_age_default) == runtime.ingress.tus_incomplete_max_age_seconds
    assert all(
        volume.get("target", "").startswith(("/landing", "/state")) for volume in service["volumes"]
    )
    healthcheck = service["healthcheck"]
    assert healthcheck["test"][:3] == ["CMD", "python", "-c"]
    assert "/health/ready" in healthcheck["test"][3]
    assert healthcheck["interval"] == "15s"


def test_jeb_compose_routes_adapters_to_the_shared_landing_contract() -> None:
    compose = yaml.safe_load((REPO / "services" / "jeb" / "compose.yaml").read_text())
    services = compose["services"]

    assert set(services) == {
        "jeb",
        "jeb-ftp",
        "jeb-ingress-init",
        "jeb-tus",
        "jeb-tusd",
    }
    assert services["jeb-tusd"]["command"][-1] == "pre-create,post-finish"
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

    adapter = REPO / "services" / "jeb" / "adapters" / "ftp" / "run-adapter.sh"
    assert adapter.is_file()
    assert os.access(adapter, os.X_OK)
    adapter_source = adapter.read_text(encoding="utf-8")
    assert "JEB_FTP_PROJECTION" in adapter_source
    assert "pure-pw mkdb" in adapter_source
    assert "sha256sum" in adapter_source


def test_jeb_service_image_runs_service_entrypoint() -> None:
    dockerfile = (REPO / "services" / "jeb" / "Dockerfile").read_text()

    assert 'CMD ["python", "-m", "jeb.service_cli", "run"]' in dockerfile


def test_jeb_tus_proxy_streams_bounded_upload_chunks() -> None:
    config = (
        REPO / "services" / "jeb" / "adapters" / "tus" / "nginx.conf"
    ).read_text(encoding="utf-8")

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
        "proxy_set_header Content-Type application/offset+octet;",
    ):
        assert directive in config
