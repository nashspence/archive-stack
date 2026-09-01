from __future__ import annotations

from collections.abc import Iterator
from typing import Annotated, Any

from fastapi import APIRouter, Header, Query, Request, Response
from http_api_contracts import (
    mutable_browse_operation,
    operation_interface,
    parse_quoted_sha256_identity,
)
from riverhog_protocol import CollectionIdParameter, ProvenanceSort, ProvenanceStatus, SortOrder
from riverhog_protocol.errors import BadRequest, PreconditionFailed, PreconditionRequired
from riverhog_protocol.paths import CanonicalRelPath
from riverhog_provenance_contracts import ProvenanceJournalId
from starlette.responses import StreamingResponse

from riverhog_api.auth import ProvenanceExporter, ProvenanceReader
from riverhog_api.browse import (
    BrowsePageTokenQuery,
    BrowseQueryParameter,
    canonical_selectors,
    page_payload,
    page_position,
)
from riverhog_api.deps import ContainerDep
from riverhog_api.schemas.provenance import (
    CollectionFileProvenanceDetailOut,
    CollectionFileProvenanceTraceOut,
    CollectionProvenanceVerificationJobOut,
    ListCollectionFileProvenanceResponse,
    ListProvenanceJournalAgentsResponse,
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
            "Accept-Ranges": {
                "required": True,
                "schema": {"type": "string", "const": "bytes"},
            },
        },
        "content": {
            "application/json-seq": {
                "schema": {"type": "string", "format": "binary"},
            }
        },
    },
    206: {
        "description": "Exact immutable provenance journal byte range.",
        "headers": {
            "Content-Length": {"required": True, "schema": {"type": "integer"}},
            "Content-Range": {"required": True, "schema": {"type": "string"}},
            "ETag": {"required": True, "schema": {"type": "string"}},
            "Accept-Ranges": {
                "required": True,
                "schema": {"type": "string", "const": "bytes"},
            },
        },
        "content": {"application/json-seq": {"schema": {"type": "string", "format": "binary"}}},
    },
}


@router.get(
    "/collections/{collection_id}/provenance/files",
    response_model=ListCollectionFileProvenanceResponse,
    openapi_extra=mutable_browse_operation(),
)
def list_collection_provenance(
    collection_id: CollectionIdParameter,
    principal: ProvenanceReader,
    container: ContainerDep,
    page_size: Annotated[int, Query(ge=1, le=100)] = 25,
    page_token: BrowsePageTokenQuery = None,
    q: BrowseQueryParameter = None,
    status: ProvenanceStatus | None = None,
    sort: ProvenanceSort = "path",
    order: SortOrder = "asc",
) -> dict[str, Any]:
    selectors = canonical_selectors(
        collection_id=collection_id, q=q, status=status, sort=sort, order=order
    )
    position = page_position(
        container,
        principal=principal,
        operation="list_collection_provenance",
        page_token=page_token,
        selectors=selectors,
    )
    return page_payload(
        container.provenance.list_files(
            collection_id,
            page_size=page_size,
            position=position,
            q=q,
            status=status,
            sort=sort,
            order=order,
            principal=principal,
        ),
        container=container,
        principal=principal,
        operation="list_collection_provenance",
        selectors=selectors,
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
    "/collections/{collection_id}/provenance/trace/{path:path}",
    response_model=CollectionFileProvenanceTraceOut,
    response_model_exclude_unset=True,
    openapi_extra=mutable_browse_operation(),
)
def trace_collection_file_provenance(
    collection_id: CollectionIdParameter,
    path: CanonicalRelPath,
    principal: ProvenanceReader,
    container: ContainerDep,
    page_size: Annotated[int, Query(ge=1, le=100)] = 25,
    page_token: BrowsePageTokenQuery = None,
) -> dict[str, Any]:
    selectors = canonical_selectors(collection_id=collection_id, path=path)
    position = page_position(
        container,
        principal=principal,
        operation="trace_collection_file_provenance",
        page_token=page_token,
        selectors=selectors,
    )
    return page_payload(
        container.provenance.trace_file(
            collection_id,
            path,
            page_size=page_size,
            position=position,
            principal=principal,
        ),
        container=container,
        principal=principal,
        operation="trace_collection_file_provenance",
        selectors=selectors,
    )


@router.head(
    "/collections/{collection_id}/provenance/journals/{journal_id}",
    include_in_schema=False,
    operation_id="head_collection_provenance_journal",
    openapi_extra=operation_interface("standard-tool/protocol"),
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
    http_request: Request,
    range_header: Annotated[str | None, Header(alias="Range")] = None,
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
) -> Response:
    byte_count, sha256 = container.provenance.journal_metadata(
        collection_id,
        journal_id,
        principal=principal,
    )

    etag = f'"{sha256}"'
    if range_header is not None:
        if if_match is None:
            raise PreconditionRequired("provenance journal continuation requires If-Match")
        if parse_quoted_sha256_identity(if_match) != sha256:
            raise PreconditionFailed("provenance journal identity changed")
    start, end = _parse_range(range_header, byte_count)
    status_code = 206 if range_header is not None else 200
    content_length = end - start
    headers = {
        "Accept-Ranges": "bytes",
        "Content-Length": str(content_length),
        "ETag": etag,
        "Content-Disposition": f'attachment; filename="{journal_id}.json-seq"',
    }
    if status_code == 206:
        headers["Content-Range"] = f"bytes {start}-{end - 1}/{byte_count}"
    if http_request.method == "HEAD":
        return Response(status_code=status_code, headers=headers)

    def content() -> Iterator[bytes]:
        if range_header is None:
            yield from container.provenance.iter_journal(
                collection_id,
                journal_id,
                principal=principal,
            )
            return
        yield from container.provenance.iter_journal_range(
            collection_id,
            journal_id,
            offset=start,
            size=content_length,
            principal=principal,
        )

    return StreamingResponse(
        content(),
        status_code=status_code,
        media_type="application/json-seq",
        headers=headers,
    )


def _parse_range(value: str | None, total_bytes: int) -> tuple[int, int]:
    if value is None:
        return 0, total_bytes
    unit, separator, raw = value.partition("=")
    if unit.casefold() != "bytes" or not separator or "," in raw:
        raise BadRequest("only one bytes range is supported")
    start_raw, dash, end_raw = raw.partition("-")
    if not dash:
        raise BadRequest("invalid bytes range")
    try:
        if not start_raw:
            suffix = int(end_raw)
            if suffix <= 0:
                raise ValueError
            start = max(0, total_bytes - suffix)
            end = total_bytes
        else:
            start = int(start_raw)
            end = total_bytes if not end_raw else int(end_raw) + 1
    except ValueError as exc:
        raise BadRequest("invalid bytes range") from exc
    if start < 0 or start >= total_bytes or end <= start or end > total_bytes:
        raise BadRequest("bytes range is outside the journal")
    return start, end


@router.get(
    "/collections/{collection_id}/provenance/journals/{journal_id}/agents",
    response_model=ListProvenanceJournalAgentsResponse,
    openapi_extra=mutable_browse_operation(),
)
def list_collection_provenance_journal_agents(
    collection_id: CollectionIdParameter,
    journal_id: ProvenanceJournalId,
    principal: ProvenanceReader,
    container: ContainerDep,
    page_size: Annotated[int, Query(ge=1, le=100)] = 25,
    page_token: BrowsePageTokenQuery = None,
) -> dict[str, Any]:
    selectors = canonical_selectors(collection_id=collection_id, journal_id=journal_id)
    position = page_position(
        container,
        principal=principal,
        operation="list_collection_provenance_journal_agents",
        page_token=page_token,
        selectors=selectors,
    )
    return page_payload(
        container.provenance.list_journal_agents(
            collection_id,
            journal_id,
            page_size=page_size,
            position=position,
            principal=principal,
        ),
        container=container,
        principal=principal,
        operation="list_collection_provenance_journal_agents",
        selectors=selectors,
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
