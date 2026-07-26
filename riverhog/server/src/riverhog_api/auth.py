from __future__ import annotations

import os
import secrets
from collections.abc import Callable
from typing import Annotated, cast

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from riverhog_core.app_permissions import (
    ARCHIVES_MANAGE,
    ARCHIVES_READ,
    CATALOG_READ,
    COLLECTION_TAGS_MANAGE,
    COLLECTIONS_CREATE,
    COLLECTIONS_DELETE,
    EVENTS_READ,
    KEYS_MANAGE,
    QUOTAS_MANAGE,
    RETRIEVAL_MANAGE,
    TAGS_CREATE,
    ApplicationAccess,
    ApplicationPrincipal,
)

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
    return container.app_keys.authenticate(supplied)


def authenticate_authorization_header(
    authorization: str | None,
    container: ServiceContainer,
) -> ApplicationPrincipal | None:
    scheme, _, token = (authorization or "").partition(" ")
    if scheme.casefold() != "bearer":
        return None
    return authenticate_token(token, container)


def require_application(
    credentials: BearerCredentials,
    container: ContainerDep,
) -> ApplicationPrincipal:
    supplied = credentials.credentials if credentials is not None else ""
    principal = authenticate_token(supplied, container)
    if principal is not None:
        return principal
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="invalid application token",
        headers={"WWW-Authenticate": "Bearer"},
    )


def require_permission(permission: str) -> PermissionDependency:
    def dependency(
        credentials: BearerCredentials,
        container: ContainerDep,
    ) -> ApplicationPrincipal:
        principal = require_application(credentials, container)
        if principal.allows(permission):
            return principal
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"application permission required: {permission}",
        )

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
CollectionTagManager = Annotated[
    ApplicationPrincipal,
    Depends(cast(Callable[..., object], require_permission(COLLECTION_TAGS_MANAGE))),
]
TagCreator = Annotated[
    ApplicationPrincipal,
    Depends(cast(Callable[..., object], require_permission(TAGS_CREATE))),
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


__all__ = [
    "ArchiveManager",
    "ArchiveReader",
    "BOOTSTRAP_TOKEN_ENV",
    "CatalogReader",
    "CollectionCreator",
    "CollectionDeleter",
    "CollectionTagManager",
    "EventsReader",
    "KeyManager",
    "QuotaManager",
    "RetrievalManager",
    "TagCreator",
    "authenticate_authorization_header",
    "authenticate_token",
    "require_application",
    "require_permission",
]
