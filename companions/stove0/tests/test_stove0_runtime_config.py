from __future__ import annotations

from pathlib import Path

import pytest
from stove0_core import Stove0RuntimeConfig


def _environment(recipes: Path) -> dict[str, str]:
    recipes.write_text("operations: []\nrecipes: []\n", encoding="utf-8")
    return {
        "STOVE0_DATABASE_URL": "postgresql+psycopg://stove0@postgres/stove0",
        "RIVERHOG_BASE_URL": "https://riverhog.invalid",
        "RIVERHOG_TOKEN": "role-specific-riverhog-token",
        "STOVE0_RECIPES_PATH": str(recipes),
    }


def test_scheduler_configuration_does_not_require_operator_api_secret(
    tmp_path: Path,
) -> None:
    config = Stove0RuntimeConfig.from_environment(
        _environment(tmp_path / "recipes.yaml"),
        require_api_token=False,
    )

    assert config.api_token is None
    assert config.riverhog_token == "role-specific-riverhog-token"


def test_runtime_configuration_connects_every_control_plane_setting(tmp_path: Path) -> None:
    environment = _environment(tmp_path / "recipes.yaml")
    environment.update(
        {
            "STOVE0_API_TOKEN": "operator-token",
            "RIVERHOG_ALLOW_INSECURE_HTTP": "true",
            "STOVE0_WORKSPACE_ASSURANCE": "ephemeral",
            "STOVE0_CLAIM_LEASE_SECONDS": "240",
            "STOVE0_CAPABILITY_TTL_SECONDS": "120",
            "STOVE0_SCHEDULER_INTERVAL_SECONDS": "0.5",
            "STOVE0_OPERATIONAL_STATE_RETENTION_SECONDS": "86400",
            "STOVE0_OBSERVERS_JSON": (
                '{"probe":{"base_url":"http://probe:8080","allow_insecure_http":true}}'
            ),
            "STOVE0_TARGETS_JSON": (
                '{"target":{"base_url":"https://target.invalid","allow_insecure_http":false}}'
            ),
        }
    )

    config = Stove0RuntimeConfig.from_environment(environment)

    assert config.api_token == "operator-token"
    assert config.riverhog_base_url == "https://riverhog.invalid"
    assert config.riverhog_allow_insecure_http is True
    assert config.recipes_path == (tmp_path / "recipes.yaml").resolve()
    assert config.observers["probe"].base_url == "http://probe:8080"
    assert config.observers["probe"].allow_insecure_http is True
    assert config.targets["target"].base_url == "https://target.invalid"
    assert config.targets["target"].allow_insecure_http is False
    assert config.workspace_assurance == "ephemeral"
    assert config.claim_lease_seconds == 240
    assert config.capability_ttl_seconds == 120
    assert config.scheduler_interval_seconds == 0.5
    assert config.operational_state_retention_seconds == 86400


def test_runtime_secrets_accept_exactly_one_direct_or_file_source(tmp_path: Path) -> None:
    environment = _environment(tmp_path / "recipes.yaml")
    token = tmp_path / "riverhog.token"
    token.write_text("from-file\n", encoding="utf-8")
    environment.pop("RIVERHOG_TOKEN")
    environment["RIVERHOG_TOKEN_FILE"] = str(token)

    config = Stove0RuntimeConfig.from_environment(
        environment,
        require_api_token=False,
    )

    assert config.riverhog_token == "from-file"
    environment["RIVERHOG_TOKEN"] = "direct"
    with pytest.raises(ValueError, match="mutually exclusive"):
        Stove0RuntimeConfig.from_environment(environment, require_api_token=False)


def test_operator_api_configuration_requires_its_bearer_secret(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="STOVE0_API_TOKEN or STOVE0_API_TOKEN_FILE is required"):
        Stove0RuntimeConfig.from_environment(_environment(tmp_path / "recipes.yaml"))


@pytest.mark.parametrize("value", ["nan", "inf", "-inf"])
def test_scheduler_interval_must_be_finite(tmp_path: Path, value: str) -> None:
    environment = _environment(tmp_path / "recipes.yaml")
    environment["STOVE0_SCHEDULER_INTERVAL_SECONDS"] = value

    with pytest.raises(ValueError, match="must be at least"):
        Stove0RuntimeConfig.from_environment(
            environment,
            require_api_token=False,
        )
