from __future__ import annotations

from typing import Literal

from pydantic import Field, field_validator, model_validator

from riverhog_api.schemas.archive import ArchiveCopyOut
from riverhog_api.schemas.common import RiverhogModel


class CollectionUploadFileIn(RiverhogModel):
    path: str
    bytes: int
    sha256: str


class CollectionNotifyConfig(RiverhogModel):
    enabled: bool = True
    recipients: list[str] = Field(default_factory=list)

    @field_validator("recipients")
    @classmethod
    def normalize_recipients(cls, value: list[str]) -> list[str]:
        allowed = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.-")
        recipients: list[str] = []
        for item in value:
            recipient = str(item).strip()
            if not recipient:
                raise ValueError("notify recipients must not be blank")
            if any(ch not in allowed for ch in recipient):
                raise ValueError(
                    "notify recipients may contain only letters, digits, dots, underscores, "
                    "and dashes"
                )
            if recipient not in recipients:
                recipients.append(recipient)
        return recipients

    @model_validator(mode="after")
    def require_recipients_when_enabled(self) -> CollectionNotifyConfig:
        if self.enabled and not self.recipients:
            raise ValueError("notify.recipients is required when notifications are enabled")
        return self


class CreateOrResumeCollectionUploadRequest(RiverhogModel):
    slug: str
    files: list[CollectionUploadFileIn]
    ingest_source: str | None = None
    upload_timestamp: str | None = None
    archive_store: str | None = None
    retain_hot: bool = True
    notify: CollectionNotifyConfig | None = None


class CreateOrResumeCollectionUploadSessionRequest(RiverhogModel):
    slug: str
    ingest_source: str | None = None
    upload_timestamp: str | None = None
    archive_store: str | None = None
    retain_hot: bool = True
    notify: CollectionNotifyConfig | None = None


class RegisterCollectionUploadSessionFileRequest(CollectionUploadFileIn):
    pass


class CollectionSummaryOut(RiverhogModel):
    id: str
    files: int
    bytes: int
    hot_files: int
    hot_bytes: int
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
    hot: bool


class CollectionDeletionObjectOut(RiverhogModel):
    store: str
    kind: Literal["archive", "manifest", "proof"]
    object_path: str
    stored_bytes: int


class CollectionDeletionHotObjectOut(RiverhogModel):
    path: str
    bytes: int


class CollectionDeletionUploadFileOut(RiverhogModel):
    path: str
    bytes: int


class CollectionDeletionPlanOut(RiverhogModel):
    status: Literal["ready", "blocked", "deleting"]
    collection_id: str
    warning: str
    expires_at: str
    challenge: str | None
    files: list[CollectionDeletionFileOut]
    file_count: int
    bytes: int
    hot_objects: list[CollectionDeletionHotObjectOut]
    hot_files: int
    hot_bytes: int
    archive_objects: list[CollectionDeletionObjectOut]
    remote_storage_bytes: int
    upload_files: list[CollectionDeletionUploadFileOut]
    archive_restores: list[str]
    metadata_rows: dict[str, int]
    blockers: list[str]
    billing_note: str


class DeleteCollectionRequest(RiverhogModel):
    challenge: str


class CollectionDeletionResultOut(RiverhogModel):
    status: Literal["deleted", "already_absent"]
    collection_id: str
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


class CollectionUploadSessionFileRegistrationOut(RiverhogModel):
    collection_id: str
    ingest_source: str | None
    retain_hot: bool
    archive_store: str
    state: Literal["open", "uploading"]
    file: CollectionUploadFileOut


class CollectionUploadSessionOut(RiverhogModel):
    collection_id: str
    ingest_source: str | None
    retain_hot: bool
    archive_store: str
    state: Literal["open", "uploading", "archiving", "finalized", "failed", "canceled", "expired"]
    files_total: int
    files_pending: int
    files_partial: int
    files_uploaded: int
    hot_materialized_files: int = 0
    bytes_total: int
    uploaded_bytes: int
    hot_materialized_bytes: int = 0
    missing_bytes: int
    upload_state_expires_at: str | None
    latest_failure: str | None = None
    archive_phase: str | None = None
    archive_phase_updated_at: str | None = None
    archive_object_path: str | None = None
    archive_uploaded_bytes: int | None = None
    archive_total_bytes: int | None = None
    archive_uploaded_parts: int | None = None
    archive_total_parts: int | None = None
    notify: CollectionNotifyConfig | None = None
    files: list[CollectionUploadFileOut]
    collection: CollectionSummaryOut | None


class CollectionFileUploadSessionOut(RiverhogModel):
    path: str
    protocol: str
    upload_url: str
    offset: int
    length: int
    checksum_algorithm: str
    expires_at: str | None


class CollectionUploadSessionFileUploadOut(CollectionFileUploadSessionOut):
    collection_id: str
    ingest_source: str | None
    state: Literal["open", "uploading"]
    file: CollectionUploadFileOut
