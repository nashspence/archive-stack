from __future__ import annotations

from typing import Literal, Self

from http_api_contracts import BrowsePageToken, CanonicalVisibleText, Sha256Identity
from lifecycle_events import EventContext
from pydantic import ConfigDict, Field, model_validator
from riverhog_protocol import (
    RETRIEVAL_FILE_BATCH_MAX,
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
    idempotency_key: CanonicalVisibleText = Field(max_length=200)
    lease_seconds: int | None = Field(default=None, ge=1)
    restore_policy: Literal["allow", "never"] = "allow"


class RetrievalPlanFileOut(ImmutableFileIdentityDocument):
    collection_id: CollectionId
    requires_restore: bool


class RetrievalPlanOut(RiverhogModel):
    format: Literal["riverhog-retrieval-plan/v1"]
    id: str
    state: Literal["planning", "ready", "consumed", "expired", "failed"]
    created_at: str
    ready_at: str | None
    expires_at: str
    failure: str | None = Field(min_length=1)
    lease_seconds: int
    restore_policy: Literal["allow", "never"]
    requires_restore: bool
    file_count: int = Field(ge=1, le=RETRIEVAL_FILE_BATCH_MAX)
    etag: Sha256Identity | None

    @model_validator(mode="after")
    def validate_state_evidence(self) -> Self:
        if (self.failure is not None) != (self.state == "failed"):
            raise ValueError("retrieval plan failure must match failed state")
        if self.state in {"ready", "consumed"} and (self.ready_at is None or self.etag is None):
            raise ValueError("sealed retrieval plans require ready evidence")
        if self.state == "planning" and (self.ready_at is not None or self.etag is not None):
            raise ValueError("planning retrieval plans cannot carry ready evidence")
        if self.state == "expired" and ((self.ready_at is None) != (self.etag is None)):
            raise ValueError("expired retrieval plan ready evidence must remain consistent")
        return self


class RetrievalPlanFilePageOut(RiverhogModel):
    format: Literal["riverhog-retrieval-plan-files/v1"]
    plan_id: str
    etag: Sha256Identity
    start_ordinal: int = Field(ge=0, le=RETRIEVAL_FILE_BATCH_MAX)
    next_ordinal: int | None = Field(
        default=None,
        ge=1,
        le=RETRIEVAL_FILE_BATCH_MAX,
    )
    complete: bool
    files: list[RetrievalPlanFileOut] = Field(max_length=100)

    @model_validator(mode="after")
    def validate_progression(self) -> Self:
        if self.complete:
            if self.next_ordinal is not None:
                raise ValueError("complete retrieval plan pages cannot continue")
        elif not self.files or self.next_ordinal != self.start_ordinal + len(self.files):
            raise ValueError("retrieval plan page continuation must advance exactly")
        return self


class CreateRetrievalJobRequest(RiverhogModel):
    plan_id: str
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
    plan_id: str
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
    next_page_token: BrowsePageToken | None
    sort: RetrievalCacheSort
    order: SortOrder
    query: str | None
    filters: RetrievalCacheObjectListFiltersOut
    objects: list[RetrievalCacheObjectOut]
