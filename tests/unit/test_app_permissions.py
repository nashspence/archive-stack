from __future__ import annotations

import pytest
from riverhog_application_access import (
    CATALOG_READ,
    COLLECTION_TAGS_MANAGE,
    COLLECTIONS_CREATE,
    RETRIEVAL_MANAGE,
    ApplicationAccess,
    ApplicationAccessError,
    access_covers,
    normalize_access,
)
from riverhog_core.app_permissions import ApplicationPrincipal

TAG_RESOURCE = "tag:camera/front"


def test_action_and_resource_bindings_are_canonical_and_independent() -> None:
    assert normalize_access(
        (
            ApplicationAccess(CATALOG_READ, TAG_RESOURCE),
            ApplicationAccess(RETRIEVAL_MANAGE, "collection:42"),
            ApplicationAccess(CATALOG_READ, TAG_RESOURCE),
        )
    ) == (
        ApplicationAccess(CATALOG_READ, TAG_RESOURCE),
        ApplicationAccess(RETRIEVAL_MANAGE, "collection:42"),
    )
    assert not access_covers(
        ApplicationAccess(CATALOG_READ, TAG_RESOURCE),
        ApplicationAccess(CATALOG_READ, "collection:42"),
    )
    assert not access_covers(
        ApplicationAccess(CATALOG_READ, TAG_RESOURCE),
        ApplicationAccess(RETRIEVAL_MANAGE, "collection:42"),
    )


def test_collection_creation_and_tag_management_support_tag_scope() -> None:
    assert normalize_access((ApplicationAccess(COLLECTIONS_CREATE),)) == (
        ApplicationAccess(COLLECTIONS_CREATE),
    )
    assert normalize_access((ApplicationAccess(COLLECTION_TAGS_MANAGE, TAG_RESOURCE),)) == (
        ApplicationAccess(COLLECTION_TAGS_MANAGE, TAG_RESOURCE),
    )
    with pytest.raises(ApplicationAccessError, match="accepts only wildcard or tag resources"):
        normalize_access(
            (
                ApplicationAccess(
                    COLLECTIONS_CREATE,
                    "collection:42",
                ),
            )
        )
    assert normalize_access((ApplicationAccess(COLLECTIONS_CREATE, TAG_RESOURCE),)) == (
        ApplicationAccess(COLLECTIONS_CREATE, TAG_RESOURCE),
    )


def test_unrestricted_delegation_does_not_grant_operational_authority() -> None:
    grantor = ApplicationPrincipal(
        app="bootstrap",
        key_id=None,
        access=frozenset(),
        unrestricted_delegation=True,
    )

    assert grantor.can_grant((ApplicationAccess(COLLECTION_TAGS_MANAGE),))
    assert not grantor.allows(COLLECTION_TAGS_MANAGE)
