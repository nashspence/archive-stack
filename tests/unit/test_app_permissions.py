from __future__ import annotations

import pytest
from riverhog_application_access import (
    CATALOG_READ,
    COLLECTION_ACCESS_GROUPS_MANAGE,
    COLLECTIONS_CREATE,
    RETRIEVAL_MANAGE,
    ApplicationAccess,
    ApplicationAccessError,
    access_covers,
    normalize_access,
)
from riverhog_core.app_permissions import ApplicationPrincipal

GROUP_RESOURCE = f"group:{'a' * 64}"


def test_action_and_resource_bindings_are_canonical_and_independent() -> None:
    assert normalize_access(
        (
            ApplicationAccess(CATALOG_READ, GROUP_RESOURCE),
            ApplicationAccess(RETRIEVAL_MANAGE, "collection:42"),
            ApplicationAccess(CATALOG_READ, GROUP_RESOURCE),
        )
    ) == (
        ApplicationAccess(CATALOG_READ, GROUP_RESOURCE),
        ApplicationAccess(RETRIEVAL_MANAGE, "collection:42"),
    )
    assert not access_covers(
        ApplicationAccess(CATALOG_READ, GROUP_RESOURCE),
        ApplicationAccess(CATALOG_READ, "collection:42"),
    )
    assert not access_covers(
        ApplicationAccess(CATALOG_READ, GROUP_RESOURCE),
        ApplicationAccess(RETRIEVAL_MANAGE, "collection:42"),
    )


def test_collection_creation_and_access_group_management_are_unscoped() -> None:
    assert normalize_access((ApplicationAccess(COLLECTIONS_CREATE),)) == (
        ApplicationAccess(COLLECTIONS_CREATE),
    )
    assert normalize_access((ApplicationAccess(COLLECTION_ACCESS_GROUPS_MANAGE),)) == (
        ApplicationAccess(COLLECTION_ACCESS_GROUPS_MANAGE),
    )
    with pytest.raises(ApplicationAccessError, match="does not accept a scoped resource"):
        normalize_access(
            (
                ApplicationAccess(
                    COLLECTIONS_CREATE,
                    "collection:42",
                ),
            )
        )
    with pytest.raises(ApplicationAccessError, match="does not accept"):
        normalize_access((ApplicationAccess(COLLECTION_ACCESS_GROUPS_MANAGE, GROUP_RESOURCE),))


def test_unrestricted_delegation_does_not_grant_operational_authority() -> None:
    grantor = ApplicationPrincipal(
        app="bootstrap",
        key_id=None,
        access=frozenset(),
        unrestricted_delegation=True,
    )

    assert grantor.can_grant((ApplicationAccess(COLLECTION_ACCESS_GROUPS_MANAGE),))
    assert not grantor.allows(COLLECTION_ACCESS_GROUPS_MANAGE)
