from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Header, Query, Response
from http_api_contracts import (
    QuotedSha256Identity,
    exact_set_page_operation,
    operation_interface,
    parse_quoted_sha256_identity,
    quote_sha256_identity,
)
from riverhog_protocol import CollectionIdParameter, PortableCollectionInventoryPage
from riverhog_protocol.errors import PreconditionRequired

from riverhog_api.auth import CatalogReader
from riverhog_api.deps import ContainerDep

router = APIRouter(tags=["catalog"])


@router.get(
    "/catalog/collections/{collection_id}/inventory",
    response_model=PortableCollectionInventoryPage,
    responses={
        200: {
            "headers": {
                "ETag": {
                    "description": "Strong identity of the immutable inventory authority.",
                    "schema": {"type": "string", "pattern": '^"[0-9a-f]{64}"$'},
                }
            }
        }
    },
    openapi_extra={
        **operation_interface("standard-tool/protocol"),
        **exact_set_page_operation(
            authority="portable-collection-inventory",
            cursor_parameter="cursor",
            limit_parameter="limit",
            validator_header="If-Match",
        ),
    },
)
def get_portable_collection_inventory(
    collection_id: CollectionIdParameter,
    principal: CatalogReader,
    container: ContainerDep,
    response: Response,
    cursor: str | None = Query(default=None, min_length=1, max_length=8192),
    limit: int = Query(default=100, ge=1, le=1000),
    if_match: Annotated[QuotedSha256Identity | None, Header(alias="If-Match")] = None,
) -> PortableCollectionInventoryPage:
    if cursor is not None and if_match is None:
        raise PreconditionRequired("inventory continuation requires If-Match")
    page = container.retrieval.collection_inventory_page(
        collection_id,
        cursor=cursor,
        limit=limit,
        expected_identity=(
            parse_quoted_sha256_identity(if_match) if if_match is not None else None
        ),
        principal=principal,
    )
    response.headers["ETag"] = quote_sha256_identity(page.authority.inventory_identity)
    return page


__all__ = ["router"]
