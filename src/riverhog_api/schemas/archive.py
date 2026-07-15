from __future__ import annotations

from typing import Literal

from riverhog_api.schemas.common import RiverhogModel


class ArchiveCopyOut(RiverhogModel):
    store: str
    state: Literal["pending", "uploading", "uploaded", "retrying", "failed"]
    object_path: str | None
    stored_bytes: int | None
    backend: str | None
    storage_class: str | None
    last_uploaded_at: str | None
    last_verified_at: str | None
    failure: str | None
    collection_manifest: CollectionManifestOut | None = None
    archive_format: str | None = None
    compression: str | None = None


class CollectionManifestOut(RiverhogModel):
    object_path: str | None = None
    sha256: str | None = None
    ots_object_path: str | None = None
    ots_sha256: str | None = None
    ots_state: Literal["pending", "uploaded", "failed"] = "pending"


class CreateArchiveCopyRequest(RiverhogModel):
    collection_id: str
    destination_store: str
    source_store: str | None = None


class ArchiveCopyJobOut(RiverhogModel):
    collection_id: str
    source_store: str | None
    destination_store: str
    state: Literal["requested", "waiting", "copying", "completed", "failed"]
    requested_at: str | None
    ready_at: str | None
    expires_at: str | None
    failure: str | None


class ArchiveCopyRetirementRequest(RiverhogModel):
    collection_id: str
    store: str


class RetireArchiveCopyRequest(ArchiveCopyRetirementRequest):
    challenge: str


class ArchiveCopyRetirementObjectOut(RiverhogModel):
    kind: Literal["archive", "manifest", "proof"]
    object_path: str
    stored_bytes: int


class ArchiveCopyRetirementTargetOut(RiverhogModel):
    store: str
    last_verified_at: str
    remote_storage_bytes: int
    objects: list[ArchiveCopyRetirementObjectOut]


class ArchiveCopyRetirementRetainedOut(RiverhogModel):
    store: str
    last_verified_at: str
    remote_storage_bytes: int


class ArchiveCopyRetirementPlanOut(RiverhogModel):
    status: Literal["ready", "blocked", "retiring"]
    collection_id: str
    store: str
    warning: str
    expires_at: str
    challenge: str | None
    target_copy: ArchiveCopyRetirementTargetOut
    retained_copies: list[ArchiveCopyRetirementRetainedOut]
    retired_restore_records: list[str]
    blockers: list[str]
    verification_note: str
    billing_note: str


class ArchiveCopyRetirementResultOut(RiverhogModel):
    status: Literal["retired", "already_absent"]
    collection_id: str
    store: str
    remote_storage_bytes: int
    verified_store: str | None
