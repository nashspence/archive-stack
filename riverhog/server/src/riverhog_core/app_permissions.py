from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from application_access import (
    ALL_PERMISSIONS,
    ALL_RESOURCES,
    APPLICATION_PERMISSIONS,
    ARCHIVES_MANAGE,
    ARCHIVES_READ,
    CATALOG_READ,
    COLLECTION_PREFIX,
    COLLECTION_SCOPED_PERMISSIONS,
    COLLECTION_TAGS_MANAGE,
    COLLECTION_TRANSFORMS_CONTROL,
    COLLECTION_TRANSFORMS_EXECUTE,
    COLLECTIONS_CREATE,
    COLLECTIONS_DELETE,
    EVENTS_READ,
    EVENTS_READ_ALL,
    KEYS_MANAGE,
    PROVENANCE_EXPORT,
    PROVENANCE_READ,
    QUOTAS_MANAGE,
    RETRIEVAL_MANAGE,
    TAG_PREFIX,
    TAGS_CREATE,
    TAGS_DELETE,
    ApplicationAccess,
    ApplicationAccessError,
    access_covers,
    permission_covers,
    resource_covers,
)
from application_access import collection_resource as _collection_resource
from application_access import normalize_access as _normalize_access
from application_access import tag_resource as _tag_resource
from riverhog_protocol.errors import BadRequest


def normalize_access(
    values: Iterable[ApplicationAccess | tuple[str, str]],
) -> tuple[ApplicationAccess, ...]:
    try:
        return _normalize_access(values)
    except ApplicationAccessError as exc:
        raise BadRequest(str(exc)) from exc


def collection_resource(collection_id: int) -> str:
    try:
        return _collection_resource(collection_id)
    except ApplicationAccessError as exc:
        raise BadRequest(str(exc)) from exc


def tag_resource(tag: str) -> str:
    try:
        return _tag_resource(tag)
    except ApplicationAccessError as exc:
        raise BadRequest(str(exc)) from exc


@dataclass(frozen=True, slots=True)
class ApplicationPrincipal:
    app: str
    key_id: str | None
    access: frozenset[ApplicationAccess]
    unrestricted_delegation: bool = False
    artifact_scope: frozenset[tuple[int, str]] | None = None

    def allows(self, permission: str, resource: str | None = None) -> bool:
        requested_resource = resource if resource is not None else ALL_RESOURCES
        return any(
            permission_covers(current.permission, permission)
            and (resource is None or resource_covers(current.resource, requested_resource))
            for current in self.access
        )

    def allows_collection(self, permission: str, collection_id: int) -> bool:
        return self.allows(permission, collection_resource(collection_id))

    def allows_tag(self, permission: str, tag: str) -> bool:
        return self.allows(permission, tag_resource(tag))

    def allows_artifact(self, permission: str, collection_id: int, path: str) -> bool:
        if not self.allows_collection(permission, collection_id):
            return False
        return self.artifact_scope is None or (int(collection_id), str(path)) in self.artifact_scope

    def can_grant(self, access: Iterable[ApplicationAccess | tuple[str, str]]) -> bool:
        requested = normalize_access(access)
        if self.unrestricted_delegation:
            return True
        return all(
            any(access_covers(held, current) for held in self.access) for current in requested
        )


__all__ = [
    "ALL_PERMISSIONS",
    "ALL_RESOURCES",
    "APPLICATION_PERMISSIONS",
    "ARCHIVES_MANAGE",
    "ARCHIVES_READ",
    "ApplicationAccess",
    "ApplicationPrincipal",
    "CATALOG_READ",
    "COLLECTIONS_CREATE",
    "COLLECTIONS_DELETE",
    "COLLECTION_PREFIX",
    "COLLECTION_SCOPED_PERMISSIONS",
    "COLLECTION_TAGS_MANAGE",
    "COLLECTION_TRANSFORMS_CONTROL",
    "COLLECTION_TRANSFORMS_EXECUTE",
    "EVENTS_READ",
    "EVENTS_READ_ALL",
    "KEYS_MANAGE",
    "PROVENANCE_EXPORT",
    "PROVENANCE_READ",
    "QUOTAS_MANAGE",
    "RETRIEVAL_MANAGE",
    "TAGS_CREATE",
    "TAGS_DELETE",
    "TAG_PREFIX",
    "access_covers",
    "collection_resource",
    "normalize_access",
    "resource_covers",
    "tag_resource",
]
