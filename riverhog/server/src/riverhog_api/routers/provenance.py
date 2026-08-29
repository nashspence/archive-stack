from __future__ import annotations

import tempfile
from collections.abc import Iterator
from typing import Annotated, Any

from fastapi import APIRouter, Query, Response
from http_api_contracts import operation_interface
from riverhog_protocol import CollectionIdParameter, ProvenanceSort, ProvenanceStatus, SortOrder
from riverhog_protocol.paths import CanonicalRelPath
from riverhog_provenance_contracts import ProvenanceJournalId
from starlette.responses import StreamingResponse

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
    CollectionProvenanceVerificationJobOut,
    ListCollectionFileProvenanceResponse,
    ListProvenanceJournalAgentsResponse,
    ProvenanceJournalAgentOut,
    ProvenanceTraceItemOut,
)

router = APIRouter(tags=["provenance"])

_PROVENANCE_JOURNAL_RESPONSE: dict[int | str, dict[str, Any]] = {
    200: {
        "description": "Exact immutable provenance journal.",
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


@router.get(
    "/collections/{collection_id}/provenance/journals/{journal_id}",
    response_class=Response,
    responses=_PROVENANCE_JOURNAL_RESPONSE,
    openapi_extra=operation_interface("client-only-primitive"),
)
def stream_collection_provenance_journal(
    collection_id: CollectionIdParameter,
    journal_id: ProvenanceJournalId,
    principal: ProvenanceExporter,
    container: ContainerDep,
) -> Response:
    byte_count, sha256 = container.provenance.journal_metadata(
        collection_id,
        journal_id,
        principal=principal,
    )

    def content() -> Iterator[bytes]:
        with tempfile.TemporaryFile(mode="w+b") as snapshot:
            for chunk in container.provenance.iter_journal(
                collection_id,
                journal_id,
                principal=principal,
            ):
                snapshot.write(chunk)
            snapshot.seek(0)
            while chunk := snapshot.read(1024 * 1024):
                yield chunk

    return StreamingResponse(
        content(),
        media_type="application/json-seq",
        headers={
            "Content-Length": str(byte_count),
            "ETag": f'"{sha256}"',
            "Content-Disposition": f'attachment; filename="{journal_id}.json-seq"',
        },
    )


@router.get(
    "/collections/{collection_id}/provenance/journals/{journal_id}/agents/stream",
    response_class=CompleteEnumerationResponse,
    openapi_extra=complete_enumeration_operation(
        paired_operation_id="list_collection_provenance_journal_agents",
        item_type=ProvenanceJournalAgentOut,
        schema_id="riverhog.provenance-journal-agent/v1",
    ),
)
def stream_collection_provenance_journal_agents(
    collection_id: CollectionIdParameter,
    journal_id: ProvenanceJournalId,
    principal: ProvenanceReader,
    container: ContainerDep,
) -> Response:
    query = {"collection_id": collection_id, "journal_id": journal_id}
    return complete_enumeration_response(
        container.provenance.iter_journal_agents(
            collection_id,
            journal_id,
            principal=principal,
        ),
        query=query,
        item_type=ProvenanceJournalAgentOut,
        schema_id="riverhog.provenance-journal-agent/v1",
    )


@router.get(
    "/collections/{collection_id}/provenance/journals/{journal_id}/agents",
    response_model=ListProvenanceJournalAgentsResponse,
    openapi_extra=bounded_list_operation(
        paired_operation_id="stream_collection_provenance_journal_agents"
    ),
)
def list_collection_provenance_journal_agents(
    collection_id: CollectionIdParameter,
    journal_id: ProvenanceJournalId,
    principal: ProvenanceReader,
    container: ContainerDep,
    page: Annotated[int, Query(ge=1)] = 1,
    per_page: Annotated[int, Query(ge=1, le=100)] = 25,
) -> dict[str, Any]:
    return container.provenance.list_journal_agents(
        collection_id,
        journal_id,
        page=page,
        per_page=per_page,
        principal=principal,
    )


@router.post(
    "/collections/{collection_id}/provenance/verification",
    response_model=CollectionProvenanceVerificationJobOut,
)
def request_collection_provenance_verification(
    collection_id: CollectionIdParameter,
    principal: ProvenanceReader,
    container: ContainerDep,
) -> dict[str, Any]:
    return container.provenance.request_verification(collection_id, principal=principal)


@router.get(
    "/collections/{collection_id}/provenance/verification",
    response_model=CollectionProvenanceVerificationJobOut,
)
def get_collection_provenance_verification(
    collection_id: CollectionIdParameter,
    principal: ProvenanceReader,
    container: ContainerDep,
) -> dict[str, Any]:
    return container.provenance.get_verification(collection_id, principal=principal)


@router.delete(
    "/collections/{collection_id}/provenance/verification",
    response_model=CollectionProvenanceVerificationJobOut,
)
def cancel_collection_provenance_verification(
    collection_id: CollectionIdParameter,
    principal: ProvenanceReader,
    container: ContainerDep,
) -> dict[str, Any]:
    return container.provenance.cancel_verification(collection_id, principal=principal)
