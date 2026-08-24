from __future__ import annotations

from application_access import (
    CATALOG_READ,
    EVENTS_READ,
    EVENTS_READ_ALL,
    PROVENANCE_EXPORT,
    PROVENANCE_READ,
    ApplicationAccess,
    collection_resource,
    normalize_access,
    permission_resources,
    tag_resource,
)


def test_public_access_contract_normalizes_and_covers_grants() -> None:
    access = normalize_access(
        (
            ApplicationAccess(EVENTS_READ_ALL),
            ApplicationAccess(PROVENANCE_EXPORT, "tag:docs"),
            ApplicationAccess(CATALOG_READ, "collection:42"),
        )
    )

    assert permission_resources(access, EVENTS_READ) == {"*"}
    assert permission_resources(access, PROVENANCE_READ) == {"tag:docs"}
    assert collection_resource(42) == "collection:42"
    assert tag_resource("docs") == "tag:docs"
