from __future__ import annotations

import os
import secrets
from collections.abc import Callable
from typing import Annotated, cast

from fastapi import Depends
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from riverhog_core.app_permissions import (
    ARCHIVES_MANAGE,
    ARCHIVES_READ,
    CATALOG_READ,
    COLLECTION_TAGS_MANAGE,
    COLLECTIONS_CREATE,
    COLLECTIONS_DELETE,
    COLLECTION_TRANSFORMS_MANAGE,
    EVENTS_READ,
    KEYS_MANAGE,
    PROVENANCE_EXPORT,
    PROVENANCE_READ,
    QUOTAS_MANAGE,
    RETRIEVAL_MANAGE,
    TAGS_CREATE,
    TAGS_DELETE,
    ApplicationAccess,
    ApplicationPrincipal,
)
from riverhog_protocol.errors import Forbidden, Unauthorized

from riverhog_api.deps import ContainerDep, ServiceContainer

BOOTSTRAP_TOKEN_ENV = "RIVERHOG_BOOTSTRAP_TOKEN"

_bearer = HTTPBearer(auto_error=False)
BearerCredentials = Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)]
PermissionDependency = Callable[..., ApplicationPrincipal]


def authenticate_token(token: str, container: ServiceContainer) -> ApplicationPrincipal | None:
    supplied = token.strip()
    if not supplied:
        return None
    bootstrap = os.getenv(BOOTSTRAP_TOKEN_ENV, "")
    if bootstrap and secrets.compare_digest(supplied, bootstrap):
        return ApplicationPrincipal(
            app="bootstrap",
            key_id=None,
            access=frozenset(
                {
                    ApplicationAccess(KEYS_MANAGE),
                    ApplicationAccess(QUOTAS_MANAGE),
                }
            ),
            unrestricted_delegation=True,
        )
    principal = container.app_keys.authenticate(supplied)
    if principal is not None:
        return principal
    return container.collection_workflows.authenticate_capability(supplied)


def require_application(
    credentials: BearerCredentials,
    container: ContainerDep,
) -> ApplicationPrincipal:
    supplied = credentials.credentials if credentials is not None else ""
    principal = authenticate_token(supplied, container)
    if principal is not None:
        return principal
    raise Unauthorized("invalid application token")


def require_permission(permission: str) -> PermissionDependency:
    def dependency(
        credentials: BearerCredentials,
        container: ContainerDep,
    ) -> ApplicationPrincipal:
        principal = require_application(credentials, container)
        if principal.allows(permission):
            return principal
        raise Forbidden(f"application permission required: {permission}")

    dependency.riverhog_permission = permission  # type: ignore[attr-defined]
    return dependency


CatalogReader = Annotated[
    ApplicationPrincipal,
    Depends(cast(Callable[..., object], require_permission(CATALOG_READ))),
]
RetrievalManager = Annotated[
    ApplicationPrincipal,
    Depends(cast(Callable[..., object], require_permission(RETRIEVAL_MANAGE))),
]
CollectionCreator = Annotated[
    ApplicationPrincipal,
    Depends(cast(Callable[..., object], require_permission(COLLECTIONS_CREATE))),
]
CollectionTransformManager = Annotated[
    ApplicationPrincipal,
    Depends(
        cast(Callable[..., object], require_permission(COLLECTION_TRANSFORMS_MANAGE))
    ),
]
CollectionTagManager = Annotated[
    ApplicationPrincipal,
    Depends(cast(Callable[..., object], require_permission(COLLECTION_TAGS_MANAGE))),
]
TagCreator = Annotated[
    ApplicationPrincipal,
    Depends(cast(Callable[..., object], require_permission(TAGS_CREATE))),
]
TagDeleter = Annotated[
    ApplicationPrincipal,
    Depends(cast(Callable[..., object], require_permission(TAGS_DELETE))),
]
CollectionDeleter = Annotated[
    ApplicationPrincipal,
    Depends(cast(Callable[..., object], require_permission(COLLECTIONS_DELETE))),
]
ArchiveReader = Annotated[
    ApplicationPrincipal,
    Depends(cast(Callable[..., object], require_permission(ARCHIVES_READ))),
]
ArchiveManager = Annotated[
    ApplicationPrincipal,
    Depends(cast(Callable[..., object], require_permission(ARCHIVES_MANAGE))),
]
KeyManager = Annotated[
    ApplicationPrincipal,
    Depends(cast(Callable[..., object], require_permission(KEYS_MANAGE))),
]
QuotaManager = Annotated[
    ApplicationPrincipal,
    Depends(cast(Callable[..., object], require_permission(QUOTAS_MANAGE))),
]
EventsReader = Annotated[
    ApplicationPrincipal,
    Depends(cast(Callable[..., object], require_permission(EVENTS_READ))),
]
ProvenanceReader = Annotated[
    ApplicationPrincipal,
    Depends(cast(Callable[..., object], require_permission(PROVENANCE_READ))),
]
ProvenanceExporter = Annotated[
    ApplicationPrincipal,
    Depends(cast(Callable[..., object], require_permission(PROVENANCE_EXPORT))),
]


__all__ = [
    "ArchiveManager",
    "ArchiveReader",
    "BOOTSTRAP_TOKEN_ENV",
    "CatalogReader",
    "CollectionCreator",
    "CollectionDeleter",
    "CollectionTagManager",
    "CollectionTransformManager",
    "EventsReader",
    "KeyManager",
    "QuotaManager",
    "ProvenanceExporter",
    "ProvenanceReader",
    "RetrievalManager",
    "TagCreator",
    "TagDeleter",
    "authenticate_token",
    "require_application",
    "require_permission",
]
