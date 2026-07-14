from __future__ import annotations

import re
from pathlib import Path

import pytest

from riverhog_core.domain.errors import InvalidTarget
from riverhog_core.domain.selectors import parse_target

REPO = Path(__file__).resolve().parents[2]
ENTRYPOINTS = {REPO / "README.md", REPO / "AGENTS.md"}
MARKDOWN_LINK_RE = re.compile(r"\[[^\]]+\]\(([^)]+)\)")
IGNORED_TREES = {
    ".git",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".venv",
    "build",
    "dist",
    "node_modules",
}


def _markdown_files() -> set[Path]:
    return {
        path.resolve()
        for path in REPO.rglob("*.md")
        if not IGNORED_TREES.intersection(path.relative_to(REPO).parts)
    }


def _local_links(path: Path) -> set[Path]:
    links: set[Path] = set()
    for target in MARKDOWN_LINK_RE.findall(path.read_text(encoding="utf-8")):
        if "://" in target or target.startswith("#"):
            continue
        relative = target.split("#", 1)[0]
        if not relative:
            continue
        links.add((path.parent / relative).resolve())
    return links


def test_all_markdown_is_reachable_and_links_resolve() -> None:
    markdown = _markdown_files()
    reachable: set[Path] = set()
    pending = list(ENTRYPOINTS)

    while pending:
        path = pending.pop()
        if path in reachable:
            continue
        assert path.is_file(), f"missing documentation target: {path.relative_to(REPO)}"
        reachable.add(path)
        for target in _local_links(path):
            assert target.exists(), (
                f"{path.relative_to(REPO)} links to missing {target.relative_to(REPO)}"
            )
            if target.suffix == ".md" and target not in reachable:
                pending.append(target)

    assert markdown == reachable


def _examples(section: str) -> list[str]:
    text = (REPO / "docs" / "selector-grammar.md").read_text(encoding="utf-8")
    match = re.search(rf"## {section}\n\n```text\n(?P<body>.*?)\n```", text, re.DOTALL)
    assert match is not None
    return [line for line in match.group("body").splitlines() if line]


@pytest.mark.parametrize("target", _examples("Valid examples"))
def test_documented_valid_selectors_use_the_real_parser(target: str) -> None:
    assert parse_target(target).canonical == target


@pytest.mark.parametrize("target", _examples("Invalid examples"))
def test_documented_invalid_selectors_use_the_real_parser(target: str) -> None:
    with pytest.raises(InvalidTarget):
        parse_target(target)
