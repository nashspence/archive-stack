from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import PurePosixPath

from riverhog_core.domain.enums import (
    CopyState,
    FetchState,
    GlacierState,
    ProtectionState,
    RecoveryCoverageState,
    RecoverySessionState,
    VerificationState,
)
from riverhog_core.domain.types import CollectionId, CopyId, FetchId, ImageId, Sha256Hex, TargetStr


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
class GlacierArchiveStatus:
    state: GlacierState = GlacierState.PENDING
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
class GlacierUsageTotals:
    collections: int
    uploaded_collections: int
    measured_storage_bytes: int


@dataclass(frozen=True)
class GlacierUsageImage:
    id: ImageId
    filename: str
    collection_ids: list[str]


@dataclass(frozen=True)
class GlacierCollectionContribution:
    image_id: ImageId
    filename: str
    represented_bytes: int


@dataclass(frozen=True)
class GlacierUsageCollection:
    id: CollectionId
    bytes: int
    measured_storage_bytes: int
    images: tuple[GlacierCollectionContribution, ...] = ()
    glacier: GlacierArchiveStatus = field(default_factory=GlacierArchiveStatus)
    collection_manifest: CollectionManifestStatus | None = None
    archive_format: str | None = None
    compression: str | None = None


@dataclass(frozen=True)
class GlacierUsageSnapshot:
    captured_at: str
    uploaded_collections: int
    measured_storage_bytes: int


@dataclass(frozen=True)
class GlacierUsageReport:
    scope: str
    measured_at: str
    totals: GlacierUsageTotals
    images: tuple[GlacierUsageImage, ...]
    collections: tuple[GlacierUsageCollection, ...]
    history: tuple[GlacierUsageSnapshot, ...] = ()


@dataclass(frozen=True)
class RecoveryNotificationStatus:
    webhook_configured: bool
    reminder_count: int
    next_reminder_at: str | None
    last_notified_at: str | None


@dataclass(frozen=True)
class RecoverySessionProgress:
    archive_verification: str = "pending"
    extraction: str = "pending"
    materialization: str = "pending"


@dataclass(frozen=True)
class RecoverySessionImage:
    id: ImageId
    filename: str
    collection_ids: tuple[CollectionId, ...] = ()
    rebuild_state: str = "pending"


@dataclass(frozen=True)
class RecoverySessionCollection:
    id: CollectionId
    glacier: GlacierArchiveStatus
    collection_manifest: CollectionManifestStatus | None
    stored_bytes: int


@dataclass(frozen=True)
class RecoverySessionSummary:
    id: str
    type: str
    state: RecoverySessionState
    created_at: str
    approved_at: str | None
    restore_requested_at: str | None
    restore_ready_at: str | None
    restore_expires_at: str | None
    completed_at: str | None
    latest_message: str | None
    warnings: tuple[str, ...]
    notification: RecoveryNotificationStatus
    progress: RecoverySessionProgress
    collections: tuple[RecoverySessionCollection, ...]
    images: tuple[RecoverySessionImage, ...]


@dataclass(frozen=True)
class CollectionCoverageImage:
    id: ImageId
    filename: str
    protection_state: ProtectionState
    physical_copies_required: int
    physical_copies_registered: int
    physical_copies_verified: int
    physical_copies_missing: int
    covered_paths: list[str]
    copies: list[CopySummary]


@dataclass(frozen=True)
class RecoveryCoverage:
    state: RecoveryCoverageState
    bytes: int


@dataclass(frozen=True)
class CollectionRecoverySummary:
    verified_physical: RecoveryCoverage
    glacier: RecoveryCoverage
    available: tuple[str, ...] = ()


@dataclass(frozen=True)
class CollectionSummary:
    id: CollectionId
    files: int
    bytes: int
    hot_bytes: int
    archived_bytes: int
    protection_state: ProtectionState = ProtectionState.UNPROTECTED
    protected_bytes: int = 0
    recovery: CollectionRecoverySummary = field(
        default_factory=lambda: CollectionRecoverySummary(
            verified_physical=RecoveryCoverage(
                state=RecoveryCoverageState.NONE,
                bytes=0,
            ),
            glacier=RecoveryCoverage(
                state=RecoveryCoverageState.NONE,
                bytes=0,
            ),
            available=(),
        )
    )
    image_coverage: list[CollectionCoverageImage] = field(default_factory=list)
    glacier: GlacierArchiveStatus = field(default_factory=GlacierArchiveStatus)
    collection_manifest: CollectionManifestStatus | None = None
    archive_format: str | None = None
    compression: str | None = None

    @property
    def pending_bytes(self) -> int:
        return self.bytes - self.archived_bytes


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
    protection_state: ProtectionState
    physical_copies_required: int
    physical_copies_registered: int
    physical_copies_verified: int
    physical_copies_missing: int
    glacier: GlacierArchiveStatus


@dataclass(frozen=True)
class CopyHistoryEntry:
    at: str
    event: str
    state: CopyState
    verification_state: VerificationState
    location: str | None


@dataclass(frozen=True)
class CopySummary:
    id: CopyId
    volume_id: str
    label_text: str
    location: str | None
    created_at: str
    state: CopyState = CopyState.REGISTERED
    verification_state: VerificationState = VerificationState.PENDING
    history: tuple[CopyHistoryEntry, ...] = ()


@dataclass(frozen=True)
class FetchCopyHint:
    id: CopyId
    volume_id: str
    location: str


@dataclass(frozen=True)
class FetchSummary:
    id: FetchId
    target: TargetStr
    state: FetchState
    files: int
    bytes: int
    copies: list[FetchCopyHint]
    entries_total: int = 0
    entries_pending: int = 0
    entries_partial: int = 0
    entries_byte_complete: int = 0
    entries_uploaded: int = 0
    uploaded_bytes: int = 0
    missing_bytes: int = 0
    upload_state_expires_at: str | None = None


@dataclass(frozen=True)
class PinSummary:
    target: TargetStr
    fetch: FetchSummary


@dataclass(frozen=True)
class FileRef:
    collection_id: CollectionId
    path: str
    bytes: int
    sha256: Sha256Hex
    copies: list[FetchCopyHint]
