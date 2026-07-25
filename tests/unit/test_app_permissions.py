from __future__ import annotations

import pytest
from riverhog_core.app_permissions import (
    CATALOG_READ,
    COLLECTIONS_UPLOAD,
    RETRIEVAL_MANAGE,
    SLUGS_CREATE,
    ApplicationAccess,
    ApplicationPrincipal,
    access_covers,
    normalize_access,
)
from riverhog_protocol.errors import BadRequest


def test_action_and_resource_bindings_are_canonical_and_independent() -> None:
    assert normalize_access(
        (
            ApplicationAccess(CATALOG_READ, "slug:photos"),
            ApplicationAccess(RETRIEVAL_MANAGE, "collection:docs/20260724T010203Z"),
            ApplicationAccess(CATALOG_READ, "slug:photos"),
        )
    ) == (
        ApplicationAccess(CATALOG_READ, "slug:photos"),
        ApplicationAccess(RETRIEVAL_MANAGE, "collection:docs/20260724T010203Z"),
    )
    assert access_covers(
        ApplicationAccess(CATALOG_READ, "slug:docs"),
        ApplicationAccess(CATALOG_READ, "collection:docs/20260724T010203Z"),
    )
    assert not access_covers(
        ApplicationAccess(CATALOG_READ, "slug:docs"),
        ApplicationAccess(RETRIEVAL_MANAGE, "collection:docs/20260724T010203Z"),
    )


def test_upload_targets_slugs_while_slug_creation_is_separate() -> None:
    assert normalize_access((ApplicationAccess(COLLECTIONS_UPLOAD, "slug:photos"),)) == (
        ApplicationAccess(COLLECTIONS_UPLOAD, "slug:photos"),
    )
    assert normalize_access((ApplicationAccess(SLUGS_CREATE),)) == (
        ApplicationAccess(SLUGS_CREATE),
    )
    with pytest.raises(BadRequest, match="must target a slug"):
        normalize_access(
            (
                ApplicationAccess(
                    COLLECTIONS_UPLOAD,
                    "collection:photos/20260724T010203Z",
                ),
            )
        )
    with pytest.raises(BadRequest, match="does not accept"):
        normalize_access((ApplicationAccess(SLUGS_CREATE, "slug:photos"),))


def test_unrestricted_delegation_does_not_grant_operational_authority() -> None:
    grantor = ApplicationPrincipal(
        app="bootstrap",
        key_id=None,
        access=frozenset(),
        unrestricted_delegation=True,
    )

    assert grantor.can_grant((ApplicationAccess(SLUGS_CREATE),))
    assert not grantor.allows(SLUGS_CREATE)
