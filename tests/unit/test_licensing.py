from __future__ import annotations

import tomllib
from pathlib import Path

import yaml

from tests.workspace import workspace_pyprojects

REPO_ROOT = Path(__file__).resolve().parents[2]
SERVER_PROJECTS = {
    Path("riverhog/server/pyproject.toml"),
    Path("riverhog/storage-adapter-aws/pyproject.toml"),
    Path("riverhog/storage-adapter-backblaze/pyproject.toml"),
    Path("companions/stove0/server/pyproject.toml"),
    Path("companions/stove0-ffprobe-sampling-observer/pyproject.toml"),
    Path("companions/stove0-nvenc-av1-opus-target/pyproject.toml"),
    Path("companions/stove0-opus-target/pyproject.toml"),
    Path("companions/stove0-review-target/pyproject.toml"),
}


def test_reuse_policy_assigns_an_apache_default_and_narrow_server_overrides() -> None:
    policy = tomllib.loads((REPO_ROOT / "REUSE.toml").read_text(encoding="utf-8"))
    assert policy["version"] == 1
    annotations = policy["annotations"]
    assert annotations[0] == {
        "path": "**",
        "precedence": "override",
        "SPDX-FileCopyrightText": "2026 Nash Spence",
        "SPDX-License-Identifier": "Apache-2.0",
    }
    assert annotations[1]["path"] == [
        "riverhog/server/**",
        "riverhog/storage-adapter-aws/**",
        "riverhog/storage-adapter-backblaze/**",
        "companions/stove0/server/**",
        "companions/stove0-ffprobe-sampling-observer/**",
        "companions/stove0-nvenc-av1-opus-target/**",
        "companions/stove0-opus-target/**",
        "companions/stove0-review-target/**",
    ]
    assert annotations[1]["SPDX-License-Identifier"] == "CAL-1.0"
    assert annotations[2]["path"] == [
        "riverhog/server/openapi/**",
    ]
    assert annotations[2]["SPDX-License-Identifier"] == "Apache-2.0"
    assert len(annotations) == 3
    minisign_license = (REPO_ROOT / "third_party/minisign/0.12/LICENSE").read_text(encoding="utf-8")
    assert "Copyright (c) 2015-2025" in minisign_license
    assert "Permission to use, copy, modify, and/or distribute" in minisign_license


def test_every_workspace_distribution_declares_and_contains_its_component_license() -> None:
    apache = (REPO_ROOT / "LICENSES/Apache-2.0.txt").read_bytes()
    cal = (REPO_ROOT / "LICENSES/CAL-1.0.txt").read_bytes()
    assert b"Take any action with the Work that would infringe any patent" in cal

    for pyproject in workspace_pyprojects(REPO_ROOT):
        relative = pyproject.relative_to(REPO_ROOT)
        expected_license = "CAL-1.0" if relative in SERVER_PROJECTS else "Apache-2.0"
        expected_text = cal if expected_license == "CAL-1.0" else apache
        config = tomllib.loads(pyproject.read_text(encoding="utf-8"))
        assert config["project"]["license"] == expected_license
        assert config["project"]["license-files"] == ["LICENSE"]
        assert (pyproject.parent / "LICENSE").read_bytes() == expected_text


def test_every_workspace_distribution_uses_the_canonical_build_system() -> None:
    for pyproject in workspace_pyprojects(REPO_ROOT):
        config = tomllib.loads(pyproject.read_text(encoding="utf-8"))
        assert config["build-system"] == {
            "requires": ["hatchling>=1.31.0"],
            "build-backend": "hatchling.build",
        }


def test_reference_recovery_is_independent_and_advertised() -> None:
    config = tomllib.loads(
        (REPO_ROOT / "riverhog/recovery/pyproject.toml").read_text(encoding="utf-8")
    )
    architecture = " ".join(
        (REPO_ROOT / "docs/architecture.md").read_text(encoding="utf-8").split()
    )

    assert config["project"]["dependencies"] == []
    assert config["project"]["scripts"] == {"riverhog-recover": "riverhog_recover.cli:main"}
    assert "permissively licensed reference" in architecture
    assert "archives remain recoverable with standard tools" in architecture


def test_published_images_carry_source_and_license_identity() -> None:
    images = {
        "riverhog/server/Dockerfile": "CAL-1.0",
        "riverhog/ftp-adapter/Dockerfile": "Apache-2.0",
        "riverhog/storage-adapter-aws/Dockerfile": "CAL-1.0",
        "riverhog/storage-adapter-backblaze/Dockerfile": "CAL-1.0",
        "companions/stove0/server/Dockerfile": "CAL-1.0",
        "companions/stove0-ffprobe-sampling-observer/Dockerfile": "CAL-1.0",
        "companions/stove0-nvenc-av1-opus-target/Dockerfile": "CAL-1.0",
        "companions/stove0-opus-target/Dockerfile": "CAL-1.0",
        "companions/stove0-review-target/Dockerfile": "CAL-1.0",
        "utilities/mango-fish/Dockerfile": "Apache-2.0",
    }
    for relative, expected_license in images.items():
        dockerfile = (REPO_ROOT / relative).read_text(encoding="utf-8")
        assert f'org.opencontainers.image.licenses="{expected_license}"' in dockerfile
        assert 'org.opencontainers.image.revision="${SOURCE_REVISION}"' in dockerfile
        assert "LICENSES/Apache-2.0.txt /usr/share/licenses/riverhog/Apache-2.0.txt" in dockerfile
        if expected_license == "CAL-1.0":
            assert "LICENSES/CAL-1.0.txt /usr/share/licenses/riverhog/CAL-1.0.txt" in dockerfile
        assert "THIRD_PARTY_NOTICES.md /usr/share/doc/riverhog/THIRD_PARTY_NOTICES.md" in dockerfile


def test_standalone_runtime_tools_preserve_their_exact_attribution_text() -> None:
    riverhog = (REPO_ROOT / "riverhog/server/Dockerfile").read_text(encoding="utf-8")
    av1 = (REPO_ROOT / "companions/stove0-nvenc-av1-opus-target/Dockerfile").read_text(
        encoding="utf-8"
    )

    assert (
        "third_party/minisign/0.12/LICENSE "
        "/usr/share/licenses/riverhog-third-party/minisign/0.12/LICENSE"
    ) in riverhog
    assert "riverhog-third-party/ffmpeg/${FFMPEG_REF}/COPYING.GPLv2" in av1
    assert "riverhog-third-party/nv-codec-headers/${NV_CODEC_HEADERS_REF}/ATTRIBUTION" in av1
    assert "awk '1; /\\*\\// { exit }'" in av1


def test_every_first_party_image_build_requests_an_sbom_attestation() -> None:
    bake = (REPO_ROOT / "docker-bake.hcl").read_text(encoding="utf-8")
    makefile = (REPO_ROOT / "Makefile").read_text(encoding="utf-8")
    workflow = yaml.safe_load((REPO_ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8"))
    compose_helper = (REPO_ROOT / "scripts/_compose_env.sh").read_text(encoding="utf-8")

    image_targets = [
        "riverhog",
        "riverhog-ftp-adapter",
        "riverhog-storage-adapter-aws",
        "riverhog-storage-adapter-backblaze",
        "stove0",
        "stove0-ffprobe-sampling-observer",
        "stove0-nvenc-av1-opus-target",
        "stove0-opus-target",
        "stove0-review-target",
        "mango-fish",
        "test",
    ]
    sbom_generator = (
        "docker.io/docker/buildkit-syft-scanner:stable-1@"
        "sha256:79e7b013cbec16bbb436f312819a49a4a57752b2270c1a9332ae1a10fcc82a68"
    )
    assert bake.count('target "') == len(image_targets) + 1
    assert 'target "image-common"' in bake
    assert f'"type=sbom,generator={sbom_generator}"' in bake
    assert all(f'target "{target}"' in bake for target in image_targets)
    assert bake.count('inherits   = ["image-common"]') == len(image_targets)
    assert 'docker buildx bake --file "$(BAKE_FILE)" --load' in makefile
    image_steps = workflow["jobs"]["images"]["steps"]
    assert (
        next(step for step in image_steps if step["name"] == "Build image")["with"]["files"]
        == "docker-bake.hcl"
    )
    assert (
        f'local sbom_generator="{sbom_generator}"' in compose_helper
        and 'compose build --sbom="generator=${sbom_generator}" "${service}"' in compose_helper
    )


def test_entrypoint_documents_route_to_one_operational_disclaimer() -> None:
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    durable_documents = [
        " ".join(path.read_text(encoding="utf-8").split())
        for path in (
            REPO_ROOT / "README.md",
            REPO_ROOT / "docs/architecture.md",
            REPO_ROOT / "docs/operator-responsibilities.md",
        )
    ]
    normalized_readme = " ".join(readme.split())

    assert "one operator per deployment" in normalized_readme
    assert "multi-tenant storage service" in normalized_readme
    assert "does not guarantee preservation" in normalized_readme
    assert "confidentiality, or recoverability" in normalized_readme
    assert (
        sum(document.count("does not guarantee preservation") for document in durable_documents)
        == 1
    )
