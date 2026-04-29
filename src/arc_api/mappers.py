from __future__ import annotations

from arc_core.domain.models import (
    CollectionCoverageImage,
    CollectionListPage,
    CollectionSummary,
    CopyHistoryEntry,
    CopySummary,
    FetchSummary,
    GlacierArchiveStatus,
    GlacierBillingActual,
    GlacierBillingActualsView,
    GlacierBillingExportBreakdown,
    GlacierBillingExportView,
    GlacierBillingForecast,
    GlacierBillingForecastView,
    GlacierBillingInvoiceSummary,
    GlacierBillingInvoicesView,
    GlacierBillingSummary,
    GlacierCollectionContribution,
    GlacierPricingBasis,
    GlacierUsageCollection,
    GlacierUsageImage,
    GlacierUsageReport,
    GlacierUsageSnapshot,
    GlacierUsageTotals,
    PinSummary,
    RecoveryCostEstimate,
    RecoveryNotificationStatus,
    RecoverySessionImage,
    RecoverySessionSummary,
)


def map_glacier(summary: GlacierArchiveStatus) -> dict[str, object]:
    return {
        "state": summary.state.value,
        "object_path": summary.object_path,
        "stored_bytes": summary.stored_bytes,
        "backend": summary.backend,
        "storage_class": summary.storage_class,
        "last_uploaded_at": summary.last_uploaded_at,
        "last_verified_at": summary.last_verified_at,
        "failure": summary.failure,
    }


def map_glacier_pricing_basis(summary: GlacierPricingBasis) -> dict[str, object]:
    return {
        "label": summary.label,
        "source": summary.source,
        "storage_class": summary.storage_class,
        "glacier_storage_rate_usd_per_gib_month": summary.glacier_storage_rate_usd_per_gib_month,
        "standard_storage_rate_usd_per_gib_month": summary.standard_storage_rate_usd_per_gib_month,
        "archived_metadata_bytes_per_object": summary.archived_metadata_bytes_per_object,
        "standard_metadata_bytes_per_object": summary.standard_metadata_bytes_per_object,
        "minimum_storage_duration_days": summary.minimum_storage_duration_days,
        "currency_code": summary.currency_code,
        "region_code": summary.region_code,
        "effective_at": summary.effective_at,
        "price_list_arn": summary.price_list_arn,
    }


def map_glacier_usage_totals(summary: GlacierUsageTotals) -> dict[str, object]:
    return {
        "images": summary.images,
        "uploaded_images": summary.uploaded_images,
        "measured_storage_bytes": summary.measured_storage_bytes,
        "estimated_billable_bytes": summary.estimated_billable_bytes,
        "estimated_monthly_cost_usd": summary.estimated_monthly_cost_usd,
    }


def map_glacier_usage_image(summary: GlacierUsageImage) -> dict[str, object]:
    return {
        "id": str(summary.id),
        "filename": summary.filename,
        "collection_ids": list(summary.collection_ids),
        "glacier": map_glacier(summary.glacier),
        "measured_storage_bytes": summary.measured_storage_bytes,
        "estimated_billable_bytes": summary.estimated_billable_bytes,
        "estimated_monthly_cost_usd": summary.estimated_monthly_cost_usd,
    }


def map_glacier_collection_contribution(
    summary: GlacierCollectionContribution,
) -> dict[str, object]:
    return {
        "image_id": str(summary.image_id),
        "filename": summary.filename,
        "glacier": map_glacier(summary.glacier),
        "represented_bytes": summary.represented_bytes,
        "represented_fraction": summary.represented_fraction,
        "derived_stored_bytes": summary.derived_stored_bytes,
        "derived_billable_bytes": summary.derived_billable_bytes,
        "estimated_monthly_cost_usd": summary.estimated_monthly_cost_usd,
    }


def map_glacier_usage_collection(summary: GlacierUsageCollection) -> dict[str, object]:
    return {
        "id": str(summary.id),
        "bytes": summary.bytes,
        "represented_bytes": summary.represented_bytes,
        "attribution_state": summary.attribution_state,
        "derived_stored_bytes": summary.derived_stored_bytes,
        "derived_billable_bytes": summary.derived_billable_bytes,
        "estimated_monthly_cost_usd": summary.estimated_monthly_cost_usd,
        "images": [map_glacier_collection_contribution(image) for image in summary.images],
    }


def map_glacier_usage_snapshot(summary: GlacierUsageSnapshot) -> dict[str, object]:
    return {
        "captured_at": summary.captured_at,
        "uploaded_images": summary.uploaded_images,
        "measured_storage_bytes": summary.measured_storage_bytes,
        "estimated_billable_bytes": summary.estimated_billable_bytes,
        "estimated_monthly_cost_usd": summary.estimated_monthly_cost_usd,
    }


def map_glacier_billing_actual(summary: GlacierBillingActual) -> dict[str, object]:
    return {
        "start": summary.start,
        "end": summary.end,
        "estimated": summary.estimated,
        "unblended_cost_usd": summary.unblended_cost_usd,
        "usage_quantity": summary.usage_quantity,
        "usage_unit": summary.usage_unit,
    }


def map_glacier_billing_actuals_view(
    summary: GlacierBillingActualsView | None,
) -> dict[str, object] | None:
    if summary is None:
        return None
    return {
        "source": summary.source,
        "scope": summary.scope,
        "filter_label": summary.filter_label,
        "service": summary.service,
        "billing_view_arn": summary.billing_view_arn,
        "granularity": summary.granularity,
        "measured_at": summary.measured_at,
        "periods": [map_glacier_billing_actual(item) for item in summary.periods],
        "notes": list(summary.notes),
    }


def map_glacier_billing_forecast(summary: GlacierBillingForecast) -> dict[str, object]:
    return {
        "start": summary.start,
        "end": summary.end,
        "mean_cost_usd": summary.mean_cost_usd,
        "lower_bound_cost_usd": summary.lower_bound_cost_usd,
        "upper_bound_cost_usd": summary.upper_bound_cost_usd,
        "currency_code": summary.currency_code,
    }


def map_glacier_billing_forecast_view(
    summary: GlacierBillingForecastView | None,
) -> dict[str, object] | None:
    if summary is None:
        return None
    return {
        "source": summary.source,
        "scope": summary.scope,
        "filter_label": summary.filter_label,
        "service": summary.service,
        "currency_code": summary.currency_code,
        "granularity": summary.granularity,
        "periods": [map_glacier_billing_forecast(item) for item in summary.periods],
        "notes": list(summary.notes),
    }


def map_glacier_billing_export_breakdown(
    summary: GlacierBillingExportBreakdown,
) -> dict[str, object]:
    return {
        "usage_type": summary.usage_type,
        "operation": summary.operation,
        "resource_id": summary.resource_id,
        "tag_value": summary.tag_value,
        "unblended_cost_usd": summary.unblended_cost_usd,
        "usage_quantity": summary.usage_quantity,
        "usage_unit": summary.usage_unit,
    }


def map_glacier_billing_export_view(
    summary: GlacierBillingExportView | None,
) -> dict[str, object] | None:
    if summary is None:
        return None
    return {
        "source": summary.source,
        "scope": summary.scope,
        "filter_label": summary.filter_label,
        "service": summary.service,
        "export_arn": summary.export_arn,
        "export_name": summary.export_name,
        "execution_id": summary.execution_id,
        "manifest_key": summary.manifest_key,
        "billing_period": summary.billing_period,
        "bucket": summary.bucket,
        "prefix": summary.prefix,
        "object_key": summary.object_key,
        "exported_at": summary.exported_at,
        "currency_code": summary.currency_code,
        "files_read": summary.files_read,
        "rows_scanned": summary.rows_scanned,
        "breakdowns": [map_glacier_billing_export_breakdown(item) for item in summary.breakdowns],
        "notes": list(summary.notes),
    }


def map_glacier_billing_invoice(summary: GlacierBillingInvoiceSummary) -> dict[str, object]:
    return {
        "invoice_id": summary.invoice_id,
        "account_id": summary.account_id,
        "billing_period_start": summary.billing_period_start,
        "billing_period_end": summary.billing_period_end,
        "invoice_type": summary.invoice_type,
        "invoicing_entity": summary.invoicing_entity,
        "issued_at": summary.issued_at,
        "due_at": summary.due_at,
        "base_currency_code": summary.base_currency_code,
        "base_total_amount": summary.base_total_amount,
        "payment_currency_code": summary.payment_currency_code,
        "payment_total_amount": summary.payment_total_amount,
        "original_invoice_id": summary.original_invoice_id,
    }


def map_glacier_billing_invoices_view(
    summary: GlacierBillingInvoicesView | None,
) -> dict[str, object] | None:
    if summary is None:
        return None
    return {
        "source": summary.source,
        "scope": summary.scope,
        "account_id": summary.account_id,
        "invoices": [map_glacier_billing_invoice(item) for item in summary.invoices],
        "notes": list(summary.notes),
    }


def map_glacier_billing_summary(summary: GlacierBillingSummary | None) -> dict[str, object] | None:
    if summary is None:
        return None
    return {
        "actuals": map_glacier_billing_actuals_view(summary.actuals),
        "forecast": map_glacier_billing_forecast_view(summary.forecast),
        "exports": map_glacier_billing_export_view(summary.exports),
        "invoices": map_glacier_billing_invoices_view(summary.invoices),
        "notes": list(summary.notes),
    }


def map_glacier_usage_report(summary: GlacierUsageReport) -> dict[str, object]:
    return {
        "scope": summary.scope,
        "measured_at": summary.measured_at,
        "pricing_basis": map_glacier_pricing_basis(summary.pricing_basis),
        "totals": map_glacier_usage_totals(summary.totals),
        "images": [map_glacier_usage_image(image) for image in summary.images],
        "collections": [map_glacier_usage_collection(item) for item in summary.collections],
        "history": [map_glacier_usage_snapshot(item) for item in summary.history],
        "billing": map_glacier_billing_summary(summary.billing),
    }


def map_collection(summary: CollectionSummary) -> dict[str, object]:
    return {
        "id": str(summary.id),
        "files": summary.files,
        "bytes": summary.bytes,
        "hot_bytes": summary.hot_bytes,
        "archived_bytes": summary.archived_bytes,
        "pending_bytes": summary.pending_bytes,
        "protection_state": summary.protection_state.value,
        "protected_bytes": summary.protected_bytes,
        "image_coverage": [
            map_collection_coverage_image(image) for image in summary.image_coverage
        ],
    }


def map_collection_list_page(summary: CollectionListPage) -> dict[str, object]:
    return {
        "page": summary.page,
        "per_page": summary.per_page,
        "total": summary.total,
        "pages": summary.pages,
        "collections": [map_collection(collection) for collection in summary.collections],
    }


def map_recovery_cost_estimate(summary: RecoveryCostEstimate) -> dict[str, object]:
    return {
        "currency_code": summary.currency_code,
        "retrieval_tier": summary.retrieval_tier,
        "hold_days": summary.hold_days,
        "image_count": summary.image_count,
        "total_bytes": summary.total_bytes,
        "restore_request_count": summary.restore_request_count,
        "retrieval_rate_usd_per_gib": summary.retrieval_rate_usd_per_gib,
        "request_rate_usd_per_1000": summary.request_rate_usd_per_1000,
        "standard_storage_rate_usd_per_gib_month": (
            summary.standard_storage_rate_usd_per_gib_month
        ),
        "retrieval_cost_usd": summary.retrieval_cost_usd,
        "request_fees_usd": summary.request_fees_usd,
        "temporary_storage_cost_usd": summary.temporary_storage_cost_usd,
        "total_estimated_cost_usd": summary.total_estimated_cost_usd,
        "assumptions": list(summary.assumptions),
    }


def map_recovery_notification(summary: RecoveryNotificationStatus) -> dict[str, object]:
    return {
        "webhook_configured": summary.webhook_configured,
        "reminder_count": summary.reminder_count,
        "next_reminder_at": summary.next_reminder_at,
        "last_notified_at": summary.last_notified_at,
    }


def map_recovery_session_image(summary: RecoverySessionImage) -> dict[str, object]:
    return {
        "id": str(summary.id),
        "filename": summary.filename,
        "glacier": map_glacier(summary.glacier),
        "stored_bytes": summary.stored_bytes,
    }


def map_recovery_session(summary: RecoverySessionSummary) -> dict[str, object]:
    return {
        "id": summary.id,
        "state": summary.state.value,
        "created_at": summary.created_at,
        "approved_at": summary.approved_at,
        "restore_requested_at": summary.restore_requested_at,
        "restore_ready_at": summary.restore_ready_at,
        "restore_expires_at": summary.restore_expires_at,
        "completed_at": summary.completed_at,
        "latest_message": summary.latest_message,
        "warnings": list(summary.warnings),
        "cost_estimate": map_recovery_cost_estimate(summary.cost_estimate),
        "notification": map_recovery_notification(summary.notification),
        "images": [map_recovery_session_image(image) for image in summary.images],
    }


def map_copy_history(entry: CopyHistoryEntry) -> dict[str, object]:
    return {
        "at": entry.at,
        "event": entry.event,
        "state": entry.state.value,
        "verification_state": entry.verification_state.value,
        "location": entry.location,
    }


def map_copy(summary: CopySummary) -> dict[str, object]:
    return {
        "id": str(summary.id),
        "volume_id": summary.volume_id,
        "label_text": summary.label_text,
        "location": summary.location,
        "created_at": summary.created_at,
        "state": summary.state.value,
        "verification_state": summary.verification_state.value,
        "history": [map_copy_history(entry) for entry in summary.history],
    }


def map_collection_coverage_image(summary: CollectionCoverageImage) -> dict[str, object]:
    return {
        "id": str(summary.id),
        "filename": summary.filename,
        "protection_state": summary.protection_state.value,
        "physical_copies_required": summary.physical_copies_required,
        "physical_copies_registered": summary.physical_copies_registered,
        "physical_copies_verified": summary.physical_copies_verified,
        "physical_copies_missing": summary.physical_copies_missing,
        "covered_paths": list(summary.covered_paths),
        "copies": [map_copy(copy) for copy in summary.copies],
        "glacier": map_glacier(summary.glacier),
    }


def map_fetch(summary: FetchSummary) -> dict[str, object]:
    return {
        "id": str(summary.id),
        "target": str(summary.target),
        "state": summary.state.value,
        "files": summary.files,
        "bytes": summary.bytes,
        "entries_total": summary.entries_total,
        "entries_pending": summary.entries_pending,
        "entries_partial": summary.entries_partial,
        "entries_byte_complete": summary.entries_byte_complete,
        "entries_uploaded": summary.entries_uploaded,
        "uploaded_bytes": summary.uploaded_bytes,
        "missing_bytes": summary.missing_bytes,
        "upload_state_expires_at": summary.upload_state_expires_at,
        "copies": [
            {"id": str(c.id), "volume_id": c.volume_id, "location": c.location}
            for c in summary.copies
        ],
    }


def map_pin(summary: PinSummary) -> dict[str, object]:
    return {
        "target": str(summary.target),
        "fetch": {
            "id": str(summary.fetch.id),
            "state": summary.fetch.state.value,
            "copies": [
                {"id": str(copy.id), "volume_id": copy.volume_id, "location": copy.location}
                for copy in summary.fetch.copies
            ],
        },
    }
