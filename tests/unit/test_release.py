from __future__ import annotations

import importlib.util
import shutil
import sys
import tomllib
from collections import Counter
from pathlib import Path
from types import ModuleType

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts/release.py"


def load_script() -> ModuleType:
    spec = importlib.util.spec_from_file_location("riverhog_release", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _copy_release_contract(module: ModuleType, destination: Path) -> None:
    for relative in ("pyproject.toml", "uv.lock", "release.toml", "docker-bake.hcl"):
        shutil.copy2(REPO_ROOT / relative, destination / relative)
    for source in module._workspace_pyprojects(REPO_ROOT):
        relative = source.relative_to(REPO_ROOT)
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)


def test_release_contract_classifies_every_coordinated_distribution() -> None:
    module = load_script()

    projects = module.validate_release_contract(REPO_ROOT)

    assert len(projects) == 31
    assert {project.version for project in projects} == {"0.1.0"}
    assert Counter(project.role for project in projects) == {
        "end_user_artifact": 5,
        "deployed_implementation": 5,
        "reusable_library": 13,
        "internal_build_unit": 8,
    }
    assert {project.name for project in projects} >= {
        "jeb-client",
        "munchy-client",
        "riverhog-client",
        "riverhog-recover",
        "riverhog-server",
    }


def test_release_plan_is_exact_sha_bound_and_excludes_the_test_image() -> None:
    module = load_script()

    plan = module.build_release_plan(REPO_ROOT, "1.0.0", allow_dirty=True)

    assert plan["tag"] == "v1.0.0"
    assert len(plan["source_sha"]) == 40
    assert all(character in "0123456789abcdef" for character in plan["source_sha"])
    assert len(plan["python"]) == 31
    assert all(len(project["artifacts"]) == 2 for project in plan["python"])
    assert {image["target"] for image in plan["images"]} == set(module.RUNTIME_IMAGE_TARGETS)
    assert all(image["platforms"] == ["linux/amd64"] for image in plan["images"])
    assert all(
        image["tags"]
        == [
            f"{image['repository']}:1.0.0",
            f"{image['repository']}:sha-{plan['source_sha']}",
        ]
        for image in plan["images"]
    )
    assert "riverhog-test:dev" not in str(plan)
    assert plan["supporting_artifacts"] == {
        "documentation": "riverhog-docs-v1.0.0.tar.gz",
        "source": "riverhog-source-v1.0.0.tar.gz",
        "evidence": [
            "release-manifest.json",
            "SHA256SUMS",
            "SHA256SUMS.minisig",
            "release.spdx.json",
            "release.intoto.jsonl",
            "THIRD_PARTY_NOTICES.md",
        ],
    }
    markdown = module.render_release_markdown(plan)
    assert markdown.startswith("# Riverhog v1.0.0\n\n")
    assert f"Source: `{plan['source_sha']}`" in markdown
    assert "Initial v1 release; there is no previous release tag." in markdown


def test_coordinated_version_application_updates_all_internal_ranges(tmp_path: Path) -> None:
    module = load_script()
    _copy_release_contract(module, tmp_path)

    projects = module.apply_release_version(tmp_path, "1.0.0")

    internal_names = {project.name for project in projects}
    for pyproject in module._workspace_pyprojects(tmp_path):
        metadata = tomllib.loads(pyproject.read_text(encoding="utf-8"))["project"]
        assert metadata["version"] == "1.0.0"
        for dependency in metadata.get("dependencies", []):
            if module._dependency_name(dependency) in internal_names:
                assert dependency.endswith(">=1.0,<2.0")


def test_v1_release_rail_rejects_another_major() -> None:
    module = load_script()

    with pytest.raises(module.ReleaseError, match="only 1.x.y"):
        module.build_release_plan(REPO_ROOT, "2.0.0", allow_dirty=True)


def test_dry_run_trust_is_scoped_to_the_exact_sha_checkout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = load_script()
    monkeypatch.setenv("MISE_TRUSTED_CONFIG_PATHS", "/already/trusted")

    assert module._trusted_config_paths(tmp_path) == (
        f"{tmp_path}{module.os.pathsep}/already/trusted"
    )
