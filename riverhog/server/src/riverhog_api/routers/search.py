from __future__ import annotations

from typing import Literal, cast

from fastapi import APIRouter, Query

from riverhog_api.auth import CatalogReader
from riverhog_api.deps import ContainerDep
from riverhog_api.schemas.search import SearchFileOut, SearchResponse

router = APIRouter(tags=["search"])


@router.get("/search", response_model=SearchResponse)
def search(
    container: ContainerDep,
    principal: CatalogReader,
    q: str | None = Query(None, min_length=1),
    page: int = Query(1, ge=1),
    per_page: int = Query(25, ge=1, le=100),
    sort: Literal[
        "logical_path",
        "collection_id",
        "collection_path",
        "bytes",
    ] = Query("logical_path"),
    order: Literal["asc", "desc"] = Query("asc"),
    collection: int | None = Query(None, ge=1),
    all_items: bool = Query(False, alias="all"),
) -> SearchResponse:
    payload = container.search.search(
        q=q,
        page=page,
        per_page=per_page,
        sort=sort,
        order=order,
        collection=collection,
        all_items=all_items,
        principal=principal,
    )
    files = cast(list[dict[str, object]], payload["files"])
    return SearchResponse.model_validate(
        {
            **payload,
            "files": [SearchFileOut.model_validate(record) for record in files],
        }
    )
