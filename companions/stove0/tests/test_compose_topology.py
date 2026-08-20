from __future__ import annotations

from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).parents[3]
COMPOSE = REPO_ROOT / "companions/stove0/compose.yaml"


def test_reference_topology_uses_one_postgres_authority_and_distinct_roles() -> None:
    payload = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))
    services = payload["services"]

    assert set(services) == {
        "api",
        "controller",
        "local-media",
        "media-sampling",
        "nvenc-media",
        "state",
        "worker",
    }
    assert services["controller"]["command"][-1] == "controller"
    assert services["worker"]["command"][-1] == "worker"
    assert (
        services["controller"]["environment"]["STOVE0_DATABASE_URL_FILE"]
        == services["worker"]["environment"]["STOVE0_DATABASE_URL_FILE"]
    )
    assert (
        services["controller"]["environment"]["RIVERHOG_TOKEN_FILE"]
        != services["worker"]["environment"]["RIVERHOG_TOKEN_FILE"]
    )
    assert "STOVE0_API_TOKEN_FILE" not in services["controller"]["environment"]
    assert "STOVE0_API_TOKEN_FILE" not in services["worker"]["environment"]
    recipe_mount = services["api"]["volumes"][0]
    assert recipe_mount == {
        "type": "bind",
        "source": "${STOVE0_RECIPES_HOST_PATH:-./config/recipes.example.yaml}",
        "target": "/etc/stove0/recipes.yaml",
        "read_only": True,
    }


def test_reference_topology_keeps_payload_scratch_ephemeral_and_extensions_private() -> None:
    payload = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))
    services = payload["services"]
    for name in (
        "api",
        "controller",
        "worker",
        "state",
        "media-sampling",
        "local-media",
        "nvenc-media",
    ):
        assert services[name]["read_only"] is True
        assert services[name]["user"] == "65532:65532"
        assert services[name]["group_add"] == ["${STOVE0_SECRET_FILE_GID:-65532}"]
        assert services[name]["cap_drop"] == ["ALL"]
    for name in ("media-sampling", "local-media", "nvenc-media"):
        assert "ports" not in services[name]
        assert any("/run/stove0-workspaces" in item for item in services[name]["tmpfs"])
    assert services["nvenc-media"]["profiles"] == ["nvenc"]
    assert services["media-sampling"]["command"][0] == "observer"
    assert services["local-media"]["command"][0] == "local-target"
    assert services["nvenc-media"]["command"][0] == "nvenc-target"
    assert payload["networks"]["stove0-internal"]["internal"] is True
    assert set(payload["volumes"]) == {
        "stove0-local-target-state",
        "stove0-nvenc-target-state",
    }


def test_reference_topology_uses_secret_files_and_explicit_lan_http_opt_in() -> None:
    payload = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))
    services = payload["services"]
    for name in ("api", "controller", "worker"):
        environment = services[name]["environment"]
        assert environment["RIVERHOG_ALLOW_INSECURE_HTTP"] == "true"
        assert environment["RIVERHOG_TOKEN_FILE"].startswith("/run/secrets/")
        assert "RIVERHOG_TOKEN" not in environment
    text = COMPOSE.read_text(encoding="utf-8")
    assert "STOVE0_API_TOKEN=" not in text
    assert "RIVERHOG_TOKEN=" not in text
