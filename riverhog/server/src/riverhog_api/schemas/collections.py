from __future__ import annotations

from typing import Any, Literal

from http_api_contracts import CanonicalVisibleText
from pydantic import ConfigDict, Field, field_validator, model_validator
from riverhog_protocol import (
    ArchiveStoreName,
    CollectionId,
    CollectionSort,
    CollectionUploadFileBatchDocument,
    CollectionUploadRegistrationConstraintsDocument,
    CollectionUploadSort,
    CollectionUploadState,
    FileProvenanceBinding,
    RetirementClaimReferenceDocument,
    SortOrder,
)
from riverhog_protocol import (
    CollectionUploadFileIn as CollectionUploadFileIn,
)
from riverhog_protocol.paths import CanonicalRelPath, CanonicalTag
from riverhog_provenance_contracts import ProvenanceJournalId, ProvenanceStateId

from riverhog_api.schemas.archive import ArchiveCopyOut
from riverhog_api.schemas.common import RiverhogModel


class CreateOrResumeCollectionUploadSessionRequest(RiverhogModel):
    model_config = ConfigDict(
        json_schema_extra={
            "oneOf": [
                {
                    "properties": {
                        "provenance_mode": {"const": "captured"},
                        "provenance_omission_reason": {"type": "null"},
                    }
                },
                {
                    "properties": {
                        "provenance_mode": {"const": "omitted"},
                        "provenance_omission_reason": {"type": "string"},
                    },
                    "required": ["provenance_mode", "provenance_omission_reason"],
                },
            ]
        }
    )

    idempotency_key: CanonicalVisibleText = Field(max_length=200)
    tags: list[CanonicalTag]
    ingest_source: str | None = None
    archive_store: ArchiveStoreName | None = None
    event_context: dict[str, Any] | None = None
    provenance_mode: Literal["captured", "omitted"] = "captured"
    provenance_omission_reason: CanonicalVisibleText | None = None

    @field_validator("tags")
    @classmethod
    def validate_unique_tags(cls, value: list[str]) -> list[str]:
        if len(value) != len(set(value)):
            raise ValueError("collection tags must not contain duplicates")
        return value

    @model_validator(mode="after")
    def validate_provenance_choice(self) -> CreateOrResumeCollectionUploadSessionRequest:
        if self.provenance_mode == "captured":
            if self.provenance_omission_reason is not None:
                raise ValueError("captured provenance cannot have an omission reason")
            return self
        reason = self.provenance_omission_reason
        if reason is None or not reason or reason.strip() != reason:
            raise ValueError("omitted provenance requires a canonical omission reason")
        return self


class RegisterCollectionUploadSessionFilesRequest(CollectionUploadFileBatchDocument):
    pass


class CompleteCollectionUploadSessionRequest(RiverhogModel):
    files_total: int = Field(ge=1, strict=True)
    content_identity: str = Field(pattern=r"^[0-9a-f]{64}$")
    provenance_identity: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")


class CollectionSummaryOut(RiverhogModel):
    id: CollectionId
    created_at: str
    tags: list[CanonicalTag]
    content_identity: str = Field(pattern=r"^[0-9a-f]{64}$")
    archive_root_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    encryption_format: str
    passphrase_id: str = Field(pattern=r"^[A-Za-z0-9_-]{16,128}$")
    files: int
    bytes: int
    remote_storage_bytes: int
    archive_copies: list[ArchiveCopyOut]


class ListCollectionsResponse(RiverhogModel):
    page: int
    per_page: int
    total: int
    pages: int
    sort: CollectionSort
    order: SortOrder
    query: str | None
    tag: CanonicalTag | None
    encryption_format: str | None
    passphrase_id: str | None
    collections: list[CollectionSummaryOut]


class CollectionDeletionArchiveCopyOut(RiverhogModel):
    store: ArchiveStoreName
    objects: int
    stored_bytes: int


class CollectionDeletionPlanOut(RiverhogModel):
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
                        "status": {"enum": ["ready", "deleting"]},
                        "challenge": {"type": "string", "minLength": 1},
                        "blockers": {"maxItems": 0},
                    }
                },
            ]
        }
    )

    status: Literal["ready", "blocked", "deleting"]
    collection_id: CollectionId
    warning: str
    expires_at: str
    challenge: str | None
    file_count: int
    bytes: int
    archive_copies: list[CollectionDeletionArchiveCopyOut]
    archive_object_count: int
    remote_storage_bytes: int
    upload_file_count: int
    record_etag: str
    metadata_rows: dict[str, int]
    retirement_claim: RetirementClaimReferenceDocument | None = None
    blockers: list[str]
    billing_note: str

    @model_validator(mode="after")
    def validate_plan_state(self) -> CollectionDeletionPlanOut:
        if self.status == "blocked":
            if self.challenge is not None or not self.blockers:
                raise ValueError("blocked collection deletion requires blockers and no challenge")
        elif not self.challenge or self.blockers:
            raise ValueError("ready collection deletion requires a challenge and no blockers")
        return self


class DeleteCollectionRequest(RiverhogModel):
    challenge: str
    retirement_claim_id: str | None = Field(default=None, min_length=1, max_length=64)
    event_context: dict[str, Any] | None = None


class CollectionDeletionResultOut(RiverhogModel):
    status: Literal["deleted", "already_absent"]
    collection_id: CollectionId
    files: int
    bytes: int
    remote_storage_bytes: int


class CollectionUploadFileOut(RiverhogModel):
    path: CanonicalRelPath
    bytes: int
    sha256: str
    upload_state: str
    uploaded_bytes: int
    upload_state_expires_at: str | None
    provenance: FileProvenanceBinding


class CollectionUploadProvenanceJournalOut(RiverhogModel):
    journal_id: ProvenanceJournalId
    bytes: int
    sha256: str
    current_state_id: ProvenanceStateId
    current_path: str
    current_bytes: int
    current_sha256: str


class CollectionUploadSessionFilesRegistrationOut(RiverhogModel):
    collection_id: CollectionId
    ingest_source: str | None
    archive_store: ArchiveStoreName
    encryption_format: str
    passphrase_id: str = Field(pattern=r"^[A-Za-z0-9_-]{16,128}$")
    state: Literal["open", "uploading"]
    files: list[CollectionUploadFileOut]
    volumes: list[CollectionUploadVolumeSummaryOut]


class CollectionUploadVolumeSummaryOut(RiverhogModel):
    volume_id: str
    sequence: int
    kind: Literal["pack", "segment"]


class ListCollectionUploadSessionFilesResponse(RiverhogModel):
    page: int
    per_page: int
    total: int
    pages: int
    files: list[CollectionUploadFileOut]


class CollectionUploadListItemOut(RiverhogModel):
    collection_id: CollectionId
    created_at: str | None
    tags: list[CanonicalTag]
    ingest_source: str | None
    archive_store: ArchiveStoreName
    encryption_format: str
    passphrase_id: str = Field(pattern=r"^[A-Za-z0-9_-]{16,128}$")
    state: Literal["open", "uploading", "finalizing", "failed"]
    files: int
    bytes: int
    uploaded_bytes: int


class CollectionUploadListFiltersOut(RiverhogModel):
    tag: CanonicalTag | None
    state: CollectionUploadState | None


class ListCollectionUploadSessionsResponse(RiverhogModel):
    page: int
    per_page: int
    total: int
    pages: int
    sort: CollectionUploadSort
    order: SortOrder
    query: str | None
    filters: CollectionUploadListFiltersOut
    uploads: list[CollectionUploadListItemOut]


class CollectionUploadRegistrationConstraintsOut(CollectionUploadRegistrationConstraintsDocument):
    pass


class CollectionUploadSessionOut(RiverhogModel):
    model_config = ConfigDict(
        json_schema_extra={
            "allOf": [
                {
                    "if": {"properties": {"state": {"const": "finalized"}}},
                    "then": {
                        "properties": {
                            "content_identity": {"type": "string"},
                            "archive_root_sha256": {"type": "string"},
                            "registration_constraints": {"type": "null"},
                            "collection": {"type": "object"},
                        }
                    },
                    "else": {
                        "properties": {
                            "content_identity": {"type": "null"},
                            "archive_root_sha256": {"type": "null"},
                            "registration_constraints": {"type": "object"},
                            "collection": {"type": "null"},
                        }
                    },
                },
                {
                    "if": {"properties": {"state": {"const": "failed"}}},
                    "then": {"properties": {"latest_failure": {"type": "string", "minLength": 1}}},
                },
            ]
        }
    )

    collection_id: CollectionId
    created_at: str
    tags: list[CanonicalTag]
    ingest_source: str | None
    provenance_mode: Literal["captured", "mixed", "omitted"]
    provenance_identity: str | None
    content_identity: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    archive_root_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    archive_store: ArchiveStoreName
    encryption_format: str
    passphrase_id: str = Field(pattern=r"^[A-Za-z0-9_-]{16,128}$")
    state: Literal["open", "uploading", "finalizing", "finalized", "failed", "canceled"]
    registration_constraints: CollectionUploadRegistrationConstraintsOut | None
    files_total: int
    files_pending: int
    files_partial: int
    files_uploaded: int
    bytes_total: int
    uploaded_bytes: int
    missing_bytes: int
    upload_state_expires_at: str | None
    latest_failure: str | None = None
    archive_phase: str | None = None
    archive_phase_updated_at: str | None = None
    archive_storage_prefix: str | None = None
    archive_uploaded_bytes: int | None = None
    archive_total_bytes: int | None = None
    archive_uploaded_parts: int | None = None
    archive_total_parts: int | None = None
    collection: CollectionSummaryOut | None

    @model_validator(mode="after")
    def validate_terminal_evidence(self) -> CollectionUploadSessionOut:
        if self.state == "finalized" and (
            self.content_identity is None
            or self.archive_root_sha256 is None
            or self.registration_constraints is not None
            or self.collection is None
        ):
            raise ValueError("finalized upload sessions require immutable collection evidence")
        if self.state != "finalized" and (
            self.content_identity is not None
            or self.archive_root_sha256 is not None
            or self.registration_constraints is None
            or self.collection is not None
        ):
            raise ValueError("nonfinal upload sessions require only registration constraints")
        if self.state == "failed" and not self.latest_failure:
            raise ValueError("failed upload sessions require failure evidence")
        return self


class CreateOrResumeCollectionUploadSessionOut(CollectionUploadSessionOut):
    resumed: bool


class CollectionUploadUnitSourceOut(RiverhogModel):
    path: str
    offset: int
    bytes: int
    sha256: str


class CollectionUploadUnitOut(RiverhogModel):
    unit: int
    payload_bytes: int
    plaintext_bytes: int
    sources: list[CollectionUploadUnitSourceOut]
    state: Literal["pending", "committed"]


class CollectionUploadVolumeOut(RiverhogModel):
    volume_id: str
    sequence: int
    kind: Literal["pack", "segment"]
    state: Literal["planned", "uploading", "sealed", "failed"]
    plan_sha256: str
    plaintext_bytes: int
    source_bytes: int
    units: list[CollectionUploadUnitOut]


class ListCollectionUploadVolumesResponse(RiverhogModel):
    collection_id: CollectionId
    volumes: list[CollectionUploadVolumeOut]
