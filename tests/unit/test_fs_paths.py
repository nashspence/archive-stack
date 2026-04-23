from __future__ import annotations

import pytest

from arc_core.fs_paths import (
    collection_id_ancestors,
    derive_collection_id_from_staging_path,
    find_collection_id_conflict,
    normalize_collection_id,
    normalize_relpath,
    normalize_root_node_name,
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


def test_derive_collection_id_from_staging_path_uses_path_beneath_staging_root() -> None:
    assert derive_collection_id_from_staging_path("/srv/archive/staging/tax/2022") == "tax/2022"


def test_collection_id_ancestors_list_parent_prefixes() -> None:
    assert collection_id_ancestors("tax/2022/invoices") == ["tax", "tax/2022"]


def test_find_collection_id_conflict_reports_ancestor_or_descendant() -> None:
    assert find_collection_id_conflict(["tax"], "tax/2022") == "tax"
    assert find_collection_id_conflict(["tax/2022"], "tax") == "tax/2022"
    assert find_collection_id_conflict(["docs"], "tax/2022") is None


def test_path_parents_lists_intermediate_dirs() -> None:
    assert path_parents("a/b/c.txt") == ["a", "a/b"]
