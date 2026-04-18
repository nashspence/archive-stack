from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field, HttpUrl


class JobCreateRequest(BaseModel):
    root_node_name: str
    description: str | None = None
    keep_buffer_after_archive: bool = False


class JobCreateResponse(BaseModel):
    job_id: str
    status: str
    keep_buffer_after_archive: bool


class JobDirectoryCreateRequest(BaseModel):
    relative_path: str


class UploadSlotCreateRequest(BaseModel):
    relative_path: str
    size_bytes: int = Field(gt=0)
    sha256: str | None = Field(default=None, min_length=64, max_length=64)
    mode: str = Field(default="0644")
    mtime: str
    uid: int | None = Field(default=None, ge=0)
    gid: int | None = Field(default=None, ge=0)


class UploadSlotCreateResponse(BaseModel):
    upload_id: str
    upload_token: str
    tus_create_url: str
    tus_metadata: dict[str, str]
    upload_stream_url: str
    aggregate_stream_url: str


class TreeNode(BaseModel):
    path: str
    kind: str
    size_bytes: int | None = None
    online: bool
    source: str | None = None
    disc_ids: list[str] = Field(default_factory=list)
    status: str | None = None
    extra: dict[str, Any] | None = None


class TreeResponse(BaseModel):
    root_id: str
    root_kind: str
    nodes: list[TreeNode]


class OfflineError(BaseModel):
    error: str
    message: str
    disc_ids: list[str] = Field(default_factory=list)


class SealJobResponse(BaseModel):
    job_id: str
    status: str
    closed_discs: list[str]
    buffer_bytes: int


class CacheSessionCreateResponse(BaseModel):
    session_id: str
    disc_id: str
    expected_total_bytes: int
    expected_files: int
    progress_stream_url: str


class CacheUploadSlotRequest(BaseModel):
    relative_path: str


class CacheSessionCompleteResponse(BaseModel):
    disc_id: str
    session_id: str
    status: str
    contents_hash: str


class IsoRegisterRequest(BaseModel):
    server_path: str


class IsoCreateRequest(BaseModel):
    volume_label: str | None = None
    overwrite: bool = False


class IsoCreateResponse(BaseModel):
    disc_id: str
    iso_path: str
    size_bytes: int


class BurnConfirmResponse(BaseModel):
    disc_id: str
    burn_confirmed_at: str
    released_job_ids: list[str] = Field(default_factory=list)


class DownloadSessionCreateResponse(BaseModel):
    session_id: str
    disc_id: str
    total_bytes: int
    progress_stream_url: str
    content_url: str


class DiscFinalizationWebhookCreateRequest(BaseModel):
    webhook_url: HttpUrl
    reminder_interval_seconds: int | None = Field(default=None, gt=0)


class DiscFinalizationWebhookCreateResponse(BaseModel):
    subscription_id: str
    webhook_url: str
    reminder_interval_seconds: int | None = None
    pending_disc_count: int
