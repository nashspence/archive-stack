from __future__ import annotations

import os
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[2]


def test_jeb_compose_exposes_readiness_healthcheck() -> None:
    compose = yaml.safe_load((REPO / "services" / "jeb" / "compose.yaml").read_text())
    service = compose["services"]["jeb"]

    assert service["environment"]["JEB_HEALTH_HOST"] == "0.0.0.0"
    assert service["environment"]["JEB_HEALTH_PORT"] == "8081"
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
    assert services["jeb-tus"]["ports"] == ["${JEB_TUS_PORT:-1081}:1081"]
    for service_name in ("jeb", "jeb-ftp", "jeb-tusd"):
        assert any(volume["target"] == "/landing" for volume in services[service_name]["volumes"])

    bootstrap = REPO / "services" / "jeb" / "adapters" / "ftp" / "bootstrap-users.sh"
    assert bootstrap.is_file()
    assert os.access(bootstrap, os.X_OK)
    bootstrap_source = bootstrap.read_text(encoding="utf-8")
    assert "JEB_FTP_ACCOUNTS" in bootstrap_source
    assert "JEB_ACCOUNT_%s_PASSWORD" in bootstrap_source


def test_jeb_service_image_runs_service_entrypoint() -> None:
    dockerfile = (REPO / "services" / "jeb" / "Dockerfile").read_text()

    assert 'CMD ["python", "-m", "jeb.service_cli", "run"]' in dockerfile
