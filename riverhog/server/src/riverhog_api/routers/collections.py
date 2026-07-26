from __future__ import annotations

from fastapi import APIRouter, Query, Request, Response
from riverhog_core.app_permissions import COLLECTIONS_DELETE

from riverhog_api.auth import CatalogReader, CollectionCreator, CollectionDeleter
from riverhog_api.deps import ContainerDep
from riverhog_api.mappers import map_collection, map_collection_list_page
from riverhog_api.schemas.collections import (
    CollectionDeletionPlanOut,
    CollectionDeletionResultOut,
    CollectionFileUploadSessionOut,
    CollectionSummaryOut,
    CollectionUploadSessionFileRegistrationOut,
    CollectionUploadSessionFileUploadOut,
    CollectionUploadSessionOut,
    CreateOrResumeCollectionUploadRequest,
    CreateOrResumeCollectionUploadSessionRequest,
    DeleteCollectionRequest,
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
    principal: CatalogReader,
    page: int = Query(1, ge=1),
    per_page: int = Query(25, ge=1, le=100),
    q: str | None = Query(None),
    sort: str = Query("id"),
    order: str = Query("asc"),
    all_items: bool = Query(False, alias="all"),
) -> ListCollectionsResponse:
    summary = container.collections.list(
        page=page,
        per_page=per_page,
        q=q,
        sort=sort,
        order=order,
        all_items=all_items,
        principal=principal,
    )
    return ListCollectionsResponse.model_validate(map_collection_list_page(summary))


@router.post("/collection-uploads", response_model=CollectionUploadSessionOut)
def create_or_resume_collection_upload(
    request: CreateOrResumeCollectionUploadRequest,
    container: ContainerDep,
    principal: CollectionCreator,
) -> CollectionUploadSessionOut:
    payload = container.collections.create_or_resume_upload(
        idempotency_key=request.idempotency_key,
        tags=request.tags,
        files=[item.model_dump() for item in request.files],
        ingest_source=request.ingest_source,
        archive_store=request.archive_store,
        initiator=principal,
        event_context=request.event_context,
    )
    return CollectionUploadSessionOut.model_validate(payload)


@router.post("/collection-upload-sessions", response_model=CollectionUploadSessionOut)
def create_or_resume_collection_upload_session(
    request: CreateOrResumeCollectionUploadSessionRequest,
    container: ContainerDep,
    principal: CollectionCreator,
) -> CollectionUploadSessionOut:
    payload = container.collections.create_or_resume_upload_session(
        idempotency_key=request.idempotency_key,
        tags=request.tags,
        ingest_source=request.ingest_source,
        archive_store=request.archive_store,
        initiator=principal,
        event_context=request.event_context,
    )
    return CollectionUploadSessionOut.model_validate(payload)


@router.post(
    "/collection-upload-sessions/{collection_id}/files",
    response_model=CollectionUploadSessionFileRegistrationOut,
)
def register_collection_upload_session_file(
    collection_id: int,
    request: RegisterCollectionUploadSessionFileRequest,
    container: ContainerDep,
    principal: CollectionCreator,
) -> CollectionUploadSessionFileRegistrationOut:
    container.collections.require_upload_access(collection_id, principal)
    payload = container.collections.register_upload_session_file(
        collection_id,
        request.model_dump(),
    )
    return CollectionUploadSessionFileRegistrationOut.model_validate(payload)


@router.post(
    "/collection-upload-sessions/{collection_id}/files/upload",
    response_model=CollectionUploadSessionFileUploadOut,
)
def create_or_resume_registered_collection_file_upload(
    collection_id: int,
    request: RegisterCollectionUploadSessionFileRequest,
    req: Request,
    response: Response,
    container: ContainerDep,
    principal: CollectionCreator,
) -> CollectionUploadSessionFileUploadOut:
    container.collections.require_upload_access(collection_id, principal)
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
    "/collection-upload-sessions/{collection_id}/complete",
    response_model=CollectionUploadSessionOut,
)
def complete_collection_upload_session(
    collection_id: int,
    container: ContainerDep,
    principal: CollectionCreator,
) -> CollectionUploadSessionOut:
    container.collections.require_upload_access(collection_id, principal)
    payload = container.collections.complete_upload_session(collection_id)
    return CollectionUploadSessionOut.model_validate(payload)


@router.post(
    "/collection-upload-sessions/{collection_id}/cancel",
    response_model=CollectionUploadSessionOut,
)
def cancel_collection_upload_session(
    collection_id: int,
    container: ContainerDep,
    principal: CollectionCreator,
) -> CollectionUploadSessionOut:
    container.collections.require_upload_access(collection_id, principal)
    payload = container.collections.cancel_upload_session(collection_id)
    return CollectionUploadSessionOut.model_validate(payload)


@router.get("/collection-uploads/{collection_id}", response_model=CollectionUploadSessionOut)
def get_collection_upload(
    collection_id: int,
    container: ContainerDep,
    principal: CollectionCreator,
) -> CollectionUploadSessionOut:
    container.collections.require_upload_access(collection_id, principal)
    payload = container.collections.get_upload(collection_id)
    return CollectionUploadSessionOut.model_validate(payload)


@router.post(
    "/collection-uploads/{collection_id}/files/{path:path}/upload",
    response_model=CollectionFileUploadSessionOut,
)
def create_or_resume_collection_file_upload(
    collection_id: int,
    path: str,
    request: Request,
    response: Response,
    container: ContainerDep,
    principal: CollectionCreator,
) -> CollectionFileUploadSessionOut:
    container.collections.require_upload_access(collection_id, principal)
    payload = container.collections.create_or_resume_file_upload(collection_id, path)
    payload["upload_url"] = public_tusd_upload_url(
        str(payload["upload_url"]),
        expires_at=str(payload["expires_at"]) if payload.get("expires_at") is not None else None,
    )
    response.headers.update(
        tus_upload_headers(payload, request=request, location=str(payload["upload_url"]))
    )
    return CollectionFileUploadSessionOut.model_validate(payload)


@router.get("/collections/{collection_id}", response_model=CollectionSummaryOut)
def get_collection(
    collection_id: int,
    container: ContainerDep,
    principal: CatalogReader,
) -> CollectionSummaryOut:
    return CollectionSummaryOut.model_validate(
        map_collection(container.collections.get(collection_id, principal=principal))
    )


@router.post(
    "/collections/{collection_id}/deletion-plan",
    response_model=CollectionDeletionPlanOut,
)
def plan_collection_deletion(
    collection_id: int,
    container: ContainerDep,
    principal: CollectionDeleter,
) -> CollectionDeletionPlanOut:
    container.collection_access.require(principal, COLLECTIONS_DELETE, collection_id)
    return CollectionDeletionPlanOut.model_validate(
        container.collection_deletions.plan(collection_id)
    )


@router.post(
    "/collections/{collection_id}/delete",
    response_model=CollectionDeletionResultOut,
)
def delete_collection(
    collection_id: int,
    request: DeleteCollectionRequest,
    container: ContainerDep,
    principal: CollectionDeleter,
) -> CollectionDeletionResultOut:
    container.collection_access.require(principal, COLLECTIONS_DELETE, collection_id)
    return CollectionDeletionResultOut.model_validate(
        container.collection_deletions.delete(
            collection_id,
            challenge=request.challenge,
            initiator=principal,
            event_context=request.event_context,
        )
    )
