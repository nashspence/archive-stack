from __future__ import annotations

from fastapi import APIRouter, Query, Response

from arc_api.deps import ContainerDep
from arc_api.schemas.files import (
    CollectionFileOut,
    CollectionFilesResponse,
    FileStateOut,
    FilesResponse,
)

router = APIRouter(tags=["files"])


@router.get("/collections/{collection_id:path}/files", response_model=CollectionFilesResponse)
def list_collection_files(
    collection_id: str,
    container: ContainerDep,
) -> CollectionFilesResponse:
    records = container.files.list_collection_files(collection_id)
    return CollectionFilesResponse(
        collection_id=collection_id,
        files=[CollectionFileOut.model_validate(r) for r in records],
    )


@router.get("/files", response_model=FilesResponse)
def query_files(
    container: ContainerDep,
    target: str = Query(..., min_length=1),
) -> FilesResponse:
    records = container.files.query_by_target(target)
    return FilesResponse(files=[FileStateOut.model_validate(r) for r in records])


@router.get("/files/{target:path}/content")
def get_file_content(
    target: str,
    container: ContainerDep,
) -> Response:
    content = container.files.get_content(target)
    return Response(content=content, media_type="application/octet-stream")
