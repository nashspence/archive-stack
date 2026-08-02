from __future__ import annotations

import json
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
BAKE_FILE = REPO_ROOT / "docker-bake.json"
CI_FILE = REPO_ROOT / ".github/workflows/ci.yml"

IMAGE_CONTRACTS = {
    "riverhog": {
        "dockerfile": "riverhog/server/Dockerfile",
        "tag": "riverhog-app:dev",
        "compose": (
            ("riverhog/server/compose.yaml", "state"),
            ("riverhog/server/compose.yaml", "app"),
        ),
    },
    "jeb": {
        "dockerfile": "companions/jeb/server/Dockerfile",
        "tag": "jeb:dev",
        "compose": (
            ("companions/jeb/server/compose.yaml", "jeb-state"),
            ("companions/jeb/server/compose.yaml", "jeb"),
        ),
    },
    "mango-fish": {
        "dockerfile": "utilities/mango-fish/Dockerfile",
        "tag": "mango-fish:dev",
        "compose": (),
    },
    "munchy-server": {
        "dockerfile": "companions/munchy/server/Dockerfile",
        "tag": "munchy-server:dev",
        "compose": (
            ("companions/munchy/server/compose.yaml", "munchy-state"),
            ("companions/munchy/server/compose.yaml", "munchy-server"),
        ),
    },
    "munchy-av1-nvenc": {
        "dockerfile": "companions/munchy/server/targets/av1-nvenc/Dockerfile",
        "tag": "munchy-av1-nvenc-target:dev",
        "compose_tag": "${MUNCHY_AV1_NVENC_IMAGE:-munchy-av1-nvenc-target:dev}",
        "compose": (
            (
                "companions/munchy/server/targets/av1-nvenc/compose.yaml",
                "api",
            ),
        ),
    },
    "test": {
        "dockerfile": "tests/Dockerfile",
        "tag": "riverhog-test:dev",
        "compose": (("riverhog/server/compose.yaml", "test"),),
    },
}


def test_bake_graph_is_the_canonical_image_build_contract() -> None:
    graph = json.loads(BAKE_FILE.read_text(encoding="utf-8"))

    assert graph["group"] == {"default": {"targets": list(IMAGE_CONTRACTS)}}
    assert set(graph["target"]) == set(IMAGE_CONTRACTS)

    for name, contract in IMAGE_CONTRACTS.items():
        target = graph["target"][name]
        assert target == {
            "context": ".",
            "dockerfile": contract["dockerfile"],
            "tags": [contract["tag"]],
            "args": {"SOURCE_REVISION": "unknown"},
        }
        dockerfile = (REPO_ROOT / contract["dockerfile"]).read_text(encoding="utf-8")
        assert "ARG SOURCE_REVISION=unknown" in dockerfile
        assert 'org.opencontainers.image.revision="${SOURCE_REVISION}"' in dockerfile


def test_compose_build_services_match_the_canonical_bake_graph() -> None:
    graph = json.loads(BAKE_FILE.read_text(encoding="utf-8"))

    for name, contract in IMAGE_CONTRACTS.items():
        target = graph["target"][name]
        target_context = (REPO_ROOT / target["context"]).resolve()
        target_dockerfile = (target_context / target["dockerfile"]).resolve()
        for compose_name, service_name in contract["compose"]:
            compose_path = REPO_ROOT / compose_name
            compose = yaml.safe_load(compose_path.read_text(encoding="utf-8"))
            service = compose["services"][service_name]
            build = service["build"]
            compose_context = (compose_path.parent / build["context"]).resolve()
            compose_dockerfile = (compose_context / build["dockerfile"]).resolve()

            assert compose_context == target_context
            assert compose_dockerfile == target_dockerfile
            assert service["image"] == contract.get("compose_tag", contract["tag"])
            assert build["args"] == {"SOURCE_REVISION": "${SOURCE_REVISION:-unknown}"}


def test_github_image_matrix_uses_bounded_per_image_bake_caches() -> None:
    workflow = yaml.safe_load(CI_FILE.read_text(encoding="utf-8"))
    assert workflow["permissions"] == {"contents": "read"}

    job = workflow["jobs"]["images"]
    assert job["strategy"]["matrix"] == {"target": list(IMAGE_CONTRACTS)}
    assert job["env"] == {"DOCKER_BUILD_RECORD_UPLOAD": "false"}
    steps = {step["name"]: step for step in job["steps"]}
    assert steps["Configure Docker Buildx"] == {
        "name": "Configure Docker Buildx",
        "uses": "docker/setup-buildx-action@bb05f3f5519dd87d3ba754cc423b652a5edd6d2c",
        "with": {"version": "v0.36.0"},
    }
    assert steps["Build image"] == {
        "name": "Build image",
        "uses": "docker/bake-action@d3418bd7d0e9324001bca92fa8ba175ea7e6dc9b",
        "with": {
            "source": ".",
            "files": "docker-bake.json",
            "targets": "${{ matrix.target }}",
            "load": True,
            "sbom": True,
            "set": (
                "*.args.SOURCE_REVISION=${{ github.sha }}\n"
                "*.cache-from=type=gha,scope=${{ matrix.target }}\n"
                "*.cache-to=type=gha,scope=${{ matrix.target }},mode=min,ignore-error=true\n"
            ),
        },
    }
