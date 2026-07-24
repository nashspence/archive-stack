from __future__ import annotations

from riverhog_protocol.errors import NotFound
from sqlalchemy import false, or_, true
from sqlalchemy.orm import InstrumentedAttribute
from sqlalchemy.sql.elements import ColumnElement

from riverhog_core.app_permissions import ApplicationPrincipal
from riverhog_core.collection_grants import COLLECTION_PREFIX, SLUG_PREFIX


def require_collection_access(
    principal: ApplicationPrincipal | None,
    collection_id: str,
) -> None:
    if principal is not None and not principal.allows_collection(collection_id):
        raise NotFound(f"collection not found: {collection_id}")


def require_slug_access(
    principal: ApplicationPrincipal | None,
    slug: str,
) -> None:
    if principal is not None and not principal.allows_slug(slug):
        raise NotFound(f"collection slug not found: {slug}")


def collection_access_filter(
    column: ColumnElement[str] | InstrumentedAttribute[str],
    principal: ApplicationPrincipal | None,
) -> ColumnElement[bool]:
    if principal is None or principal.unrestricted_delegation or "*" in principal.collection_grants:
        return true()
    filters: list[ColumnElement[bool]] = []
    for grant in principal.collection_grants:
        if grant.startswith(SLUG_PREFIX):
            filters.append(column.like(f"{grant.removeprefix(SLUG_PREFIX)}/%"))
        elif grant.startswith(COLLECTION_PREFIX):
            filters.append(column == grant.removeprefix(COLLECTION_PREFIX))
    return or_(*filters) if filters else false()


__all__ = [
    "collection_access_filter",
    "require_collection_access",
    "require_slug_access",
]
