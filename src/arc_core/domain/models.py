from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import PurePosixPath

from arc_core.domain.enums import (
    CopyState,
    FetchState,
    GlacierState,
    ProtectionState,
    RecoverySessionState,
    VerificationState,
)
from arc_core.domain.types import CollectionId, CopyId, FetchId, ImageId, Sha256Hex, TargetStr


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
class GlacierPricingBasis:
    label: str
    storage_class: str
    glacier_storage_rate_usd_per_gib_month: float
    standard_storage_rate_usd_per_gib_month: float
    archived_metadata_bytes_per_object: int
    standard_metadata_bytes_per_object: int
    minimum_storage_duration_days: int
    source: str = "manual"
    currency_code: str | None = None
    region_code: str | None = None
    effective_at: str | None = None
    price_list_arn: str | None = None


@dataclass(frozen=True)
class GlacierUsageTotals:
    images: int
    uploaded_images: int
    measured_storage_bytes: int
    estimated_billable_bytes: int
    estimated_monthly_cost_usd: float


@dataclass(frozen=True)
class GlacierUsageImage:
    id: ImageId
    filename: str
    collection_ids: list[str]
    glacier: GlacierArchiveStatus
    measured_storage_bytes: int
    estimated_billable_bytes: int
    estimated_monthly_cost_usd: float


@dataclass(frozen=True)
class GlacierCollectionContribution:
    image_id: ImageId
    filename: str
    glacier: GlacierArchiveStatus
    represented_bytes: int
    represented_fraction: float | None
    derived_stored_bytes: int | None
    derived_billable_bytes: int | None
    estimated_monthly_cost_usd: float | None


@dataclass(frozen=True)
class GlacierUsageCollection:
    id: CollectionId
    bytes: int
    represented_bytes: int
    attribution_state: str
    derived_stored_bytes: int
    derived_billable_bytes: int
    estimated_monthly_cost_usd: float
    images: tuple[GlacierCollectionContribution, ...] = ()


@dataclass(frozen=True)
class GlacierUsageSnapshot:
    captured_at: str
    uploaded_images: int
    measured_storage_bytes: int
    estimated_billable_bytes: int
    estimated_monthly_cost_usd: float


@dataclass(frozen=True)
class GlacierBillingActual:
    start: str
    end: str
    estimated: bool
    unblended_cost_usd: float
    usage_quantity: float | None = None
    usage_unit: str | None = None


@dataclass(frozen=True)
class GlacierBillingForecast:
    start: str
    end: str
    mean_cost_usd: float
    lower_bound_cost_usd: float | None = None
    upper_bound_cost_usd: float | None = None
    currency_code: str | None = None


@dataclass(frozen=True)
class GlacierBillingActualsView:
    source: str
    scope: str
    filter_label: str | None = None
    service: str | None = None
    billing_view_arn: str | None = None
    granularity: str | None = None
    measured_at: str | None = None
    periods: tuple[GlacierBillingActual, ...] = ()
    notes: tuple[str, ...] = ()


@dataclass(frozen=True)
class GlacierBillingForecastView:
    source: str
    scope: str
    filter_label: str | None = None
    service: str | None = None
    currency_code: str | None = None
    granularity: str | None = None
    periods: tuple[GlacierBillingForecast, ...] = ()
    notes: tuple[str, ...] = ()


@dataclass(frozen=True)
class GlacierBillingExportBreakdown:
    usage_type: str | None
    operation: str | None
    resource_id: str | None
    tag_value: str | None
    unblended_cost_usd: float
    usage_quantity: float | None = None
    usage_unit: str | None = None


@dataclass(frozen=True)
class GlacierBillingExportView:
    source: str
    scope: str
    filter_label: str | None = None
    service: str | None = None
    export_arn: str | None = None
    export_name: str | None = None
    execution_id: str | None = None
    manifest_key: str | None = None
    billing_period: str | None = None
    bucket: str | None = None
    prefix: str | None = None
    object_key: str | None = None
    exported_at: str | None = None
    currency_code: str | None = None
    files_read: int = 0
    rows_scanned: int = 0
    breakdowns: tuple[GlacierBillingExportBreakdown, ...] = ()
    notes: tuple[str, ...] = ()


@dataclass(frozen=True)
class GlacierBillingInvoiceSummary:
    invoice_id: str | None
    account_id: str | None
    billing_period_start: str | None
    billing_period_end: str | None
    invoice_type: str | None
    invoicing_entity: str | None
    issued_at: str | None
    due_at: str | None
    base_currency_code: str | None = None
    base_total_amount: float | None = None
    payment_currency_code: str | None = None
    payment_total_amount: float | None = None
    original_invoice_id: str | None = None


@dataclass(frozen=True)
class GlacierBillingInvoicesView:
    source: str
    scope: str
    account_id: str | None = None
    invoices: tuple[GlacierBillingInvoiceSummary, ...] = ()
    notes: tuple[str, ...] = ()


@dataclass(frozen=True)
class GlacierBillingSummary:
    actuals: GlacierBillingActualsView | None = None
    forecast: GlacierBillingForecastView | None = None
    exports: GlacierBillingExportView | None = None
    invoices: GlacierBillingInvoicesView | None = None
    notes: tuple[str, ...] = ()


@dataclass(frozen=True)
class GlacierUsageReport:
    scope: str
    measured_at: str
    pricing_basis: GlacierPricingBasis
    totals: GlacierUsageTotals
    images: tuple[GlacierUsageImage, ...]
    collections: tuple[GlacierUsageCollection, ...]
    history: tuple[GlacierUsageSnapshot, ...] = ()
    billing: GlacierBillingSummary | None = None


@dataclass(frozen=True)
class GlacierReportingContext:
    pricing_basis: GlacierPricingBasis
    billing: GlacierBillingSummary | None = None


@dataclass(frozen=True)
class RecoveryCostEstimate:
    currency_code: str
    retrieval_tier: str
    hold_days: int
    image_count: int
    total_bytes: int
    restore_request_count: int
    retrieval_rate_usd_per_gib: float
    request_rate_usd_per_1000: float
    standard_storage_rate_usd_per_gib_month: float
    retrieval_cost_usd: float
    request_fees_usd: float
    temporary_storage_cost_usd: float
    total_estimated_cost_usd: float
    assumptions: tuple[str, ...] = ()


@dataclass(frozen=True)
class RecoveryNotificationStatus:
    webhook_configured: bool
    reminder_count: int
    next_reminder_at: str | None
    last_notified_at: str | None


@dataclass(frozen=True)
class RecoverySessionImage:
    id: ImageId
    filename: str
    glacier: GlacierArchiveStatus
    stored_bytes: int


@dataclass(frozen=True)
class RecoverySessionSummary:
    id: str
    state: RecoverySessionState
    created_at: str
    approved_at: str | None
    restore_requested_at: str | None
    restore_ready_at: str | None
    restore_expires_at: str | None
    completed_at: str | None
    latest_message: str | None
    warnings: tuple[str, ...]
    cost_estimate: RecoveryCostEstimate
    notification: RecoveryNotificationStatus
    images: tuple[RecoverySessionImage, ...]


@dataclass(frozen=True)
class CollectionCoverageImage:
    id: ImageId
    filename: str
    protection_state: ProtectionState
    physical_copies_required: int
    physical_copies_registered: int
    physical_copies_missing: int
    copies: list[CopySummary]
    glacier: GlacierArchiveStatus


@dataclass(frozen=True)
class CollectionSummary:
    id: CollectionId
    files: int
    bytes: int
    hot_bytes: int
    archived_bytes: int
    protection_state: ProtectionState = ProtectionState.UNPROTECTED
    protected_bytes: int = 0
    image_coverage: list[CollectionCoverageImage] = field(default_factory=list)

    @property
    def pending_bytes(self) -> int:
        return self.bytes - self.archived_bytes


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
