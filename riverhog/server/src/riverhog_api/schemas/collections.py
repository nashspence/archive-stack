from __future__ import annotations

from typing import Any, Literal

from pydantic import Field
from riverhog_protocol import COLLECTION_UPLOAD_FILE_BATCH_MAX

from riverhog_api.schemas.archive import ArchiveCopyOut
from riverhog_api.schemas.common import RiverhogModel
from riverhog_api.schemas.provenance import FileProvenanceBinding


class CollectionUploadFileIn(RiverhogModel):
    path: str
    bytes: int
    sha256: str
    raw_parts: CollectionUploadRawPartsIn | None = None
    provenance: FileProvenanceBinding


class CollectionUploadRawPartsIn(RiverhogModel):
    part_plaintext_bytes: int = Field(ge=65536)
    sha256s: list[str] = Field(min_length=1)


class CreateOrResumeCollectionUploadSessionRequest(RiverhogModel):
    idempotency_key: str
    tags: list[str]
    ingest_source: str | None = None
    archive_store: str | None = None
    event_context: dict[str, Any] | None = None
    provenance_mode: Literal["captured", "omitted"] = "captured"
    provenance_omission_reason: str | None = None


class RegisterCollectionUploadSessionFilesRequest(RiverhogModel):
    files: list[CollectionUploadFileIn] = Field(
        min_length=1,
        max_length=COLLECTION_UPLOAD_FILE_BATCH_MAX,
    )


class CompleteCollectionUploadSessionRequest(RiverhogModel):
    files_total: int = Field(ge=1)
    content_identity: str = Field(pattern=r"^[0-9a-f]{64}$")
    provenance_identity: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")


class CollectionSummaryOut(RiverhogModel):
    id: int
    created_at: str
    tags: list[str]
    content_identity: str = Field(pattern=r"^[0-9a-f]{64}$")
    manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
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
    sort: str
    order: Literal["asc", "desc"]
    query: str | None
    tag: str | None
    encryption_format: str | None
    passphrase_id: str | None
    collections: list[CollectionSummaryOut]


class CollectionDeletionArchiveCopyOut(RiverhogModel):
    store: str
    objects: int
    stored_bytes: int


class CollectionDeletionPlanOut(RiverhogModel):
    status: Literal["ready", "blocked", "deleting"]
    collection_id: int
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
    retirement_claim: dict[str, Any] | None = None
    blockers: list[str]
    billing_note: str


class DeleteCollectionRequest(RiverhogModel):
    challenge: str
    retirement_claim_id: str | None = Field(default=None, min_length=1, max_length=64)
    event_context: dict[str, Any] | None = None


class CollectionDeletionResultOut(RiverhogModel):
    status: Literal["deleted", "already_absent"]
    collection_id: int
    files: int
    bytes: int
    remote_storage_bytes: int


class CollectionUploadFileOut(RiverhogModel):
    path: str
    bytes: int
    sha256: str
    upload_state: str
    uploaded_bytes: int
    upload_state_expires_at: str | None
    provenance: FileProvenanceBinding


class CollectionUploadProvenanceJournalOut(RiverhogModel):
    journal_id: str
    bytes: int
    sha256: str
    current_state_id: str
    current_path: str
    current_bytes: int
    current_sha256: str


class CollectionUploadSessionFilesRegistrationOut(RiverhogModel):
    collection_id: int
    ingest_source: str | None
    archive_store: str
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
    collection_id: int
    created_at: str | None
    tags: list[str]
    ingest_source: str | None
    archive_store: str
    encryption_format: str
    passphrase_id: str = Field(pattern=r"^[A-Za-z0-9_-]{16,128}$")
    state: Literal["open", "uploading", "finalizing", "failed"]
    files: int
    bytes: int
    uploaded_bytes: int


class CollectionUploadListFiltersOut(RiverhogModel):
    tag: str | None
    state: str | None


class ListCollectionUploadSessionsResponse(RiverhogModel):
    page: int
    per_page: int
    total: int
    pages: int
    sort: str
    order: Literal["asc", "desc"]
    query: str | None
    filters: CollectionUploadListFiltersOut
    uploads: list[CollectionUploadListItemOut]


class CollectionUploadLayoutOut(RiverhogModel):
    pack_source_bytes: int
    pack_files: int
    pack_member_bytes: int
    pack_part_plaintext_bytes: int
    raw_volume_plaintext_bytes: int
    raw_part_plaintext_bytes: int


class CollectionUploadSessionOut(RiverhogModel):
    collection_id: int
    created_at: str
    tags: list[str]
    ingest_source: str | None
    provenance_mode: Literal["captured", "mixed", "omitted"]
    provenance_identity: str | None
    content_identity: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    manifest_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    archive_store: str
    encryption_format: str
    passphrase_id: str = Field(pattern=r"^[A-Za-z0-9_-]{16,128}$")
    state: Literal["open", "uploading", "finalizing", "finalized", "failed", "canceled"]
    layout: CollectionUploadLayoutOut | None
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
    collection_id: int
    volumes: list[CollectionUploadVolumeOut]
