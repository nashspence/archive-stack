from __future__ import annotations

from typing import Any, Literal, Self

from application_access import ApplicationKeyId, ApplicationName
from pydantic import ConfigDict, model_validator
from riverhog_protocol import (
    ArchiveCopySort,
    ArchiveCopyState,
    ArchiveCopyStoreSelectionDocument,
    ArchiveStoreName,
    SortOrder,
)

from riverhog_api.schemas.common import RiverhogModel


class ArchiveCopyOut(RiverhogModel):
    store: ArchiveStoreName
    state: Literal["pending", "uploading", "uploaded", "retrying", "failed"]
    storage_prefix: str | None
    object_count: int
    stored_bytes: int | None
    last_uploaded_at: str | None
    last_verified_at: str | None
    failure: str | None
    archive_root: ArchiveRootPublicationOut | None = None


class ArchiveRootPublicationOut(RiverhogModel):
    object_path: str | None = None
    sha256: str | None = None
    proof_object_path: str | None = None
    proof_sha256: str | None = None
    proof_state: Literal["pending", "uploaded", "failed"] = "pending"


class CreateArchiveCopyRequest(ArchiveCopyStoreSelectionDocument):
    collection_id: int
    event_context: dict[str, Any] | None = None


class ArchiveCopyJobOut(RiverhogModel):
    model_config = ConfigDict(
        json_schema_extra={
            "allOf": [
                {
                    "if": {"properties": {"state": {"const": "completed"}}},
                    "then": {
                        "properties": {
                            "completed_at": {"type": "string"},
                            "failure": {"type": "null"},
                        }
                    },
                },
                {
                    "if": {"properties": {"state": {"const": "canceled"}}},
                    "then": {"properties": {"completed_at": {"type": "string"}}},
                },
                {
                    "if": {"properties": {"state": {"const": "failed"}}},
                    "then": {"properties": {"failure": {"type": "string", "minLength": 1}}},
                },
            ]
        }
    )

    collection_id: int
    source_store: ArchiveStoreName | None
    destination_store: ArchiveStoreName
    initiated_by_app: ApplicationName | None
    initiated_by_key_id: ApplicationKeyId | None
    state: ArchiveCopyState
    requested_at: str | None
    ready_at: str | None
    expires_at: str | None
    completed_at: str | None
    failure: str | None

    @model_validator(mode="after")
    def validate_terminal_evidence(self) -> Self:
        if self.state == "completed" and (self.completed_at is None or self.failure is not None):
            raise ValueError("completed archive-copy jobs require completion evidence")
        if self.state == "canceled" and self.completed_at is None:
            raise ValueError("canceled archive-copy jobs require completed_at")
        if self.state == "failed" and not self.failure:
            raise ValueError("failed archive-copy jobs require failure evidence")
        return self


class ArchiveCopyJobListFiltersOut(RiverhogModel):
    state: ArchiveCopyState | None = None


class ArchiveCopyJobListOut(RiverhogModel):
    page: int
    per_page: int
    total: int
    pages: int
    sort: ArchiveCopySort
    order: SortOrder
    query: str | None
    filters: ArchiveCopyJobListFiltersOut
    copies: list[ArchiveCopyJobOut]


class ArchiveCopyRetirementRequest(RiverhogModel):
    collection_id: int
    store: ArchiveStoreName


class RetireArchiveCopyRequest(ArchiveCopyRetirementRequest):
    challenge: str


class ArchiveCopyRetirementTargetOut(RiverhogModel):
    store: ArchiveStoreName
    last_verified_at: str
    remote_storage_bytes: int
    object_count: int


class ArchiveCopyRetirementRetainedOut(RiverhogModel):
    store: ArchiveStoreName
    last_verified_at: str
    remote_storage_bytes: int


class ArchiveCopyRetirementPlanOut(RiverhogModel):
    model_config = ConfigDict(
        json_schema_extra={
            "oneOf": [
                {
                    "properties": {
                        "status": {"const": "blocked"},
                        "challenge": {"type": "null"},
                        "blockers": {"minItems": 1},
                    }
                },
                {
                    "properties": {
                        "status": {"enum": ["ready", "retiring"]},
                        "challenge": {"type": "string", "minLength": 1},
                        "blockers": {"maxItems": 0},
                    }
                },
            ]
        }
    )

    status: Literal["ready", "blocked", "retiring"]
    collection_id: int
    store: ArchiveStoreName
    warning: str
    expires_at: str
    challenge: str | None
    target_copy: ArchiveCopyRetirementTargetOut
    retained_copies: list[ArchiveCopyRetirementRetainedOut]
    retired_retrieval_job_count: int
    blockers: list[str]
    verification_note: str
    billing_note: str

    @model_validator(mode="after")
    def validate_plan_state(self) -> Self:
        if self.status == "blocked":
            if self.challenge is not None or not self.blockers:
                raise ValueError(
                    "blocked archive-copy retirement requires blockers and no challenge"
                )
        elif not self.challenge or self.blockers:
            raise ValueError("ready archive-copy retirement requires a challenge and no blockers")
        return self


class ArchiveCopyRetirementResultOut(RiverhogModel):
    status: Literal["retired", "already_absent"]
    collection_id: int
    store: ArchiveStoreName
    remote_storage_bytes: int
    verified_store: ArchiveStoreName | None
