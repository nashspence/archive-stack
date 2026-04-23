from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Header, Request, Response

from arc_api.deps import ContainerDep
from arc_api.mappers import map_fetch
from arc_api.schemas.fetches import (
    CompleteFetchResponse,
    FetchManifestResponse,
    FetchSummaryOut,
    FetchUploadSessionResponse,
)
from arc_core.domain.errors import BadRequest

router = APIRouter(tags=["fetches"])


@router.get("/fetches/{fetch_id}", response_model=FetchSummaryOut)
def get_fetch(fetch_id: str, container: ContainerDep) -> FetchSummaryOut:
    summary = container.fetches.get(fetch_id)
    return FetchSummaryOut.model_validate(map_fetch(summary))


@router.get("/fetches/{fetch_id}/manifest", response_model=FetchManifestResponse)
def get_manifest(fetch_id: str, container: ContainerDep) -> FetchManifestResponse:
    payload = container.fetches.manifest(fetch_id)
    return FetchManifestResponse.model_validate(payload)


@router.post(
    "/fetches/{fetch_id}/entries/{entry_id}/upload", response_model=FetchUploadSessionResponse
)
def create_or_resume_fetch_entry_upload(
    fetch_id: str,
    entry_id: str,
    request: Request,
    container: ContainerDep,
) -> FetchUploadSessionResponse:
    payload = container.fetches.create_or_resume_upload(fetch_id=fetch_id, entry_id=entry_id)
    payload["upload_url"] = str(
        request.url_for("patch_fetch_entry_upload", fetch_id=fetch_id, entry_id=entry_id)
    )
    return FetchUploadSessionResponse.model_validate(payload)


@router.patch(
    "/uploads/fetches/{fetch_id}/entries/{entry_id}",
    include_in_schema=False,
    name="patch_fetch_entry_upload",
    status_code=204,
)
async def patch_fetch_entry_upload(
    fetch_id: str,
    entry_id: str,
    request: Request,
    upload_offset: Annotated[int, Header(alias="Upload-Offset")],
    upload_checksum: Annotated[str, Header(alias="Upload-Checksum")],
    tus_resumable: Annotated[str, Header(alias="Tus-Resumable")],
    container: ContainerDep,
) -> Response:
    if tus_resumable != "1.0.0":
        raise BadRequest("Tus-Resumable must be 1.0.0")
    content = await request.body()
    payload = container.fetches.append_upload_chunk(
        fetch_id=fetch_id,
        entry_id=entry_id,
        offset=upload_offset,
        checksum=upload_checksum,
        content=content,
    )
    headers = {
        "Tus-Resumable": "1.0.0",
        "Upload-Offset": str(payload["offset"]),
    }
    if payload.get("expires_at") is not None:
        headers["Upload-Expires"] = str(payload["expires_at"])
    return Response(status_code=204, headers=headers)


@router.post("/fetches/{fetch_id}/complete", response_model=CompleteFetchResponse)
def complete_fetch(fetch_id: str, container: ContainerDep) -> CompleteFetchResponse:
    payload = container.fetches.complete(fetch_id)
    return CompleteFetchResponse.model_validate(payload)
