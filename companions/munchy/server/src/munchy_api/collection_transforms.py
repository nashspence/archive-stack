"""HTTP binding for the collection-set-to-one-collection transform contract."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from http_api_contracts import operation_interface
from munchy_core.persistence.application_keys import SUBMISSIONS_MANAGE, MunchyPrincipal
from munchy_core.services.collection_transforms import MunchyCollectionTransformService
from pydantic import BaseModel, ConfigDict, Field

router = APIRouter(prefix="/collection-transforms", tags=["collection-transforms"])


class CollectionTransformCreateIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    job_id: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")
    claim_id: str = Field(min_length=1, max_length=160)
    fence: int = Field(ge=1)
    capability_token: str = Field(min_length=1, max_length=1000)
    intent: dict[str, Any]


class CollectionTransformOut(BaseModel):
    model_config = ConfigDict(extra="allow")

    format: str
    job_id: str
    claim_id: str
    fence: int
    request_sha256: str
    state: str
    phase: str
    created_at: str
    updated_at: str
    output_collection_id: int | None = None
    output_manifest_sha256: str | None = None
    output_content_etag: str | None = None
    derivation: dict[str, Any] | None = None
    error: dict[str, Any] | None = None


def _principal(request: Request) -> MunchyPrincipal:
    principal = getattr(request.state, "principal", None)
    if not isinstance(principal, MunchyPrincipal) or not principal.allows(SUBMISSIONS_MANAGE):
        raise HTTPException(status_code=403, detail="collection transform access denied")
    return principal


def _service(request: Request) -> MunchyCollectionTransformService:
    service = getattr(request.app.state, "collection_transform_service", None)
    if not isinstance(service, MunchyCollectionTransformService):
        raise HTTPException(
            status_code=503, detail="collection transform executor is not configured"
        )
    return service


Principal = Annotated[MunchyPrincipal, Depends(_principal)]
Service = Annotated[MunchyCollectionTransformService, Depends(_service)]


@router.post(
    "",
    response_model=CollectionTransformOut,
    openapi_extra=operation_interface("client-only-primitive"),
)
def create_or_resume_collection_transform(
    payload: CollectionTransformCreateIn,
    background: BackgroundTasks,
    service: Service,
    principal: Principal,
) -> CollectionTransformOut:
    job = service.create_or_resume(
        job_id=payload.job_id,
        claim_id=payload.claim_id,
        fence=payload.fence,
        capability_token=payload.capability_token,
        intent=payload.intent,
        owner_app=principal.app,
    )
    if str(job.get("state") or "") not in {"succeeded", "failed", "canceled"}:
        background.add_task(service.run, payload.job_id)
    return CollectionTransformOut.model_validate(job)


@router.get("/{job_id}", response_model=CollectionTransformOut)
def get_collection_transform(
    job_id: str,
    service: Service,
    principal: Principal,
) -> CollectionTransformOut:
    try:
        job = service.get(job_id)
    except KeyError as exc:
        raise HTTPException(
            status_code=404, detail=f"unknown collection transform: {job_id}"
        ) from exc
    owner = job.get("owner_app")
    if owner is not None and owner != principal.app:
        raise HTTPException(status_code=404, detail=f"unknown collection transform: {job_id}")
    return CollectionTransformOut.model_validate(job)
