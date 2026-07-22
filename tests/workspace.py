from __future__ import annotations

import tomllib
from pathlib import Path


def workspace_pyprojects(root: Path) -> list[Path]:
    config = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    members = config["tool"]["uv"]["workspace"]["members"]
    projects: list[Path] = []
    for pattern in members:
        for member in root.glob(pattern):
            pyproject = member / "pyproject.toml"
            if not pyproject.is_file():
                raise AssertionError(f"workspace member lacks pyproject.toml: {member}")
            projects.append(pyproject)
    return sorted(projects)
