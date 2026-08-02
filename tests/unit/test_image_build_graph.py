from __future__ import annotations

import re
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
BAKE_FILE = REPO_ROOT / "docker-bake.hcl"
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


def _bake_graph() -> dict[str, object]:
    text = BAKE_FILE.read_text(encoding="utf-8")
    group_match = re.search(r'group "default" \{(?P<body>.*?)\n\}', text, re.DOTALL)
    assert group_match is not None
    group_targets_match = re.search(
        r"targets\s*=\s*\[(?P<targets>.*?)\]",
        group_match.group("body"),
        re.DOTALL,
    )
    assert group_targets_match is not None
    group_targets = re.findall(r'"([^"]+)"', group_targets_match.group("targets"))

    targets: dict[str, dict[str, object]] = {}
    for match in re.finditer(
        r'target "(?P<name>[^"]+)" \{(?P<body>.*?)\n\}',
        text,
        re.DOTALL,
    ):
        body = match.group("body")
        context_match = re.search(r'context\s*=\s*"([^"]+)"', body)
        dockerfile_match = re.search(r'dockerfile\s*=\s*"([^"]+)"', body)
        tags_match = re.search(r"tags\s*=\s*\[(.*?)\]", body)
        revision_match = re.search(r'SOURCE_REVISION\s*=\s*"([^"]+)"', body)
        assert context_match is not None
        assert dockerfile_match is not None
        assert tags_match is not None
        assert revision_match is not None
        targets[match.group("name")] = {
            "context": context_match.group(1),
            "dockerfile": dockerfile_match.group(1),
            "tags": re.findall(r'"([^"]+)"', tags_match.group(1)),
            "args": {"SOURCE_REVISION": revision_match.group(1)},
        }

    return {"group": {"default": {"targets": group_targets}}, "target": targets}


def test_bake_graph_is_the_canonical_image_build_contract() -> None:
    graph = _bake_graph()

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
    graph = _bake_graph()

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
            "files": "docker-bake.hcl",
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
