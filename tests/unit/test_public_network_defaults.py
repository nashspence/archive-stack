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
        for owner in ("companions", "riverhog", "utilities")
        for path in sorted((ROOT / owner).rglob("*"))
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


def test_stove0_and_adapter_apis_default_to_loopback() -> None:
    compose = yaml.safe_load((ROOT / "companions/stove0/compose.yaml").read_text(encoding="utf-8"))
    assert _defaults(compose["services"]["api"]["ports"][0]).startswith("127.0.0.1:")
    adapters = yaml.safe_load((ROOT / "riverhog/ftp-adapter/compose.yaml").read_text(encoding="utf-8"))
    assert _defaults(adapters["services"]["ftp-adapter"]["ports"][0]).startswith(
        "127.0.0.1:"
    )
