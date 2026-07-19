from __future__ import annotations

import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_repo_owns_toolchain_python_lock_and_runtime_exports() -> None:
    mise = tomllib.loads((REPO_ROOT / "mise.toml").read_text(encoding="utf-8"))
    pyproject = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    gitignore = (REPO_ROOT / ".gitignore").read_text(encoding="utf-8")

    assert mise["tools"]["python"] == "3.12.3"
    assert mise["tools"]["uv"] == "0.11.24"
    assert mise["settings"]["lockfile"] is True
    assert "dev" in pyproject["dependency-groups"]
    assert pyproject["tool"]["uv"]["workspace"]["members"] == [
        "apps/*",
        "packages/*",
        "tools/*",
    ]
    assert (REPO_ROOT / "mise.lock").is_file()
    assert (REPO_ROOT / "uv.lock").is_file()
    assert "mise.local.toml" in gitignore
    assert "mise.local.lock" in gitignore

    members = sorted(
        path
        for owner in ("apps", "packages", "tools")
        for path in (REPO_ROOT / owner).glob("*/pyproject.toml")
    )
    assert members
    for member in members:
        project = tomllib.loads(member.read_text(encoding="utf-8"))["project"]
        assert project["requires-python"] == ">=3.12"


def test_workspace_distributions_publish_their_inline_types() -> None:
    members = sorted(
        path
        for owner in ("apps", "packages", "tools")
        for path in (REPO_ROOT / owner).glob("*/pyproject.toml")
    )

    for member in members:
        config = tomllib.loads(member.read_text(encoding="utf-8"))
        package_paths = config["tool"]["hatch"]["build"]["targets"]["wheel"]["packages"]
        for package_path in package_paths:
            marker = member.parent / package_path / "py.typed"
            assert marker.is_file(), f"{marker.relative_to(REPO_ROOT)} is missing"
