from __future__ import annotations

from collections.abc import Iterable

from application_access import permission_resources as access_permission_resources
from riverhog_protocol.errors import BadRequest, NotFound
from riverhog_protocol.paths import PathNormalizationError, normalize_collection_id
from sqlalchemy import exists, false, or_, select, true
from sqlalchemy.orm import InstrumentedAttribute, Session
from sqlalchemy.sql.elements import ColumnElement

from riverhog_core.app_permissions import (
    ALL_RESOURCES,
    COLLECTION_PREFIX,
    TAG_PREFIX,
    ApplicationPrincipal,
)
from riverhog_core.catalog_db import SessionFactory, make_session_factory, session_scope
from riverhog_core.catalog_models import CollectionRecord, CollectionTagRecord
from riverhog_core.runtime_config import RuntimeConfig


class SqlAlchemyCollectionAccessService:
    def __init__(
        self,
        config: RuntimeConfig,
        *,
        session_factory: SessionFactory | None = None,
    ) -> None:
        self._session_factory = session_factory or make_session_factory(config.database_url)

    def require(
        self,
        principal: ApplicationPrincipal,
        permission: str,
        collection_id: int,
    ) -> int:
        try:
            normalized = normalize_collection_id(collection_id)
        except PathNormalizationError as exc:
            raise BadRequest(str(exc)) from exc
        with session_scope(self._session_factory) as session:
            if session.get(CollectionRecord, normalized) is None:
                raise NotFound(f"collection not found: {normalized}")
            require_collection_access(session, principal, permission, normalized)
        return normalized


def require_collection_access(
    session: Session,
    principal: ApplicationPrincipal | None,
    permission: str,
    collection_id: int,
) -> None:
    if principal is None:
        return
    resources = permission_resources(principal, permission)
    if ALL_RESOURCES in resources or f"{COLLECTION_PREFIX}{collection_id}" in resources:
        return
    allowed_tags = tag_ids(resources)
    if (
        allowed_tags
        and session.scalar(
            select(CollectionTagRecord.collection_id)
            .where(CollectionTagRecord.collection_id == collection_id)
            .where(CollectionTagRecord.tag_id.in_(allowed_tags))
            .limit(1)
        )
        is not None
    ):
        return
    raise NotFound(f"collection not found: {collection_id}")


def require_collection_create_access(
    principal: ApplicationPrincipal | None,
    permission: str,
    tags: Iterable[str],
) -> None:
    if principal is None:
        return
    resources = permission_resources(principal, permission)
    if ALL_RESOURCES in resources:
        return
    requested = {f"{TAG_PREFIX}{tag}" for tag in tags}
    if requested and requested <= resources:
        return
    raise NotFound("collection tags are not available")


def collection_access_filter(
    column: ColumnElement[int] | InstrumentedAttribute[int],
    principal: ApplicationPrincipal | None,
    permission: str,
) -> ColumnElement[bool]:
    if principal is None:
        return true()
    resources = permission_resources(principal, permission)
    if ALL_RESOURCES in resources:
        return true()
    allowed_collection_ids = collection_ids(resources)
    allowed_tag_ids = tag_ids(resources)
    filters: list[ColumnElement[bool]] = []
    if allowed_collection_ids:
        filters.append(column.in_(allowed_collection_ids))
    if allowed_tag_ids:
        filters.append(
            exists(
                select(1).where(
                    CollectionTagRecord.collection_id == column,
                    CollectionTagRecord.tag_id.in_(allowed_tag_ids),
                )
            )
        )
    return or_(*filters) if filters else false()


def tag_access_filter(
    column: ColumnElement[str] | InstrumentedAttribute[str],
    principal: ApplicationPrincipal | None,
    permission: str,
) -> ColumnElement[bool]:
    if principal is None:
        return true()
    resources = permission_resources(principal, permission)
    if ALL_RESOURCES in resources:
        return true()
    allowed_tag_ids = tag_ids(resources)
    allowed_collection_ids = collection_ids(resources)
    filters: list[ColumnElement[bool]] = []
    if allowed_tag_ids:
        filters.append(column.in_(allowed_tag_ids))
    if allowed_collection_ids:
        filters.append(
            exists(
                select(1).where(
                    CollectionTagRecord.tag_id == column,
                    CollectionTagRecord.collection_id.in_(allowed_collection_ids),
                )
            )
        )
    return or_(*filters) if filters else false()


def permission_resources(principal: ApplicationPrincipal, permission: str) -> set[str]:
    return access_permission_resources(principal.access, permission)


def collection_ids(resources: Iterable[str]) -> set[int]:
    return {
        int(resource.removeprefix(COLLECTION_PREFIX))
        for resource in resources
        if resource.startswith(COLLECTION_PREFIX)
    }


def tag_ids(resources: Iterable[str]) -> set[str]:
    return {
        resource.removeprefix(TAG_PREFIX)
        for resource in resources
        if resource.startswith(TAG_PREFIX)
    }


__all__ = [
    "SqlAlchemyCollectionAccessService",
    "collection_ids",
    "collection_access_filter",
    "permission_resources",
    "require_collection_access",
    "require_collection_create_access",
    "tag_ids",
    "tag_access_filter",
]
