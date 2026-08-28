from __future__ import annotations

from typing import Annotated, cast

from fastapi import APIRouter, Query, Response
from riverhog_protocol import CollectionIdParameter, SearchSort, SortOrder

from riverhog_api.auth import CatalogReader
from riverhog_api.complete_enumeration import (
    CompleteEnumerationResponse,
    bounded_list_operation,
    complete_enumeration_operation,
    complete_enumeration_response,
)
from riverhog_api.deps import ContainerDep
from riverhog_api.schemas.search import SearchFileOut, SearchResponse

router = APIRouter(tags=["search"])


@router.get(
    "/search",
    response_model=SearchResponse,
    openapi_extra=bounded_list_operation(paired_operation_id="stream_search"),
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


@router.get(
    "/search/stream",
    response_class=CompleteEnumerationResponse,
    openapi_extra=complete_enumeration_operation(
        paired_operation_id="search",
        item_type=SearchFileOut,
        schema_id="riverhog.search-file/v1",
    ),
)
def stream_search(
    container: ContainerDep,
    principal: CatalogReader,
    q: str | None = Query(None, min_length=1),
    sort: Annotated[SearchSort, Query()] = "file_ref",
    order: Annotated[SortOrder, Query()] = "asc",
    collection: Annotated[CollectionIdParameter | None, Query()] = None,
) -> Response:
    query = {"q": q, "sort": sort, "order": order, "collection": collection}
    return complete_enumeration_response(
        container.search.iter_files(
            q=q,
            sort=sort,
            order=order,
            collection=collection,
            principal=principal,
        ),
        query=query,
        item_type=SearchFileOut,
        schema_id="riverhog.search-file/v1",
    )
