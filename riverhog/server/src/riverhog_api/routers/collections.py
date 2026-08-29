from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Body, Header, HTTPException, Query, Request, Response
from http_api_contracts import (
    QuotedSha256Identity,
    Sha256Identity,
    operation_interface,
    parse_quoted_sha256_identity,
)
from riverhog_core.app_permissions import COLLECTIONS_DELETE
from riverhog_protocol import (
    CollectionIdParameter,
    CollectionSort,
    CollectionUploadSort,
    CollectionUploadState,
    CollectionUploadUnitNumber,
    CollectionUploadVolumeId,
    ProcessingClaimId,
    SortOrder,
)
from riverhog_protocol.paths import CanonicalTag
from riverhog_provenance_contracts import ProvenanceJournalId
from starlette.concurrency import run_in_threadpool

from riverhog_api.auth import (
    CatalogReader,
    CollectionCreator,
    CollectionDeleter,
    CollectionUploadReader,
)
from riverhog_api.complete_enumeration import (
    CompleteEnumerationResponse,
    bounded_list_operation,
    complete_enumeration_operation,
    complete_enumeration_response,
)
from riverhog_api.deps import ContainerDep
from riverhog_api.mappers import map_collection, map_collection_list_page
from riverhog_api.schemas.collections import (
    CollectionDeletionPlanOut,
    CollectionDeletionResultOut,
    CollectionSummaryOut,
    CollectionUploadDiscardPlanOut,
    CollectionUploadDiscardResultOut,
    CollectionUploadFileOut,
    CollectionUploadListItemOut,
    CollectionUploadProvenanceJournalOut,
    CollectionUploadSessionFilesRegistrationOut,
    CollectionUploadSessionOut,
    CollectionUploadUnitOut,
    CollectionUploadVolumeOut,
    CompleteCollectionUploadSessionRequest,
    CreateOrResumeCollectionUploadSessionOut,
    CreateOrResumeCollectionUploadSessionRequest,
    DeleteCollectionRequest,
    DiscardCollectionUploadRequest,
    ListCollectionsResponse,
    ListCollectionUploadSessionFilesResponse,
    ListCollectionUploadSessionsResponse,
    ListCollectionUploadVolumesResponse,
    RegisterCollectionUploadSessionFilesRequest,
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
_PROVENANCE_JOURNAL_RESPONSE: dict[int | str, dict[str, Any]] = {
    200: {
        "description": "Exact staged provenance journal.",
        "headers": {
            "Content-Length": {
                "required": True,
                "description": "Exact response-body length in bytes.",
                "schema": {"type": "integer", "minimum": 0},
            },
            "ETag": {
                "required": True,
                "description": "Quoted SHA-256 identity of the journal bytes.",
                "schema": {"type": "string", "pattern": '^"[0-9a-f]{64}"$'},
            },
        },
        "content": {
            "application/json-seq": {
                "schema": {"type": "string", "format": "binary"},
            }
        },
    }
}


@router.get(
    "/collections",
    response_model=ListCollectionsResponse,
    openapi_extra=bounded_list_operation(paired_operation_id="stream_collections"),
)
def list_collections(
    container: ContainerDep,
    principal: CatalogReader,
    page: int = Query(1, ge=1),
    per_page: int = Query(25, ge=1, le=100),
    q: str | None = Query(None),
    sort: Annotated[CollectionSort, Query()] = "id",
    order: Annotated[SortOrder, Query()] = "asc",
    tag: Annotated[CanonicalTag | None, Query()] = None,
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
        principal=principal,
    )
    return ListCollectionsResponse.model_validate(map_collection_list_page(summary))


@router.get(
    "/collections/stream",
    response_class=CompleteEnumerationResponse,
    openapi_extra=complete_enumeration_operation(
        paired_operation_id="list_collections",
        item_type=CollectionSummaryOut,
        schema_id="riverhog.collection-summary/v1",
    ),
)
def stream_collections(
    container: ContainerDep,
    principal: CatalogReader,
    q: str | None = Query(None),
    sort: Annotated[CollectionSort, Query()] = "id",
    order: Annotated[SortOrder, Query()] = "asc",
    tag: Annotated[CanonicalTag | None, Query()] = None,
    encryption_format: str | None = Query(None),
    passphrase_id: str | None = Query(None),
) -> Response:
    query = {
        "q": q,
        "sort": sort,
        "order": order,
        "tag": tag,
        "encryption_format": encryption_format,
        "passphrase_id": passphrase_id,
    }
    summaries = container.collections.iter_collections(
        q=q,
        tag=tag,
        encryption_format=encryption_format,
        passphrase_id=passphrase_id,
        sort=sort,
        order=order,
        principal=principal,
    )
    items = (CollectionSummaryOut.model_validate(map_collection(item)) for item in summaries)
    return complete_enumeration_response(
        items,
        query=query,
        item_type=CollectionSummaryOut,
        schema_id="riverhog.collection-summary/v1",
    )


@router.get(
    "/collection-upload-sessions",
    response_model=ListCollectionUploadSessionsResponse,
    openapi_extra=bounded_list_operation(paired_operation_id="stream_collection_upload_sessions"),
)
def list_collection_upload_sessions(
    container: ContainerDep,
    principal: CollectionUploadReader,
    page: int = Query(1, ge=1),
    per_page: int = Query(25, ge=1, le=100),
    q: str | None = Query(None),
    tag: Annotated[CanonicalTag | None, Query()] = None,
    state: Annotated[CollectionUploadState | None, Query()] = None,
    sort: Annotated[CollectionUploadSort, Query()] = "created_at",
    order: Annotated[SortOrder, Query()] = "desc",
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
            principal=principal,
        )
    )


@router.get(
    "/collection-upload-sessions/stream",
    response_class=CompleteEnumerationResponse,
    openapi_extra=complete_enumeration_operation(
        paired_operation_id="list_collection_upload_sessions",
        item_type=CollectionUploadListItemOut,
        schema_id="riverhog.collection-upload-session-summary/v1",
    ),
)
def stream_collection_upload_sessions(
    container: ContainerDep,
    principal: CollectionUploadReader,
    q: str | None = Query(None),
    tag: Annotated[CanonicalTag | None, Query()] = None,
    state: Annotated[CollectionUploadState | None, Query()] = None,
    sort: Annotated[CollectionUploadSort, Query()] = "created_at",
    order: Annotated[SortOrder, Query()] = "desc",
) -> Response:
    query = {"q": q, "tag": tag, "state": state, "sort": sort, "order": order}
    items = container.collection_uploads.iter_uploads(
        q=q,
        tag=tag,
        state=state,
        sort=sort,
        order=order,
        principal=principal,
    )
    return complete_enumeration_response(
        items,
        query=query,
        item_type=CollectionUploadListItemOut,
        schema_id="riverhog.collection-upload-session-summary/v1",
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
        custody_mode=request.custody_mode,
    )
    return CreateOrResumeCollectionUploadSessionOut.model_validate(payload)


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


@router.put(
    "/collection-upload-sessions/{collection_id}/provenance/journals/{journal_id}",
    response_model=CollectionUploadProvenanceJournalOut,
    openapi_extra=_CLIENT_BINARY_OPERATION,
)
async def put_collection_upload_session_provenance_journal(
    collection_id: CollectionIdParameter,
    journal_id: ProvenanceJournalId,
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
    provenance_sha256: Annotated[
        Sha256Identity,
        Header(alias="X-Riverhog-Provenance-SHA256"),
    ],
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
    response_class=Response,
    responses=_PROVENANCE_JOURNAL_RESPONSE,
    openapi_extra=operation_interface("client-only-primitive"),
)
def export_collection_upload_session_provenance_journal(
    collection_id: CollectionIdParameter,
    journal_id: ProvenanceJournalId,
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
    openapi_extra=bounded_list_operation(
        paired_operation_id="stream_collection_upload_session_files"
    ),
)
def list_collection_upload_session_files(
    collection_id: CollectionIdParameter,
    container: ContainerDep,
    principal: CollectionUploadReader,
    page: int = Query(1, ge=1),
    per_page: int = Query(25, ge=1, le=100),
) -> ListCollectionUploadSessionFilesResponse:
    container.collection_uploads.require_read_access(collection_id, principal)
    payload = container.collection_uploads.list_files(
        collection_id,
        page=page,
        per_page=per_page,
    )
    return ListCollectionUploadSessionFilesResponse.model_validate(payload)


@router.get(
    "/collection-upload-sessions/{collection_id}/files/stream",
    response_class=CompleteEnumerationResponse,
    openapi_extra=complete_enumeration_operation(
        paired_operation_id="list_collection_upload_session_files",
        item_type=CollectionUploadFileOut,
        schema_id="riverhog.collection-upload-file/v1",
    ),
)
def stream_collection_upload_session_files(
    collection_id: CollectionIdParameter,
    container: ContainerDep,
    principal: CollectionUploadReader,
) -> Response:
    container.collection_uploads.require_read_access(collection_id, principal)
    return complete_enumeration_response(
        container.collection_uploads.iter_files(collection_id),
        query={"collection_id": collection_id},
        item_type=CollectionUploadFileOut,
        schema_id="riverhog.collection-upload-file/v1",
    )


@router.post(
    "/collection-upload-sessions/{collection_id}/complete",
    response_model=CollectionUploadSessionOut,
    openapi_extra=operation_interface("client-only-primitive"),
)
def complete_collection_upload_session(
    collection_id: CollectionIdParameter,
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
    "/collection-upload-sessions/{collection_id}/volumes",
    response_model=ListCollectionUploadVolumesResponse,
    openapi_extra=operation_interface("client-only-primitive"),
)
def list_collection_upload_session_volumes(
    collection_id: CollectionIdParameter,
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
    collection_id: CollectionIdParameter,
    volume_id: CollectionUploadVolumeId,
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
