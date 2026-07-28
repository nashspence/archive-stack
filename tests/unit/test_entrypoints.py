from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
ENTRYPOINTS = {REPO / "README.md", REPO / "AGENTS.md"}
DURABLE_CONTEXT = {
    REPO / "docs/architecture.md",
    REPO / "docs/archive-operations.md",
    REPO / "docs/recovery-without-riverhog.md",
}
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


def test_both_entrypoints_route_to_each_durable_context_document() -> None:
    for entrypoint in ENTRYPOINTS:
        assert DURABLE_CONTEXT <= _local_links(entrypoint)
