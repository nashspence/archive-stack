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
        "ffprobe-sampling-observer",
        "nvenc-av1-opus-review-sampler",
        "nvenc-av1-opus-target",
        "opus-review-sampler",
        "opus-target",
        "review-target",
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


def test_reference_topology_keeps_payload_scratch_ephemeral_and_roles_private() -> None:
    payload = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))
    services = payload["services"]
    for name in (
        "api",
        "controller",
        "worker",
        "state",
        "ffprobe-sampling-observer",
        "nvenc-av1-opus-review-sampler",
        "nvenc-av1-opus-target",
        "opus-review-sampler",
        "opus-target",
        "review-target",
    ):
        assert services[name]["read_only"] is True
        assert services[name]["user"] == "65532:65532"
        assert services[name]["group_add"] == ["${STOVE0_SECRET_FILE_GID:-65532}"]
        assert services[name]["cap_drop"] == ["ALL"]
    for name in (
        "ffprobe-sampling-observer",
        "nvenc-av1-opus-review-sampler",
        "nvenc-av1-opus-target",
        "opus-review-sampler",
        "opus-target",
        "review-target",
    ):
        assert "ports" not in services[name]
    assert services["nvenc-av1-opus-target"]["profiles"] == ["nvenc"]
    assert services["nvenc-av1-opus-review-sampler"]["profiles"] == ["nvenc"]
    assert services["ffprobe-sampling-observer"]["command"][0] == (
        "stove0-ffprobe-sampling-observer"
    )
    assert services["opus-target"]["command"][0] == "stove0-opus-target"
    assert services["opus-review-sampler"]["command"][0] == "stove0-opus-review-sampler"
    assert services["review-target"]["command"][0] == "stove0-review-target"
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
        "stove0-review-target-state",
        "stove0-review-workspace",
    }


def test_sampler_containers_share_only_ephemeral_workspace_and_no_authority() -> None:
    payload = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))
    services = payload["services"]
    for name in ("opus-review-sampler", "nvenc-av1-opus-review-sampler"):
        service = services[name]
        assert service["networks"] == ["review-sampler"]
        assert service["volumes"] == ["stove0-review-workspace:/run/stove0-review-target"]
        assert all("riverhog" not in item for item in service["secrets"])
        assert all("target_token" not in item for item in service["secrets"])
        assert "STOVE0_DATABASE_URL_FILE" not in service["environment"]
    assert set(services["review-target"]["networks"]) == {
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
    assert services["opus-target"]["build"]["dockerfile"] == (
        "extensions/stove0/opus-target/Dockerfile"
    )
    assert services["nvenc-av1-opus-target"]["build"]["dockerfile"] == (
        "extensions/stove0/nvenc-av1-opus-target/Dockerfile"
    )
    assert "gpus" not in services["opus-target"]
    assert "profiles" not in services["opus-target"]


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
