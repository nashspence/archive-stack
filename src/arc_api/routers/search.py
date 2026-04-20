from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from arc_api.deps import ServiceContainer, get_container
from arc_api.schemas.search import SearchResponse

router = APIRouter(tags=["search"])


@router.get("/search", response_model=SearchResponse)
def search(
    q: str = Query(..., min_length=1),
    limit: int = Query(25, ge=1, le=100),
    container: ServiceContainer = Depends(get_container),
) -> SearchResponse:
    results = container.search.search(q, limit)
    return SearchResponse(query=q, results=results)  # type: ignore[arg-type]
