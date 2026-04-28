from __future__ import annotations

from typing import Literal

from arc_api.schemas.archive import GlacierArchiveOut
from arc_api.schemas.common import ArcModel


class GlacierPricingBasisOut(ArcModel):
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


class GlacierUsageTotalsOut(ArcModel):
    images: int
    uploaded_images: int
    measured_storage_bytes: int
    estimated_billable_bytes: int
    estimated_monthly_cost_usd: float


class GlacierUsageImageOut(ArcModel):
    id: str
    filename: str
    collection_ids: list[str]
    glacier: GlacierArchiveOut
    measured_storage_bytes: int
    estimated_billable_bytes: int
    estimated_monthly_cost_usd: float


class GlacierCollectionContributionOut(ArcModel):
    image_id: str
    filename: str
    glacier: GlacierArchiveOut
    represented_bytes: int
    represented_fraction: float | None
    derived_stored_bytes: int | None
    derived_billable_bytes: int | None
    estimated_monthly_cost_usd: float | None


class GlacierUsageCollectionOut(ArcModel):
    id: str
    bytes: int
    represented_bytes: int
    attribution_state: Literal["derived", "unavailable"]
    derived_stored_bytes: int
    derived_billable_bytes: int
    estimated_monthly_cost_usd: float
    images: list[GlacierCollectionContributionOut]


class GlacierUsageSnapshotOut(ArcModel):
    captured_at: str
    uploaded_images: int
    measured_storage_bytes: int
    estimated_billable_bytes: int
    estimated_monthly_cost_usd: float


class GlacierBillingActualOut(ArcModel):
    start: str
    end: str
    estimated: bool
    unblended_cost_usd: float
    usage_quantity: float | None = None
    usage_unit: str | None = None


class GlacierBillingForecastOut(ArcModel):
    start: str
    end: str
    mean_cost_usd: float
    lower_bound_cost_usd: float | None = None
    upper_bound_cost_usd: float | None = None
    currency_code: str | None = None


class GlacierBillingSummaryOut(ArcModel):
    source: str
    scope: str
    filter_label: str | None = None
    service: str | None = None
    currency_code: str | None = None
    history_granularity: str | None = None
    forecast_granularity: str | None = None
    actuals: list[GlacierBillingActualOut]
    forecast: list[GlacierBillingForecastOut]
    notes: list[str]


class GlacierUsageReportOut(ArcModel):
    scope: Literal["all", "image", "collection", "filtered"]
    measured_at: str
    pricing_basis: GlacierPricingBasisOut
    totals: GlacierUsageTotalsOut
    images: list[GlacierUsageImageOut]
    collections: list[GlacierUsageCollectionOut]
    history: list[GlacierUsageSnapshotOut]
    billing: GlacierBillingSummaryOut | None = None
