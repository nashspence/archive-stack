from __future__ import annotations

import tomllib
from pathlib import Path

from tests.workspace import workspace_pyprojects

REPO_ROOT = Path(__file__).resolve().parents[2]
SERVER_PROJECTS = {
    Path("riverhog/server/pyproject.toml"),
    Path("companions/munchy/server/pyproject.toml"),
    Path("companions/munchy/server/targets/av1-nvenc/pyproject.toml"),
    Path("companions/jeb/server/pyproject.toml"),
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
        "companions/munchy/server/**",
        "companions/jeb/server/**",
    ]
    assert annotations[1]["SPDX-License-Identifier"] == "CAL-1.0"
    assert annotations[2]["path"] == [
        "riverhog/server/openapi/**",
        "companions/munchy/server/openapi/**",
        "companions/jeb/server/openapi/**",
    ]
    assert annotations[2]["SPDX-License-Identifier"] == "Apache-2.0"


def test_every_workspace_distribution_declares_and_contains_its_component_license() -> None:
    apache = (REPO_ROOT / "LICENSES/Apache-2.0.txt").read_bytes()
    cal = (REPO_ROOT / "LICENSES/CAL-1.0.txt").read_bytes()
    assert b"Take any action with the Work that would infringe any patent" in cal

    for pyproject in workspace_pyprojects(REPO_ROOT):
        relative = pyproject.relative_to(REPO_ROOT)
        expected_license = "CAL-1.0" if relative in SERVER_PROJECTS else "Apache-2.0"
        expected_text = cal if expected_license == "CAL-1.0" else apache
        config = tomllib.loads(pyproject.read_text(encoding="utf-8"))
        assert config["build-system"]["requires"] == ["hatchling>=1.27.0"]
        assert config["project"]["license"] == expected_license
        assert config["project"]["license-files"] == ["LICENSE"]
        assert (pyproject.parent / "LICENSE").read_bytes() == expected_text


def test_reference_recovery_is_independent_and_advertised() -> None:
    config = tomllib.loads(
        (REPO_ROOT / "riverhog/recovery/pyproject.toml").read_text(encoding="utf-8")
    )
    architecture = " ".join(
        (REPO_ROOT / "docs/architecture.md").read_text(encoding="utf-8").split()
    )

    assert config["project"]["dependencies"] == ["PyYAML>=6,<7"]
    assert config["project"]["scripts"] == {"riverhog-recover": "riverhog_recover.cli:main"}
    assert "independently packaged, permissively licensed reference implementation" in architecture
    assert "recoverable without Riverhog using standard tools" in architecture


def test_published_images_carry_source_and_license_identity() -> None:
    images = {
        "riverhog/server/Dockerfile": "CAL-1.0",
        "companions/munchy/server/Dockerfile": "CAL-1.0",
        "companions/munchy/server/targets/av1-nvenc/Dockerfile": "CAL-1.0",
        "companions/jeb/server/Dockerfile": "CAL-1.0",
        "utilities/mango-fish/Dockerfile": "Apache-2.0",
    }
    for relative, expected_license in images.items():
        dockerfile = (REPO_ROOT / relative).read_text(encoding="utf-8")
        assert f'org.opencontainers.image.licenses="{expected_license}"' in dockerfile
        assert 'org.opencontainers.image.revision="${SOURCE_REVISION}"' in dockerfile
        assert "LICENSES/Apache-2.0.txt /usr/share/licenses/riverhog/Apache-2.0.txt" in dockerfile
        assert "LICENSES/CAL-1.0.txt /usr/share/licenses/riverhog/CAL-1.0.txt" in dockerfile
        assert "THIRD_PARTY_NOTICES.md /usr/share/doc/riverhog/THIRD_PARTY_NOTICES.md" in dockerfile


def test_every_first_party_image_build_requests_an_sbom_attestation() -> None:
    makefile = (REPO_ROOT / "Makefile").read_text(encoding="utf-8")
    riverhog_build = (REPO_ROOT / "scripts/build_riverhog.sh").read_text(encoding="utf-8")
    test_build = (REPO_ROOT / "scripts/build_test.sh").read_text(encoding="utf-8")
    compose_helper = (REPO_ROOT / "scripts/_compose_env.sh").read_text(encoding="utf-8")

    assert makefile.count("--sbom=true") == 4
    assert "compose build --sbom=true app" in riverhog_build
    assert "compose build --sbom=true test" in test_build
    assert 'compose build --sbom=true "${service}"' in compose_helper


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
