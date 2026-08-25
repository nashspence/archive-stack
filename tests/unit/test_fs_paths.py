from __future__ import annotations

import pytest
from pydantic import TypeAdapter, ValidationError
from riverhog_protocol.paths import (
    CollectionId,
    CollectionIdParameter,
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


def test_collection_id_transport_projections_are_exact() -> None:
    assert TypeAdapter(CollectionId).validate_python(42) == 42
    assert TypeAdapter(CollectionIdParameter).validate_python("42") == 42

    for value in ("42", True, 0, -1):
        with pytest.raises(ValidationError):
            TypeAdapter(CollectionId).validate_python(value)
    for value in ("01", "0", "-1", " 1"):
        with pytest.raises(ValidationError):
            TypeAdapter(CollectionIdParameter).validate_python(value)


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
