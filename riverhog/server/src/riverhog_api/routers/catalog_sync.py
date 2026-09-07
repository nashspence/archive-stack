from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query, Response
from http_api_contracts import (
    cursor_feed_operation,
    exact_authority_page_operation,
    operation_interface,
)
from riverhog_protocol import (
    CATALOG_SYNC_PAGE_SIZE_MAX,
    CatalogSyncChangePage,
    CatalogSyncCheckpoint,
    CatalogSyncCollectionPage,
    CatalogSyncCursor,
)

from riverhog_api.auth import CatalogReader
from riverhog_api.deps import ContainerDep

router = APIRouter(tags=["catalog synchronization"])


def _no_store(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store"
    response.headers["Vary"] = "Authorization"


@router.get(
    "/catalog-sync/checkpoint",
    response_model=CatalogSyncCheckpoint,
    openapi_extra=operation_interface("client-only-primitive"),
)
def create_catalog_sync_checkpoint(
    response: Response,
    container: ContainerDep,
    principal: CatalogReader,
) -> CatalogSyncCheckpoint:
    _no_store(response)
    return container.catalog_sync.checkpoint(principal=principal)


@router.get(
    "/catalog-sync/collections",
    response_model=CatalogSyncCollectionPage,
    openapi_extra=exact_authority_page_operation(
        authority="catalog-sync-bootstrap",
        authority_parameter=None,
        cursor_parameter="cursor",
        limit_parameter="limit",
    ),
)
def list_catalog_sync_collections(
    response: Response,
    container: ContainerDep,
    principal: CatalogReader,
    cursor: Annotated[CatalogSyncCursor, Query()],
    limit: int = Query(default=100, ge=1, le=CATALOG_SYNC_PAGE_SIZE_MAX),
) -> CatalogSyncCollectionPage:
    _no_store(response)
    return container.catalog_sync.collections(cursor=cursor, limit=limit, principal=principal)


@router.get(
    "/catalog-sync/changes",
    response_model=CatalogSyncChangePage,
    openapi_extra=cursor_feed_operation(cursor_parameter="cursor", limit_parameter="limit"),
)
def list_catalog_sync_changes(
    response: Response,
    container: ContainerDep,
    principal: CatalogReader,
    cursor: Annotated[CatalogSyncCursor, Query()],
    limit: int = Query(default=100, ge=1, le=CATALOG_SYNC_PAGE_SIZE_MAX),
) -> CatalogSyncChangePage:
    _no_store(response)
    return container.catalog_sync.changes(cursor=cursor, limit=limit, principal=principal)


__all__ = ["router"]
