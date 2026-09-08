from __future__ import annotations

from pathlib import Path

import pytest
from stove0_core import Stove0RuntimeConfig, database_url_from_environment


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
    admissions = tmp_path / "admissions.json"
    admissions.write_text(
        '{"format":"stove0-admissions/v1","policies":[{'
        '"id":"camera","revision":1,"required_tags":["camera"],'
        '"recipe_id":"fixture/v1","recipe_revision":1,'
        '"recipe_sha256":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",'
        '"effective_intent":{}}]}',
        encoding="utf-8",
    )
    environment.update(
        {
            "STOVE0_API_TOKEN": "operator-token",
            "RIVERHOG_ALLOW_INSECURE_HTTP": "true",
            "STOVE0_WORKSPACE_ASSURANCE": "ephemeral",
            "STOVE0_CLAIM_LEASE_SECONDS": "240",
            "STOVE0_CAPABILITY_TTL_SECONDS": "120",
            "STOVE0_SCHEDULER_INTERVAL_SECONDS": "0.5",
            "STOVE0_OPERATIONAL_STATE_RETENTION_SECONDS": "86400",
            "STOVE0_BROWSE_TOKEN_SIGNING_KEY": "stove0-test-browse-token-signing-key-v1",
            "STOVE0_BROWSE_TOKEN_LIFETIME_SECONDS": "7200",
            "STOVE0_OBSERVERS_JSON": (
                '{"probe":{"base_url":"http://probe:8080","allow_insecure_http":true,'
                '"semantic_validator_providers":["fixture"]}}'
            ),
            "STOVE0_TARGETS_JSON": (
                '{"target":{"base_url":"https://target.invalid","allow_insecure_http":false}}'
            ),
            "STOVE0_TARGET_CALLBACK_BASE_URL": "http://stove0.internal:8080",
            "STOVE0_TARGET_CALLBACK_ALLOW_INSECURE_HTTP": "true",
            "STOVE0_TARGET_CALLBACK_SIGNING_KEY": "target-callback-signing-key",
            "STOVE0_TARGET_AUTHORITY_BATCH_SIZE": "17",
            "STOVE0_ADMISSIONS_PATH": str(admissions),
        }
    )

    config = Stove0RuntimeConfig.from_environment(environment)

    assert config.api_token == "operator-token"
    assert config.riverhog_base_url == "https://riverhog.invalid"
    assert config.riverhog_allow_insecure_http is True
    assert config.recipes_path == (tmp_path / "recipes.yaml").resolve()
    assert config.admissions.policies[0].id == "camera"
    assert config.observers["probe"].base_url == "http://probe:8080"
    assert config.observers["probe"].allow_insecure_http is True
    assert config.observers["probe"].semantic_validator_providers == ("fixture",)
    assert config.targets["target"].base_url == "https://target.invalid"
    assert config.targets["target"].allow_insecure_http is False
    assert config.targets["target"].semantic_validator_providers == ()
    assert config.target_callback_base_url == "http://stove0.internal:8080"
    assert config.target_callback_allow_insecure_http is True
    assert config.target_callback_signing_key == "target-callback-signing-key"
    assert config.target_authority_batch_size == 17
    assert config.workspace_assurance == "ephemeral"
    assert config.claim_lease_seconds == 240
    assert config.capability_ttl_seconds == 120
    assert config.scheduler_interval_seconds == 0.5
    assert config.operational_state_retention_seconds == 86400
    assert config.browse_token_signing_key == "stove0-test-browse-token-signing-key-v1"
    assert config.browse_token_lifetime_seconds == 7200


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


def test_configured_targets_require_an_independent_callback_signing_secret(
    tmp_path: Path,
) -> None:
    environment = _environment(tmp_path / "recipes.yaml")
    environment["STOVE0_TARGETS_JSON"] = '{"target":{"base_url":"https://target.invalid"}}'
    environment["STOVE0_TARGET_CALLBACK_BASE_URL"] = "https://stove0.invalid"

    with pytest.raises(
        ValueError,
        match=(
            "STOVE0_TARGET_CALLBACK_SIGNING_KEY or "
            "STOVE0_TARGET_CALLBACK_SIGNING_KEY_FILE is required"
        ),
    ):
        Stove0RuntimeConfig.from_environment(environment, require_api_token=False)


def test_runtime_database_is_postgresql_only(tmp_path: Path) -> None:
    environment = _environment(tmp_path / "recipes.yaml")
    environment["STOVE0_DATABASE_URL"] = "sqlite+pysqlite:///:memory:"

    with pytest.raises(ValueError, match="STOVE0_DATABASE_URL must use postgresql"):
        Stove0RuntimeConfig.from_environment(environment, require_api_token=False)
    with pytest.raises(ValueError, match="STOVE0_DATABASE_URL must use postgresql"):
        database_url_from_environment(environment)


def test_semantic_validator_providers_are_observer_only_and_unique(tmp_path: Path) -> None:
    environment = _environment(tmp_path / "recipes.yaml")
    environment["STOVE0_OBSERVERS_JSON"] = (
        '{"probe":{"base_url":"https://probe.invalid",'
        '"semantic_validator_providers":["same","same"]}}'
    )
    with pytest.raises(ValueError, match="providers repeat"):
        Stove0RuntimeConfig.from_environment(environment, require_api_token=False)

    environment["STOVE0_OBSERVERS_JSON"] = "{}"
    environment["STOVE0_TARGETS_JSON"] = (
        '{"target":{"base_url":"https://target.invalid",'
        '"semantic_validator_providers":["fixture"]}}'
    )
    with pytest.raises(ValueError, match="cannot configure semantic validators"):
        Stove0RuntimeConfig.from_environment(environment, require_api_token=False)


@pytest.mark.parametrize("value", ["nan", "inf", "-inf"])
def test_scheduler_interval_must_be_finite(tmp_path: Path, value: str) -> None:
    environment = _environment(tmp_path / "recipes.yaml")
    environment["STOVE0_SCHEDULER_INTERVAL_SECONDS"] = value

    with pytest.raises(ValueError, match="must be at least"):
        Stove0RuntimeConfig.from_environment(
            environment,
            require_api_token=False,
        )


@pytest.mark.parametrize("value", ["0", "129"])
def test_target_authority_batch_size_is_bounded(tmp_path: Path, value: str) -> None:
    environment = _environment(tmp_path / "recipes.yaml")
    environment["STOVE0_TARGET_AUTHORITY_BATCH_SIZE"] = value

    with pytest.raises(ValueError, match="must be at"):
        Stove0RuntimeConfig.from_environment(
            environment,
            require_api_token=False,
        )
