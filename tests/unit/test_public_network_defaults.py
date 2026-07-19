from __future__ import annotations

import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
COMPOSE_NAMES = frozenset(
    {"compose.yml", "compose.yaml", "docker-compose.yml", "docker-compose.yaml"}
)
DEFAULT_INTERPOLATION = re.compile(r"\$\{[A-Z0-9_]+(?::?-)([^}]*)\}")


def _defaults(value: str) -> str:
    return DEFAULT_INTERPOLATION.sub(lambda match: match.group(1), value)


def _public_compose_files() -> tuple[Path, ...]:
    return tuple(
        path
        for path in sorted((ROOT / "apps").rglob("*"))
        if path.is_file() and path.name in COMPOSE_NAMES
    )


def test_public_compose_published_ports_default_to_loopback() -> None:
    published: list[tuple[str, str, str]] = []
    for path in _public_compose_files():
        compose = yaml.safe_load(path.read_text(encoding="utf-8"))
        for service_name, service in compose.get("services", {}).items():
            for port in service.get("ports", []):
                published.append((str(path.relative_to(ROOT)), service_name, _defaults(str(port))))

    assert published
    for path, service, port in published:
        assert port.startswith("127.0.0.1:"), f"{path} service {service} publishes {port}"


def test_public_munchy_host_network_services_default_to_loopback() -> None:
    compose = yaml.safe_load(
        (ROOT / "apps/munchy/runner/docker-compose.yaml").read_text(encoding="utf-8")
    )
    assert {
        name
        for name, service in compose["services"].items()
        if service.get("network_mode") == "host"
    } == {"munchy-runner", "munchy-runner-tusd", "munchy-runner-lan-gateway"}

    runner = compose["services"]["munchy-runner"]
    assert _defaults(runner["environment"]["MUNCHY_RUNNER_HOST"]) == "127.0.0.1"

    tusd = compose["services"]["munchy-runner-tusd"]
    host_argument = tusd["command"].index("-host") + 1
    assert tusd["command"][host_argument] == "127.0.0.1"

    gateway = compose["services"]["munchy-runner-lan-gateway"]
    assert _defaults(gateway["environment"]["MUNCHY_GATEWAY_BIND_ADDR"]) == "127.0.0.1"

    nginx = (ROOT / "apps/munchy/runner/config/nginx-lan-gateway.conf").read_text(encoding="utf-8")
    assert "listen ${MUNCHY_GATEWAY_BIND_ADDR}:8092;" in nginx
    assert "listen ${MUNCHY_GATEWAY_BIND_ADDR}:8093;" in nginx
