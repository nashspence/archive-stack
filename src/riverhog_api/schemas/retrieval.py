from __future__ import annotations

from typing import Literal

from pydantic import Field

from riverhog_api.schemas.common import RiverhogModel


class RetrievalFileIn(RiverhogModel):
    collection_id: str
    path: str


class RetrievalPlanRequest(RiverhogModel):
    files: list[RetrievalFileIn] = Field(min_length=1)
    lease_seconds: int | None = Field(default=None, ge=1)


class RetrievalPlanFileOut(RetrievalFileIn):
    bytes: int
    sha256: str


class RetrievalPlanObjectOut(RiverhogModel):
    collection_id: str
    source_store: str
    object_id: str
    kind: Literal["pack", "file", "segment", "manifest", "proof"]
    stored_bytes: int
    read_mode: Literal["immediate", "restore_required", "cache"]


class RetrievalPlanOut(RiverhogModel):
    format: Literal["riverhog-retrieval-plan/v1"]
    lease_seconds: int
    files: list[RetrievalPlanFileOut]
    objects: list[RetrievalPlanObjectOut]
    etag: str


class CreateRetrievalJobRequest(RetrievalPlanRequest):
    pass


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
