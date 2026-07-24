from __future__ import annotations

from fastapi import APIRouter, Query
from riverhog_protocol.errors import Forbidden

from riverhog_api.auth import QuotaManager, RetrievalManager
from riverhog_api.deps import ContainerDep
from riverhog_api.schemas.quotas import (
    KeyDownloadQuotaListOut,
    KeyDownloadQuotaOut,
    SetKeyDownloadQuotaRequest,
)

router = APIRouter(tags=["download quotas"])


@router.get("/download-quota", response_model=KeyDownloadQuotaOut)
def get_own_download_quota(
    container: ContainerDep,
    principal: RetrievalManager,
) -> KeyDownloadQuotaOut:
    if principal.key_id is None:
        raise Forbidden("a stored application key is required")
    return KeyDownloadQuotaOut.model_validate(
        container.download_quotas.get_key_quota(key_id=principal.key_id)
    )


@router.get("/download-quotas", response_model=KeyDownloadQuotaListOut)
def list_download_quotas(
    container: ContainerDep,
    _principal: QuotaManager,
    page: int = Query(1, ge=1),
    per_page: int = Query(25, ge=1, le=100),
    sort: str = Query("app"),
    order: str = Query("asc"),
    q: str | None = Query(None),
    app: str | None = Query(None),
    active: bool | None = Query(None),
    all_items: bool = Query(False, alias="all"),
) -> KeyDownloadQuotaListOut:
    return KeyDownloadQuotaListOut.model_validate(
        container.download_quotas.list_key_quotas(
            page=page,
            per_page=per_page,
            q=q,
            sort=sort,
            order=order,
            app=app,
            active=active,
            all_items=all_items,
        )
    )


@router.put(
    "/apps/{app}/keys/{key_id}/download-quota",
    response_model=KeyDownloadQuotaOut,
)
def set_download_quota(
    app: str,
    key_id: str,
    request: SetKeyDownloadQuotaRequest,
    container: ContainerDep,
    _principal: QuotaManager,
) -> KeyDownloadQuotaOut:
    return KeyDownloadQuotaOut.model_validate(
        container.download_quotas.set_key_quota(
            app=app,
            key_id=key_id,
            monthly_bytes=request.monthly_bytes,
        )
    )
