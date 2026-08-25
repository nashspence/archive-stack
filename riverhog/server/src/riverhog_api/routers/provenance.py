from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Query, Response
from riverhog_protocol import ProvenanceSort, ProvenanceStatus, SortOrder

from riverhog_api.auth import ProvenanceExporter, ProvenanceReader
from riverhog_api.deps import ContainerDep
from riverhog_api.schemas.provenance import (
    CollectionFileProvenanceDetailOut,
    CollectionFileProvenanceTraceOut,
    CollectionProvenanceVerificationOut,
    ListCollectionFileProvenanceResponse,
)

router = APIRouter(tags=["provenance"])


@router.get(
    "/collections/{collection_id}/provenance/files",
    response_model=ListCollectionFileProvenanceResponse,
)
def list_collection_provenance(
    collection_id: int,
    principal: ProvenanceReader,
    container: ContainerDep,
    page: Annotated[int, Query(ge=1)] = 1,
    per_page: Annotated[int, Query(ge=1, le=100)] = 25,
    q: str | None = None,
    status: ProvenanceStatus | None = None,
    sort: ProvenanceSort = "path",
    order: SortOrder = "asc",
    all_items: Annotated[bool, Query(alias="all")] = False,
) -> dict[str, Any]:
    return container.provenance.list_files(
        collection_id,
        page=page,
        per_page=per_page,
        q=q,
        status=status,
        sort=sort,
        order=order,
        all_items=all_items,
        principal=principal,
    )


@router.get(
    "/collections/{collection_id}/provenance/files/{path:path}",
    response_model=CollectionFileProvenanceDetailOut,
    response_model_exclude_unset=True,
)
def get_collection_file_provenance(
    collection_id: int,
    path: str,
    principal: ProvenanceReader,
    container: ContainerDep,
) -> dict[str, Any]:
    return container.provenance.show_file(collection_id, path, principal=principal)


@router.get(
    "/collections/{collection_id}/provenance/trace/{path:path}",
    response_model=CollectionFileProvenanceTraceOut,
    response_model_exclude_unset=True,
)
def trace_collection_file_provenance(
    collection_id: int,
    path: str,
    principal: ProvenanceReader,
    container: ContainerDep,
) -> dict[str, Any]:
    return container.provenance.trace_file(collection_id, path, principal=principal)


@router.get("/collections/{collection_id}/provenance/journals/{journal_id}")
def export_collection_provenance_journal(
    collection_id: int,
    journal_id: str,
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
    collection_id: int,
    principal: ProvenanceReader,
    container: ContainerDep,
) -> dict[str, Any]:
    return container.provenance.verify(collection_id, principal=principal)
