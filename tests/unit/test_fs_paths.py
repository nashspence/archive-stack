from __future__ import annotations

import pytest

from riverhog_core.fs_paths import (
    collection_id_for_upload,
    normalize_collection_id,
    normalize_relpath,
    normalize_upload_slug,
    normalize_upload_timestamp,
    path_parents,
)


def test_normalize_relpath_strips_and_normalizes() -> None:
    assert normalize_relpath(" a\\b/c ") == "a/b/c"


def test_normalize_relpath_rejects_escape() -> None:
    with pytest.raises(ValueError):
        normalize_relpath("../x")


def test_collection_id_for_upload_uses_year_timestamp_and_slug() -> None:
    assert (
        collection_id_for_upload("Family Photos", "20250712T213200Z")
        == "2025/20250712T213200Z__family-photos"
    )


def test_normalize_collection_id_accepts_canonical_upload_id() -> None:
    collection_id = "2025/20250712T213200Z__family-photos"
    assert normalize_collection_id(collection_id) == collection_id


@pytest.mark.parametrize(
    "raw",
    [
        "2025/20250712T213200Z__Family-Photos",
        "2024/20250712T213200Z__family-photos",
        "2025/20250230T213200Z__family-photos",
        "2025/20250712T213200Z__family_photos",
        " 2025/20250712T213200Z__family-photos ",
        "2025//20250712T213200Z__family-photos",
        "/2025/20250712T213200Z__family-photos",
    ],
)
def test_normalize_collection_id_requires_canonical_upload_id(raw: str) -> None:
    with pytest.raises(ValueError):
        normalize_collection_id(raw)


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


def test_path_parents_lists_intermediate_dirs() -> None:
    assert path_parents("a/b/c.txt") == ["a", "a/b"]
