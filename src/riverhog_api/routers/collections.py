from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Query, Request, Response

from riverhog_api.deps import ContainerDep
from riverhog_api.mappers import map_collection, map_collection_list_page
from riverhog_api.schemas.collections import (
    CollectionFileUploadSessionOut,
    CollectionSummaryOut,
    CollectionUploadSessionFileRegistrationOut,
    CollectionUploadSessionFileUploadOut,
    CollectionUploadSessionOut,
    CreateOrResumeCollectionUploadRequest,
    CreateOrResumeCollectionUploadSessionRequest,
    ListCollectionsResponse,
    RegisterCollectionUploadSessionFileRequest,
)
from riverhog_api.tus import (
    tus_upload_headers,
)
from riverhog_api.urls import public_tusd_upload_url

router = APIRouter(tags=["collections"])


@router.get("/collections", response_model=ListCollectionsResponse)
def list_collections(
    container: ContainerDep,
    page: int = Query(1, ge=1),
    per_page: int = Query(25, ge=1, le=100),
    q: str | None = Query(None),
    sort: str = Query("id"),
    order: str = Query("asc"),
    disc_redundancy: Literal["none", "partial", "full"] | None = Query(None),
) -> ListCollectionsResponse:
    summary = container.collections.list(
        page=page,
        per_page=per_page,
        q=q,
        disc_redundancy_state=disc_redundancy,
        sort=sort,
        order=order,
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
        notify=request.notify.model_dump() if request.notify is not None else None,
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
        notify=request.notify.model_dump() if request.notify is not None else None,
    )
    return CollectionUploadSessionOut.model_validate(payload)


@router.post(
    "/collection-upload-sessions/{collection_id:path}/files",
    response_model=CollectionUploadSessionFileRegistrationOut,
)
def register_collection_upload_session_file(
    collection_id: str,
    request: RegisterCollectionUploadSessionFileRequest,
    container: ContainerDep,
) -> CollectionUploadSessionFileRegistrationOut:
    payload = container.collections.register_upload_session_file(
        collection_id,
        request.model_dump(),
    )
    return CollectionUploadSessionFileRegistrationOut.model_validate(payload)


@router.post(
    "/collection-upload-sessions/{collection_id:path}/files/upload",
    response_model=CollectionUploadSessionFileUploadOut,
)
def create_or_resume_registered_collection_file_upload(
    collection_id: str,
    request: RegisterCollectionUploadSessionFileRequest,
    req: Request,
    response: Response,
    container: ContainerDep,
) -> CollectionUploadSessionFileUploadOut:
    payload = container.collections.create_or_resume_registered_file_upload(
        collection_id,
        request.model_dump(),
    )
    payload["upload_url"] = public_tusd_upload_url(
        str(payload["upload_url"]),
        expires_at=str(payload["expires_at"]) if payload.get("expires_at") is not None else None,
    )
    response.headers.update(
        tus_upload_headers(payload, request=req, location=str(payload["upload_url"]))
    )
    return CollectionUploadSessionFileUploadOut.model_validate(payload)


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
    payload["upload_url"] = public_tusd_upload_url(
        str(payload["upload_url"]),
        expires_at=str(payload["expires_at"]) if payload.get("expires_at") is not None else None,
    )
    response.headers.update(
        tus_upload_headers(payload, request=request, location=str(payload["upload_url"]))
    )
    return CollectionFileUploadSessionOut.model_validate(payload)


@router.get("/collections/{collection_id:path}", response_model=CollectionSummaryOut)
def get_collection(
    collection_id: str,
    container: ContainerDep,
    coverage_path_limit: int = Query(100, ge=0, le=100),
) -> CollectionSummaryOut:
    summary = container.collections.get(
        collection_id,
        coverage_path_limit=coverage_path_limit,
    )
    return CollectionSummaryOut.model_validate(
        map_collection(summary, coverage_path_limit=coverage_path_limit)
    )
