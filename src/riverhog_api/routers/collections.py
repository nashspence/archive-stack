from __future__ import annotations

from enum import StrEnum
from typing import Annotated

from fastapi import APIRouter, Query, Request, Response

from riverhog_api.deps import ContainerDep
from riverhog_api.mappers import map_collection, map_collection_list_page
from riverhog_api.schemas.collections import (
    CollectionFileUploadSessionOut,
    CollectionSummaryOut,
    CollectionUploadSessionOut,
    CreateOrResumeCollectionUploadRequest,
    CreateOrResumeCollectionUploadSessionRequest,
    ListCollectionsResponse,
    RegisterCollectionUploadSessionFileRequest,
)
from riverhog_api.tus import (
    tus_delete_headers,
    tus_options_headers,
    tus_upload_headers,
    validate_tus_chunk_request,
)
from riverhog_api.urls import public_request_url

router = APIRouter(tags=["collections"])


class CollectionProtectionFilter(StrEnum):
    UNDER_PROTECTED = "under_protected"
    CLOUD_ONLY = "cloud_only"
    PHYSICAL_ONLY = "physical_only"
    FULLY_PROTECTED = "fully_protected"


_CORE_PROTECTION_FILTERS = {
    "under_protected": "partially_protected",
    "cloud_only": "unprotected",
    "physical_only": "partially_protected",
    "fully_protected": "protected",
}


@router.get("/collections", response_model=ListCollectionsResponse)
def list_collections(
    container: ContainerDep,
    page: int = Query(1, ge=1),
    per_page: int = Query(25, ge=1, le=100),
    q: str | None = Query(None),
    protection_state: Annotated[CollectionProtectionFilter | None, Query()] = None,
) -> ListCollectionsResponse:
    service_protection_state = (
        _CORE_PROTECTION_FILTERS[protection_state.value]
        if protection_state is not None
        else None
    )
    summary = container.collections.list(
        page=page,
        per_page=per_page,
        q=q,
        protection_state=service_protection_state,
    )
    return ListCollectionsResponse.model_validate(map_collection_list_page(summary))


@router.post("/collection-uploads", response_model=CollectionUploadSessionOut)
def create_or_resume_collection_upload(
    request: CreateOrResumeCollectionUploadRequest,
    container: ContainerDep,
) -> CollectionUploadSessionOut:
    payload = container.collections.create_or_resume_upload(
        upload_slug=request.slug,
        files=[item.model_dump() for item in request.files],
        ingest_source=request.ingest_source,
        upload_timestamp=request.upload_timestamp,
    )
    return CollectionUploadSessionOut.model_validate(payload)


@router.post("/collection-upload-sessions", response_model=CollectionUploadSessionOut)
def create_or_resume_collection_upload_session(
    request: CreateOrResumeCollectionUploadSessionRequest,
    container: ContainerDep,
) -> CollectionUploadSessionOut:
    payload = container.collections.create_or_resume_upload_session(
        upload_slug=request.slug,
        ingest_source=request.ingest_source,
        upload_timestamp=request.upload_timestamp,
    )
    return CollectionUploadSessionOut.model_validate(payload)


@router.post(
    "/collection-upload-sessions/{collection_id:path}/files",
    response_model=CollectionUploadSessionOut,
)
def register_collection_upload_session_file(
    collection_id: str,
    request: RegisterCollectionUploadSessionFileRequest,
    container: ContainerDep,
) -> CollectionUploadSessionOut:
    payload = container.collections.register_upload_session_file(
        collection_id,
        request.model_dump(),
    )
    return CollectionUploadSessionOut.model_validate(payload)


@router.post(
    "/collection-upload-sessions/{collection_id:path}/complete",
    response_model=CollectionUploadSessionOut,
)
def complete_collection_upload_session(
    collection_id: str,
    container: ContainerDep,
) -> CollectionUploadSessionOut:
    payload = container.collections.complete_upload_session(collection_id)
    return CollectionUploadSessionOut.model_validate(payload)


@router.post(
    "/collection-upload-sessions/{collection_id:path}/cancel",
    response_model=CollectionUploadSessionOut,
)
def cancel_collection_upload_session(
    collection_id: str,
    container: ContainerDep,
) -> CollectionUploadSessionOut:
    payload = container.collections.cancel_upload_session(collection_id)
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
    response: Response,
    container: ContainerDep,
) -> CollectionFileUploadSessionOut:
    payload = container.collections.create_or_resume_file_upload(collection_id, path)
    payload["upload_url"] = public_request_url(request)
    response.headers.update(tus_upload_headers(payload, request=request))
    return CollectionFileUploadSessionOut.model_validate(payload)


@router.patch("/collection-uploads/{collection_id:path}/files/{path:path}/upload", status_code=204)
async def append_collection_file_upload_chunk(
    collection_id: str,
    path: str,
    request: Request,
    container: ContainerDep,
) -> Response:
    offset, checksum = validate_tus_chunk_request(request)
    payload = container.collections.append_upload_chunk(
        collection_id,
        path,
        offset=offset,
        checksum=checksum,
        content=await request.body(),
    )
    headers = tus_upload_headers(payload, request=request)
    return Response(status_code=204, headers=headers)


@router.head("/collection-uploads/{collection_id:path}/files/{path:path}/upload", status_code=204)
def head_collection_file_upload(
    collection_id: str,
    path: str,
    request: Request,
    container: ContainerDep,
) -> Response:
    payload = container.collections.get_file_upload(collection_id, path)
    return Response(status_code=204, headers=tus_upload_headers(payload, request=request))


@router.delete("/collection-uploads/{collection_id:path}/files/{path:path}/upload", status_code=204)
def delete_collection_file_upload(
    collection_id: str,
    path: str,
    container: ContainerDep,
) -> Response:
    container.collections.cancel_file_upload(collection_id, path)
    return Response(status_code=204, headers=tus_delete_headers())


@router.options(
    "/collection-uploads/{collection_id:path}/files/{path:path}/upload",
    status_code=204,
)
def options_collection_file_upload() -> Response:
    return Response(status_code=204, headers=tus_options_headers())


@router.get("/collections/{collection_id:path}", response_model=CollectionSummaryOut)
def get_collection(
    collection_id: str,
    container: ContainerDep,
) -> CollectionSummaryOut:
    summary = container.collections.get(collection_id)
    return CollectionSummaryOut.model_validate(map_collection(summary))
