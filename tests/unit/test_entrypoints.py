from __future__ import annotations

import re
from pathlib import Path

from tests.workspace import workspace_pyprojects

REPO = Path(__file__).resolve().parents[2]
ENTRYPOINTS = {REPO / "README.md", REPO / "AGENTS.md"}
DURABLE_CONTEXT = {REPO / "docs/architecture.md"}
REPOSITORY_MAP_TARGETS = {
    REPO / "riverhog/server",
    REPO / "riverhog/client",
    REPO / "riverhog/recovery",
    REPO / "companions",
    REPO / "reference/gogurt",
    REPO / "reference/riverhog",
    REPO / "reference/stove0",
    REPO / "utilities",
    REPO / "packages",
}
MARKDOWN_LINK_RE = re.compile(r"\[[^\]]+\]\(([^)]+)\)")
ARCHITECTURE_CATEGORY_RE = re.compile(r"^- \*\*([^*]+)\.\*\*", flags=re.MULTILINE)
ARCHITECTURE_CATEGORIES = {
    "Authority model": [
        "Archive authority",
        "Trust boundary",
        "Operational state",
        "Provenance authority",
        "Collection views",
        "Deployment configuration",
    ],
    "Boundary model": [
        "Implementation ownership",
        "Public contracts",
        "Riverhog platform",
        "Ingress adapters",
        "Storage adapters",
        "Companions",
        "Extensions",
        "Transfer path",
    ],
}
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


def test_main_context_documents_are_exact_and_directly_routed() -> None:
    assert set((REPO / "docs").glob("*.md")) == DURABLE_CONTEXT

    for entrypoint in ENTRYPOINTS:
        links = [
            (entrypoint.parent / target.split("#", 1)[0]).resolve()
            for target in MARKDOWN_LINK_RE.findall(entrypoint.read_text(encoding="utf-8"))
            if "://" not in target and not target.startswith("#")
        ]
        direct_context = [link for link in links if link.parent == REPO / "docs"]
        assert len(direct_context) == len(DURABLE_CONTEXT)
        assert set(direct_context) == DURABLE_CONTEXT


def test_readme_section_order_is_intentional() -> None:
    readme = (REPO / "README.md").read_text(encoding="utf-8")

    assert re.findall(r"^## (.+)$", readme, flags=re.MULTILINE) == [
        "Deployment scope",
        "Contributions",
        "Start here",
        "Context",
    ]


def test_agents_requires_post_push_github_validation() -> None:
    agents = " ".join((REPO / "AGENTS.md").read_text(encoding="utf-8").split())

    assert "watch the pushed commit's GitHub Actions checks through completion" in agents
    assert "Required GitHub checks are part of complete validation" in agents
    assert "`release.toml` owns the release-governance policy" in agents
    assert "fully green `release/v1` pull request" in agents
    assert "fast-forward `main` to that exact accepted commit" in agents
    assert "Provider qualification stays disabled" in agents
    assert "never moves a v1 tag" in agents


def test_agents_requires_locked_disposable_container_tool_stages() -> None:
    agents = " ".join((REPO / "AGENTS.md").read_text(encoding="utf-8").split())

    assert "mise install --locked" in agents
    assert "digest-pinned disposable build stage" in agents
    assert "copy only its required runtime artifacts forward" in agents


def test_architecture_is_scoped_to_quick_context() -> None:
    architecture = (REPO / "docs/architecture.md").read_text(encoding="utf-8")

    assert re.findall(r"^## (.+)$", architecture, flags=re.MULTILINE) == [
        "Authority model",
        "Boundary model",
        "Repository map",
    ]
    assert len(architecture.split()) <= 425


def test_architecture_categories_follow_the_durable_mental_model() -> None:
    architecture = (REPO / "docs/architecture.md").read_text(encoding="utf-8")

    for heading, expected in ARCHITECTURE_CATEGORIES.items():
        section = architecture.partition(f"## {heading}\n")[2].partition("\n## ")[0]
        assert ARCHITECTURE_CATEGORY_RE.findall(section) == expected


def test_architecture_states_the_repo_wide_provenance_authority_policy() -> None:
    architecture = " ".join((REPO / "docs/architecture.md").read_text(encoding="utf-8").split())

    assert "Per-file provenance is append-only custody history" in architecture
    assert "Journals remain exact prefixes across handoffs" in architecture
    assert "omissions require a reason" in architecture
    assert "database rows are a rebuildable projection" in architecture


def test_architecture_states_the_direct_to_final_ingress_authority_policy() -> None:
    architecture = " ".join((REPO / "docs/architecture.md").read_text(encoding="utf-8").split())

    assert (
        "Ingress encrypts there before writing immutable final-object units to the selected "
        "archive store; ingress is not a storage tier"
    ) in architecture
    assert (
        "Only sealed objects and published immutable roots are archive authority"
    ) in architecture


def test_repository_map_exactly_covers_the_workspace_layout() -> None:
    architecture_path = REPO / "docs/architecture.md"
    architecture = architecture_path.read_text(encoding="utf-8")
    section = architecture.partition("## Repository map\n")[2].partition("\n## ")[0]
    targets = [
        (architecture_path.parent / target.split("#", 1)[0]).resolve()
        for target in MARKDOWN_LINK_RE.findall(section)
        if "://" not in target and not target.startswith("#")
    ]
    projects = {path.parent.resolve() for path in workspace_pyprojects(REPO)}

    assert len(targets) == len(REPOSITORY_MAP_TARGETS)
    assert set(targets) == REPOSITORY_MAP_TARGETS
    assert all(
        any(project == target or target in project.parents for project in projects)
        for target in targets
    )
    assert all(
        any(project == target or target in project.parents for target in targets)
        for project in projects
    )
