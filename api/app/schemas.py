from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field, HttpUrl


class CollectionCreateRequest(BaseModel):
    root_node_name: str
    description: str | None = None
    keep_buffer_after_archive: bool = False


class CollectionCreateResponse(BaseModel):
    collection_id: str
    status: str
    keep_buffer_after_archive: bool


class CollectionDirectoryCreateRequest(BaseModel):
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
    active: bool
    source: str | None = None
    container_ids: list[str] = Field(default_factory=list)
    status: str | None = None
    extra: dict[str, Any] | None = None


class TreeResponse(BaseModel):
    root_id: str
    root_kind: str
    nodes: list[TreeNode]


class InactiveError(BaseModel):
    error: str
    message: str
    container_ids: list[str] = Field(default_factory=list)


class SealCollectionResponse(BaseModel):
    collection_id: str
    status: str
    closed_containers: list[str]
    buffer_bytes: int


class ActivationSessionCreateResponse(BaseModel):
    session_id: str
    container_id: str
    expected_total_bytes: int
    expected_files: int
    progress_stream_url: str


class ActivationUploadSlotRequest(BaseModel):
    relative_path: str


class ActivationSessionCompleteResponse(BaseModel):
    container_id: str
    session_id: str
    status: str
    contents_hash: str


class IsoRegisterRequest(BaseModel):
    server_path: str


class IsoCreateRequest(BaseModel):
    volume_label: str | None = None
    overwrite: bool = False


class IsoCreateResponse(BaseModel):
    container_id: str
    iso_path: str
    size_bytes: int


class BurnConfirmResponse(BaseModel):
    container_id: str
    burn_confirmed_at: str
    released_collection_ids: list[str] = Field(default_factory=list)


class DownloadSessionCreateResponse(BaseModel):
    session_id: str
    container_id: str
    total_bytes: int
    progress_stream_url: str
    content_url: str


class ContainerFinalizationWebhookCreateRequest(BaseModel):
    webhook_url: HttpUrl
    reminder_interval_seconds: int | None = Field(default=None, gt=0)


class ContainerFinalizationWebhookCreateResponse(BaseModel):
    subscription_id: str
    webhook_url: str
    reminder_interval_seconds: int | None = None
    pending_container_count: int
