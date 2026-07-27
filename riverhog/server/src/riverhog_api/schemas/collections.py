from __future__ import annotations

from typing import Any, Literal

from pydantic import Field

from riverhog_api.schemas.archive import ArchiveCopyOut, ArchiveOwnedObjectKind
from riverhog_api.schemas.common import RiverhogModel


class CollectionUploadFileIn(RiverhogModel):
    path: str
    bytes: int
    sha256: str


class CreateOrResumeCollectionUploadSessionRequest(RiverhogModel):
    idempotency_key: str
    tags: list[str]
    ingest_source: str | None = None
    archive_store: str | None = None
    event_context: dict[str, Any] | None = None


class RegisterCollectionUploadSessionFilesRequest(RiverhogModel):
    files: list[CollectionUploadFileIn] = Field(min_length=1, max_length=100)


class CompleteCollectionUploadSessionRequest(RiverhogModel):
    files_total: int = Field(ge=1)
    content_etag: str = Field(pattern=r"^[0-9a-f]{64}$")


class CollectionSummaryOut(RiverhogModel):
    id: int
    created_at: str
    tags: list[str]
    files: int
    bytes: int
    archive_copies: list[ArchiveCopyOut]


class ListCollectionsResponse(RiverhogModel):
    page: int
    per_page: int
    total: int
    pages: int
    collections: list[CollectionSummaryOut]


class CollectionDeletionFileOut(RiverhogModel):
    path: str
    bytes: int


class CollectionDeletionObjectOut(RiverhogModel):
    store: str
    kind: ArchiveOwnedObjectKind
    object_path: str
    stored_bytes: int


class CollectionDeletionUploadFileOut(RiverhogModel):
    path: str
    bytes: int


class CollectionDeletionPlanOut(RiverhogModel):
    status: Literal["ready", "blocked", "deleting"]
    collection_id: int
    warning: str
    expires_at: str
    challenge: str | None
    files: list[CollectionDeletionFileOut]
    file_count: int
    bytes: int
    archive_objects: list[CollectionDeletionObjectOut]
    remote_storage_bytes: int
    upload_files: list[CollectionDeletionUploadFileOut]
    record_etag: str
    metadata_rows: dict[str, int]
    blockers: list[str]
    billing_note: str


class DeleteCollectionRequest(RiverhogModel):
    challenge: str
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


class CollectionUploadSessionFilesRegistrationOut(RiverhogModel):
    collection_id: int
    ingest_source: str | None
    archive_store: str
    state: Literal["open", "uploading"]
    files: list[CollectionUploadFileOut]


class ListCollectionUploadSessionFilesResponse(RiverhogModel):
    page: int
    per_page: int
    total: int
    pages: int
    files: list[CollectionUploadFileOut]


class CollectionUploadSessionOut(RiverhogModel):
    collection_id: int
    created_at: str
    tags: list[str]
    ingest_source: str | None
    archive_store: str
    state: Literal["open", "uploading", "archiving", "finalized", "failed", "canceled", "expired"]
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


class CollectionUploadEncryptionOut(RiverhogModel):
    format: Literal["age-v1-scrypt-resumable"]
    passphrase: str = Field(json_schema_extra={"writeOnly": True})
    state: dict[str, object]
    plaintext_bytes: int
    ciphertext_bytes: int
    chunk_bytes: int


class CollectionFileUploadSessionOut(RiverhogModel):
    path: str
    protocol: str
    upload_url: str
    offset: int
    length: int
    checksum_algorithm: str
    expires_at: str | None
    encryption: CollectionUploadEncryptionOut


class CollectionUploadSessionFileUploadOut(CollectionFileUploadSessionOut):
    collection_id: int
    ingest_source: str | None
    archive_store: str
    state: Literal["open", "uploading"]
    file: CollectionUploadFileOut
