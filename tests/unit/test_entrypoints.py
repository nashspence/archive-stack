import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
README = REPO / "README.md"
AGENTS = REPO / "AGENTS.md"

README_REQUIRED_SECTIONS = [
    "# riverhog",
    "## Critical Risks",
    "## Normal Work",
    "## Validation",
    "## Ownership and Routes",
]

AGENTS_REQUIRED_SECTIONS = [
    "# AGENTS.md",
    "## Identity",
    "## Critical Risks",
    "## Normal Work",
    "## Validation",
    "## Ownership and Routes",
]

ENTRYPOINTS = [README, AGENTS]


def markdown_links(text: str) -> list[str]:
    return re.findall(r"\[[^\]]+\]\(([^)]+)\)", text)


def test_entrypoints_are_concise_and_structured() -> None:
    readme = README.read_text(encoding="utf-8")
    agents = AGENTS.read_text(encoding="utf-8")

    assert len(readme.splitlines()) <= 90
    assert len(agents.splitlines()) <= 90
    for section in README_REQUIRED_SECTIONS:
        assert section in readme
    for section in AGENTS_REQUIRED_SECTIONS:
        assert section in agents


def test_entrypoints_route_to_existing_docs() -> None:
    for entrypoint in ENTRYPOINTS:
        text = entrypoint.read_text(encoding="utf-8")
        for link in markdown_links(text):
            if "://" in link or link.startswith("#"):
                continue
            assert (REPO / link).exists(), f"{entrypoint.name} links to missing doc: {link}"


def test_entrypoints_keep_public_private_boundary_visible() -> None:
    combined = "\n".join(path.read_text(encoding="utf-8") for path in ENTRYPOINTS)
    normalized = " ".join(combined.split())

    for phrase in [
        "Keep public code generic",
        "Do not add private deployment details",
        "downstream private configuration",
        "real deployment identity",
    ]:
        assert phrase in normalized

    for private_marker in [
        "0819870",
        "client.riverhog",
        "Nash",
        "MacBook",
        "iMac",
        "Clover",
        "Gumshoe",
    ]:
        assert private_marker not in combined
