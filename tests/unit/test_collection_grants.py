from __future__ import annotations

import pytest
from riverhog_core.collection_grants import (
    grant_allows_collection,
    grant_allows_slug,
    grant_covers,
    normalize_collection_grants,
)
from riverhog_protocol.errors import BadRequest


def test_collection_grants_normalize_to_explicit_canonical_capabilities() -> None:
    assert normalize_collection_grants(
        (
            "slug:photos",
            "collection:docs/20260724T010203Z",
            "slug:photos",
        )
    ) == (
        "collection:docs/20260724T010203Z",
        "slug:photos",
    )
    assert grant_allows_slug("slug:photos", "photos")
    assert grant_allows_collection("slug:photos", "photos/20260724T010203Z")
    assert grant_allows_collection(
        "collection:docs/20260724T010203Z",
        "docs/20260724T010203Z",
    )
    assert grant_covers(
        "slug:docs",
        "collection:docs/20260724T010203Z",
    )


def test_all_collections_is_one_unambiguous_grant() -> None:
    assert normalize_collection_grants(("*",)) == ("*",)
    assert grant_allows_slug("*", "photos")
    assert grant_allows_collection("*", "photos/20260724T010203Z")

    with pytest.raises(BadRequest, match="must be used alone"):
        normalize_collection_grants(("*", "slug:photos"))

