from __future__ import annotations

import pytest

from riverhog_core.fs_paths import (
    collection_id_ancestors,
    find_collection_id_conflict,
    normalize_collection_id,
    normalize_relpath,
    normalize_root_node_name,
    normalize_upload_slug,
    normalize_upload_timestamp,
    path_parents,
)


def test_normalize_relpath_strips_and_normalizes() -> None:
    assert normalize_relpath(" a\\b/c ") == "a/b/c"


def test_normalize_relpath_rejects_escape() -> None:
    with pytest.raises(ValueError):
        normalize_relpath("../x")


def test_normalize_collection_id_accepts_nested_canonical_ids() -> None:
    assert normalize_collection_id("tax/2022") == "tax/2022"


@pytest.mark.parametrize("raw", [" tax/2022 ", "tax//2022", "tax/./2022", "tax\\2022", "/tax/2022"])
def test_normalize_collection_id_rejects_non_canonical_ids(raw: str) -> None:
    with pytest.raises(ValueError):
        normalize_collection_id(raw)


def test_normalize_root_node_name_rejects_nested() -> None:
    with pytest.raises(ValueError):
        normalize_root_node_name("a/b")


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Mom iPhone Photos", "mom-iphone-photos"),
        ("  Grandma: loose papers!!  ", "grandma-loose-papers"),
        ("München 2026", "munchen-2026"),
        ("unknown/photo\\envelope", "unknown-photo-envelope"),
    ],
)
def test_normalize_upload_slug_fold_and_collapse(raw: str, expected: str) -> None:
    assert normalize_upload_slug(raw) == expected


def test_normalize_upload_slug_rejects_empty_slug() -> None:
    with pytest.raises(ValueError):
        normalize_upload_slug(" -- ")


def test_normalize_upload_timestamp_accepts_utc_basic_form() -> None:
    assert normalize_upload_timestamp(" 20250712T213200Z ") == "20250712T213200Z"


@pytest.mark.parametrize(
    "raw",
    [
        "2025-07-12T21:32:00Z",
        "20250712T213200",
        "20250712T213200+0000",
        "20250230T213200Z",
    ],
)
def test_normalize_upload_timestamp_rejects_non_canonical_or_invalid_values(
    raw: str,
) -> None:
    with pytest.raises(ValueError):
        normalize_upload_timestamp(raw)


def test_collection_id_ancestors_list_parent_prefixes() -> None:
    assert collection_id_ancestors("tax/2022/invoices") == ["tax", "tax/2022"]


def test_find_collection_id_conflict_reports_ancestor_or_descendant() -> None:
    assert find_collection_id_conflict(["tax"], "tax/2022") == "tax"
    assert find_collection_id_conflict(["tax/2022"], "tax") == "tax/2022"
    assert find_collection_id_conflict(["docs"], "tax/2022") is None


def test_path_parents_lists_intermediate_dirs() -> None:
    assert path_parents("a/b/c.txt") == ["a", "a/b"]
