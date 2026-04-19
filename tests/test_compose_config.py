from __future__ import annotations

from pathlib import Path

import yaml


def test_compose_uses_internal_service_urls():
    repo_root = Path(__file__).resolve().parents[1]
    compose = yaml.safe_load((repo_root / "docker-compose.yml").read_text(encoding="utf-8"))
    services = compose["services"]

    ui_env = services["ui"]["environment"]
    api_volumes = services["api"]["volumes"]

    assert ui_env["RIVERHOG_API_BASE_URL"] == "http://api:8080"
    assert "./data/archive:/var/lib/archive" in api_volumes
    assert "./data/uploads:/var/lib/uploads" in api_volumes
    assert "redis" not in services


def test_test_compose_mounts_live_repo_and_sets_pytest_defaults():
    repo_root = Path(__file__).resolve().parents[1]
    compose = yaml.safe_load((repo_root / "docker-compose.test.yml").read_text(encoding="utf-8"))
    test_service = compose["services"]["test"]

    assert test_service["working_dir"] == "/workspace"
    assert test_service["command"] == ["pytest"]
    assert ".:/workspace" in test_service["volumes"]
    assert test_service["environment"]["PYTEST_ADDOPTS"] == "-o cache_dir=/tmp/pytest-cache"
