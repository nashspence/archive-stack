from __future__ import annotations

from dataclasses import dataclass, field

from riverhog_core.domain.enums import ArchiveRestoreState, ArchiveState, FetchState
from riverhog_core.domain.types import CollectionId, FetchId, Sha256Hex


@dataclass(frozen=True)
class ArchiveStatus:
    state: ArchiveState = ArchiveState.PENDING
    object_path: str | None = None
    stored_bytes: int | None = None
    backend: str | None = None
    storage_class: str | None = None
    last_uploaded_at: str | None = None
    last_verified_at: str | None = None
    failure: str | None = None


@dataclass(frozen=True)
class CollectionManifestStatus:
    object_path: str | None = None
    sha256: str | None = None
    ots_object_path: str | None = None
    ots_state: str = "pending"
    ots_sha256: str | None = None


@dataclass(frozen=True)
class ArchiveUsageTotals:
    collections: int
    uploaded_collections: int
    measured_storage_bytes: int


@dataclass(frozen=True)
class ArchiveUsageCollection:
    id: CollectionId
    bytes: int
    measured_storage_bytes: int
    archive: ArchiveStatus = field(default_factory=ArchiveStatus)
    collection_manifest: CollectionManifestStatus | None = None
    archive_format: str | None = None
    compression: str | None = None


@dataclass(frozen=True)
class ArchiveUsageSnapshot:
    captured_at: str
    uploaded_collections: int
    measured_storage_bytes: int


@dataclass(frozen=True)
class ArchiveUsageReport:
    scope: str
    measured_at: str
    totals: ArchiveUsageTotals
    collections: tuple[ArchiveUsageCollection, ...]
    history: tuple[ArchiveUsageSnapshot, ...] = ()


@dataclass(frozen=True)
class ArchiveRestoreNotificationStatus:
    webhook_configured: bool
    failure_count: int = 0
    last_failure_at: str | None = None
    last_failure: str | None = None


@dataclass(frozen=True)
class ArchiveRestoreProgress:
    archive_verification: str = "pending"
    extraction: str = "pending"
    materialization: str = "pending"


@dataclass(frozen=True)
class ArchiveRestoreCollection:
    id: CollectionId
    archive: ArchiveStatus
    collection_manifest: CollectionManifestStatus | None
    stored_bytes: int


@dataclass(frozen=True)
class ArchiveRestoreSummary:
    id: str
    state: ArchiveRestoreState
    created_at: str
    requested_at: str | None
    ready_at: str | None
    expires_at: str | None
    completed_at: str | None
    canceled_at: str | None
    latest_message: str | None
    warnings: tuple[str, ...]
    notification: ArchiveRestoreNotificationStatus
    progress: ArchiveRestoreProgress
    collections: tuple[ArchiveRestoreCollection, ...]


@dataclass(frozen=True)
class ArchiveRestoreListPage:
    page: int
    per_page: int
    total: int
    pages: int
    sort: str
    order: str
    terminal: str
    state: str | None
    collection: str | None
    restores: list[ArchiveRestoreSummary]


@dataclass(frozen=True)
class CollectionSummary:
    id: CollectionId
    files: int
    bytes: int
    hot_files: int
    hot_bytes: int
    archive: ArchiveStatus = field(default_factory=ArchiveStatus)
    collection_manifest: CollectionManifestStatus | None = None
    archive_format: str | None = None
    compression: str | None = None


@dataclass(frozen=True)
class CollectionListPage:
    page: int
    per_page: int
    total: int
    pages: int
    collections: list[CollectionSummary]


@dataclass(frozen=True)
class FetchSummary:
    id: FetchId
    name: str
    collections: tuple[CollectionId, ...]
    state: FetchState
    files: int
    bytes: int
    hot_files: int = 0
    hot_bytes: int = 0
    missing_files: int = 0
    missing_bytes: int = 0


@dataclass(frozen=True)
class FetchListPage:
    page: int
    per_page: int
    total: int
    pages: int
    fetches: list[FetchSummary]


@dataclass(frozen=True)
class FileRef:
    collection_id: CollectionId
    path: str
    bytes: int
    sha256: Sha256Hex
