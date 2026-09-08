from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Body, Header, HTTPException, Query, Request, Response
from http_api_contracts import (
    QuotedSha256Identity,
    mutable_browse_operation,
    operation_interface,
    parse_quoted_sha256_identity,
)
from riverhog_core.app_permissions import COLLECTIONS_DELETE
from riverhog_protocol import (
    COLLECTION_UPLOAD_PROVENANCE_APPEND_BYTES_MAX,
    CollectionIdParameter,
    CollectionSort,
    CollectionUploadProvenanceJournalCreateDocument,
    CollectionUploadRawDigestBatchDocument,
    CollectionUploadRawDigestProgressDocument,
    CollectionUploadSort,
    CollectionUploadState,
    CollectionUploadUnitNumber,
    CollectionUploadVolumeId,
    ProcessingClaimId,
    SortOrder,
)
from riverhog_provenance_contracts import ProvenanceJournalId
from starlette.concurrency import run_in_threadpool

from riverhog_api.auth import (
    CatalogReader,
    CollectionCreator,
    CollectionDeleter,
    CollectionDescriptionManager,
    CollectionUploadReader,
)
from riverhog_api.browse import (
    BrowsePageTokenQuery,
    BrowseQueryParameter,
    canonical_selectors,
    page_payload,
    page_position,
)
from riverhog_api.deps import ContainerDep
from riverhog_api.mappers import map_collection, map_collection_list_page
from riverhog_api.schemas.collections import (
    AddCollectionUploadTagsRequest,
    CollectionArchiveCopyListOut,
    CollectionDeletionPlanOut,
    CollectionDeletionResultOut,
    CollectionDescriptionOut,
    CollectionSummaryOut,
    CollectionTagSelectorBatch,
    CollectionUploadDiscardPlanOut,
    CollectionUploadDiscardResultOut,
    CollectionUploadProvenanceJournalOut,
    CollectionUploadSessionFilesRegistrationOut,
    CollectionUploadSessionOut,
    CollectionUploadTagsOut,
    CollectionUploadUnitOut,
    CollectionUploadWorkBatchOut,
    CreateOrResumeCollectionUploadSessionOut,
    CreateOrResumeCollectionUploadSessionRequest,
    DeleteCollectionRequest,
    DiscardCollectionUploadRequest,
    ListCollectionsResponse,
    ListCollectionUploadSessionFilesResponse,
    ListCollectionUploadSessionsResponse,
    RegisterCollectionUploadSessionFilesRequest,
    ReplaceCollectionDescriptionRequest,
)

router = APIRouter(tags=["collections"])

_CLIENT_BINARY_OPERATION = {
    **operation_interface("client-only-primitive"),
    "parameters": [
        {
            "name": "Content-Length",
            "in": "header",
            "required": True,
            "description": "Exact request-body length in bytes.",
            "schema": {"type": "integer", "minimum": 0},
        }
    ],
}
_CLIENT_PROVENANCE_BINARY_OPERATION = {
    **operation_interface("client-only-primitive"),
    "parameters": [
        {
            "name": "Content-Length",
            "in": "header",
            "required": True,
            "description": "Exact bounded append length in bytes.",
            "schema": {
                "type": "integer",
                "minimum": 1,
                "maximum": COLLECTION_UPLOAD_PROVENANCE_APPEND_BYTES_MAX,
            },
        }
    ],
    "requestBody": {
        "required": True,
        "content": {
            "application/json-seq": {
                "schema": {"type": "string", "format": "binary"},
            }
        },
    },
}


@router.get(
    "/collections",
    response_model=ListCollectionsResponse,
    openapi_extra=mutable_browse_operation(),
)
def list_collections(
    container: ContainerDep,
    principal: CatalogReader,
    page_size: int = Query(25, ge=1, le=100),
    page_token: BrowsePageTokenQuery = None,
    q: BrowseQueryParameter = None,
    sort: Annotated[CollectionSort, Query()] = "id",
    order: Annotated[SortOrder, Query()] = "asc",
    encryption_format: str | None = Query(None),
    passphrase_id: str | None = Query(None),
    tags: Annotated[CollectionTagSelectorBatch | None, Query()] = None,
) -> ListCollectionsResponse:
    selectors = canonical_selectors(
        q=q,
        sort=sort,
        order=order,
        encryption_format=encryption_format,
        passphrase_id=passphrase_id,
        tags=tags or [],
    )
    summary = container.collections.list(
        page_size=page_size,
        position=page_position(
            container,
            principal,
            operation="list_collections",
            page_token=page_token,
            selectors=selectors,
        ),
        q=q,
        encryption_format=encryption_format,
        passphrase_id=passphrase_id,
        tags=tags or [],
        sort=sort,
        order=order,
        principal=principal,
    )
    return ListCollectionsResponse.model_validate(
        page_payload(
            map_collection_list_page(summary),
            container=container,
            principal=principal,
            operation="list_collections",
            selectors=selectors,
        )
    )


@router.get(
    "/collections/{collection_id}/archive-copies",
    response_model=CollectionArchiveCopyListOut,
    openapi_extra=mutable_browse_operation(),
)
def list_collection_archive_copies(
    collection_id: CollectionIdParameter,
    container: ContainerDep,
    principal: CatalogReader,
    page_size: int = Query(25, ge=1, le=100),
    page_token: BrowsePageTokenQuery = None,
) -> CollectionArchiveCopyListOut:
    selectors = canonical_selectors(collection_id=collection_id)
    payload = container.collections.list_archive_copies(
        collection_id,
        page_size=page_size,
        position=page_position(
            container,
            principal,
            operation="list_collection_archive_copies",
            page_token=page_token,
            selectors=selectors,
        ),
        principal=principal,
    )
    return CollectionArchiveCopyListOut.model_validate(
        page_payload(
            payload,
            container=container,
            principal=principal,
            operation="list_collection_archive_copies",
            selectors=selectors,
        )
    )


@router.get(
    "/collection-upload-sessions",
    response_model=ListCollectionUploadSessionsResponse,
    openapi_extra=mutable_browse_operation(),
)
def list_collection_upload_sessions(
    container: ContainerDep,
    principal: CollectionUploadReader,
    page_size: int = Query(25, ge=1, le=100),
    page_token: BrowsePageTokenQuery = None,
    q: BrowseQueryParameter = None,
    state: Annotated[CollectionUploadState | None, Query()] = None,
    sort: Annotated[CollectionUploadSort, Query()] = "created_at",
    order: Annotated[SortOrder, Query()] = "desc",
) -> ListCollectionUploadSessionsResponse:
    selectors = canonical_selectors(q=q, state=state, sort=sort, order=order)
    payload = container.collection_uploads.list(
        page_size=page_size,
        position=page_position(
            container,
            principal,
            operation="list_collection_upload_sessions",
            page_token=page_token,
            selectors=selectors,
        ),
        q=q,
        state=state,
        sort=sort,
        order=order,
        principal=principal,
    )
    return ListCollectionUploadSessionsResponse.model_validate(
        page_payload(
            payload,
            container=container,
            principal=principal,
            operation="list_collection_upload_sessions",
            selectors=selectors,
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
        ingest_source=request.ingest_source,
        description=request.description,
        tags=request.tags,
        archive_store=request.archive_store,
        initiator=principal,
        event_context=request.event_context,
        provenance_mode=request.provenance_mode,
        provenance_omission_reason=request.provenance_omission_reason,
        custody_mode=request.custody_mode,
    )
    return CreateOrResumeCollectionUploadSessionOut.model_validate(payload)


@router.post(
    "/collection-upload-sessions/{collection_id}/tags",
    response_model=CollectionUploadTagsOut,
    openapi_extra=operation_interface("client-only-primitive"),
)
def add_collection_upload_session_tags(
    collection_id: CollectionIdParameter,
    request: AddCollectionUploadTagsRequest,
    container: ContainerDep,
    principal: CollectionCreator,
) -> CollectionUploadTagsOut:
    return CollectionUploadTagsOut.model_validate(
        container.collection_uploads.add_tags(
            collection_id,
            request.tags,
            principal=principal,
        )
    )


@router.post(
    "/collection-upload-sessions/{collection_id}/files",
    response_model=CollectionUploadSessionFilesRegistrationOut,
    openapi_extra=operation_interface("client-only-primitive"),
)
def register_collection_upload_session_files(
    collection_id: CollectionIdParameter,
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


@router.post(
    "/collection-upload-sessions/{collection_id}/raw-part-digests",
    response_model=CollectionUploadRawDigestProgressDocument,
    openapi_extra=operation_interface("client-only-primitive"),
)
def register_collection_upload_session_raw_part_digests(
    collection_id: CollectionIdParameter,
    request: CollectionUploadRawDigestBatchDocument,
    container: ContainerDep,
    principal: CollectionCreator,
) -> CollectionUploadRawDigestProgressDocument:
    container.collection_uploads.require_access(collection_id, principal)
    return CollectionUploadRawDigestProgressDocument.model_validate(
        container.collection_uploads.register_raw_part_digests(collection_id, request)
    )


@router.put(
    "/collection-upload-sessions/{collection_id}/provenance/journals/{journal_id}",
    response_model=CollectionUploadProvenanceJournalOut,
    openapi_extra=operation_interface("client-only-primitive"),
)
def create_collection_upload_session_provenance_journal(
    collection_id: CollectionIdParameter,
    journal_id: ProvenanceJournalId,
    request: CollectionUploadProvenanceJournalCreateDocument,
    container: ContainerDep,
    principal: CollectionCreator,
) -> CollectionUploadProvenanceJournalOut:
    container.collection_uploads.require_access(collection_id, principal)
    return CollectionUploadProvenanceJournalOut.model_validate(
        container.collection_uploads.create_provenance_journal(collection_id, journal_id, request)
    )


@router.patch(
    "/collection-upload-sessions/{collection_id}/provenance/journals/{journal_id}",
    response_model=CollectionUploadProvenanceJournalOut,
    openapi_extra=_CLIENT_PROVENANCE_BINARY_OPERATION,
)
async def append_collection_upload_session_provenance_journal(
    collection_id: CollectionIdParameter,
    journal_id: ProvenanceJournalId,
    request: Request,
    container: ContainerDep,
    principal: CollectionCreator,
    upload_offset: Annotated[int, Header(alias="Upload-Offset", ge=0)],
) -> CollectionUploadProvenanceJournalOut:
    container.collection_uploads.require_access(collection_id, principal)
    declared = request.headers.get("content-length")
    if declared is None or not declared.isdecimal():
        raise HTTPException(status_code=411, detail="Content-Length is required")
    expected = int(declared)
    if expected < 1 or expected > COLLECTION_UPLOAD_PROVENANCE_APPEND_BYTES_MAX:
        raise HTTPException(status_code=413, detail="provenance append exceeds 1 MiB")
    content = bytearray()
    async for chunk in request.stream():
        content.extend(chunk)
        if len(content) > expected:
            raise HTTPException(status_code=400, detail="Content-Length does not match")
    if len(content) != expected:
        raise HTTPException(status_code=400, detail="Content-Length does not match")
    return CollectionUploadProvenanceJournalOut.model_validate(
        container.collection_uploads.append_provenance_journal(
            collection_id,
            journal_id,
            offset=upload_offset,
            content=bytes(content),
        )
    )


@router.post(
    "/collection-upload-sessions/{collection_id}/provenance/journals/{journal_id}/seal",
    response_model=CollectionUploadProvenanceJournalOut,
    openapi_extra=operation_interface("client-only-primitive"),
)
def seal_collection_upload_session_provenance_journal(
    collection_id: CollectionIdParameter,
    journal_id: ProvenanceJournalId,
    container: ContainerDep,
    principal: CollectionCreator,
) -> CollectionUploadProvenanceJournalOut:
    container.collection_uploads.require_access(collection_id, principal)
    return CollectionUploadProvenanceJournalOut.model_validate(
        container.collection_uploads.seal_provenance_journal(collection_id, journal_id)
    )


@router.get(
    "/collection-upload-sessions/{collection_id}/provenance/journals/{journal_id}",
    response_model=CollectionUploadProvenanceJournalOut,
    openapi_extra=operation_interface("client-only-primitive"),
)
def get_collection_upload_session_provenance_journal(
    collection_id: CollectionIdParameter,
    journal_id: ProvenanceJournalId,
    container: ContainerDep,
    principal: CollectionCreator,
) -> CollectionUploadProvenanceJournalOut:
    container.collection_uploads.require_access(collection_id, principal)
    return CollectionUploadProvenanceJournalOut.model_validate(
        container.collection_uploads.get_provenance_journal(collection_id, journal_id)
    )


@router.get(
    "/collection-upload-sessions/{collection_id}/files",
    response_model=ListCollectionUploadSessionFilesResponse,
    openapi_extra=mutable_browse_operation(),
)
def list_collection_upload_session_files(
    collection_id: CollectionIdParameter,
    container: ContainerDep,
    principal: CollectionUploadReader,
    page_size: int = Query(25, ge=1, le=100),
    page_token: BrowsePageTokenQuery = None,
) -> ListCollectionUploadSessionFilesResponse:
    container.collection_uploads.require_read_access(collection_id, principal)
    selectors = canonical_selectors(collection_id=collection_id)
    payload = container.collection_uploads.list_files(
        collection_id,
        page_size=page_size,
        position=page_position(
            container,
            principal,
            operation="list_collection_upload_session_files",
            page_token=page_token,
            selectors=selectors,
        ),
    )
    return ListCollectionUploadSessionFilesResponse.model_validate(
        page_payload(
            payload,
            container=container,
            principal=principal,
            operation="list_collection_upload_session_files",
            selectors=selectors,
        )
    )


@router.post(
    "/collection-upload-sessions/{collection_id}/complete",
    response_model=CollectionUploadSessionOut,
    openapi_extra=operation_interface("client-only-primitive"),
)
def complete_collection_upload_session(
    collection_id: CollectionIdParameter,
    container: ContainerDep,
    principal: CollectionCreator,
) -> CollectionUploadSessionOut:
    container.collection_uploads.require_access(collection_id, principal)
    payload = container.collection_uploads.complete(
        collection_id,
    )
    return CollectionUploadSessionOut.model_validate(payload)


@router.post(
    "/collection-upload-sessions/{collection_id}/cancel",
    response_model=CollectionUploadSessionOut,
)
def cancel_collection_upload_session(
    collection_id: CollectionIdParameter,
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
    collection_id: CollectionIdParameter,
    container: ContainerDep,
    principal: CollectionUploadReader,
) -> CollectionUploadSessionOut:
    container.collection_uploads.require_read_access(collection_id, principal)
    payload = container.collection_uploads.get(collection_id)
    return CollectionUploadSessionOut.model_validate(payload)


@router.post(
    "/collection-upload-sessions/{collection_id}/heartbeat",
    response_model=CollectionUploadSessionOut,
    openapi_extra=operation_interface("client-only-primitive"),
)
def heartbeat_collection_upload_session(
    collection_id: CollectionIdParameter,
    container: ContainerDep,
    principal: CollectionCreator,
) -> CollectionUploadSessionOut:
    container.collection_uploads.require_access(collection_id, principal)
    return CollectionUploadSessionOut.model_validate(
        container.collection_uploads.heartbeat(collection_id)
    )


@router.post(
    "/collection-upload-sessions/{collection_id}/discard-plan",
    response_model=CollectionUploadDiscardPlanOut,
)
def plan_collection_upload_discard(
    collection_id: CollectionIdParameter,
    container: ContainerDep,
    principal: CollectionDeleter,
) -> CollectionUploadDiscardPlanOut:
    container.collection_uploads.require_discard_access(collection_id, principal)
    return CollectionUploadDiscardPlanOut.model_validate(
        container.collection_uploads.plan_orphan_discard(collection_id)
    )


@router.post(
    "/collection-upload-sessions/{collection_id}/discard",
    response_model=CollectionUploadDiscardResultOut,
)
def discard_collection_upload(
    collection_id: CollectionIdParameter,
    request: DiscardCollectionUploadRequest,
    container: ContainerDep,
    principal: CollectionDeleter,
) -> CollectionUploadDiscardResultOut:
    container.collection_uploads.require_discard_access(collection_id, principal)
    return CollectionUploadDiscardResultOut.model_validate(
        container.collection_uploads.discard_orphan(
            collection_id,
            challenge=request.challenge,
        )
    )


@router.get(
    "/collection-upload-sessions/{collection_id}/work",
    response_model=CollectionUploadWorkBatchOut,
    openapi_extra=operation_interface("client-only-primitive"),
)
def acquire_collection_upload_session_work(
    collection_id: CollectionIdParameter,
    container: ContainerDep,
    principal: CollectionCreator,
    limit: Annotated[int, Query(ge=1, le=64)] = 16,
) -> CollectionUploadWorkBatchOut:
    container.collection_uploads.require_access(collection_id, principal)
    return CollectionUploadWorkBatchOut.model_validate(
        container.collection_uploads.acquire_work(collection_id, limit=limit)
    )


@router.get(
    "/collection-upload-sessions/{collection_id}/volumes/{volume_id}/units/{unit}",
    response_model=CollectionUploadUnitOut,
    openapi_extra=operation_interface("client-only-primitive"),
)
def get_collection_upload_session_unit(
    collection_id: CollectionIdParameter,
    volume_id: CollectionUploadVolumeId,
    unit: CollectionUploadUnitNumber,
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
    openapi_extra=_CLIENT_BINARY_OPERATION,
)
async def put_collection_upload_session_unit(
    collection_id: CollectionIdParameter,
    volume_id: CollectionUploadVolumeId,
    unit: CollectionUploadUnitNumber,
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
    if_match: Annotated[QuotedSha256Identity, Header(alias="If-Match")],
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
        plan_sha256=parse_quoted_sha256_identity(if_match),
        content=content,
    )
    return CollectionUploadUnitOut.model_validate(payload)


@router.get("/collections/{collection_id}", response_model=CollectionSummaryOut)
def get_collection(
    collection_id: CollectionIdParameter,
    container: ContainerDep,
    principal: CatalogReader,
) -> CollectionSummaryOut:
    return CollectionSummaryOut.model_validate(
        map_collection(container.collections.get(collection_id, principal=principal))
    )


@router.put(
    "/collections/{collection_id}/description",
    response_model=CollectionDescriptionOut,
)
def replace_collection_description(
    collection_id: CollectionIdParameter,
    request: ReplaceCollectionDescriptionRequest,
    response: Response,
    if_match: Annotated[QuotedSha256Identity, Header(alias="If-Match")],
    container: ContainerDep,
    principal: CollectionDescriptionManager,
) -> CollectionDescriptionOut:
    payload = container.collection_descriptions.replace(
        collection_id,
        description=request.description,
        expected_identity=parse_quoted_sha256_identity(if_match),
        principal=principal,
    )
    result = CollectionDescriptionOut.model_validate(payload)
    response.headers["ETag"] = f'"{result.description_identity}"'
    return result


@router.post(
    "/collections/{collection_id}/deletion-plan",
    response_model=CollectionDeletionPlanOut,
)
def plan_collection_deletion(
    collection_id: CollectionIdParameter,
    container: ContainerDep,
    principal: CollectionDeleter,
    retirement_claim_id: ProcessingClaimId | None = None,
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
    collection_id: CollectionIdParameter,
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
