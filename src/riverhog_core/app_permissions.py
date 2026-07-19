from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from riverhog_core.domain.errors import BadRequest

ALL_PERMISSIONS = "*"
CATALOG_READ = "catalog:read"
RETRIEVAL_MANAGE = "retrieval:manage"
COLLECTIONS_UPLOAD = "collections:upload"
COLLECTIONS_DELETE = "collections:delete"
ARCHIVES_READ = "archives:read"
ARCHIVES_MANAGE = "archives:manage"
KEYS_MANAGE = "keys:manage"

APPLICATION_PERMISSIONS = frozenset(
    {
        CATALOG_READ,
        RETRIEVAL_MANAGE,
        COLLECTIONS_UPLOAD,
        COLLECTIONS_DELETE,
        ARCHIVES_READ,
        ARCHIVES_MANAGE,
        KEYS_MANAGE,
    }
)


def normalize_permissions(values: Iterable[str]) -> tuple[str, ...]:
    permissions = tuple(sorted({str(value).strip().casefold() for value in values}))
    if not permissions or any(not permission for permission in permissions):
        raise BadRequest("at least one application permission is required")
    unknown = set(permissions) - APPLICATION_PERMISSIONS - {ALL_PERMISSIONS}
    if unknown:
        raise BadRequest(f"unknown application permission: {sorted(unknown)[0]}")
    if ALL_PERMISSIONS in permissions and len(permissions) != 1:
        raise BadRequest("the wildcard application permission must be used alone")
    return permissions


@dataclass(frozen=True, slots=True)
class ApplicationPrincipal:
    app: str
    key_id: str | None
    permissions: frozenset[str]
    unrestricted_delegation: bool = False

    def allows(self, permission: str) -> bool:
        return ALL_PERMISSIONS in self.permissions or permission in self.permissions

    def can_grant(self, permissions: Iterable[str]) -> bool:
        requested = frozenset(normalize_permissions(permissions))
        if self.unrestricted_delegation or ALL_PERMISSIONS in self.permissions:
            return True
        return ALL_PERMISSIONS not in requested and requested <= self.permissions


__all__ = [
    "ALL_PERMISSIONS",
    "APPLICATION_PERMISSIONS",
    "ARCHIVES_MANAGE",
    "ARCHIVES_READ",
    "ApplicationPrincipal",
    "CATALOG_READ",
    "COLLECTIONS_DELETE",
    "COLLECTIONS_UPLOAD",
    "KEYS_MANAGE",
    "RETRIEVAL_MANAGE",
    "normalize_permissions",
]
