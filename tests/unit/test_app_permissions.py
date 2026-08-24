from __future__ import annotations

import pytest
from application_access import (
    CATALOG_READ,
    COLLECTIONS_CREATE,
    RETRIEVAL_MANAGE,
    TAGS_CREATE,
    ApplicationAccess,
    ApplicationAccessError,
    access_covers,
    normalize_access,
)
from riverhog_core.app_permissions import ApplicationPrincipal


def test_action_and_resource_bindings_are_canonical_and_independent() -> None:
    assert normalize_access(
        (
            ApplicationAccess(CATALOG_READ, "tag:photos"),
            ApplicationAccess(RETRIEVAL_MANAGE, "collection:42"),
            ApplicationAccess(CATALOG_READ, "tag:photos"),
        )
    ) == (
        ApplicationAccess(CATALOG_READ, "tag:photos"),
        ApplicationAccess(RETRIEVAL_MANAGE, "collection:42"),
    )
    assert not access_covers(
        ApplicationAccess(CATALOG_READ, "tag:docs"),
        ApplicationAccess(CATALOG_READ, "collection:42"),
    )
    assert not access_covers(
        ApplicationAccess(CATALOG_READ, "tag:docs"),
        ApplicationAccess(RETRIEVAL_MANAGE, "collection:42"),
    )


def test_collection_creation_targets_tags_while_tag_creation_is_separate() -> None:
    assert normalize_access((ApplicationAccess(COLLECTIONS_CREATE, "tag:photos"),)) == (
        ApplicationAccess(COLLECTIONS_CREATE, "tag:photos"),
    )
    assert normalize_access((ApplicationAccess(TAGS_CREATE),)) == (ApplicationAccess(TAGS_CREATE),)
    with pytest.raises(ApplicationAccessError, match="must target a tag"):
        normalize_access(
            (
                ApplicationAccess(
                    COLLECTIONS_CREATE,
                    "collection:42",
                ),
            )
        )
    with pytest.raises(ApplicationAccessError, match="does not accept"):
        normalize_access((ApplicationAccess(TAGS_CREATE, "tag:photos"),))


def test_unrestricted_delegation_does_not_grant_operational_authority() -> None:
    grantor = ApplicationPrincipal(
        app="bootstrap",
        key_id=None,
        access=frozenset(),
        unrestricted_delegation=True,
    )

    assert grantor.can_grant((ApplicationAccess(TAGS_CREATE),))
    assert not grantor.allows(TAGS_CREATE)
