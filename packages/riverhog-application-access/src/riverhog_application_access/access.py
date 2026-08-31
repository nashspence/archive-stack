from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Annotated, Any, Literal, cast

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    GetJsonSchemaHandler,
    RootModel,
    model_validator,
)
from pydantic.json_schema import JsonSchemaValue
from pydantic_core import CoreSchema
from riverhog_protocol.paths import normalize_collection_id, normalize_tag

ALL_PERMISSIONS: Literal["*"] = "*"
ALL_RESOURCES = "*"
CATALOG_READ: Literal["catalog:read"] = "catalog:read"
RETRIEVAL_MANAGE: Literal["retrieval:manage"] = "retrieval:manage"
COLLECTIONS_CREATE: Literal["collections:create"] = "collections:create"
COLLECTION_TRANSFORMS_CONTROL: Literal["collection-transforms:control"] = (
    "collection-transforms:control"
)
COLLECTION_TRANSFORMS_EXECUTE: Literal["collection-transforms:execute"] = (
    "collection-transforms:execute"
)
COLLECTION_TAGS_MANAGE: Literal["collection-tags:manage"] = "collection-tags:manage"
TAGS_CREATE: Literal["tags:create"] = "tags:create"
TAGS_DELETE: Literal["tags:delete"] = "tags:delete"
COLLECTIONS_DELETE: Literal["collections:delete"] = "collections:delete"
ARCHIVES_READ: Literal["archives:read"] = "archives:read"
ARCHIVES_MANAGE: Literal["archives:manage"] = "archives:manage"
KEYS_MANAGE: Literal["keys:manage"] = "keys:manage"
QUOTAS_MANAGE: Literal["quotas:manage"] = "quotas:manage"
EVENTS_READ: Literal["events:read"] = "events:read"
EVENTS_READ_ALL: Literal["events:read_all"] = "events:read_all"
PROVENANCE_READ: Literal["provenance:read"] = "provenance:read"
PROVENANCE_EXPORT: Literal["provenance:export"] = "provenance:export"

COLLECTION_PREFIX = "collection:"
TAG_PREFIX = "tag:"

type ApplicationPermission = Literal[
    "*",
    "catalog:read",
    "retrieval:manage",
    "collections:create",
    "collection-transforms:control",
    "collection-transforms:execute",
    "collection-tags:manage",
    "tags:create",
    "tags:delete",
    "collections:delete",
    "archives:read",
    "archives:manage",
    "keys:manage",
    "quotas:manage",
    "events:read",
    "events:read_all",
    "provenance:read",
    "provenance:export",
]
type ApplicationResource = Annotated[
    str,
    Field(pattern=r"^(?:\*|tag:[a-z0-9]+(?:-[a-z0-9]+)*|collection:[1-9][0-9]*)$"),
]

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


class ApplicationAccessError(ValueError):
    """An application grant is outside the public Riverhog access grammar."""


@dataclass(frozen=True, order=True, slots=True)
class ApplicationAccess:
    permission: ApplicationPermission
    resource: ApplicationResource = ALL_RESOURCES

    def __post_init__(self) -> None:
        permission, resource = _normalize_access_fields(self.permission, self.resource)
        if permission != self.permission or resource != self.resource:
            raise ApplicationAccessError("application access grants must be canonical")


class ApplicationAccessGrant(BaseModel):
    """One canonical public application-access request or response grant."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    permission: ApplicationPermission
    resource: ApplicationResource = ALL_RESOURCES

    @model_validator(mode="after")
    def validate_relationship(self) -> ApplicationAccessGrant:
        ApplicationAccess(self.permission, self.resource)
        return self

    @classmethod
    def __get_pydantic_json_schema__(
        cls,
        core_schema: CoreSchema,
        handler: GetJsonSchemaHandler,
    ) -> JsonSchemaValue:
        schema = handler(core_schema)
        schema.setdefault("allOf", []).append({"oneOf": _grant_relationship_schemas()})
        return schema

    def as_access(self) -> ApplicationAccess:
        return ApplicationAccess(self.permission, self.resource)


class ApplicationAccessGrantSet(RootModel[list[ApplicationAccessGrant]]):
    """A nonempty, duplicate-free public grant set with canonical wildcard use."""

    @model_validator(mode="after")
    def validate_set(self) -> ApplicationAccessGrantSet:
        normalized = normalize_access(item.as_access() for item in self.root)
        if len(normalized) != len(self.root):
            raise ValueError("application access grants must not contain duplicates")
        return self

    @classmethod
    def __get_pydantic_json_schema__(
        cls,
        core_schema: CoreSchema,
        handler: GetJsonSchemaHandler,
    ) -> JsonSchemaValue:
        schema = handler(core_schema)
        schema["minItems"] = 1
        schema["uniqueItems"] = True
        schema.setdefault("allOf", []).append(
            {
                "if": {
                    "contains": {
                        "type": "object",
                        "required": ["permission"],
                        "properties": {"permission": {"const": ALL_PERMISSIONS}},
                    }
                },
                "then": {
                    "maxItems": 1,
                    "x-riverhog-extent": {
                        "policy": "contract_max",
                        "reason": "wildcard-access-grant-is-exclusive",
                    },
                },
            }
        )
        return schema


def normalize_access(
    values: Iterable[ApplicationAccess | tuple[str, str]],
) -> tuple[ApplicationAccess, ...]:
    normalized = tuple(sorted({_normalize_access(value) for value in values}))
    if not normalized:
        raise ApplicationAccessError("at least one application access grant is required")
    wildcard = ApplicationAccess(ALL_PERMISSIONS, ALL_RESOURCES)
    if wildcard in normalized and len(normalized) != 1:
        raise ApplicationAccessError("wildcard application access must be used alone")
    return normalized


def access_covers(grantor: ApplicationAccess, requested: ApplicationAccess) -> bool:
    return permission_covers(grantor.permission, requested.permission) and resource_covers(
        grantor.resource, requested.resource
    )


def permission_covers(grantor: str, requested: str) -> bool:
    return (
        grantor == ALL_PERMISSIONS
        or grantor == requested
        or (grantor == EVENTS_READ_ALL and requested == EVENTS_READ)
        or (grantor == PROVENANCE_EXPORT and requested == PROVENANCE_READ)
    )


def resource_covers(grantor: str, requested: str) -> bool:
    return grantor == ALL_RESOURCES or grantor == requested


def collection_resource(collection_id: int | str) -> str:
    try:
        normalized = normalize_collection_id(collection_id)
    except ValueError as exc:
        raise ApplicationAccessError(str(exc)) from exc
    return f"{COLLECTION_PREFIX}{normalized}"


def tag_resource(tag: str) -> str:
    return f"{TAG_PREFIX}{_canonical_tag(tag)}"


def permission_resources(access: Iterable[ApplicationAccess], permission: str) -> set[str]:
    return {
        current.resource for current in access if permission_covers(current.permission, permission)
    }


def _normalize_access(value: ApplicationAccess | tuple[str, str]) -> ApplicationAccess:
    if isinstance(value, ApplicationAccess):
        raw_permission: str = value.permission
        resource = value.resource
    else:
        raw_permission, resource = value
    normalized_permission, normalized_resource = _normalize_access_fields(
        raw_permission,
        resource,
    )
    return ApplicationAccess(normalized_permission, normalized_resource)


def _normalize_access_fields(
    raw_permission: str,
    resource: str,
) -> tuple[ApplicationPermission, ApplicationResource]:
    if not isinstance(raw_permission, str) or not isinstance(resource, str):
        raise ApplicationAccessError("application permission and resource must be strings")
    normalized_permission = raw_permission.strip().casefold()
    if normalized_permission not in APPLICATION_PERMISSIONS | {ALL_PERMISSIONS}:
        raise ApplicationAccessError(f"unknown application permission: {normalized_permission}")
    normalized_resource = _normalize_resource(str(resource))
    if normalized_permission == ALL_PERMISSIONS and normalized_resource != ALL_RESOURCES:
        raise ApplicationAccessError("wildcard permission requires wildcard resource access")
    if normalized_permission == COLLECTIONS_CREATE:
        if normalized_resource.startswith(COLLECTION_PREFIX):
            raise ApplicationAccessError("collections:create must target a tag or all tags")
    elif (
        normalized_permission not in COLLECTION_SCOPED_PERMISSIONS
        and normalized_resource != ALL_RESOURCES
    ):
        raise ApplicationAccessError(f"{normalized_permission} does not accept a scoped resource")
    return (
        cast(ApplicationPermission, normalized_permission),
        normalized_resource,
    )


def _grant_relationship_schemas() -> list[dict[str, Any]]:
    collection_scoped = sorted(COLLECTION_SCOPED_PERMISSIONS)
    unscoped = sorted(
        APPLICATION_PERMISSIONS - COLLECTION_SCOPED_PERMISSIONS - {COLLECTIONS_CREATE}
    )
    return [
        {
            "required": ["permission"],
            "properties": {
                "permission": {"const": ALL_PERMISSIONS},
                "resource": {"const": ALL_RESOURCES},
            },
        },
        {
            "required": ["permission"],
            "properties": {
                "permission": {"const": COLLECTIONS_CREATE},
                "resource": {
                    "type": "string",
                    "pattern": r"^(?:\*|tag:[a-z0-9]+(?:-[a-z0-9]+)*)$",
                },
            },
        },
        {
            "required": ["permission"],
            "properties": {
                "permission": {"enum": collection_scoped},
                "resource": {
                    "type": "string",
                    "pattern": (
                        r"^(?:\*|tag:[a-z0-9]+(?:-[a-z0-9]+)*|"
                        r"collection:[1-9][0-9]*)$"
                    ),
                },
            },
        },
        {
            "required": ["permission"],
            "properties": {
                "permission": {"enum": unscoped},
                "resource": {"const": ALL_RESOURCES},
            },
        },
    ]


def _normalize_resource(value: str) -> str:
    candidate = value.strip()
    folded = candidate.casefold()
    if folded == ALL_RESOURCES:
        return ALL_RESOURCES
    if folded.startswith(TAG_PREFIX):
        return tag_resource(candidate[len(TAG_PREFIX) :])
    if folded.startswith(COLLECTION_PREFIX):
        return collection_resource(candidate[len(COLLECTION_PREFIX) :])
    raise ApplicationAccessError("application resources must be *, tag:<tag>, or collection:<id>")


def _canonical_tag(value: str) -> str:
    try:
        normalized = normalize_tag(value)
    except ValueError as exc:
        raise ApplicationAccessError(str(exc)) from exc
    if value != normalized:
        raise ApplicationAccessError("application access tag must be canonical")
    return normalized


__all__ = [
    "ALL_PERMISSIONS",
    "ALL_RESOURCES",
    "APPLICATION_PERMISSIONS",
    "ARCHIVES_MANAGE",
    "ARCHIVES_READ",
    "ApplicationAccess",
    "ApplicationAccessError",
    "ApplicationAccessGrant",
    "ApplicationAccessGrantSet",
    "ApplicationPermission",
    "ApplicationResource",
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
    "permission_covers",
    "permission_resources",
    "resource_covers",
    "tag_resource",
]
