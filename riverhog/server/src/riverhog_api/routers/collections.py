from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Body, Header, HTTPException, Query, Request, Response
from http_api_contracts import operation_interface
from riverhog_core.app_permissions import COLLECTIONS_DELETE
from starlette.concurrency import run_in_threadpool

from riverhog_api.auth import CatalogReader, CollectionCreator, CollectionDeleter
from riverhog_api.deps import ContainerDep
from riverhog_api.mappers import map_collection, map_collection_list_page
from riverhog_api.schemas.collections import (
    CollectionDeletionPlanOut,
    CollectionDeletionResultOut,
    CollectionSummaryOut,
    CollectionUploadProvenanceJournalOut,
    CollectionUploadSessionFilesRegistrationOut,
    CollectionUploadSessionOut,
    CollectionUploadUnitOut,
    CollectionUploadVolumeOut,
    CompleteCollectionUploadSessionRequest,
    CreateOrResumeCollectionUploadSessionOut,
    CreateOrResumeCollectionUploadSessionRequest,
    DeleteCollectionRequest,
    ListCollectionsResponse,
    ListCollectionUploadSessionFilesResponse,
    ListCollectionUploadSessionsResponse,
    ListCollectionUploadVolumesResponse,
    RegisterCollectionUploadSessionFilesRequest,
)

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
    tag: str | None = Query(None),
    encryption_format: str | None = Query(None),
    passphrase_id: str | None = Query(None),
) -> ListCollectionsResponse:
    summary = container.collections.list(
        page=page,
        per_page=per_page,
        q=q,
        tag=tag,
        encryption_format=encryption_format,
        passphrase_id=passphrase_id,
        sort=sort,
        order=order,
        all_items=all_items,
        principal=principal,
    )
    return ListCollectionsResponse.model_validate(map_collection_list_page(summary))


@router.get(
    "/collection-upload-sessions",
    response_model=ListCollectionUploadSessionsResponse,
)
def list_collection_upload_sessions(
    container: ContainerDep,
    principal: CollectionCreator,
    page: int = Query(1, ge=1),
    per_page: int = Query(25, ge=1, le=100),
    q: str | None = Query(None),
    tag: str | None = Query(None),
    state: str | None = Query(None),
    sort: str = Query("created_at"),
    order: str = Query("desc"),
    all_items: bool = Query(False, alias="all"),
) -> ListCollectionUploadSessionsResponse:
    return ListCollectionUploadSessionsResponse.model_validate(
        container.collection_uploads.list(
            page=page,
            per_page=per_page,
            q=q,
            tag=tag,
            state=state,
            sort=sort,
            order=order,
            all_items=all_items,
            principal=principal,
        )
    )


@router.post(
    "/collection-upload-sessions",
    response_model=CreateOrResumeCollectionUploadSessionOut,
    openapi_extra=operation_interface("client-only-primitive"),
)
def create_or_resume_collection_upload_session(
    request: CreateOrResumeCollectionUploadSessionRequest,
    container: ContainerDep,
    principal: CollectionCreator,
) -> CreateOrResumeCollectionUploadSessionOut:
    payload = container.collection_uploads.create_or_resume(
        idempotency_key=request.idempotency_key,
        tags=request.tags,
        ingest_source=request.ingest_source,
        archive_store=request.archive_store,
        initiator=principal,
        event_context=request.event_context,
        provenance_mode=request.provenance_mode,
        provenance_omission_reason=request.provenance_omission_reason,
    )
    return CreateOrResumeCollectionUploadSessionOut.model_validate(payload)


@router.post(
    "/collection-upload-sessions/{collection_id}/files",
    response_model=CollectionUploadSessionFilesRegistrationOut,
    openapi_extra=operation_interface("client-only-primitive"),
)
def register_collection_upload_session_files(
    collection_id: int,
    request: RegisterCollectionUploadSessionFilesRequest,
    container: ContainerDep,
    principal: CollectionCreator,
) -> CollectionUploadSessionFilesRegistrationOut:
    container.collection_uploads.require_access(collection_id, principal)
    payload = container.collection_uploads.register_files(
        collection_id,
        [item.model_dump() for item in request.files],
    )
    return CollectionUploadSessionFilesRegistrationOut.model_validate(payload)


@router.put(
    "/collection-upload-sessions/{collection_id}/provenance/journals/{journal_id}",
    response_model=CollectionUploadProvenanceJournalOut,
    openapi_extra=operation_interface("client-only-primitive"),
)
async def put_collection_upload_session_provenance_journal(
    collection_id: int,
    journal_id: str,
    request: Request,
    content: Annotated[
        bytes,
        Body(
            media_type="application/json-seq",
            json_schema_extra={"format": "binary"},
        ),
    ],
    container: ContainerDep,
    principal: CollectionCreator,
    provenance_sha256: str = Header(alias="X-Riverhog-Provenance-SHA256"),
) -> CollectionUploadProvenanceJournalOut:
    container.collection_uploads.require_access(collection_id, principal)
    declared = request.headers.get("content-length")
    if declared is None or not declared.isdecimal():
        raise HTTPException(status_code=411, detail="Content-Length is required")
    if int(declared) != len(content):
        raise HTTPException(status_code=400, detail="Content-Length does not match the journal")
    payload = await run_in_threadpool(
        container.collection_uploads.put_provenance_journal,
        collection_id,
        journal_id,
        content=content,
        sha256=provenance_sha256,
    )
    return CollectionUploadProvenanceJournalOut.model_validate(payload)


@router.get(
    "/collection-upload-sessions/{collection_id}/provenance/journals/{journal_id}",
    openapi_extra=operation_interface("client-only-primitive"),
)
def export_collection_upload_session_provenance_journal(
    collection_id: int,
    journal_id: str,
    container: ContainerDep,
    principal: CollectionCreator,
) -> Response:
    container.collection_uploads.require_access(collection_id, principal)
    content, sha256 = container.collection_uploads.export_provenance_journal(
        collection_id,
        journal_id,
    )
    return Response(
        content=content,
        media_type="application/json-seq",
        headers={
            "Content-Length": str(len(content)),
            "ETag": f'"{sha256}"',
        },
    )


@router.get(
    "/collection-upload-sessions/{collection_id}/files",
    response_model=ListCollectionUploadSessionFilesResponse,
    openapi_extra=operation_interface("client-only-primitive"),
)
def list_collection_upload_session_files(
    collection_id: int,
    container: ContainerDep,
    principal: CollectionCreator,
    page: int = Query(1, ge=1),
    per_page: int = Query(25, ge=1, le=100),
    all_items: bool = Query(False, alias="all"),
) -> ListCollectionUploadSessionFilesResponse:
    container.collection_uploads.require_access(collection_id, principal)
    payload = container.collection_uploads.list_files(
        collection_id,
        page=page,
        per_page=per_page,
        all_items=all_items,
    )
    return ListCollectionUploadSessionFilesResponse.model_validate(payload)


@router.post(
    "/collection-upload-sessions/{collection_id}/complete",
    response_model=CollectionUploadSessionOut,
    openapi_extra=operation_interface("client-only-primitive"),
)
def complete_collection_upload_session(
    collection_id: int,
    request: CompleteCollectionUploadSessionRequest,
    container: ContainerDep,
    principal: CollectionCreator,
) -> CollectionUploadSessionOut:
    container.collection_uploads.require_access(collection_id, principal)
    payload = container.collection_uploads.complete(
        collection_id,
        files_total=request.files_total,
        content_identity=request.content_identity,
        provenance_identity=request.provenance_identity,
    )
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
    container.collection_uploads.require_access(collection_id, principal)
    payload = container.collection_uploads.cancel(collection_id)
    return CollectionUploadSessionOut.model_validate(payload)


@router.get(
    "/collection-upload-sessions/{collection_id}", response_model=CollectionUploadSessionOut
)
def get_collection_upload_session(
    collection_id: int,
    container: ContainerDep,
    principal: CollectionCreator,
) -> CollectionUploadSessionOut:
    container.collection_uploads.require_access(collection_id, principal)
    payload = container.collection_uploads.get(collection_id)
    return CollectionUploadSessionOut.model_validate(payload)


@router.get(
    "/collection-upload-sessions/{collection_id}/volumes",
    response_model=ListCollectionUploadVolumesResponse,
    openapi_extra=operation_interface("client-only-primitive"),
)
def list_collection_upload_session_volumes(
    collection_id: int,
    container: ContainerDep,
    principal: CollectionCreator,
) -> ListCollectionUploadVolumesResponse:
    container.collection_uploads.require_access(collection_id, principal)
    return ListCollectionUploadVolumesResponse.model_validate(
        container.collection_uploads.list_volumes(collection_id)
    )


@router.get(
    "/collection-upload-sessions/{collection_id}/volumes/{volume_id}",
    response_model=CollectionUploadVolumeOut,
    openapi_extra=operation_interface("client-only-primitive"),
)
def get_collection_upload_session_volume(
    collection_id: int,
    volume_id: str,
    container: ContainerDep,
    principal: CollectionCreator,
) -> CollectionUploadVolumeOut:
    container.collection_uploads.require_access(collection_id, principal)
    return CollectionUploadVolumeOut.model_validate(
        container.collection_uploads.get_volume(collection_id, volume_id)
    )


@router.get(
    "/collection-upload-sessions/{collection_id}/volumes/{volume_id}/units/{unit}",
    response_model=CollectionUploadUnitOut,
    openapi_extra=operation_interface("client-only-primitive"),
)
def get_collection_upload_session_unit(
    collection_id: int,
    volume_id: str,
    unit: int,
    container: ContainerDep,
    principal: CollectionCreator,
) -> CollectionUploadUnitOut:
    container.collection_uploads.require_access(collection_id, principal)
    return CollectionUploadUnitOut.model_validate(
        container.collection_uploads.get_unit(collection_id, volume_id, unit)
    )


@router.put(
    "/collection-upload-sessions/{collection_id}/volumes/{volume_id}/units/{unit}",
    response_model=CollectionUploadUnitOut,
    openapi_extra=operation_interface("client-only-primitive"),
)
async def put_collection_upload_session_unit(
    collection_id: int,
    volume_id: str,
    unit: int,
    request: Request,
    content: Annotated[
        bytes,
        Body(
            media_type="application/octet-stream",
            json_schema_extra={"format": "binary"},
        ),
    ],
    container: ContainerDep,
    principal: CollectionCreator,
    if_match: str = Header(alias="If-Match"),
) -> CollectionUploadUnitOut:
    container.collection_uploads.require_access(collection_id, principal)
    work = container.collection_uploads.get_unit(collection_id, volume_id, unit)
    declared = request.headers.get("content-length")
    if declared is None or not declared.isdecimal():
        raise HTTPException(status_code=411, detail="Content-Length is required")
    expected_bytes = work.get("payload_bytes")
    if isinstance(expected_bytes, bool) or not isinstance(expected_bytes, int):
        raise RuntimeError("collection upload unit payload size is invalid")
    if int(declared) != expected_bytes:
        raise HTTPException(status_code=400, detail="Content-Length does not match the upload unit")
    payload = await run_in_threadpool(
        container.collection_uploads.upload_unit,
        collection_id,
        volume_id,
        unit,
        plan_sha256=if_match.strip('"'),
        content=content,
    )
    return CollectionUploadUnitOut.model_validate(payload)


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
    retirement_claim_id: str | None = None,
) -> CollectionDeletionPlanOut:
    container.collection_access.require(principal, COLLECTIONS_DELETE, collection_id)
    return CollectionDeletionPlanOut.model_validate(
        container.collection_deletions.plan(
            collection_id,
            principal=principal,
            retirement_claim_id=retirement_claim_id,
        )
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
            retirement_claim_id=request.retirement_claim_id,
        )
    )
