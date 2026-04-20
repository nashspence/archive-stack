from __future__ import annotations

from fastapi import APIRouter, Depends

from arc_api.deps import ServiceContainer, get_container
from arc_api.mappers import map_collection
from arc_api.schemas.collections import CloseCollectionRequest, CloseCollectionResponse, CollectionSummaryOut

router = APIRouter(tags=["collections"])


@router.post("/collections/close", response_model=CloseCollectionResponse)
def close_collection(
    request: CloseCollectionRequest,
    container: ServiceContainer = Depends(get_container),
) -> CloseCollectionResponse:
    summary = container.collections.close(request.path)
    return CloseCollectionResponse(collection=CollectionSummaryOut.model_validate(map_collection(summary)))


@router.get("/collections/{collection_id}", response_model=CollectionSummaryOut)
def get_collection(
    collection_id: str,
    container: ServiceContainer = Depends(get_container),
) -> CollectionSummaryOut:
    summary = container.collections.get(collection_id)  # type: ignore[attr-defined]
    return CollectionSummaryOut.model_validate(map_collection(summary))
