from __future__ import annotations

from dataclasses import dataclass

from riverhog_core.domain.enums import ArchiveState
from riverhog_core.domain.types import CollectionId, Sha256Hex


@dataclass(frozen=True)
class ArchiveCopyStatus:
    store: str
    state: ArchiveState = ArchiveState.PENDING
    storage_prefix: str | None = None
    object_count: int = 0
    stored_bytes: int | None = None
    last_uploaded_at: str | None = None
    last_verified_at: str | None = None
    failure: str | None = None
    archive_root: ArchiveRootPublicationStatus | None = None


@dataclass(frozen=True)
class ArchiveRootPublicationStatus:
    object_path: str | None = None
    sha256: str | None = None
    proof_object_path: str | None = None
    proof_state: str = "pending"
    proof_sha256: str | None = None


@dataclass(frozen=True)
class ArchiveDownloadAllowance:
    store: str
    state: str
    month_started_at: str
    resets_at: str
    allowance_bytes: int
    safety_buffer_bytes: int
    effective_limit_bytes: int
    accounted_bytes: int
    reserved_bytes: int
    remaining_bytes: int


@dataclass(frozen=True)
class ArchiveStoreSummary:
    store: str
    read_mode: str
    read_priority: int
    write_target: bool
    collections: int
    objects: int
    stored_bytes: int
    download_allowance: ArchiveDownloadAllowance | None = None


@dataclass(frozen=True)
class ArchiveStoreListPage:
    page: int
    per_page: int
    total: int
    pages: int
    sort: str
    order: str
    query: str | None
    stores: list[ArchiveStoreSummary]


@dataclass(frozen=True)
class CollectionSummary:
    id: CollectionId
    created_at: str
    tag_count: int
    content_identity: str
    archive_root_sha256: str
    encryption_format: str
    passphrase_id: str
    files: int
    bytes: int
    remote_storage_bytes: int = 0
    archive_copy_count: int = 0


@dataclass(frozen=True)
class CollectionListPage:
    page: int
    per_page: int
    total: int
    pages: int
    sort: str
    order: str
    query: str | None
    tag: str | None
    encryption_format: str | None
    passphrase_id: str | None
    collections: list[CollectionSummary]


@dataclass(frozen=True)
class FileRef:
    collection_id: CollectionId
    path: str
    bytes: int
    sha256: Sha256Hex
