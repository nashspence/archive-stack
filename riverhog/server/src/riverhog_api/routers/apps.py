from __future__ import annotations

from datetime import timedelta
from typing import Annotated

from fastapi import APIRouter, Query
from http_api_contracts import bounded_list_operation
from riverhog_application_access import (
    ApplicationKeyId,
    ApplicationName,
    ApplicationPermission,
    ApplicationResource,
)
from riverhog_protocol import (
    ApplicationAccessSort,
    ApplicationKeySort,
    ApplicationSort,
    SortOrder,
)

from riverhog_api.auth import KeyManager
from riverhog_api.deps import ContainerDep
from riverhog_api.schemas.apps import (
    AppAccessListOut,
    AppAccessSetOut,
    AppKeyCreatedOut,
    AppKeyListOut,
    AppKeyOut,
    AppListOut,
    CreateAppKeyRequest,
    MutateAppAccessRequest,
    ReplaceAppAccessRequest,
)

router = APIRouter(tags=["apps"])


@router.get(
    "/apps",
    response_model=AppListOut,
    openapi_extra=bounded_list_operation(),
)
def list_apps(
    container: ContainerDep,
    _principal: KeyManager,
    page: int = Query(1, ge=1),
    per_page: int = Query(25, ge=1, le=100),
    sort: Annotated[ApplicationSort, Query()] = "name",
    order: Annotated[SortOrder, Query()] = "asc",
    q: str | None = Query(None),
    active: bool | None = Query(None),
) -> AppListOut:
    return AppListOut.model_validate(
        container.app_keys.list_apps(
            page=page,
            per_page=per_page,
            q=q,
            sort=sort,
            order=order,
            active=active,
        )
    )


@router.post("/apps/{app}/keys", response_model=AppKeyCreatedOut)
def create_app_key(
    app: ApplicationName,
    request: CreateAppKeyRequest,
    container: ContainerDep,
    principal: KeyManager,
) -> AppKeyCreatedOut:
    return AppKeyCreatedOut.model_validate(
        container.app_keys.create(
            app=app,
            access=[(current.permission, current.resource) for current in request.access.root],
            grantor=principal,
            expires_in=(
                timedelta(seconds=request.expires_in_seconds)
                if request.expires_in_seconds is not None
                else None
            ),
        )
    )


@router.post("/apps/{app}/keys/{key_id}/rotate", response_model=AppKeyCreatedOut)
def rotate_app_key(
    app: ApplicationName,
    key_id: ApplicationKeyId,
    container: ContainerDep,
    principal: KeyManager,
) -> AppKeyCreatedOut:
    return AppKeyCreatedOut.model_validate(
        container.app_keys.rotate(app=app, key_id=key_id, grantor=principal)
    )


@router.get(
    "/app-key-access",
    response_model=AppAccessListOut,
    openapi_extra=bounded_list_operation(),
)
def list_app_key_access(
    container: ContainerDep,
    _principal: KeyManager,
    page: int = Query(1, ge=1),
    per_page: int = Query(25, ge=1, le=100),
    sort: Annotated[ApplicationAccessSort, Query()] = "permission",
    order: Annotated[SortOrder, Query()] = "asc",
    q: str | None = Query(None),
    app: Annotated[ApplicationName | None, Query()] = None,
    key_id: Annotated[ApplicationKeyId | None, Query(alias="key")] = None,
    permission: Annotated[ApplicationPermission | None, Query()] = None,
    resource: Annotated[ApplicationResource | None, Query()] = None,
    active: bool | None = Query(None),
) -> AppAccessListOut:
    return AppAccessListOut.model_validate(
        container.app_keys.list_access(
            page=page,
            per_page=per_page,
            q=q,
            sort=sort,
            order=order,
            app=app,
            key_id=key_id,
            permission=permission,
            resource=resource,
            active=active,
        )
    )


@router.put(
    "/apps/{app}/keys/{key_id}/access",
    response_model=AppAccessSetOut,
)
def replace_app_key_access(
    app: ApplicationName,
    key_id: ApplicationKeyId,
    request: ReplaceAppAccessRequest,
    container: ContainerDep,
    principal: KeyManager,
) -> AppAccessSetOut:
    return AppAccessSetOut.model_validate(
        container.app_keys.replace_access(
            app=app,
            key_id=key_id,
            access=[(current.permission, current.resource) for current in request.access.root],
            grantor=principal,
        )
    )


@router.post(
    "/apps/{app}/keys/{key_id}/access",
    response_model=AppAccessSetOut,
)
def add_app_key_access(
    app: ApplicationName,
    key_id: ApplicationKeyId,
    request: MutateAppAccessRequest,
    container: ContainerDep,
    principal: KeyManager,
) -> AppAccessSetOut:
    return AppAccessSetOut.model_validate(
        container.app_keys.add_access(
            app=app,
            key_id=key_id,
            access=(request.permission, request.resource),
            grantor=principal,
        )
    )


@router.delete(
    "/apps/{app}/keys/{key_id}/access",
    response_model=AppAccessSetOut,
)
def remove_app_key_access(
    app: ApplicationName,
    key_id: ApplicationKeyId,
    request: MutateAppAccessRequest,
    container: ContainerDep,
    _principal: KeyManager,
) -> AppAccessSetOut:
    return AppAccessSetOut.model_validate(
        container.app_keys.remove_access(
            app=app,
            key_id=key_id,
            access=(request.permission, request.resource),
        )
    )


@router.get(
    "/apps/{app}/keys",
    response_model=AppKeyListOut,
    openapi_extra=bounded_list_operation(),
)
def list_app_keys(
    app: ApplicationName,
    container: ContainerDep,
    _principal: KeyManager,
    page: int = Query(1, ge=1),
    per_page: int = Query(25, ge=1, le=100),
    sort: Annotated[ApplicationKeySort, Query()] = "created_at",
    order: Annotated[SortOrder, Query()] = "desc",
    q: str | None = Query(None),
    active: bool | None = Query(None),
) -> AppKeyListOut:
    return AppKeyListOut.model_validate(
        container.app_keys.list_keys(
            app=app,
            page=page,
            per_page=per_page,
            q=q,
            sort=sort,
            order=order,
            active=active,
        )
    )


@router.post("/apps/{app}/keys/{key_id}/revoke", response_model=AppKeyOut)
def revoke_app_key(
    app: ApplicationName,
    key_id: ApplicationKeyId,
    container: ContainerDep,
    _principal: KeyManager,
) -> AppKeyOut:
    return AppKeyOut.model_validate(container.app_keys.revoke(app=app, key_id=key_id))
