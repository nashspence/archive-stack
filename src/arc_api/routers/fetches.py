from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Header, Request

from arc_api.deps import ServiceContainer, get_container
from arc_api.mappers import map_fetch
from arc_api.schemas.fetches import CompleteFetchResponse, FetchManifestResponse, FetchSummaryOut, UploadEntryResponse

router = APIRouter(tags=["fetches"])


@router.get("/fetches/{fetch_id}", response_model=FetchSummaryOut)
def get_fetch(fetch_id: str, container: ServiceContainer = Depends(get_container)) -> FetchSummaryOut:
    summary = container.fetches.get(fetch_id)
    return FetchSummaryOut.model_validate(map_fetch(summary))


@router.get("/fetches/{fetch_id}/manifest", response_model=FetchManifestResponse)
def get_manifest(fetch_id: str, container: ServiceContainer = Depends(get_container)) -> FetchManifestResponse:
    payload = container.fetches.manifest(fetch_id)
    return FetchManifestResponse.model_validate(payload)


@router.put("/fetches/{fetch_id}/files/{entry_id}", response_model=UploadEntryResponse)
async def upload_fetch_entry(
    fetch_id: str,
    entry_id: str,
    request: Request,
    x_sha256: Annotated[str, Header(alias="X-Sha256")],
    container: ServiceContainer = Depends(get_container),
) -> UploadEntryResponse:
    content = await request.body()
    payload = container.fetches.upload_entry(fetch_id=fetch_id, entry_id=entry_id, sha256=x_sha256, content=content)
    return UploadEntryResponse.model_validate(payload)


@router.post("/fetches/{fetch_id}/complete", response_model=CompleteFetchResponse)
def complete_fetch(fetch_id: str, container: ServiceContainer = Depends(get_container)) -> CompleteFetchResponse:
    payload = container.fetches.complete(fetch_id)
    return CompleteFetchResponse.model_validate(payload)
