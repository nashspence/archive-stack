from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import PurePosixPath

from riverhog_core.domain.enums import (
    ArchiveRestoreState,
    ArchiveState,
    CoverageState,
    DiscState,
    FetchState,
    VerificationState,
)
from riverhog_core.domain.types import CollectionId, DiscId, FetchId, ImageId, Sha256Hex, TargetStr


@dataclass(frozen=True)
class Target:
    path: PurePosixPath
    is_dir: bool

    @property
    def canonical(self) -> str:
        canonical = str(self.path)
        if self.is_dir:
            canonical += "/"
        return canonical


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
class ArchiveUsageImage:
    id: ImageId
    filename: str
    collection_ids: list[str]


@dataclass(frozen=True)
class ArchiveCollectionContribution:
    image_id: ImageId
    filename: str
    represented_bytes: int


@dataclass(frozen=True)
class ArchiveUsageCollection:
    id: CollectionId
    bytes: int
    measured_storage_bytes: int
    images: tuple[ArchiveCollectionContribution, ...] = ()
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
    images: tuple[ArchiveUsageImage, ...]
    collections: tuple[ArchiveUsageCollection, ...]
    history: tuple[ArchiveUsageSnapshot, ...] = ()


@dataclass(frozen=True)
class ArchiveRestoreNotificationStatus:
    webhook_configured: bool
    reminder_count: int
    next_reminder_at: str | None
    last_notified_at: str | None
    failure_count: int = 0
    last_failure_at: str | None = None
    last_failure: str | None = None


@dataclass(frozen=True)
class ArchiveRestoreProgress:
    archive_verification: str = "pending"
    extraction: str = "pending"
    materialization: str = "pending"


@dataclass(frozen=True)
class ArchiveRestoreImage:
    id: ImageId
    filename: str
    collection_ids: tuple[CollectionId, ...] = ()
    rebuild_state: str = "pending"


@dataclass(frozen=True)
class ArchiveRestoreCollection:
    id: CollectionId
    archive: ArchiveStatus
    collection_manifest: CollectionManifestStatus | None
    stored_bytes: int


@dataclass(frozen=True)
class ArchiveRestoreSummary:
    id: str
    type: str
    state: ArchiveRestoreState
    created_at: str
    requested_at: str | None
    ready_at: str | None
    expires_at: str | None
    completed_at: str | None
    canceled_at: str | None
    paused_at: str | None
    paused_from_state: str | None
    paths: tuple[str, ...] | None
    latest_message: str | None
    warnings: tuple[str, ...]
    notification: ArchiveRestoreNotificationStatus
    progress: ArchiveRestoreProgress
    collections: tuple[ArchiveRestoreCollection, ...]
    images: tuple[ArchiveRestoreImage, ...]


@dataclass(frozen=True)
class ArchiveRestoreListPage:
    page: int
    per_page: int
    total: int
    pages: int
    sort: str
    order: str
    terminal: str
    type: str | None
    state: str | None
    collection: str | None
    image: str | None
    restores: list[ArchiveRestoreSummary]


@dataclass(frozen=True)
class CollectionCoverageImage:
    id: ImageId
    filename: str
    disc_redundancy_state: CoverageState
    discs_required: int
    discs_registered: int
    discs_verified: int
    discs_missing: int
    covered_paths: list[str]
    discs: list[DiscSummary]
    covered_paths_total: int | None = None


@dataclass(frozen=True)
class Coverage:
    state: CoverageState
    bytes: int


@dataclass(frozen=True)
class CollectionSummary:
    id: CollectionId
    files: int
    bytes: int
    hot_bytes: int
    disc_coverage: Coverage = field(
        default_factory=lambda: Coverage(state=CoverageState.NONE, bytes=0)
    )
    disc_redundancy: Coverage = field(
        default_factory=lambda: Coverage(state=CoverageState.NONE, bytes=0)
    )
    image_coverage: list[CollectionCoverageImage] = field(default_factory=list)
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
class ImageSummary:
    id: ImageId
    filename: str
    finalized_at: str
    bytes: int
    fill: float
    files: int
    collections: int
    collection_ids: list[str]
    iso_ready: bool
    disc_redundancy_state: CoverageState
    discs_required: int
    discs_registered: int
    discs_verified: int
    discs_missing: int
    archive: ArchiveStatus


@dataclass(frozen=True)
class DiscHistoryEntry:
    at: str
    event: str
    state: DiscState
    verification_state: VerificationState
    location: str | None


@dataclass(frozen=True)
class DiscSummary:
    disc_id: DiscId
    image_id: str
    label_text: str
    location: str | None
    created_at: str
    state: DiscState = DiscState.REGISTERED
    verification_state: VerificationState = VerificationState.PENDING
    history: tuple[DiscHistoryEntry, ...] = ()


@dataclass(frozen=True)
class FetchDiscHint:
    disc_id: DiscId
    image_id: str
    location: str


@dataclass(frozen=True)
class FetchSummary:
    id: FetchId
    name: str
    targets: tuple[TargetStr, ...]
    state: FetchState
    files: int
    bytes: int
    discs: list[FetchDiscHint]
    entries_total: int = 0
    entries_pending: int = 0
    entries_partial: int = 0
    entries_byte_complete: int = 0
    entries_uploaded: int = 0
    uploaded_bytes: int = 0
    missing_bytes: int = 0
    upload_state_expires_at: str | None = None


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
    discs: list[FetchDiscHint]
