from __future__ import annotations

from typing import Literal, cast

from fastapi import APIRouter, Query

from riverhog_api.deps import ContainerDep
from riverhog_api.schemas.search import SearchFileOut, SearchResponse

router = APIRouter(tags=["search"])


@router.get("/search", response_model=SearchResponse)
def search(
    container: ContainerDep,
    q: str | None = Query(None, min_length=1),
    page: int = Query(1, ge=1),
    per_page: int = Query(25, ge=1, le=100),
    sort: Literal[
        "logical_path",
        "collection_id",
        "collection_path",
        "bytes",
        "hot",
    ] = Query("logical_path"),
    order: Literal["asc", "desc"] = Query("asc"),
    collection: str | None = Query(None, min_length=1),
    hot: bool | None = Query(None),
) -> SearchResponse:
    payload = container.search.search(
        q=q,
        page=page,
        per_page=per_page,
        sort=sort,
        order=order,
        collection=collection,
        hot=hot,
    )
    files = cast(list[dict[str, object]], payload["files"])
    return SearchResponse.model_validate(
        {
            **payload,
            "files": [SearchFileOut.model_validate(record) for record in files],
        }
    )
