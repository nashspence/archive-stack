from __future__ import annotations

import re
import shlex
import tomllib
from pathlib import Path

import yaml

from tests.workspace import workspace_pyprojects

REPO_ROOT = Path(__file__).resolve().parents[2]


def _make_words(name: str) -> list[str]:
    lines = (REPO_ROOT / "Makefile").read_text(encoding="utf-8").splitlines()
    start = next(
        index
        for index, line in enumerate(lines)
        if re.match(rf"^{re.escape(name)}\s*(?:\?|:) ?=", line)
        or re.match(rf"^{re.escape(name)}\s*=", line)
    )
    value = lines[start].split("=", maxsplit=1)[1].strip()
    while value.endswith("\\"):
        value = value[:-1].rstrip() + " " + lines[start + 1].strip()
        start += 1
    return shlex.split(value)


def test_ignore_files_are_concise_and_cover_local_state() -> None:
    gitignore = (REPO_ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
    dockerignore = (REPO_ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()

    assert len(gitignore) == len(set(gitignore))
    assert len(dockerignore) == len(set(dockerignore))
    assert {
        ".env",
        ".env.*",
        "/.riverhog/",
        "/build/",
        "/dist/",
        "mise.local.lock",
        "mise.local.toml",
    } <= set(gitignore)
    assert {
        "/.git",
        "/.riverhog/",
        "/.venv/",
        "/build/",
        "/dist/",
        "/docs/",
        "**/.env",
        "**/.env.*",
        "**/__pycache__/",
        "**/*.pyc",
    } <= set(dockerignore)


def test_repo_owns_toolchain_python_lock_and_runtime_exports() -> None:
    mise = tomllib.loads((REPO_ROOT / "mise.toml").read_text(encoding="utf-8"))
    pyproject = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    gitignore = (REPO_ROOT / ".gitignore").read_text(encoding="utf-8")

    assert mise["tools"]["python"] == "3.12.3"
    assert mise["tools"]["uv"] == "0.11.24"
    assert mise["tools"]["age"] == "1.3.1"
    assert mise["tools"]["minisign"] == "0.12"
    assert mise["settings"]["http_retries"] == 5
    assert mise["settings"]["lockfile"] is True
    assert "dev" in pyproject["dependency-groups"]
    assert pyproject["tool"]["uv"]["workspace"]["members"] == [
        "companions/*/client",
        "companions/*/server",
        "packages/*",
        "reference/gogurt/*",
        "reference/riverhog/*/*",
        "reference/stove0/observers/*",
        "reference/stove0/targets/*/target",
        "reference/stove0/targets/*/review-sampler",
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


def test_every_workspace_component_enters_the_default_python_gates() -> None:
    config = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    mypy_sources = set(_make_words("MYPY_SOURCES"))
    unit_roots = tuple(REPO_ROOT / path for path in _make_words("TESTS"))
    compile_roots = tuple(REPO_ROOT / path for path in _make_words("PYTHON_PATHS"))
    pytest_roots = tuple(
        REPO_ROOT / path for path in config["tool"]["pytest"]["ini_options"]["testpaths"]
    )

    for pyproject in workspace_pyprojects(REPO_ROOT):
        component = pyproject.parent
        source = component / "src"
        if source.is_dir():
            assert source.relative_to(REPO_ROOT).as_posix() in mypy_sources
            assert any(source.is_relative_to(root) for root in compile_roots)

        tests = component / "tests"
        if tests.is_dir() and any(tests.rglob("test_*.py")):
            assert any(tests.is_relative_to(root) for root in unit_roots)
            assert any(tests.is_relative_to(root) for root in pytest_roots)


def test_native_test_tools_are_pinned_to_reproducible_sources() -> None:
    mise = tomllib.loads((REPO_ROOT / "mise.toml").read_text(encoding="utf-8"))
    assert mise["tools"]["http:exiftool"] == {
        "version": "13.59",
        "bin": "exiftool",
        "url": "https://github.com/exiftool/exiftool/archive/refs/tags/13.59.tar.gz",
        "checksum": "sha256:87d3317882fdae9cb4dcfe57a96a378d0132ffc02c731315bf128b19ddcf7aac",
        "size": 8653399,
        "strip_components": "1",
    }

    vector_runner = REPO_ROOT / "scripts/test_c2sp_vectors.sh"
    vector_script = vector_runner.read_text(encoding="utf-8")
    assert vector_runner.stat().st_mode & 0o111
    assert "1e3d2860d46e94e777e1b17c7a6f2436387e3ecc" in vector_script
    assert "516ce226b3d53c9859fcc973edc8976078dcee5600f72f7c27442857e4a3d16c" in vector_script

    makefile = (REPO_ROOT / "Makefile").read_text(encoding="utf-8")
    assert 'c2sp-vectors:\n\t@MISE_BIN="$(MISE_BIN)" ./scripts/test_c2sp_vectors.sh' in makefile
    assert (
        "dependency-readiness:\n"
        "\t$(call UV_CMD,python scripts/check_dependency_readiness.py $(args))"
    ) in makefile


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


def test_dependabot_owns_every_release_dependency_ecosystem() -> None:
    config = yaml.safe_load((REPO_ROOT / ".github/dependabot.yml").read_text(encoding="utf-8"))
    updates = {entry["package-ecosystem"]: entry for entry in config["updates"]}

    assert config["version"] == 2
    assert set(updates) == {"uv", "github-actions", "docker"}
    assert updates["uv"]["directory"] == "/"
    assert updates["github-actions"]["directory"] == "/"

    docker_directories = {
        f"/{path.parent.relative_to(REPO_ROOT)}" for path in REPO_ROOT.rglob("Dockerfile")
    }
    assert set(updates["docker"]["directories"]) == docker_directories
    assert updates["docker"]["ignore"] == [
        {
            "dependency-name": "python",
            "versions": [">= 3.13"],
        }
    ]


def test_dependabot_updates_share_one_low_noise_policy() -> None:
    config = yaml.safe_load((REPO_ROOT / ".github/dependabot.yml").read_text(encoding="utf-8"))
    expected_groups = {
        "version-updates": {
            "applies-to": "version-updates",
            "patterns": ["*"],
        },
        "security-updates": {
            "applies-to": "security-updates",
            "patterns": ["*"],
        },
    }
    expected_schedule = {
        "interval": "weekly",
        "day": "monday",
        "time": "04:00",
        "timezone": "Etc/UTC",
    }

    for update in config["updates"]:
        assert update["schedule"] == expected_schedule
        assert update["open-pull-requests-limit"] == 1
        assert update["assignees"] == ["nashspence"]
        assert update["commit-message"] == {"prefix": "deps"}
        assert update["groups"] == expected_groups
