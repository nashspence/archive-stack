from __future__ import annotations

import json
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
        "exiftool-observer",
        "ffprobe-sampling-observer",
        "nvenc-av1-opus-review-sampler",
        "nvenc-av1-opus-target",
        "opus-review-sampler",
        "opus-target",
        "review-rclone-effect-target",
        "review-materialize-target",
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
    configuration_mounts = services["api"]["volumes"][:2]
    assert configuration_mounts == [
        {
            "type": "bind",
            "source": "${STOVE0_RECIPES_HOST_PATH:?STOVE0_RECIPES_HOST_PATH is required}",
            "target": "/etc/stove0/recipes.yaml",
            "read_only": True,
        },
        {
            "type": "bind",
            "source": "${STOVE0_ADMISSIONS_HOST_PATH:?STOVE0_ADMISSIONS_HOST_PATH is required}",
            "target": "/etc/stove0/admissions.json",
            "read_only": True,
        },
    ]
    for name in ("api", "controller", "worker"):
        assert services[name]["environment"]["STOVE0_ADMISSIONS_PATH"] == (
            "/etc/stove0/admissions.json"
        )


def test_reference_topology_keeps_payload_scratch_ephemeral_and_roles_private() -> None:
    payload = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))
    services = payload["services"]
    for name in (
        "api",
        "controller",
        "worker",
        "state",
        "exiftool-observer",
        "ffprobe-sampling-observer",
        "nvenc-av1-opus-review-sampler",
        "nvenc-av1-opus-target",
        "opus-review-sampler",
        "opus-target",
        "review-rclone-effect-target",
        "review-materialize-target",
    ):
        assert services[name]["read_only"] is True
        assert services[name]["user"] == "65532:65532"
        assert services[name]["group_add"] == ["${STOVE0_SECRET_FILE_GID:-65532}"]
        assert services[name]["cap_drop"] == ["ALL"]
    for name in (
        "exiftool-observer",
        "ffprobe-sampling-observer",
        "nvenc-av1-opus-review-sampler",
        "nvenc-av1-opus-target",
        "opus-review-sampler",
        "opus-target",
        "review-rclone-effect-target",
        "review-materialize-target",
    ):
        assert "ports" not in services[name]
    assert services["nvenc-av1-opus-target"]["profiles"] == ["nvenc"]
    assert services["nvenc-av1-opus-review-sampler"]["profiles"] == ["nvenc"]
    assert services["ffprobe-sampling-observer"]["command"][0] == (
        "stove0-ffprobe-sampling-observer"
    )
    assert services["exiftool-observer"]["command"][0] == "stove0-exiftool-observer"
    assert services["opus-target"]["command"][0] == "stove0-opus-target"
    assert services["opus-review-sampler"]["command"][0] == "stove0-opus-review-sampler"
    assert services["review-materialize-target"]["command"][0] == "stove0-review-materialize-target"
    assert (
        services["review-rclone-effect-target"]["command"][0]
        == "stove0-review-rclone-effect-target"
    )
    assert services["nvenc-av1-opus-target"]["command"][0] == "stove0-nvenc-av1-opus-target"
    assert (
        services["nvenc-av1-opus-review-sampler"]["command"][0]
        == "stove0-nvenc-av1-opus-review-sampler"
    )
    assert payload["networks"]["stove0-internal"]["internal"] is True
    assert payload["networks"]["review-sampler"]["internal"] is True
    assert set(payload["volumes"]) == {
        "stove0-nvenc-av1-opus-target-state",
        "stove0-opus-target-state",
        "stove0-review-materialize-target-state",
        "stove0-review-rclone-effect-target-state",
        "stove0-review-effect-delivery",
        "stove0-review-workspace",
    }


def test_sampler_containers_share_only_ephemeral_workspace_and_no_authority() -> None:
    payload = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))
    services = payload["services"]
    for name in ("opus-review-sampler", "nvenc-av1-opus-review-sampler"):
        service = services[name]
        assert service["networks"] == ["review-sampler"]
        assert service["volumes"] == ["stove0-review-workspace:/run/stove0-review"]
        assert all("riverhog" not in item for item in service["secrets"])
        assert all("target_token" not in item for item in service["secrets"])
        assert "STOVE0_DATABASE_URL_FILE" not in service["environment"]
    for name in ("review-materialize-target", "review-rclone-effect-target"):
        assert set(services[name]["networks"]) == {
            "review-sampler",
            "riverhog-control",
            "stove0-internal",
        }
    workspace = payload["volumes"]["stove0-review-workspace"]
    assert workspace["driver_opts"]["type"] == "tmpfs"


def test_paired_target_and_sampler_roles_bind_the_same_image_digest() -> None:
    payload = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))
    services = payload["services"]
    assert services["opus-target"]["image"] == services["opus-review-sampler"]["image"]
    assert (
        services["opus-target"]["environment"]["STOVE0_OPUS_TARGET_IMAGE_DIGEST"]
        == services["opus-review-sampler"]["environment"]["STOVE0_OPUS_REVIEW_SAMPLER_IMAGE_DIGEST"]
    )
    assert (
        services["nvenc-av1-opus-target"]["image"]
        == services["nvenc-av1-opus-review-sampler"]["image"]
    )
    assert (
        services["nvenc-av1-opus-target"]["environment"][
            "STOVE0_NVENC_AV1_OPUS_TARGET_IMAGE_DIGEST"
        ]
        == services["nvenc-av1-opus-review-sampler"]["environment"][
            "STOVE0_NVENC_AV1_OPUS_REVIEW_SAMPLER_IMAGE_DIGEST"
        ]
    )
    assert services["opus-target"]["image"] != services["nvenc-av1-opus-target"]["image"]
    assert (
        services["review-materialize-target"]["image"]
        != services["review-rclone-effect-target"]["image"]
    )
    assert services["review-materialize-target"]["build"]["dockerfile"] == (
        "reference/stove0/targets/review/materialize-target/Dockerfile"
    )
    assert services["review-rclone-effect-target"]["build"]["dockerfile"] == (
        "reference/stove0/targets/review/rclone-effect-target/Dockerfile"
    )
    assert services["opus-target"]["build"]["dockerfile"] == (
        "reference/stove0/targets/opus/Dockerfile"
    )
    assert services["nvenc-av1-opus-target"]["build"]["dockerfile"] == (
        "reference/stove0/targets/nvenc-av1-opus/Dockerfile"
    )
    assert "gpus" not in services["opus-target"]
    assert "profiles" not in services["opus-target"]


def test_reference_topology_uses_secret_files_and_explicit_lan_http_opt_in() -> None:
    payload = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))
    services = payload["services"]
    for name in ("api", "controller", "worker"):
        environment = services[name]["environment"]
        assert environment["RIVERHOG_ALLOW_INSECURE_HTTP"] == "true"
        assert environment["STOVE0_TARGET_CALLBACK_BASE_URL"] == "http://api:8080"
        assert environment["STOVE0_TARGET_CALLBACK_ALLOW_INSECURE_HTTP"] == "true"
        assert environment["STOVE0_TARGET_CALLBACK_SIGNING_KEY_FILE"] == (
            "/run/secrets/stove0_target_callback_signing_key"
        )
        assert "stove0_target_callback_signing_key" in services[name]["secrets"]
        assert environment["RIVERHOG_TOKEN_FILE"].startswith("/run/secrets/")
        assert "RIVERHOG_TOKEN" not in environment
    text = COMPOSE.read_text(encoding="utf-8")
    assert "STOVE0_API_TOKEN=" not in text
    assert "RIVERHOG_TOKEN=" not in text


def test_reference_observer_registrations_connect_exact_one_role_services() -> None:
    payload = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))
    services = payload["services"]
    expected = {
        "exiftool": (
            "http://exiftool-observer:8080",
            "STOVE0_EXIFTOOL_OBSERVER_TOKEN",
            "stove0_exiftool_observer_token",
            ["media-metadata"],
        ),
        "ffprobe-sampling": (
            "http://ffprobe-sampling-observer:8080",
            "STOVE0_FFPROBE_SAMPLING_OBSERVER_TOKEN",
            "stove0_ffprobe_sampling_observer_token",
            ["media-sampling"],
        ),
    }
    for role in ("api", "controller", "worker"):
        service = services[role]
        registrations = json.loads(service["environment"]["STOVE0_OBSERVERS_JSON"])
        assert set(registrations) == set(expected)
        for registration, (base_url, token_env, secret, providers) in expected.items():
            assert registrations[registration] == {
                "base_url": base_url,
                "token_env": token_env,
                "allow_insecure_http": True,
                "semantic_validator_providers": providers,
            }
            assert service["environment"][f"{token_env}_FILE"] == f"/run/secrets/{secret}"
            assert secret in service["secrets"]


def test_reference_target_registrations_bind_fixed_review_result_modes() -> None:
    payload = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))
    services = payload["services"]
    for role in ("api", "controller", "worker"):
        service = services[role]
        registrations = json.loads(service["environment"]["STOVE0_TARGETS_JSON"])
        assert registrations["review"] == {
            "base_url": "http://review-materialize-target:8080",
            "token_env": "STOVE0_REVIEW_MATERIALIZE_TARGET_TOKEN",
            "allow_insecure_http": True,
        }
        assert registrations["review-effect"] == {
            "base_url": "http://review-rclone-effect-target:8080",
            "token_env": "STOVE0_REVIEW_RCLONE_EFFECT_TARGET_TOKEN",
            "allow_insecure_http": True,
        }
        assert "stove0_review_materialize_target_token" in service["secrets"]
        assert "stove0_review_rclone_effect_target_token" in service["secrets"]


def test_reference_topology_connects_bounded_operational_state_retention() -> None:
    payload = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))
    services = payload["services"]
    for name in ("api", "controller", "worker"):
        assert (
            services[name]["environment"]["STOVE0_OPERATIONAL_STATE_RETENTION_SECONDS"]
            == "${STOVE0_OPERATIONAL_STATE_RETENTION_SECONDS:-2592000}"
        )
    for name in (
        "opus-target",
        "nvenc-av1-opus-target",
        "review-materialize-target",
        "review-rclone-effect-target",
    ):
        assert (
            services[name]["environment"]["STOVE0_TARGET_TERMINAL_STATE_RETENTION_SECONDS"]
            == "${STOVE0_TARGET_TERMINAL_STATE_RETENTION_SECONDS:-2592000}"
        )
