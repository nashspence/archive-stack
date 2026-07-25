from __future__ import annotations

from riverhog_protocol.errors import NotFound
from sqlalchemy import false, or_, true
from sqlalchemy.orm import InstrumentedAttribute
from sqlalchemy.sql.elements import ColumnElement

from riverhog_core.app_permissions import (
    ALL_RESOURCES,
    COLLECTION_PREFIX,
    SLUG_PREFIX,
    ApplicationPrincipal,
)


def require_collection_access(
    principal: ApplicationPrincipal | None,
    permission: str,
    collection_id: str,
) -> None:
    if principal is not None and not principal.allows_collection(permission, collection_id):
        raise NotFound(f"collection not found: {collection_id}")


def require_slug_access(
    principal: ApplicationPrincipal | None,
    permission: str,
    slug: str,
) -> None:
    if principal is not None and not principal.allows_slug(permission, slug):
        raise NotFound(f"collection slug not found: {slug}")


def collection_access_filter(
    column: ColumnElement[str] | InstrumentedAttribute[str],
    principal: ApplicationPrincipal | None,
    permission: str,
) -> ColumnElement[bool]:
    if principal is None:
        return true()
    resources = _resources(principal, permission)
    if ALL_RESOURCES in resources:
        return true()
    filters: list[ColumnElement[bool]] = []
    for resource in resources:
        if resource.startswith(SLUG_PREFIX):
            filters.append(column.like(f"{resource.removeprefix(SLUG_PREFIX)}/%"))
        elif resource.startswith(COLLECTION_PREFIX):
            filters.append(column == resource.removeprefix(COLLECTION_PREFIX))
    return or_(*filters) if filters else false()


def slug_access_filter(
    column: ColumnElement[str] | InstrumentedAttribute[str],
    principal: ApplicationPrincipal | None,
    permission: str,
) -> ColumnElement[bool]:
    if principal is None:
        return true()
    resources = _resources(principal, permission)
    if ALL_RESOURCES in resources:
        return true()
    filters = [
        column == resource.removeprefix(SLUG_PREFIX)
        for resource in resources
        if resource.startswith(SLUG_PREFIX)
    ]
    return or_(*filters) if filters else false()


def _resources(principal: ApplicationPrincipal, permission: str) -> set[str]:
    return {
        current.resource
        for current in principal.access
        if current.permission in {"*", permission}
        or (current.permission == "events:read_all" and permission == "events:read")
    }


__all__ = [
    "collection_access_filter",
    "require_collection_access",
    "require_slug_access",
    "slug_access_filter",
]
