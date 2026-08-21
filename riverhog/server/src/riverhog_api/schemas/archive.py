from __future__ import annotations

from typing import Any, Literal

from riverhog_api.schemas.common import RiverhogModel


class ArchiveCopyOut(RiverhogModel):
    store: str
    state: Literal["pending", "uploading", "uploaded", "retrying", "failed"]
    storage_prefix: str | None
    object_count: int
    stored_bytes: int | None
    last_uploaded_at: str | None
    last_verified_at: str | None
    failure: str | None
    collection_manifest: CollectionManifestOut | None = None


class CollectionManifestOut(RiverhogModel):
    object_path: str | None = None
    sha256: str | None = None
    proof_object_path: str | None = None
    proof_sha256: str | None = None
    proof_state: Literal["pending", "uploaded", "failed"] = "pending"


class CreateArchiveCopyRequest(RiverhogModel):
    collection_id: int
    destination_store: str
    source_store: str | None = None
    event_context: dict[str, Any] | None = None


class ArchiveCopyJobOut(RiverhogModel):
    collection_id: int
    source_store: str | None
    destination_store: str
    initiated_by_app: str | None
    initiated_by_key_id: str | None
    state: Literal[
        "requested",
        "waiting",
        "checking",
        "copying",
        "canceling",
        "completed",
        "failed",
        "canceled",
    ]
    requested_at: str | None
    ready_at: str | None
    expires_at: str | None
    completed_at: str | None
    failure: str | None


class ArchiveCopyJobListOut(RiverhogModel):
    page: int
    per_page: int
    total: int
    pages: int
    sort: str
    order: Literal["asc", "desc"]
    query: str | None
    filters: dict[str, str]
    copies: list[ArchiveCopyJobOut]


class ArchiveCopyRetirementRequest(RiverhogModel):
    collection_id: int
    store: str


class RetireArchiveCopyRequest(ArchiveCopyRetirementRequest):
    challenge: str


class ArchiveCopyRetirementTargetOut(RiverhogModel):
    store: str
    last_verified_at: str
    remote_storage_bytes: int
    object_count: int


class ArchiveCopyRetirementRetainedOut(RiverhogModel):
    store: str
    last_verified_at: str
    remote_storage_bytes: int


class ArchiveCopyRetirementPlanOut(RiverhogModel):
    status: Literal["ready", "blocked", "retiring"]
    collection_id: int
    store: str
    warning: str
    expires_at: str
    challenge: str | None
    target_copy: ArchiveCopyRetirementTargetOut
    retained_copies: list[ArchiveCopyRetirementRetainedOut]
    retired_retrieval_job_count: int
    blockers: list[str]
    verification_note: str
    billing_note: str


class ArchiveCopyRetirementResultOut(RiverhogModel):
    status: Literal["retired", "already_absent"]
    collection_id: int
    store: str
    remote_storage_bytes: int
    verified_store: str | None
