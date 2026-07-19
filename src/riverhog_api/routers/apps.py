from __future__ import annotations

from datetime import timedelta

from fastapi import APIRouter, Query

from riverhog_api.auth import KeyManager
from riverhog_api.deps import ContainerDep
from riverhog_api.schemas.apps import (
    AppKeyCreatedOut,
    AppKeyListOut,
    AppKeyOut,
    AppListOut,
    CreateAppKeyRequest,
)

router = APIRouter(tags=["apps"])


@router.get("/apps", response_model=AppListOut)
def list_apps(
    container: ContainerDep,
    _principal: KeyManager,
    page: int = Query(1, ge=1),
    per_page: int = Query(25, ge=1, le=100),
    sort: str = Query("name"),
    order: str = Query("asc"),
    q: str | None = Query(None),
    active: bool | None = Query(None),
    all_items: bool = Query(False, alias="all"),
) -> AppListOut:
    return AppListOut.model_validate(
        container.app_keys.list_apps(
            page=page,
            per_page=per_page,
            q=q,
            sort=sort,
            order=order,
            active=active,
            all_items=all_items,
        )
    )


@router.post("/apps/{app}/keys", response_model=AppKeyCreatedOut)
def create_app_key(
    app: str,
    request: CreateAppKeyRequest,
    container: ContainerDep,
    principal: KeyManager,
) -> AppKeyCreatedOut:
    return AppKeyCreatedOut.model_validate(
        container.app_keys.create(
            app=app,
            permissions=request.permissions,
            grantor=principal,
            expires_in=(
                timedelta(seconds=request.expires_in_seconds)
                if request.expires_in_seconds is not None
                else None
            ),
        )
    )


@router.get("/apps/{app}/keys", response_model=AppKeyListOut)
def list_app_keys(
    app: str,
    container: ContainerDep,
    _principal: KeyManager,
    page: int = Query(1, ge=1),
    per_page: int = Query(25, ge=1, le=100),
    sort: str = Query("created_at"),
    order: str = Query("desc"),
    q: str | None = Query(None),
    active: bool | None = Query(None),
    all_items: bool = Query(False, alias="all"),
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
            all_items=all_items,
        )
    )


@router.post("/apps/{app}/keys/{key_id}/revoke", response_model=AppKeyOut)
def revoke_app_key(
    app: str,
    key_id: str,
    container: ContainerDep,
    _principal: KeyManager,
) -> AppKeyOut:
    return AppKeyOut.model_validate(container.app_keys.revoke(app=app, key_id=key_id))
