from __future__ import annotations

from typing import Any, Literal

from pydantic import Field

from riverhog_api.schemas.common import RiverhogModel


class RetrievalFileIn(RiverhogModel):
    collection_id: int
    path: str


class RetrievalPlanRequest(RiverhogModel):
    files: list[RetrievalFileIn] = Field(min_length=1)
    lease_seconds: int | None = Field(default=None, ge=1)


class RetrievalPlanFileOut(RetrievalFileIn):
    bytes: int
    sha256: str


class RetrievalPlanObjectPlacementOut(RiverhogModel):
    path: str
    sequence: int
    file_offset: int
    bytes: int
    member: str | None


class RetrievalPlanObjectOut(RiverhogModel):
    collection_id: int
    source_store: str
    object_id: str
    kind: Literal["pack", "file", "segment", "manifest", "proof"]
    plaintext_bytes: int
    stored_bytes: int
    sha256: str
    read_mode: Literal["immediate", "restore_required", "cache"]
    placements: list[RetrievalPlanObjectPlacementOut]


class RetrievalPlanOut(RiverhogModel):
    format: Literal["riverhog-retrieval-plan/v1"]
    lease_seconds: int
    files: list[RetrievalPlanFileOut]
    objects: list[RetrievalPlanObjectOut]
    etag: str


class CreateRetrievalJobRequest(RetrievalPlanRequest):
    event_context: dict[str, Any] | None = None


class RetrievalJobOut(RiverhogModel):
    id: str
    state: Literal["requested", "ready", "completed", "expired", "failed", "canceled"]
    plan_etag: str
    created_at: str
    requested_at: str | None
    ready_at: str | None
    expires_at: str | None
    completed_at: str | None
    canceled_at: str | None
    failure: str | None
    files: list[RetrievalPlanFileOut]
    objects: list[RetrievalPlanObjectOut]
