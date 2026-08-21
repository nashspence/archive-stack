from __future__ import annotations

import tomllib
from pathlib import Path

from tests.workspace import workspace_pyprojects

REPO_ROOT = Path(__file__).resolve().parents[2]


def normalize_name(name: str) -> str:
    return name.replace("_", "-").lower()


def test_every_workspace_distribution_is_present_once_in_the_uv_lock() -> None:
    workspace_projects = {
        normalize_name(tomllib.loads(path.read_text(encoding="utf-8"))["project"]["name"])
        for path in workspace_pyprojects(REPO_ROOT)
    }
    locked = tomllib.loads((REPO_ROOT / "uv.lock").read_text(encoding="utf-8"))
    locked_names = [normalize_name(package["name"]) for package in locked["package"]]

    assert workspace_projects
    for name in workspace_projects:
        assert locked_names.count(name) == 1


def test_workspace_packages_resolve_internal_dependencies_through_uv_sources() -> None:
    workspace_config = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    workspace_sources = workspace_config.get("tool", {}).get("uv", {}).get("sources", {})
    workspace_projects = {
        normalize_name(tomllib.loads(path.read_text(encoding="utf-8"))["project"]["name"])
        for path in workspace_pyprojects(REPO_ROOT)
    }

    for path in workspace_pyprojects(REPO_ROOT):
        pyproject = tomllib.loads(path.read_text(encoding="utf-8"))
        dependencies = {
            normalize_name(dependency.split("[", 1)[0].split(">", 1)[0].split("=", 1)[0])
            for dependency in pyproject["project"].get("dependencies", [])
        }
        internal = dependencies & workspace_projects
        raw_sources = {
            **workspace_sources,
            **pyproject.get("tool", {}).get("uv", {}).get("sources", {}),
        }
        sources = {normalize_name(name): source for name, source in raw_sources.items()}
        assert internal <= sources.keys()
        assert all(sources[name] == {"workspace": True} for name in internal)


def test_storage_adapter_client_declares_its_http2_transport() -> None:
    pyproject = tomllib.loads(
        (REPO_ROOT / "packages/riverhog-storage-adapter-support/pyproject.toml").read_text(
            encoding="utf-8"
        )
    )

    assert any(
        dependency.startswith("httpx[http2]") for dependency in pyproject["project"]["dependencies"]
    )
