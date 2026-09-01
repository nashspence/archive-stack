from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query
from http_api_contracts import mutable_browse_operation
from riverhog_application_access import ApplicationKeyId, ApplicationName
from riverhog_protocol import DownloadQuotaSort, SortOrder
from riverhog_protocol.errors import Forbidden

from riverhog_api.auth import QuotaManager, RetrievalManager
from riverhog_api.browse import (
    BrowsePageTokenQuery,
    BrowseQueryParameter,
    canonical_selectors,
    page_payload,
    page_position,
)
from riverhog_api.deps import ContainerDep
from riverhog_api.schemas.quotas import (
    KeyDownloadQuotaListOut,
    KeyDownloadQuotaOut,
    SetKeyDownloadQuotaRequest,
)

router = APIRouter(tags=["download quotas"])


@router.get("/download-quota", response_model=KeyDownloadQuotaOut)
def get_download_quota(
    container: ContainerDep,
    principal: RetrievalManager,
) -> KeyDownloadQuotaOut:
    if principal.key_id is None:
        raise Forbidden("a stored application key is required")
    return KeyDownloadQuotaOut.model_validate(
        container.download_quotas.get_key_quota(key_id=principal.key_id)
    )


@router.get(
    "/download-quotas",
    response_model=KeyDownloadQuotaListOut,
    openapi_extra=mutable_browse_operation(),
)
def list_download_quotas(
    container: ContainerDep,
    principal: QuotaManager,
    page_size: int = Query(25, ge=1, le=100),
    page_token: BrowsePageTokenQuery = None,
    sort: Annotated[DownloadQuotaSort, Query()] = "app",
    order: Annotated[SortOrder, Query()] = "asc",
    q: BrowseQueryParameter = None,
    app: Annotated[ApplicationName | None, Query()] = None,
    active: bool | None = Query(None),
) -> KeyDownloadQuotaListOut:
    selectors = canonical_selectors(q=q, sort=sort, order=order, app=app, active=active)
    position = page_position(
        container,
        principal=principal,
        operation="list_download_quotas",
        page_token=page_token,
        selectors=selectors,
    )
    return KeyDownloadQuotaListOut.model_validate(
        page_payload(
            container.download_quotas.list_key_quotas(
                page_size=page_size,
                position=position,
                q=q,
                sort=sort,
                order=order,
                app=app,
                active=active,
            ),
            container=container,
            principal=principal,
            operation="list_download_quotas",
            selectors=selectors,
        )
    )


@router.put(
    "/apps/{app}/keys/{key_id}/download-quota",
    response_model=KeyDownloadQuotaOut,
)
def set_app_key_download_quota(
    app: ApplicationName,
    key_id: ApplicationKeyId,
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
