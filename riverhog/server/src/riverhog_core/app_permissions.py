from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from riverhog_protocol.errors import BadRequest
from riverhog_protocol.paths import PathNormalizationError, normalize_collection_id, normalize_tag

ALL_PERMISSIONS = "*"
ALL_RESOURCES = "*"
CATALOG_READ = "catalog:read"
RETRIEVAL_MANAGE = "retrieval:manage"
COLLECTIONS_CREATE = "collections:create"
COLLECTION_TRANSFORMS_CONTROL = "collection-transforms:control"
COLLECTION_TRANSFORMS_EXECUTE = "collection-transforms:execute"
COLLECTION_TAGS_MANAGE = "collection-tags:manage"
TAGS_CREATE = "tags:create"
TAGS_DELETE = "tags:delete"
COLLECTIONS_DELETE = "collections:delete"
ARCHIVES_READ = "archives:read"
ARCHIVES_MANAGE = "archives:manage"
KEYS_MANAGE = "keys:manage"
QUOTAS_MANAGE = "quotas:manage"
EVENTS_READ = "events:read"
EVENTS_READ_ALL = "events:read_all"
PROVENANCE_READ = "provenance:read"
PROVENANCE_EXPORT = "provenance:export"

COLLECTION_PREFIX = "collection:"
TAG_PREFIX = "tag:"

APPLICATION_PERMISSIONS = frozenset(
    {
        CATALOG_READ,
        RETRIEVAL_MANAGE,
        COLLECTIONS_CREATE,
        COLLECTION_TRANSFORMS_CONTROL,
        COLLECTION_TRANSFORMS_EXECUTE,
        COLLECTION_TAGS_MANAGE,
        TAGS_CREATE,
        TAGS_DELETE,
        COLLECTIONS_DELETE,
        ARCHIVES_READ,
        ARCHIVES_MANAGE,
        KEYS_MANAGE,
        QUOTAS_MANAGE,
        EVENTS_READ,
        EVENTS_READ_ALL,
        PROVENANCE_READ,
        PROVENANCE_EXPORT,
    }
)

COLLECTION_SCOPED_PERMISSIONS = frozenset(
    {
        CATALOG_READ,
        RETRIEVAL_MANAGE,
        COLLECTION_TAGS_MANAGE,
        COLLECTIONS_DELETE,
        ARCHIVES_READ,
        ARCHIVES_MANAGE,
        PROVENANCE_READ,
        PROVENANCE_EXPORT,
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
        or (grantor.permission == EVENTS_READ_ALL and requested.permission == EVENTS_READ)
        or (grantor.permission == PROVENANCE_EXPORT and requested.permission == PROVENANCE_READ)
    )
    return permission_matches and resource_covers(grantor.resource, requested.resource)


def resource_covers(grantor: str, requested: str) -> bool:
    return grantor == ALL_RESOURCES or grantor == requested


def collection_resource(collection_id: int) -> str:
    try:
        normalized = normalize_collection_id(collection_id)
    except PathNormalizationError as exc:
        raise BadRequest(str(exc)) from exc
    return f"{COLLECTION_PREFIX}{normalized}"


def tag_resource(tag: str) -> str:
    return f"{TAG_PREFIX}{_canonical_tag(tag)}"


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
                or (current.permission == PROVENANCE_EXPORT and permission == PROVENANCE_READ)
            )
            and (resource is None or resource_covers(current.resource, requested_resource))
            for current in self.access
        )

    def allows_collection(self, permission: str, collection_id: int) -> bool:
        return self.allows(permission, collection_resource(collection_id))

    def allows_tag(self, permission: str, tag: str) -> bool:
        return self.allows(permission, tag_resource(tag))

    def can_grant(self, access: Iterable[ApplicationAccess | tuple[str, str]]) -> bool:
        requested = normalize_access(access)
        if self.unrestricted_delegation:
            return True
        return all(
            any(access_covers(held, current) for held in self.access) for current in requested
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
    if normalized_permission == COLLECTIONS_CREATE:
        if normalized_resource.startswith(COLLECTION_PREFIX):
            raise BadRequest("collections:create must target a tag or all tags")
    elif (
        normalized_permission not in COLLECTION_SCOPED_PERMISSIONS
        and normalized_resource != ALL_RESOURCES
    ):
        raise BadRequest(f"{normalized_permission} does not accept a scoped resource")
    return ApplicationAccess(normalized_permission, normalized_resource)


def _normalize_resource(value: str) -> str:
    candidate = value.strip()
    folded = candidate.casefold()
    if folded == ALL_RESOURCES:
        return ALL_RESOURCES
    if folded.startswith(TAG_PREFIX):
        return tag_resource(candidate[len(TAG_PREFIX) :])
    if folded.startswith(COLLECTION_PREFIX):
        try:
            collection_id = normalize_collection_id(candidate[len(COLLECTION_PREFIX) :])
        except PathNormalizationError as exc:
            raise BadRequest(str(exc)) from exc
        return collection_resource(collection_id)
    raise BadRequest("application resources must be *, tag:<tag>, or collection:<id>")


def _canonical_tag(value: str) -> str:
    try:
        normalized = normalize_tag(value)
    except PathNormalizationError as exc:
        raise BadRequest(str(exc)) from exc
    if value != normalized:
        raise BadRequest("application access tag must be canonical")
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
    "COLLECTIONS_CREATE",
    "COLLECTION_TRANSFORMS_CONTROL",
    "COLLECTION_TRANSFORMS_EXECUTE",
    "COLLECTIONS_DELETE",
    "COLLECTION_PREFIX",
    "COLLECTION_SCOPED_PERMISSIONS",
    "COLLECTION_TAGS_MANAGE",
    "EVENTS_READ",
    "EVENTS_READ_ALL",
    "KEYS_MANAGE",
    "QUOTAS_MANAGE",
    "PROVENANCE_EXPORT",
    "PROVENANCE_READ",
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
