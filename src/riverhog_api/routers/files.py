from __future__ import annotations

from typing import cast

from fastapi import APIRouter, Query, Response

from riverhog_api.deps import ContainerDep
from riverhog_api.schemas.files import (
    FilesResponse,
    FileStateOut,
)

router = APIRouter(tags=["files"])


@router.get("/files", response_model=FilesResponse)
def query_files(
    container: ContainerDep,
    path: str = Query(..., min_length=1),
    page: int = Query(1, ge=1),
    per_page: int = Query(25, ge=1, le=100),
) -> FilesResponse:
    payload = container.files.query_by_path(
        path,
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


@router.get("/files/{path:path}/content")
def get_file_content(
    path: str,
    container: ContainerDep,
) -> Response:
    content = container.files.get_content(path)
    return Response(content=content, media_type="application/octet-stream")
