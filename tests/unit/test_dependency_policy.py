from __future__ import annotations

import os
import re
import tomllib
from pathlib import Path

from tests.workspace import workspace_pyprojects

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_repo_owns_toolchain_python_lock_and_runtime_exports() -> None:
    mise = tomllib.loads((REPO_ROOT / "mise.toml").read_text(encoding="utf-8"))
    pyproject = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    gitignore = (REPO_ROOT / ".gitignore").read_text(encoding="utf-8")

    assert mise["tools"]["python"] == "3.12.3"
    assert mise["tools"]["uv"] == "0.11.24"
    assert mise["tools"]["age"] == "1.3.1"
    assert mise["tools"]["minisign"] == "0.12"
    assert mise["env"]["_"]["path"] == "tools"
    assert mise["settings"]["lockfile"] is True
    assert "dev" in pyproject["dependency-groups"]
    assert pyproject["tool"]["uv"]["workspace"]["members"] == [
        "companions/*/client",
        "companions/*/server",
        "packages/*",
        "riverhog/*",
        "utilities/*",
    ]
    assert (REPO_ROOT / "mise.lock").is_file()
    assert (REPO_ROOT / "uv.lock").is_file()
    assert "mise.local.toml" in gitignore
    assert "mise.local.lock" in gitignore

    members = workspace_pyprojects(REPO_ROOT)
    assert members
    for member in members:
        project = tomllib.loads(member.read_text(encoding="utf-8"))["project"]
        assert project["requires-python"] == ">=3.12"


def test_native_test_tools_are_pinned_to_reproducible_sources() -> None:
    exiftool = REPO_ROOT / "tools/exiftool"
    stub = tomllib.loads(exiftool.read_text(encoding="utf-8").split("\n", 1)[1])

    assert os.access(exiftool, os.X_OK)
    assert stub == {
        "version": "13.59",
        "bin": "exiftool",
        "url": "https://github.com/exiftool/exiftool/archive/refs/tags/13.59.tar.gz",
        "checksum": "sha256:87d3317882fdae9cb4dcfe57a96a378d0132ffc02c731315bf128b19ddcf7aac",
        "size": 8653399,
        "strip_components": "1",
    }

    vector_runner = REPO_ROOT / "scripts/test_c2sp_vectors.sh"
    vector_script = vector_runner.read_text(encoding="utf-8")
    assert os.access(vector_runner, os.X_OK)
    assert "1e3d2860d46e94e777e1b17c7a6f2436387e3ecc" in vector_script
    assert "516ce226b3d53c9859fcc973edc8976078dcee5600f72f7c27442857e4a3d16c" in vector_script

    makefile = (REPO_ROOT / "Makefile").read_text(encoding="utf-8")
    assert 'c2sp-vectors:\n\t@MISE_BIN="$(MISE_BIN)" ./scripts/test_c2sp_vectors.sh' in makefile


def test_workspace_distributions_publish_their_inline_types() -> None:
    members = workspace_pyprojects(REPO_ROOT)

    for member in members:
        config = tomllib.loads(member.read_text(encoding="utf-8"))
        package_paths = config["tool"]["hatch"]["build"]["targets"]["wheel"]["packages"]
        for package_path in package_paths:
            marker = member.parent / package_path / "py.typed"
            assert marker.is_file(), f"{marker.relative_to(REPO_ROOT)} is missing"


def test_workspace_distributions_bound_internal_dependency_versions() -> None:
    configs = {
        member: tomllib.loads(member.read_text(encoding="utf-8"))
        for member in workspace_pyprojects(REPO_ROOT)
    }
    workspace_names = {
        str(config["project"]["name"]).replace("_", "-").lower() for config in configs.values()
    }

    for member, config in configs.items():
        for requirement in config["project"].get("dependencies", []):
            name = re.split(r"[<>=!~;\[]", str(requirement), maxsplit=1)[0]
            if name.replace("_", "-").lower() not in workspace_names:
                continue
            assert ">=" in requirement and "<" in requirement, (
                f"{member.relative_to(REPO_ROOT)} does not bound internal dependency "
                f"{requirement!r}"
            )
