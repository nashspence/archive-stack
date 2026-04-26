from __future__ import annotations

from fastapi import APIRouter, Request, Response

from arc_api.deps import ContainerDep
from arc_api.mappers import map_collection
from arc_api.schemas.collections import (
    CollectionFileUploadSessionOut,
    CollectionSummaryOut,
    CollectionUploadSessionOut,
    CreateOrResumeCollectionUploadRequest,
)
from arc_core.domain.errors import BadRequest

router = APIRouter(tags=["collections"])


@router.post("/collection-uploads", response_model=CollectionUploadSessionOut)
def create_or_resume_collection_upload(
    request: CreateOrResumeCollectionUploadRequest,
    container: ContainerDep,
) -> CollectionUploadSessionOut:
    payload = container.collections.create_or_resume_upload(
        collection_id=request.collection_id,
        files=[item.model_dump() for item in request.files],
        ingest_source=request.ingest_source,
    )
    return CollectionUploadSessionOut.model_validate(payload)


@router.get("/collection-uploads/{collection_id:path}", response_model=CollectionUploadSessionOut)
def get_collection_upload(
    collection_id: str,
    container: ContainerDep,
) -> CollectionUploadSessionOut:
    payload = container.collections.get_upload(collection_id)
    return CollectionUploadSessionOut.model_validate(payload)


@router.post(
    "/collection-uploads/{collection_id:path}/files/{path:path}/upload",
    response_model=CollectionFileUploadSessionOut,
)
def create_or_resume_collection_file_upload(
    collection_id: str,
    path: str,
    request: Request,
    container: ContainerDep,
) -> CollectionFileUploadSessionOut:
    payload = container.collections.create_or_resume_file_upload(collection_id, path)
    payload["upload_url"] = str(request.url)
    return CollectionFileUploadSessionOut.model_validate(payload)


@router.patch("/collection-uploads/{collection_id:path}/files/{path:path}/upload", status_code=204)
async def append_collection_file_upload_chunk(
    collection_id: str,
    path: str,
    request: Request,
    container: ContainerDep,
) -> Response:
    raw_offset = request.headers.get("Upload-Offset")
    raw_checksum = request.headers.get("Upload-Checksum")
    if raw_offset is None:
        raise BadRequest("missing Upload-Offset header")
    if raw_checksum is None:
        raise BadRequest("missing Upload-Checksum header")
    try:
        offset = int(raw_offset)
    except ValueError as exc:
        raise BadRequest("Upload-Offset header must be an integer") from exc

    payload = container.collections.append_upload_chunk(
        collection_id,
        path,
        offset=offset,
        checksum=raw_checksum,
        content=await request.body(),
    )
    headers = {"Upload-Offset": str(payload["offset"])}
    if payload["expires_at"] is not None:
        headers["Upload-Expires"] = str(payload["expires_at"])
    return Response(status_code=204, headers=headers)


@router.get("/collections/{collection_id:path}", response_model=CollectionSummaryOut)
def get_collection(
    collection_id: str,
    container: ContainerDep,
) -> CollectionSummaryOut:
    summary = container.collections.get(collection_id)  # type: ignore[attr-defined]
    return CollectionSummaryOut.model_validate(map_collection(summary))
