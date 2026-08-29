from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Query, Response
from riverhog_protocol import CollectionIdParameter, ProvenanceSort, ProvenanceStatus, SortOrder
from riverhog_protocol.paths import CanonicalRelPath
from riverhog_provenance_contracts import ProvenanceJournalId

from riverhog_api.auth import ProvenanceExporter, ProvenanceReader
from riverhog_api.complete_enumeration import (
    CompleteEnumerationResponse,
    bounded_list_operation,
    complete_enumeration_operation,
    complete_enumeration_response,
)
from riverhog_api.deps import ContainerDep
from riverhog_api.schemas.provenance import (
    CollectionFileProvenanceDetailOut,
    CollectionFileProvenanceOut,
    CollectionFileProvenanceTraceOut,
    CollectionProvenanceVerificationOut,
    ListCollectionFileProvenanceResponse,
    ProvenanceTraceItemOut,
)

router = APIRouter(tags=["provenance"])


@router.get(
    "/collections/{collection_id}/provenance/files",
    response_model=ListCollectionFileProvenanceResponse,
    openapi_extra=bounded_list_operation(paired_operation_id="stream_collection_provenance"),
)
def list_collection_provenance(
    collection_id: CollectionIdParameter,
    principal: ProvenanceReader,
    container: ContainerDep,
    page: Annotated[int, Query(ge=1)] = 1,
    per_page: Annotated[int, Query(ge=1, le=100)] = 25,
    q: str | None = None,
    status: ProvenanceStatus | None = None,
    sort: ProvenanceSort = "path",
    order: SortOrder = "asc",
) -> dict[str, Any]:
    return container.provenance.list_files(
        collection_id,
        page=page,
        per_page=per_page,
        q=q,
        status=status,
        sort=sort,
        order=order,
        principal=principal,
    )


@router.get(
    "/collections/{collection_id}/provenance/files/stream",
    response_class=CompleteEnumerationResponse,
    openapi_extra=complete_enumeration_operation(
        paired_operation_id="list_collection_provenance",
        item_type=CollectionFileProvenanceOut,
        schema_id="riverhog.collection-file-provenance/v1",
    ),
)
def stream_collection_provenance(
    collection_id: CollectionIdParameter,
    principal: ProvenanceReader,
    container: ContainerDep,
    q: str | None = None,
    status: ProvenanceStatus | None = None,
    sort: ProvenanceSort = "path",
    order: SortOrder = "asc",
) -> Response:
    query = {
        "collection_id": collection_id,
        "q": q,
        "status": status,
        "sort": sort,
        "order": order,
    }
    return complete_enumeration_response(
        container.provenance.iter_files(
            collection_id,
            q=q,
            status=status,
            sort=sort,
            order=order,
            principal=principal,
        ),
        query=query,
        item_type=CollectionFileProvenanceOut,
        schema_id="riverhog.collection-file-provenance/v1",
    )


@router.get(
    "/collections/{collection_id}/provenance/files/{path:path}",
    response_model=CollectionFileProvenanceDetailOut,
    response_model_exclude_unset=True,
)
def get_collection_file_provenance(
    collection_id: CollectionIdParameter,
    path: CanonicalRelPath,
    principal: ProvenanceReader,
    container: ContainerDep,
) -> dict[str, Any]:
    return container.provenance.show_file(collection_id, path, principal=principal)


@router.get(
    "/collections/{collection_id}/provenance/trace/{path:path}/stream",
    response_class=CompleteEnumerationResponse,
    openapi_extra=complete_enumeration_operation(
        paired_operation_id="trace_collection_file_provenance",
        item_type=ProvenanceTraceItemOut,
        schema_id="riverhog.provenance-trace-item/v1",
    ),
)
def stream_collection_file_provenance_trace(
    collection_id: CollectionIdParameter,
    path: CanonicalRelPath,
    principal: ProvenanceReader,
    container: ContainerDep,
) -> Response:
    query = {"collection_id": collection_id, "path": path}
    return complete_enumeration_response(
        container.provenance.iter_trace_file(collection_id, path, principal=principal),
        query=query,
        item_type=ProvenanceTraceItemOut,
        schema_id="riverhog.provenance-trace-item/v1",
    )


@router.get(
    "/collections/{collection_id}/provenance/trace/{path:path}",
    response_model=CollectionFileProvenanceTraceOut,
    response_model_exclude_unset=True,
    openapi_extra=bounded_list_operation(
        paired_operation_id="stream_collection_file_provenance_trace"
    ),
)
def trace_collection_file_provenance(
    collection_id: CollectionIdParameter,
    path: CanonicalRelPath,
    principal: ProvenanceReader,
    container: ContainerDep,
    page: Annotated[int, Query(ge=1)] = 1,
    per_page: Annotated[int, Query(ge=1, le=100)] = 25,
) -> dict[str, Any]:
    return container.provenance.trace_file(
        collection_id,
        path,
        page=page,
        per_page=per_page,
        principal=principal,
    )


@router.get("/collections/{collection_id}/provenance/journals/{journal_id}")
def export_collection_provenance_journal(
    collection_id: CollectionIdParameter,
    journal_id: ProvenanceJournalId,
    principal: ProvenanceExporter,
    container: ContainerDep,
) -> Response:
    content, sha256 = container.provenance.export_journal(
        collection_id,
        journal_id,
        principal=principal,
    )
    return Response(
        content=content,
        media_type="application/json-seq",
        headers={
            "Content-Length": str(len(content)),
            "ETag": f'"{sha256}"',
            "Content-Disposition": f'attachment; filename="{journal_id}.json-seq"',
        },
    )


@router.post(
    "/collections/{collection_id}/provenance/verify",
    response_model=CollectionProvenanceVerificationOut,
)
def verify_collection_provenance(
    collection_id: CollectionIdParameter,
    principal: ProvenanceReader,
    container: ContainerDep,
) -> dict[str, Any]:
    return container.provenance.verify(collection_id, principal=principal)
