from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from riverhog_protocol.errors import BadRequest
from riverhog_protocol.paths import (
    PathNormalizationError,
    normalize_collection_id,
    normalize_upload_slug,
)

ALL_PERMISSIONS = "*"
ALL_RESOURCES = "*"
CATALOG_READ = "catalog:read"
RETRIEVAL_MANAGE = "retrieval:manage"
COLLECTIONS_UPLOAD = "collections:upload"
SLUGS_CREATE = "slugs:create"
COLLECTIONS_DELETE = "collections:delete"
ARCHIVES_READ = "archives:read"
ARCHIVES_MANAGE = "archives:manage"
KEYS_MANAGE = "keys:manage"
QUOTAS_MANAGE = "quotas:manage"
EVENTS_READ = "events:read"
EVENTS_READ_ALL = "events:read_all"

COLLECTION_PREFIX = "collection:"
SLUG_PREFIX = "slug:"

APPLICATION_PERMISSIONS = frozenset(
    {
        CATALOG_READ,
        RETRIEVAL_MANAGE,
        COLLECTIONS_UPLOAD,
        SLUGS_CREATE,
        COLLECTIONS_DELETE,
        ARCHIVES_READ,
        ARCHIVES_MANAGE,
        KEYS_MANAGE,
        QUOTAS_MANAGE,
        EVENTS_READ,
        EVENTS_READ_ALL,
    }
)

COLLECTION_SCOPED_PERMISSIONS = frozenset(
    {
        CATALOG_READ,
        RETRIEVAL_MANAGE,
        COLLECTIONS_UPLOAD,
        COLLECTIONS_DELETE,
        ARCHIVES_READ,
        ARCHIVES_MANAGE,
    }
)


@dataclass(frozen=True, order=True, slots=True)
class ApplicationAccess:
    permission: str
    resource: str = ALL_RESOURCES


def normalize_access(
    values: Iterable[ApplicationAccess | tuple[str, str]],
) -> tuple[ApplicationAccess, ...]:
    normalized = tuple(sorted({_normalize_access(value) for value in values}))
    if not normalized:
        raise BadRequest("at least one application access grant is required")
    wildcard = ApplicationAccess(ALL_PERMISSIONS, ALL_RESOURCES)
    if wildcard in normalized and len(normalized) != 1:
        raise BadRequest("wildcard application access must be used alone")
    return normalized


def access_covers(grantor: ApplicationAccess, requested: ApplicationAccess) -> bool:
    permission_matches = (
        grantor.permission == ALL_PERMISSIONS
        or grantor.permission == requested.permission
        or (
            grantor.permission == EVENTS_READ_ALL
            and requested.permission == EVENTS_READ
        )
    )
    return permission_matches and resource_covers(grantor.resource, requested.resource)


def resource_covers(grantor: str, requested: str) -> bool:
    if grantor == ALL_RESOURCES or grantor == requested:
        return True
    if grantor.startswith(SLUG_PREFIX) and requested.startswith(COLLECTION_PREFIX):
        return requested.removeprefix(COLLECTION_PREFIX).startswith(
            f"{grantor.removeprefix(SLUG_PREFIX)}/"
        )
    return False


def collection_resource(collection_id: str) -> str:
    try:
        normalized = normalize_collection_id(collection_id)
    except PathNormalizationError as exc:
        raise BadRequest(str(exc)) from exc
    return f"{COLLECTION_PREFIX}{normalized}"


def slug_resource(slug: str) -> str:
    return f"{SLUG_PREFIX}{_canonical_slug(slug)}"


@dataclass(frozen=True, slots=True)
class ApplicationPrincipal:
    app: str
    key_id: str | None
    access: frozenset[ApplicationAccess]
    unrestricted_delegation: bool = False

    def allows(self, permission: str, resource: str | None = None) -> bool:
        requested_resource = resource if resource is not None else ALL_RESOURCES
        return any(
            (
                current.permission == ALL_PERMISSIONS
                or current.permission == permission
                or (current.permission == EVENTS_READ_ALL and permission == EVENTS_READ)
            )
            and (resource is None or resource_covers(current.resource, requested_resource))
            for current in self.access
        )

    def allows_collection(self, permission: str, collection_id: str) -> bool:
        return self.allows(permission, collection_resource(collection_id))

    def allows_slug(self, permission: str, slug: str) -> bool:
        return self.allows(permission, slug_resource(slug))

    def can_grant(self, access: Iterable[ApplicationAccess | tuple[str, str]]) -> bool:
        requested = normalize_access(access)
        if self.unrestricted_delegation:
            return True
        return all(
            any(access_covers(held, current) for held in self.access)
            for current in requested
        )


def _normalize_access(value: ApplicationAccess | tuple[str, str]) -> ApplicationAccess:
    if isinstance(value, ApplicationAccess):
        permission = value.permission
        resource = value.resource
    else:
        permission, resource = value
    normalized_permission = str(permission).strip().casefold()
    if normalized_permission not in APPLICATION_PERMISSIONS | {ALL_PERMISSIONS}:
        raise BadRequest(f"unknown application permission: {normalized_permission}")
    normalized_resource = _normalize_resource(str(resource))
    if normalized_permission == ALL_PERMISSIONS and normalized_resource != ALL_RESOURCES:
        raise BadRequest("wildcard permission requires wildcard resource access")
    if (
        normalized_permission not in COLLECTION_SCOPED_PERMISSIONS
        and normalized_resource != ALL_RESOURCES
    ):
        raise BadRequest(f"{normalized_permission} does not accept a collection resource")
    if (
        normalized_permission == COLLECTIONS_UPLOAD
        and normalized_resource.startswith(COLLECTION_PREFIX)
    ):
        raise BadRequest("collections:upload must target a slug or all slugs")
    return ApplicationAccess(normalized_permission, normalized_resource)


def _normalize_resource(value: str) -> str:
    candidate = value.strip()
    folded = candidate.casefold()
    if folded == ALL_RESOURCES:
        return ALL_RESOURCES
    if folded.startswith(SLUG_PREFIX):
        return slug_resource(candidate[len(SLUG_PREFIX) :])
    if folded.startswith(COLLECTION_PREFIX):
        return collection_resource(candidate[len(COLLECTION_PREFIX) :])
    raise BadRequest("application resources must be *, slug:<slug>, or collection:<id>")


def _canonical_slug(value: str) -> str:
    try:
        normalized = normalize_upload_slug(value)
    except PathNormalizationError as exc:
        raise BadRequest(str(exc)) from exc
    if value != normalized:
        raise BadRequest("application access slug must be canonical")
    return normalized


__all__ = [
    "ALL_PERMISSIONS",
    "ALL_RESOURCES",
    "APPLICATION_PERMISSIONS",
    "ARCHIVES_MANAGE",
    "ARCHIVES_READ",
    "ApplicationAccess",
    "ApplicationPrincipal",
    "CATALOG_READ",
    "COLLECTIONS_DELETE",
    "COLLECTIONS_UPLOAD",
    "COLLECTION_PREFIX",
    "COLLECTION_SCOPED_PERMISSIONS",
    "EVENTS_READ",
    "EVENTS_READ_ALL",
    "KEYS_MANAGE",
    "QUOTAS_MANAGE",
    "RETRIEVAL_MANAGE",
    "SLUGS_CREATE",
    "SLUG_PREFIX",
    "access_covers",
    "collection_resource",
    "normalize_access",
    "resource_covers",
    "slug_resource",
]
