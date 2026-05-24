from __future__ import annotations

from typing import Literal

from riverhog_api.schemas.archive import CollectionArchiveManifestOut, GlacierArchiveOut
from riverhog_api.schemas.common import RiverhogModel


class GlacierPricingBasisOut(RiverhogModel):
    label: str
    source: str
    storage_class: str
    glacier_storage_rate_usd_per_gib_month: float
    standard_storage_rate_usd_per_gib_month: float
    archived_metadata_bytes_per_object: int
    standard_metadata_bytes_per_object: int
    minimum_storage_duration_days: int
    currency_code: str | None = None
    region_code: str | None = None
    effective_at: str | None = None
    price_list_arn: str | None = None


class GlacierUsageTotalsOut(RiverhogModel):
    collections: int
    uploaded_collections: int
    measured_storage_bytes: int
    estimated_billable_bytes: int
    estimated_monthly_cost_usd: float


class GlacierUsageImageOut(RiverhogModel):
    id: str
    filename: str
    collection_ids: list[str]


class GlacierCollectionContributionOut(RiverhogModel):
    image_id: str
    filename: str
    represented_bytes: int


class GlacierUsageCollectionOut(RiverhogModel):
    id: str
    bytes: int
    glacier: GlacierArchiveOut | None = None
    archive_manifest: CollectionArchiveManifestOut | None = None
    archive_format: str | None = None
    compression: str | None = None
    measured_storage_bytes: int
    estimated_billable_bytes: int
    estimated_monthly_cost_usd: float
    images: list[GlacierCollectionContributionOut]


class GlacierUsageSnapshotOut(RiverhogModel):
    captured_at: str
    uploaded_collections: int
    measured_storage_bytes: int
    estimated_billable_bytes: int
    estimated_monthly_cost_usd: float


class GlacierBillingActualOut(RiverhogModel):
    start: str
    end: str
    estimated: bool
    unblended_cost_usd: float
    usage_quantity: float | None = None
    usage_unit: str | None = None


class GlacierBillingActualsViewOut(RiverhogModel):
    source: str
    scope: str
    filter_label: str | None = None
    service: str | None = None
    billing_view_arn: str | None = None
    granularity: str | None = None
    measured_at: str | None = None
    periods: list[GlacierBillingActualOut]
    notes: list[str]


class GlacierBillingForecastOut(RiverhogModel):
    start: str
    end: str
    mean_cost_usd: float
    lower_bound_cost_usd: float | None = None
    upper_bound_cost_usd: float | None = None
    currency_code: str | None = None


class GlacierBillingForecastViewOut(RiverhogModel):
    source: str
    scope: str
    filter_label: str | None = None
    service: str | None = None
    currency_code: str | None = None
    granularity: str | None = None
    periods: list[GlacierBillingForecastOut]
    notes: list[str]


class GlacierBillingExportBreakdownOut(RiverhogModel):
    usage_type: str | None = None
    operation: str | None = None
    resource_id: str | None = None
    tag_value: str | None = None
    unblended_cost_usd: float
    usage_quantity: float | None = None
    usage_unit: str | None = None


class GlacierBillingExportViewOut(RiverhogModel):
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
    files_read: int
    rows_scanned: int
    breakdowns: list[GlacierBillingExportBreakdownOut]
    notes: list[str]


class GlacierBillingInvoiceSummaryOut(RiverhogModel):
    invoice_id: str | None = None
    account_id: str | None = None
    billing_period_start: str | None = None
    billing_period_end: str | None = None
    invoice_type: str | None = None
    invoicing_entity: str | None = None
    issued_at: str | None = None
    due_at: str | None = None
    base_currency_code: str | None = None
    base_total_amount: float | None = None
    payment_currency_code: str | None = None
    payment_total_amount: float | None = None
    original_invoice_id: str | None = None


class GlacierBillingInvoicesViewOut(RiverhogModel):
    source: str
    scope: str
    account_id: str | None = None
    invoices: list[GlacierBillingInvoiceSummaryOut]
    notes: list[str]


class GlacierBillingSummaryOut(RiverhogModel):
    actuals: GlacierBillingActualsViewOut | None = None
    forecast: GlacierBillingForecastViewOut | None = None
    exports: GlacierBillingExportViewOut | None = None
    invoices: GlacierBillingInvoicesViewOut | None = None
    notes: list[str]


class GlacierUsageReportOut(RiverhogModel):
    scope: Literal["all", "collection", "filtered"]
    measured_at: str
    pricing_basis: GlacierPricingBasisOut
    totals: GlacierUsageTotalsOut
    images: list[GlacierUsageImageOut]
    collections: list[GlacierUsageCollectionOut]
    history: list[GlacierUsageSnapshotOut]
    billing: GlacierBillingSummaryOut | None = None
