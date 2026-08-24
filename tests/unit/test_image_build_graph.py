from __future__ import annotations

import re
import shlex
import tomllib
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
BAKE_FILE = REPO_ROOT / "docker-bake.hcl"
CI_FILE = REPO_ROOT / ".github/workflows/ci.yml"
SBOM_GENERATOR = (
    "docker.io/docker/buildkit-syft-scanner:stable-1@"
    "sha256:79e7b013cbec16bbb436f312819a49a4a57752b2270c1a9332ae1a10fcc82a68"
)
MISE_IMAGE = (
    "jdxcode/mise:2026.7.13@sha256:7fe9145156e33f95f7712632dbadb418592ed0f60fb46e5bcc8c113d372ad8a3"
)
MISE_CONTAINER_TOOLS = {
    "riverhog": {"minisign", "uv"},
    "riverhog-ftp-adapter": {"uv"},
    "riverhog-storage-adapter-aws": {"uv"},
    "riverhog-storage-adapter-backblaze": {"uv"},
    "stove0": {"uv"},
    "stove0-exiftool-observer": {"http:exiftool", "uv"},
    "stove0-ffprobe-sampling-observer": {"uv"},
    "stove0-nvenc-av1-opus-target": {"uv"},
    "stove0-opus-target": {"uv"},
    "stove0-review-target": {"rclone", "uv"},
    "mango-fish": {"uv"},
    "test": {"age", "http:exiftool", "minisign", "uv"},
}
NON_ROOT_RUNTIME_IMAGES = set(MISE_CONTAINER_TOOLS) - {"test"}

IMAGE_CONTRACTS = {
    "riverhog": {
        "dockerfile": "riverhog/server/Dockerfile",
        "tag": "riverhog-app:dev",
        "title": "Riverhog",
        "license": "CAL-1.0",
        "compose": (
            ("riverhog/server/compose.yaml", "state"),
            ("riverhog/server/compose.yaml", "app"),
        ),
    },
    "riverhog-ftp-adapter": {
        "dockerfile": "reference/riverhog/ingress/ftp/Dockerfile",
        "tag": "riverhog-ftp-adapter:dev",
        "title": "Riverhog FTP adapter",
        "license": "Apache-2.0",
        "compose": (("reference/riverhog/ingress/ftp/compose.yaml", "ftp-adapter"),),
    },
    "riverhog-storage-adapter-aws": {
        "dockerfile": "reference/riverhog/storage/aws/Dockerfile",
        "tag": "riverhog-storage-adapter-aws:dev",
        "title": "Riverhog AWS storage adapter",
        "license": "CAL-1.0",
        "compose": (),
    },
    "riverhog-storage-adapter-backblaze": {
        "dockerfile": "reference/riverhog/storage/backblaze/Dockerfile",
        "tag": "riverhog-storage-adapter-backblaze:dev",
        "title": "Riverhog Backblaze storage adapter",
        "license": "CAL-1.0",
        "compose": (),
    },
    "stove0": {
        "dockerfile": "companions/stove0/server/Dockerfile",
        "tag": "stove0:dev",
        "title": "stove0",
        "license": "CAL-1.0",
        "compose": (
            ("companions/stove0/compose.yaml", "state"),
            ("companions/stove0/compose.yaml", "api"),
            ("companions/stove0/compose.yaml", "controller"),
            ("companions/stove0/compose.yaml", "worker"),
        ),
    },
    "stove0-exiftool-observer": {
        "dockerfile": "reference/stove0/observers/exiftool/Dockerfile",
        "tag": "stove0-exiftool-observer:dev",
        "title": "stove0 ExifTool observer",
        "license": "CAL-1.0",
        "compose": (("companions/stove0/compose.yaml", "exiftool-observer"),),
    },
    "stove0-ffprobe-sampling-observer": {
        "dockerfile": "reference/stove0/observers/ffprobe-sampling/Dockerfile",
        "tag": "stove0-ffprobe-sampling-observer:dev",
        "title": "stove0 FFprobe sampling observer",
        "license": "CAL-1.0",
        "compose": (("companions/stove0/compose.yaml", "ffprobe-sampling-observer"),),
    },
    "stove0-nvenc-av1-opus-target": {
        "dockerfile": "reference/stove0/targets/nvenc-av1-opus/Dockerfile",
        "tag": "stove0-nvenc-av1-opus-target:dev",
        "title": "stove0 NVENC AV1 + Opus target",
        "license": "CAL-1.0",
        "compose": (
            ("companions/stove0/compose.yaml", "nvenc-av1-opus-target"),
            ("companions/stove0/compose.yaml", "nvenc-av1-opus-review-sampler"),
        ),
    },
    "stove0-opus-target": {
        "dockerfile": "reference/stove0/targets/opus/Dockerfile",
        "tag": "stove0-opus-target:dev",
        "title": "stove0 Opus target",
        "license": "CAL-1.0",
        "compose": (
            ("companions/stove0/compose.yaml", "opus-target"),
            ("companions/stove0/compose.yaml", "opus-review-sampler"),
        ),
    },
    "stove0-review-target": {
        "dockerfile": "reference/stove0/targets/review/Dockerfile",
        "tag": "stove0-review-target:dev",
        "title": "stove0 review target",
        "license": "CAL-1.0",
        "compose": (
            ("companions/stove0/compose.yaml", "review-target"),
            ("companions/stove0/compose.yaml", "review-effect-target"),
        ),
    },
    "mango-fish": {
        "dockerfile": "utilities/mango-fish/Dockerfile",
        "tag": "mango-fish:dev",
        "title": "Mango Fish",
        "license": "Apache-2.0",
        "compose": (),
    },
    "test": {
        "dockerfile": "tests/Dockerfile",
        "tag": "riverhog-test:dev",
        "title": "Riverhog Test Suite",
        "license": "CAL-1.0 AND Apache-2.0",
        "compose": (("riverhog/server/compose.yaml", "test"),),
    },
}

PINNED_EXTERNAL_IMAGES = {
    "python:3.12-slim@sha256:090ba77e2958f6af52a5341f788b50b032dd4ca28377d2893dcf1ecbdfdfe203",
    MISE_IMAGE,
    "nvidia/cuda:13.0.0-devel-ubuntu24.04@sha256:1e8ac7a54c184a1af8ef2167f28fa98281892a835c981ebcddb1fad04bdd452d",
    "nvidia/cuda:13.0.0-runtime-ubuntu24.04@sha256:95318efecfd68ab3d109da5277863257b06137c84f34a87f38de970d5cd035d3",
}

PINNED_EXTERNAL_COMPOSE_IMAGES = {
    "alpine:3.22@sha256:14358309a308569c32bdc37e2e0e9694be33a9d99e68afb0f5ff33cc1f695dce",
    "dxflrs/garage:b72b090a097c8ee2711c8fb065d250ed68dcd0bf@sha256:f22f09abe741e54ab244e95638310e040b81eda41e2c6ab9b7373cda4b9e955c",
    "postgres:16-alpine@sha256:57c72fd2a128e416c7fcc499958864df5301e940bca0a56f58fddf30ffc07777",
    "stilliard/pure-ftpd:trixie-latest@sha256:12b5aeb1a371b789e77d0b6217434a7a5ded9a3b251d52dab1f2e85ccde4cbf8",
}


def test_runtime_image_isolation_is_derived_for_every_runtime_image() -> None:
    workflow = yaml.safe_load(CI_FILE.read_text(encoding="utf-8"))
    steps = {step["name"]: step for step in workflow["jobs"]["images"]["steps"]}
    assert steps["Verify runtime image isolation"] == {
        "name": "Verify runtime image isolation",
        "if": "matrix.target != 'test'",
        "env": {"IMAGE_TARGET": "${{ matrix.target }}"},
        "run": 'python3 scripts/check_runtime_image.py "$IMAGE_TARGET"',
    }

    checker = (REPO_ROOT / "scripts/check_runtime_image.py").read_text(encoding="utf-8")
    assert "docker-bake.hcl" in checker
    assert "_workspace_dependency_closure" in checker
    assert "FORBIDDEN_RUNTIME_COMMANDS" in checker
    assert "locked_runtime_payloads" in checker
    assert "STANDALONE_ATTRIBUTION_PROGRAM" in checker


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

    common_match = re.search(r'target "image-common" \{(?P<body>.*?)\n\}', text, re.DOTALL)
    assert common_match is not None
    common_body = common_match.group("body")
    platforms_match = re.search(r"platforms\s*=\s*\[(.*?)\]", common_body, re.DOTALL)
    epoch_match = re.search(r'SOURCE_DATE_EPOCH\s*=\s*"([^"]+)"', common_body)
    attest_match = re.search(r"attest\s*=\s*\[(.*?)\]", common_body, re.DOTALL)
    assert platforms_match is not None
    assert epoch_match is not None
    assert attest_match is not None

    targets: dict[str, dict[str, object]] = {}
    for match in re.finditer(
        r'target "(?P<name>[^"]+)" \{(?P<body>.*?)\n\}',
        text,
        re.DOTALL,
    ):
        if match.group("name") == "image-common":
            continue
        body = match.group("body")
        inherits_match = re.search(r"inherits\s*=\s*\[(.*?)\]", body)
        context_match = re.search(r'context\s*=\s*"([^"]+)"', body)
        dockerfile_match = re.search(r'dockerfile\s*=\s*"([^"]+)"', body)
        tags_match = re.search(r"tags\s*=\s*\[(.*?)\]", body)
        revision_match = re.search(r'SOURCE_REVISION\s*=\s*"([^"]+)"', body)
        assert context_match is not None
        assert dockerfile_match is not None
        assert tags_match is not None
        assert revision_match is not None
        assert inherits_match is not None
        targets[match.group("name")] = {
            "inherits": re.findall(r'"([^"]+)"', inherits_match.group(1)),
            "context": context_match.group(1),
            "dockerfile": dockerfile_match.group(1),
            "tags": re.findall(r'"([^"]+)"', tags_match.group(1)),
            "args": {"SOURCE_REVISION": revision_match.group(1)},
        }

    return {
        "group": {"default": {"targets": group_targets}},
        "common": {
            "platforms": re.findall(r'"([^"]+)"', platforms_match.group(1)),
            "args": {"SOURCE_DATE_EPOCH": epoch_match.group(1)},
            "attest": re.findall(r'"([^"]+)"', attest_match.group(1)),
        },
        "target": targets,
    }


def test_bake_graph_is_the_canonical_image_build_contract() -> None:
    graph = _bake_graph()

    assert graph["group"] == {"default": {"targets": list(IMAGE_CONTRACTS)}}
    assert graph["common"] == {
        "platforms": ["linux/amd64"],
        "args": {"SOURCE_DATE_EPOCH": "0"},
        "attest": [f"type=sbom,generator={SBOM_GENERATOR}"],
    }
    assert set(graph["target"]) == set(IMAGE_CONTRACTS)

    for name, contract in IMAGE_CONTRACTS.items():
        target = graph["target"][name]
        assert target == {
            "inherits": ["image-common"],
            "context": ".",
            "dockerfile": contract["dockerfile"],
            "tags": [contract["tag"]],
            "args": {"SOURCE_REVISION": "unknown"},
        }
        dockerfile = (REPO_ROOT / contract["dockerfile"]).read_text(encoding="utf-8")
        assert "ARG SOURCE_DATE_EPOCH=0" in dockerfile
        assert "SOURCE_DATE_EPOCH=${SOURCE_DATE_EPOCH}" in dockerfile
        assert "ARG BUILD_CREATED=1970-01-01T00:00:00Z" in dockerfile
        assert "ARG RELEASE_VERSION=development" in dockerfile
        assert "ARG SOURCE_REVISION=unknown" in dockerfile
        assert f'org.opencontainers.image.title="{contract["title"]}"' in dockerfile
        assert f'org.opencontainers.image.licenses="{contract["license"]}"' in dockerfile
        assert (
            'org.opencontainers.image.source="https://github.com/nashspence/riverhog"' in dockerfile
        )
        assert 'org.opencontainers.image.revision="${SOURCE_REVISION}"' in dockerfile
        assert 'org.opencontainers.image.version="${RELEASE_VERSION}"' in dockerfile
        assert 'org.opencontainers.image.created="${BUILD_CREATED}"' in dockerfile
        assert (
            'org.opencontainers.image.documentation="https://nashspence.github.io/riverhog/v1/"'
            in dockerfile
        )


def test_every_external_image_input_is_versioned_and_digest_pinned() -> None:
    observed: set[str] = set()
    for contract in IMAGE_CONTRACTS.values():
        dockerfile = (REPO_ROOT / contract["dockerfile"]).read_text(encoding="utf-8")
        observed.update(re.findall(r"(?m)^FROM (\S+)", dockerfile))
        observed.update(
            reference
            for reference in re.findall(r"(?m)^COPY --from=(\S+)", dockerfile)
            if "/" in reference
        )

    assert observed == PINNED_EXTERNAL_IMAGES


def test_every_external_compose_image_is_versioned_and_digest_pinned() -> None:
    observed: set[str] = set()
    for compose_path in REPO_ROOT.rglob("compose.yaml"):
        compose = yaml.safe_load(compose_path.read_text(encoding="utf-8"))
        for service in compose.get("services", {}).values():
            if "build" not in service:
                observed.add(str(service["image"]))

    assert observed == PINNED_EXTERNAL_COMPOSE_IMAGES


def test_mise_container_tools_use_disposable_locked_build_stages() -> None:
    mise_config = tomllib.loads((REPO_ROOT / "mise.toml").read_text(encoding="utf-8"))
    container_tools = set().union(*MISE_CONTAINER_TOOLS.values())
    assert container_tools | {"python"} == set(mise_config["tools"])

    observed_images = set()
    for name, contract in IMAGE_CONTRACTS.items():
        dockerfile = (REPO_ROOT / contract["dockerfile"]).read_text(encoding="utf-8")
        if f"FROM {MISE_IMAGE} AS mise" not in dockerfile:
            continue
        observed_images.add(name)

        assert f"FROM {MISE_IMAGE} AS mise" in dockerfile
        assert "COPY --from=mise /usr/local/bin/mise /usr/local/bin/mise" in dockerfile
        assert "COPY mise.toml mise.lock ./" in dockerfile
        install_match = re.search(r"mise install --locked (?P<tools>[^\\\n]+)", dockerfile)
        assert install_match is not None
        assert set(shlex.split(install_match.group("tools"))) == MISE_CONTAINER_TOOLS[name]
        assert "COPY --from=locked-tools " in dockerfile

        final_stage = dockerfile.rsplit("\nFROM ", maxsplit=1)[1]
        assert "/usr/local/bin/mise" not in final_stage

    assert observed_images == set(IMAGE_CONTRACTS)


def test_mise_artifacts_match_each_image_role() -> None:
    for name, contract in IMAGE_CONTRACTS.items():
        dockerfile = (REPO_ROOT / contract["dockerfile"]).read_text(encoding="utf-8")
        assert '"$(mise which uv)" /opt/riverhog-tools/bin/uv' in dockerfile
        if name == "test":
            assert "COPY --from=locked-tools /opt/riverhog-tools/bin/ /usr/local/bin/" in dockerfile
        else:
            assert (
                "COPY --from=locked-tools /opt/riverhog-tools/bin/uv /usr/local/bin/uv"
                in dockerfile
            )

    riverhog = (REPO_ROOT / IMAGE_CONTRACTS["riverhog"]["dockerfile"]).read_text(encoding="utf-8")
    assert '"$(mise which minisign)" /opt/riverhog-tools/bin/minisign' in riverhog
    assert 'test "$(minisign -v)" = "minisign 0.12"' in riverhog

    test = (REPO_ROOT / IMAGE_CONTRACTS["test"]["dockerfile"]).read_text(encoding="utf-8")
    for binary in ("age", "age-keygen", "age-plugin-batchpass", "minisign", "uv"):
        assert f'"$(mise which {binary})" /opt/riverhog-tools/bin/{binary}' in test
    assert 'test "$(exiftool -ver)" = "13.59"' in test
    observer = (REPO_ROOT / IMAGE_CONTRACTS["stove0-exiftool-observer"]["dockerfile"]).read_text(
        encoding="utf-8"
    )
    assert 'test "$(exiftool -ver)" = "13.59"' in observer


def test_production_images_use_the_common_unprivileged_runtime_identity() -> None:
    for name in NON_ROOT_RUNTIME_IMAGES:
        contract = IMAGE_CONTRACTS[name]
        dockerfile = (REPO_ROOT / contract["dockerfile"]).read_text(encoding="utf-8")
        final_stage = dockerfile.rsplit("\nFROM ", maxsplit=1)[1]
        assert "\nUSER 65532:65532\n" in final_stage

    mango_fish = (REPO_ROOT / IMAGE_CONTRACTS["mango-fish"]["dockerfile"]).read_text(
        encoding="utf-8"
    )
    assert "install -d -o 65532 -g 65532 -m 0700 /state" in mango_fish

    riverhog = (REPO_ROOT / IMAGE_CONTRACTS["riverhog"]["dockerfile"]).read_text(encoding="utf-8")
    assert "HOME=/tmp" in riverhog

    compose = yaml.safe_load(
        (REPO_ROOT / "riverhog/server/compose.yaml").read_text(encoding="utf-8")
    )
    for service_name in ("state", "app"):
        service = compose["services"][service_name]
        assert service["read_only"] is True
        assert service["tmpfs"] == ["/tmp:rw,noexec,nosuid,nodev,mode=700,uid=65532,gid=65532"]


def test_container_python_ownership_matches_the_supported_runtime_minor() -> None:
    mise_config = tomllib.loads((REPO_ROOT / "mise.toml").read_text(encoding="utf-8"))
    supported_minor = ".".join(mise_config["tools"]["python"].split(".")[:2])
    observed_python_bases: set[str] = set()

    for contract in IMAGE_CONTRACTS.values():
        dockerfile = (REPO_ROOT / contract["dockerfile"]).read_text(encoding="utf-8")
        observed_python_bases.update(re.findall(r"(?m)^FROM python:(\d+\.\d+)-slim@", dockerfile))

    assert observed_python_bases == {supported_minor}

    av1_dockerfile = (
        REPO_ROOT / IMAGE_CONTRACTS["stove0-nvenc-av1-opus-target"]["dockerfile"]
    ).read_text(encoding="utf-8")
    assert "assert sys.version_info[:2] == (3, 12)" in av1_dockerfile


def test_av1_source_builds_verify_the_exact_requested_commits() -> None:
    dockerfile = (
        REPO_ROOT / IMAGE_CONTRACTS["stove0-nvenc-av1-opus-target"]["dockerfile"]
    ).read_text(encoding="utf-8")

    assert 'test "$(git rev-parse HEAD)" = "${NV_CODEC_HEADERS_REF}"' in dockerfile
    assert 'test "$(git rev-parse HEAD)" = "${FFMPEG_REF}"' in dockerfile


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
        "with": {"version": "v0.36.0", "driver": "docker"},
    }
    assert steps["Install Mango Fish smoke toolchain"] == {
        "name": "Install Mango Fish smoke toolchain",
        "if": "matrix.target == 'mango-fish'",
        "uses": "jdx/mise-action@9e7f7633ff6f6d6048a9418a68d48f288f50eb14",
        "with": {"install_args": "python"},
    }
    assert steps["Resolve image metadata"] == {
        "name": "Resolve image metadata",
        "id": "image-metadata",
        "run": (
            'echo "created=$(git show -s --format=%cI HEAD)" >> "$GITHUB_OUTPUT"\n'
            'echo "epoch=$(git show -s --format=%ct HEAD)" >> "$GITHUB_OUTPUT"\n'
        ),
    }
    assert steps["Build image"] == {
        "name": "Build image",
        "uses": "docker/bake-action@d3418bd7d0e9324001bca92fa8ba175ea7e6dc9b",
        "with": {
            "source": ".",
            "files": "docker-bake.hcl",
            "targets": "${{ matrix.target }}",
            "load": True,
            "set": (
                "*.args.SOURCE_REVISION=${{ inputs.ref || github.sha }}\n"
                "*.args.BUILD_CREATED=${{ steps.image-metadata.outputs.created }}\n"
                "*.args.SOURCE_DATE_EPOCH=${{ steps.image-metadata.outputs.epoch }}\n"
                "*.args.RELEASE_VERSION=development\n"
                "*.cache-from=type=gha,scope=${{ matrix.target }}\n"
                "*.cache-to=type=gha,scope=${{ matrix.target }},mode=min,ignore-error=true\n"
            ),
        },
    }
    assert steps["Smoke Mango Fish image"] == {
        "name": "Smoke Mango Fish image",
        "if": "matrix.target == 'mango-fish'",
        "run": "make mango-fish-smoke",
    }
