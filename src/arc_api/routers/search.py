from __future__ import annotations

from fastapi import APIRouter, Query

from arc_api.deps import ContainerDep
from arc_api.schemas.search import SearchResponse

router = APIRouter(tags=["search"])


@router.get("/search", response_model=SearchResponse)
def search(
    container: ContainerDep,
    q: str = Query(..., min_length=1),
    limit: int = Query(25, ge=1, le=100),
) -> SearchResponse:
    results = container.search.search(q, limit)
    return SearchResponse(query=q, results=results)  # type: ignore[arg-type]
