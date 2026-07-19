from __future__ import annotations

import ast
import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]

APPLICATION_MODULES = {
    "riverhog": {"riverhog_api", "riverhog_cli", "riverhog_core"},
    "munchy": {"munchy", "munchy_av1_nvenc", "munchy_cli", "munchy_runner"},
    "jeb": {"jeb"},
    "mango-fish": {"mango_fish"},
}
TOOL_MODULES = {"gogurt": {"gogurt"}}
OWNED_MODULES = {
    **APPLICATION_MODULES,
    **TOOL_MODULES,
}
ALL_OWNED_MODULES = set().union(*OWNED_MODULES.values())


def imported_roots(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(alias.name.partition(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            roots.add(node.module.partition(".")[0])
    return roots


def test_application_and_tool_implementations_do_not_cross_owner_boundaries() -> None:
    owners = {
        **{name: REPO / "apps" / name / "src" for name in APPLICATION_MODULES},
        **{name: REPO / "tools" / name / "src" for name in TOOL_MODULES},
    }
    violations: list[str] = []

    for owner, source in owners.items():
        foreign_modules = ALL_OWNED_MODULES - OWNED_MODULES[owner]
        for path in source.rglob("*.py"):
            crossed = sorted(imported_roots(path) & foreign_modules)
            if crossed:
                violations.append(f"{path.relative_to(REPO)} imports {', '.join(crossed)}")

    assert not violations, "\n".join(violations)


def test_focused_packages_do_not_import_application_or_tool_implementations() -> None:
    violations = [
        f"{path.relative_to(REPO)} imports {', '.join(crossed)}"
        for path in (REPO / "packages").rglob("*.py")
        if (crossed := sorted(imported_roots(path) & ALL_OWNED_MODULES))
    ]

    assert not violations, "\n".join(violations)


def test_application_images_copy_only_their_own_application_source() -> None:
    dockerfiles = {
        "riverhog": REPO / "apps/riverhog/Dockerfile",
        "jeb": REPO / "apps/jeb/Dockerfile",
        "mango-fish": REPO / "apps/mango-fish/Dockerfile",
        "munchy": REPO / "apps/munchy/runner/Dockerfile",
        "munchy-av1": REPO / "apps/munchy/targets/av1-nvenc/Dockerfile",
    }

    for owner, path in dockerfiles.items():
        copied_apps = {
            match.group(1)
            for match in re.finditer(r"^COPY apps/([^/\s]+)", path.read_text(), re.MULTILINE)
        }
        expected = "munchy" if owner.startswith("munchy") else owner
        assert copied_apps == {expected}
