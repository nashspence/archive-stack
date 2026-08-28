from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query, Response
from riverhog_application_access import ApplicationKeyId, ApplicationName
from riverhog_protocol import DownloadQuotaSort, SortOrder
from riverhog_protocol.errors import Forbidden

from riverhog_api.auth import QuotaManager, RetrievalManager
from riverhog_api.complete_enumeration import (
    CompleteEnumerationResponse,
    bounded_list_operation,
    complete_enumeration_operation,
    complete_enumeration_response,
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
    openapi_extra=bounded_list_operation(paired_operation_id="stream_download_quotas"),
)
def list_download_quotas(
    container: ContainerDep,
    _principal: QuotaManager,
    page: int = Query(1, ge=1),
    per_page: int = Query(25, ge=1, le=100),
    sort: Annotated[DownloadQuotaSort, Query()] = "app",
    order: Annotated[SortOrder, Query()] = "asc",
    q: str | None = Query(None),
    app: Annotated[ApplicationName | None, Query()] = None,
    active: bool | None = Query(None),
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
        )
    )


@router.get(
    "/download-quotas/stream",
    response_class=CompleteEnumerationResponse,
    openapi_extra=complete_enumeration_operation(
        paired_operation_id="list_download_quotas",
        item_type=KeyDownloadQuotaOut,
        schema_id="riverhog.key-download-quota/v1",
    ),
)
def stream_download_quotas(
    container: ContainerDep,
    _principal: QuotaManager,
    sort: Annotated[DownloadQuotaSort, Query()] = "app",
    order: Annotated[SortOrder, Query()] = "asc",
    q: str | None = Query(None),
    app: Annotated[ApplicationName | None, Query()] = None,
    active: bool | None = Query(None),
) -> Response:
    query = {"q": q, "sort": sort, "order": order, "app": app, "active": active}
    return complete_enumeration_response(
        container.download_quotas.iter_key_quotas(
            q=q, sort=sort, order=order, app=app, active=active
        ),
        query=query,
        item_type=KeyDownloadQuotaOut,
        schema_id="riverhog.key-download-quota/v1",
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
