from __future__ import annotations

from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[2]


def test_jeb_compose_exposes_readiness_healthcheck() -> None:
    compose = yaml.safe_load((REPO / "services" / "jeb" / "compose.yaml").read_text())
    service = compose["services"]["jeb"]

    assert service["environment"]["JEB_HEALTH_HOST"] == "0.0.0.0"
    assert service["environment"]["JEB_HEALTH_PORT"] == "8081"
    healthcheck = service["healthcheck"]
    assert healthcheck["test"][:3] == ["CMD", "python", "-c"]
    assert "/health/ready" in healthcheck["test"][3]
    assert healthcheck["interval"] == "15s"
