from __future__ import annotations

from typing import Literal, Self

from http_api_contracts import Sha256Identity
from lifecycle_events import EventContext
from pydantic import ConfigDict, Field, model_validator
from riverhog_protocol import (
    ArchiveStoreName,
    CollectionId,
    ImmutableFileIdentityDocument,
    RetrievalCacheProtection,
    RetrievalCacheSort,
    RetrievalCacheState,
    RetrievalCacheStoreName,
    RetrievalFileReferenceDocument,
    RetrievalFileReferenceSetDocument,
    SortOrder,
)

from riverhog_api.schemas.common import RiverhogModel


class RetrievalFileIn(RetrievalFileReferenceDocument):
    pass


class RetrievalPlanRequest(RetrievalFileReferenceSetDocument):
    lease_seconds: int | None = Field(default=None, ge=1)
    restore_policy: Literal["allow", "never"] = "allow"


class RetrievalPlanFileOut(ImmutableFileIdentityDocument):
    collection_id: CollectionId


class RetrievalPlanObjectPlacementOut(RiverhogModel):
    path: str
    sequence: int
    file_offset: int
    object_offset: int
    bytes: int
    member: str | None


class RetrievalPlanObjectOut(RiverhogModel):
    collection_id: CollectionId
    source_store: ArchiveStoreName
    object_id: str
    kind: Literal["pack", "segment"]
    plaintext_bytes: int
    stored_bytes: int
    sha256: str | None
    retrieval_bytes: int
    read_mode: Literal["immediate", "restore_required", "cache"]
    cache_store: RetrievalCacheStoreName | None
    placements: list[RetrievalPlanObjectPlacementOut]


class RetrievalPlanOut(RiverhogModel):
    format: Literal["riverhog-retrieval-plan/v1"]
    lease_seconds: int
    restore_policy: Literal["allow", "never"]
    requires_restore: bool
    files: list[RetrievalPlanFileOut]
    objects: list[RetrievalPlanObjectOut]
    etag: Sha256Identity


class CreateRetrievalJobRequest(RetrievalPlanRequest):
    event_context: EventContext | None = None


class RenewRetrievalJobRequest(RiverhogModel):
    lease_seconds: int = Field(ge=1)


class RetrievalJobOut(RiverhogModel):
    model_config = ConfigDict(
        json_schema_extra={
            "allOf": [
                {
                    "if": {"properties": {"state": {"const": "completed"}}},
                    "then": {"properties": {"completed_at": {"type": "string"}}},
                    "else": {"properties": {"completed_at": {"type": "null"}}},
                },
                {
                    "if": {"properties": {"state": {"const": "canceled"}}},
                    "then": {"properties": {"canceled_at": {"type": "string"}}},
                    "else": {"properties": {"canceled_at": {"type": "null"}}},
                },
                {
                    "if": {"properties": {"state": {"const": "failed"}}},
                    "then": {"properties": {"failure": {"type": "string", "minLength": 1}}},
                },
                {
                    "if": {"properties": {"state": {"enum": ["requested", "failed"]}}},
                    "else": {"properties": {"failure": {"type": "null"}}},
                },
            ]
        }
    )

    id: str
    state: Literal["requested", "ready", "completed", "expired", "failed", "canceled"]
    plan_etag: Sha256Identity
    created_at: str
    requested_at: str | None
    restore_requested_at: str | None
    ready_at: str | None
    expires_at: str | None
    completed_at: str | None
    canceled_at: str | None
    failure: str | None = Field(min_length=1)
    lease_seconds: int
    restore_policy: Literal["allow", "never"]
    requires_restore: bool
    files: list[RetrievalPlanFileOut]
    objects: list[RetrievalPlanObjectOut]

    @model_validator(mode="after")
    def validate_terminal_evidence(self) -> Self:
        if (self.completed_at is not None) != (self.state == "completed"):
            raise ValueError("retrieval completed_at must match completed state")
        if (self.canceled_at is not None) != (self.state == "canceled"):
            raise ValueError("retrieval canceled_at must match canceled state")
        if self.state == "failed" and not self.failure:
            raise ValueError("failed retrieval jobs require failure evidence")
        if self.state not in {"requested", "failed"} and self.failure is not None:
            raise ValueError("retrieval failure evidence is only valid while requested or failed")
        return self


class RetrievalCachePolicyOut(RiverhogModel):
    new_archive_lease_seconds: int
    retrieval_default_lease_seconds: int
    retrieval_max_lease_seconds: int
    pending_timeout_seconds: int
    sweep_interval_seconds: int
    restore_poll_interval_seconds: int


class RetrievalCacheStoreStatusOut(RiverhogModel):
    cache_store: RetrievalCacheStoreName
    priority: int = Field(ge=1)
    admission_enabled: bool
    admission_budget_bytes: int | None = Field(default=None, ge=1)
    reserved_bytes: int = Field(ge=0)
    committed_bytes: int = Field(ge=0)


class RetrievalCacheStatusOut(RiverhogModel):
    configured: bool
    new_archive_enabled: bool
    objects: int
    stored_bytes: int
    protected_objects: int
    unleased_objects: int
    stores: list[RetrievalCacheStoreStatusOut]
    policy: RetrievalCachePolicyOut


class RetrievalCacheObjectOut(RiverhogModel):
    collection_id: CollectionId
    source_store: ArchiveStoreName
    cache_store: RetrievalCacheStoreName
    object_id: str
    state: RetrievalCacheState
    stored_bytes: int
    stored_sha256: str | None
    cached_at: str
    verified_at: str
    protected_until: str | None
    new_archive_expires_at: str | None
    lease_categories: list[Literal["new_archive", "retrieval_job"]]
    retrieval_job_leases: int
    tag_count: int = Field(ge=0, strict=True)


class RetrievalCacheObjectListFiltersOut(RiverhogModel):
    tag: str | None
    collection_id: CollectionId | None
    source_store: ArchiveStoreName | None
    cache_store: RetrievalCacheStoreName | None
    state: RetrievalCacheState | None
    protection: RetrievalCacheProtection | None
    expires_before: str | None
    expires_after: str | None


class RetrievalCacheObjectListOut(RiverhogModel):
    page_size: int = Field(ge=1, le=100)
    next_page_token: str | None
    sort: RetrievalCacheSort
    order: SortOrder
    query: str | None
    filters: RetrievalCacheObjectListFiltersOut
    objects: list[RetrievalCacheObjectOut]
