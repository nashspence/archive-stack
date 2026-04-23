from __future__ import annotations

from fastapi import APIRouter

from arc_api.deps import ContainerDep
from arc_api.mappers import map_collection
from arc_api.schemas.collections import (
    CloseCollectionRequest,
    CloseCollectionResponse,
    CollectionSummaryOut,
)

router = APIRouter(tags=["collections"])


@router.post("/collections/close", response_model=CloseCollectionResponse)
def close_collection(
    request: CloseCollectionRequest,
    container: ContainerDep,
) -> CloseCollectionResponse:
    summary = container.collections.close(request.path)
    return CloseCollectionResponse(
        collection=CollectionSummaryOut.model_validate(map_collection(summary))
    )


@router.get("/collections/{collection_id:path}", response_model=CollectionSummaryOut)
def get_collection(
    collection_id: str,
    container: ContainerDep,
) -> CollectionSummaryOut:
    summary = container.collections.get(collection_id)  # type: ignore[attr-defined]
    return CollectionSummaryOut.model_validate(map_collection(summary))
