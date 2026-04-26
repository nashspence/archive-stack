from __future__ import annotations

from fastapi import APIRouter

from arc_api.deps import ContainerDep
from arc_api.mappers import map_fetch
from arc_api.schemas.fetches import (
    CompleteFetchResponse,
    FetchManifestResponse,
    FetchSummaryOut,
    FetchUploadSessionResponse,
)

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
    container: ContainerDep,
) -> FetchUploadSessionResponse:
    payload = container.fetches.create_or_resume_upload(fetch_id=fetch_id, entry_id=entry_id)
    return FetchUploadSessionResponse.model_validate(payload)


@router.post("/fetches/{fetch_id}/complete", response_model=CompleteFetchResponse)
def complete_fetch(fetch_id: str, container: ContainerDep) -> CompleteFetchResponse:
    payload = container.fetches.complete(fetch_id)
    return CompleteFetchResponse.model_validate(payload)
