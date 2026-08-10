from __future__ import annotations

import pytest
from riverhog_protocol.paths import (
    normalize_collection_id,
    normalize_relpath,
    normalize_tag,
)


def test_normalize_relpath_strips_and_normalizes() -> None:
    assert normalize_relpath(" a\\b/c ") == "a/b/c"


def test_normalize_relpath_rejects_escape() -> None:
    with pytest.raises(ValueError):
        normalize_relpath("../x")


def test_normalize_collection_id_accepts_canonical_integer() -> None:
    assert normalize_collection_id("42") == 42
    assert normalize_collection_id(42) == 42


@pytest.mark.parametrize(
    "raw",
    [
        "0",
        "01",
        "-1",
        " 1",
        "1 ",
        "1.0",
        True,
    ],
)
def test_normalize_collection_id_requires_canonical_positive_integer(raw: object) -> None:
    with pytest.raises(ValueError):
        normalize_collection_id(raw)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Mom iPhone Photos", "mom-iphone-photos"),
        ("  Grandma: loose papers!!  ", "grandma-loose-papers"),
        ("München 2026", "munchen-2026"),
        ("unknown/photo\\envelope", "unknown-photo-envelope"),
    ],
)
def test_normalize_tag_folds_and_collapses(raw: str, expected: str) -> None:
    assert normalize_tag(raw) == expected


def test_normalize_tag_rejects_empty_tag() -> None:
    with pytest.raises(ValueError):
        normalize_tag(" -- ")
