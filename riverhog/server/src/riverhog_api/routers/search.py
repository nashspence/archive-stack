from __future__ import annotations

from typing import Annotated, cast

from fastapi import APIRouter, Query
from http_api_contracts import bounded_list_operation
from riverhog_protocol import CollectionIdParameter, SearchSort, SortOrder

from riverhog_api.auth import CatalogReader
from riverhog_api.deps import ContainerDep
from riverhog_api.schemas.search import SearchFileOut, SearchResponse

router = APIRouter(tags=["search"])


@router.get(
    "/search",
    response_model=SearchResponse,
    openapi_extra=bounded_list_operation(),
)
def search(
    container: ContainerDep,
    principal: CatalogReader,
    q: str | None = Query(None, min_length=1),
    page: int = Query(1, ge=1),
    per_page: int = Query(25, ge=1, le=100),
    sort: Annotated[SearchSort, Query()] = "file_ref",
    order: Annotated[SortOrder, Query()] = "asc",
    collection: Annotated[CollectionIdParameter | None, Query()] = None,
) -> SearchResponse:
    payload = container.search.search(
        q=q,
        page=page,
        per_page=per_page,
        sort=sort,
        order=order,
        collection=collection,
        principal=principal,
    )
    files = cast(list[dict[str, object]], payload["files"])
    return SearchResponse.model_validate(
        {
            **payload,
            "files": [SearchFileOut.model_validate(record) for record in files],
        }
    )
