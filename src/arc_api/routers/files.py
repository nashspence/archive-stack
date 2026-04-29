from __future__ import annotations

from typing import cast

from fastapi import APIRouter, Query, Response

from arc_api.deps import ContainerDep
from arc_api.schemas.files import (
    CollectionFileOut,
    CollectionFilesResponse,
    FilesResponse,
    FileStateOut,
)

router = APIRouter(tags=["files"])


@router.get("/collection-files/{collection_id:path}", response_model=CollectionFilesResponse)
def list_collection_files(
    collection_id: str,
    container: ContainerDep,
    page: int = Query(1, ge=1),
    per_page: int = Query(25, ge=1, le=100),
) -> CollectionFilesResponse:
    payload = container.files.list_collection_files(
        collection_id,
        page=page,
        per_page=per_page,
    )
    files = cast(list[dict[str, object]], payload["files"])
    return CollectionFilesResponse.model_validate(
        {
            **payload,
            "files": [CollectionFileOut.model_validate(record) for record in files],
        }
    )


@router.get("/files", response_model=FilesResponse)
def query_files(
    container: ContainerDep,
    target: str = Query(..., min_length=1),
    page: int = Query(1, ge=1),
    per_page: int = Query(25, ge=1, le=100),
) -> FilesResponse:
    payload = container.files.query_by_target(
        target,
        page=page,
        per_page=per_page,
    )
    files = cast(list[dict[str, object]], payload["files"])
    return FilesResponse.model_validate(
        {
            **payload,
            "files": [FileStateOut.model_validate(record) for record in files],
        }
    )


@router.get("/files/{target:path}/content")
def get_file_content(
    target: str,
    container: ContainerDep,
) -> Response:
    content = container.files.get_content(target)
    return Response(content=content, media_type="application/octet-stream")
