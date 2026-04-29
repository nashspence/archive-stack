from __future__ import annotations

import csv
import gzip
import io
import json
import zipfile
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

from arc_core.domain.models import (
    GlacierBillingActual,
    GlacierBillingActualsView,
    GlacierBillingExportBreakdown,
    GlacierBillingExportView,
    GlacierBillingForecast,
    GlacierBillingForecastView,
    GlacierBillingInvoiceSummary,
    GlacierBillingInvoicesView,
    GlacierBillingSummary,
)
from arc_core.runtime_config import RuntimeConfig
from arc_core.stores.s3_support import _require_boto3

_AWS_COST_EXPLORER_RESOURCE_SOURCE = "aws_cost_explorer_resource"
_AWS_COST_EXPLORER_SOURCE = "aws_cost_explorer"
_AWS_CUR_SOURCE = "aws_cur_s3"
_AWS_DATA_EXPORTS_SOURCE = "aws_data_exports_s3"
_AWS_INVOICING_SOURCE = "aws_invoicing"
_UNAVAILABLE_SOURCE = "unavailable"
_S3_SERVICE_NAME = "Amazon Simple Storage Service"
_RESOURCE_LOOKBACK_DAYS = 14
_SERVICE_COLUMN_CANDIDATES = (
    "line_item_product_code",
    "product_servicecode",
    "product_product_name",
    "product_ProductName",
    "service",
)
_USAGE_TYPE_COLUMN_CANDIDATES = (
    "line_item_usage_type",
    "lineItem/UsageType",
    "usage_type",
)
_OPERATION_COLUMN_CANDIDATES = (
    "line_item_operation",
    "lineItem/Operation",
    "operation",
)
_RESOURCE_ID_COLUMN_CANDIDATES = (
    "line_item_resource_id",
    "lineItem/ResourceId",
    "resource_id",
    "Resource",
)
_COST_COLUMN_CANDIDATES = (
    "line_item_unblended_cost",
    "lineItem/UnblendedCost",
    "unblended_cost",
)
_USAGE_QUANTITY_COLUMN_CANDIDATES = (
    "line_item_usage_amount",
    "lineItem/UsageAmount",
    "usage_quantity",
)
_USAGE_UNIT_COLUMN_CANDIDATES = (
    "line_item_usage_unit",
    "lineItem/UsageUnit",
    "pricing_unit",
    "usage_unit",
)


@dataclass(frozen=True)
class _CostExplorerScope:
    name: str
    expression: dict[str, object]
    label: str
    notes: tuple[str, ...]


@dataclass(frozen=True)
class _BucketActualsAttempt:
    view: GlacierBillingActualsView | None
    fallback_note: str | None = None


@dataclass(frozen=True)
class _ManifestSelection:
    source: str
    bucket: str
    prefix: str
    manifest_key: str
    exported_at: str | None
    object_keys: tuple[str, ...]
    export_arn: str | None = None
    export_name: str | None = None
    execution_id: str | None = None
    billing_period: str | None = None
    notes: tuple[str, ...] = ()


@dataclass(frozen=True)
class _ExportBreakdownTotals:
    cost: Decimal = Decimal("0")
    quantity: Decimal = Decimal("0")
    quantity_seen: bool = False
    unit: str | None = None


def resolve_glacier_billing(
    config: RuntimeConfig,
    *,
    include: bool,
) -> GlacierBillingSummary | None:
    if not include:
        return None
    if config.glacier_billing_mode == "disabled":
        return _unavailable_summary(
            config,
            reason="AWS Glacier billing queries are disabled for this runtime.",
        )
    if config.glacier_billing_mode == "auto" and not _should_try_aws_billing(config):
        return _unavailable_summary(
            config,
            reason="AWS Glacier billing is unavailable for this runtime.",
        )

    actuals: GlacierBillingActualsView
    forecast: GlacierBillingForecastView
    notes: list[str] = []
    try:
        client = _create_cost_explorer_client(config)
        bucket_attempt = _resolve_bucket_actuals(client, config=config)
        if bucket_attempt.view is not None:
            actuals = bucket_attempt.view
        else:
            actuals = _resolve_scope_actuals(
                client,
                config=config,
                fallback_note=bucket_attempt.fallback_note,
            )
        forecast = _resolve_forecast(
            client,
            config=config,
            actuals=actuals,
        )
    except Exception:
        if config.glacier_billing_mode == "aws":
            raise
        unavailable = _unavailable_summary(
            config,
            reason="AWS Cost Explorer billing could not be resolved for this runtime.",
        )
        actuals = unavailable.actuals or _unavailable_actuals_view(
            "AWS Cost Explorer actuals could not be resolved for this runtime."
        )
        forecast = unavailable.forecast or _unavailable_forecast_view(
            config,
            "AWS Cost Explorer forecast could not be resolved for this runtime.",
        )
        notes.extend(unavailable.notes)

    exports = _resolve_billing_exports(config)
    invoices = _resolve_invoices(config)
    return GlacierBillingSummary(
        actuals=actuals,
        forecast=forecast,
        exports=exports,
        invoices=invoices,
        notes=tuple(notes),
    )


def _unavailable_summary(config: RuntimeConfig, *, reason: str) -> GlacierBillingSummary:
    return GlacierBillingSummary(
        actuals=_unavailable_actuals_view(reason),
        forecast=_unavailable_forecast_view(config, reason),
        exports=_unavailable_exports_view(reason),
        invoices=_unavailable_invoices_view(reason),
        notes=(reason,),
    )


def _unavailable_actuals_view(reason: str) -> GlacierBillingActualsView:
    return GlacierBillingActualsView(
        source=_UNAVAILABLE_SOURCE,
        scope="unavailable",
        periods=(),
        notes=(reason,),
    )


def _unavailable_forecast_view(
    config: RuntimeConfig,
    reason: str,
) -> GlacierBillingForecastView:
    return GlacierBillingForecastView(
        source=_UNAVAILABLE_SOURCE,
        scope="unavailable",
        currency_code=config.glacier_billing_currency_code,
        periods=(),
        notes=(reason,),
    )


def _unavailable_exports_view(reason: str) -> GlacierBillingExportView:
    return GlacierBillingExportView(
        source=_UNAVAILABLE_SOURCE,
        scope="unavailable",
        breakdowns=(),
        notes=(reason,),
    )


def _unavailable_invoices_view(reason: str) -> GlacierBillingInvoicesView:
    return GlacierBillingInvoicesView(
        source=_UNAVAILABLE_SOURCE,
        scope="unavailable",
        invoices=(),
        notes=(reason,),
    )


def _should_try_aws_billing(config: RuntimeConfig) -> bool:
    if config.glacier_billing_mode == "aws":
        return True
    if config.glacier_backend.casefold() == "aws":
        return True
    endpoint = config.glacier_endpoint_url.casefold()
    return "amazonaws.com" in endpoint


def _billing_scope(config: RuntimeConfig) -> _CostExplorerScope:
    base_filters: list[dict[str, object]] = [
        _dimension_expression("SERVICE", [_S3_SERVICE_NAME]),
        _dimension_expression("REGION", [config.glacier_pricing_region_code]),
    ]
    if config.glacier_billing_tag_key and config.glacier_billing_tag_value:
        base_filters.append(
            {
                "Tags": {
                    "Key": config.glacier_billing_tag_key,
                    "Values": [config.glacier_billing_tag_value],
                }
            }
        )
        return _CostExplorerScope(
            name="tag",
            expression={"And": base_filters},
            label=f"{config.glacier_billing_tag_key}={config.glacier_billing_tag_value}",
            notes=(),
        )
    return _CostExplorerScope(
        name="service",
        expression={"And": base_filters},
        label=f"{_S3_SERVICE_NAME} in {config.glacier_pricing_region_code}",
        notes=(
            (
                "Forecast and fallback actuals are scoped to the Amazon S3 service "
                "in the configured Glacier region."
            ),
            (
                "Set ARC_GLACIER_BILLING_TAG_KEY and ARC_GLACIER_BILLING_TAG_VALUE "
                "for archive-specific forecast and fallback billing attribution."
            ),
        ),
    )


def _create_cost_explorer_client(config: RuntimeConfig) -> Any:
    boto3, Config = _require_boto3()
    return boto3.client(
        "ce",
        region_name=config.glacier_billing_api_region,
        config=Config(retries={"max_attempts": 3, "mode": "standard"}),
    )


def _create_billing_client(config: RuntimeConfig) -> Any:
    boto3, Config = _require_boto3()
    return boto3.client(
        "billing",
        region_name=config.glacier_billing_api_region,
        config=Config(retries={"max_attempts": 3, "mode": "standard"}),
    )


def _create_data_exports_client(config: RuntimeConfig) -> Any:
    boto3, Config = _require_boto3()
    return boto3.client(
        "bcm-data-exports",
        region_name=config.glacier_billing_api_region,
        config=Config(retries={"max_attempts": 3, "mode": "standard"}),
    )


def _create_billing_s3_client(config: RuntimeConfig) -> Any:
    boto3, Config = _require_boto3()
    return boto3.client(
        "s3",
        region_name=config.glacier_billing_export_region,
        config=Config(retries={"max_attempts": 3, "mode": "standard"}),
    )


def _create_invoicing_client(config: RuntimeConfig) -> Any:
    boto3, Config = _require_boto3()
    return boto3.client(
        "invoicing",
        region_name=config.glacier_billing_api_region,
        config=Config(retries={"max_attempts": 3, "mode": "standard"}),
    )


def _create_sts_client(config: RuntimeConfig) -> Any:
    boto3, Config = _require_boto3()
    return boto3.client(
        "sts",
        region_name=config.glacier_billing_api_region,
        config=Config(retries={"max_attempts": 3, "mode": "standard"}),
    )


def _resolve_bucket_actuals(client: Any, *, config: RuntimeConfig) -> _BucketActualsAttempt:
    billing_view_arn = _resolve_billing_view_arn(config)
    if not hasattr(client, "get_cost_and_usage_with_resources"):
        return _BucketActualsAttempt(
            view=None,
            fallback_note=(
                "Bucket-scoped Cost Explorer actuals require the AWS resource-level "
                "cost API support available through GetCostAndUsageWithResources."
            ),
        )

    notes = [
        (
            "Bucket-scoped Cost Explorer actuals use AWS resource-level daily data "
            "and are limited to the last 14 days."
        )
    ]
    if billing_view_arn:
        notes.append(
            "Riverhog queried bucket-scoped actuals through the resolved AWS billing view."
        )
    start = date.today() - timedelta(days=_RESOURCE_LOOKBACK_DAYS - 1)
    end = date.today() + timedelta(days=1)
    kwargs: dict[str, object] = {
        "TimePeriod": {"Start": start.isoformat(), "End": end.isoformat()},
        "Granularity": "DAILY",
        "Metrics": ["UnblendedCost", "UsageQuantity"],
        "Filter": {
            "And": [
                _dimension_expression("SERVICE", [_S3_SERVICE_NAME]),
                _dimension_expression("REGION", [config.glacier_pricing_region_code]),
            ]
        },
        "GroupBy": [{"Type": "DIMENSION", "Key": "RESOURCE_ID"}],
    }
    if billing_view_arn:
        kwargs["BillingViewArn"] = billing_view_arn

    periods_by_range: dict[tuple[str, str, bool], dict[str, object]] = {}
    next_page_token: str | None = None
    resource_ids = set(_bucket_resource_ids(config))
    matched_resource = False
    try:
        while True:
            page_kwargs = dict(kwargs)
            if next_page_token:
                page_kwargs["NextPageToken"] = next_page_token
            response = client.get_cost_and_usage_with_resources(**page_kwargs)
            for item in response.get("ResultsByTime", []):
                if not isinstance(item, dict):
                    continue
                matching_groups = _matching_resource_groups(
                    item.get("Groups", []),
                    resource_ids=resource_ids,
                )
                if not matching_groups:
                    continue
                matched_resource = True
                key = _time_period_key(item)
                aggregate = periods_by_range.setdefault(
                    key,
                    {
                        "cost": Decimal("0"),
                        "quantity": Decimal("0"),
                        "quantity_seen": False,
                        "unit": None,
                    },
                )
                for group in matching_groups:
                    _accumulate_group_metrics(aggregate, group)
            next_page_token = _optional_str(response.get("NextPageToken"))
            if next_page_token is None:
                break
    except Exception as exc:
        if _resource_level_unavailable(exc):
            return _BucketActualsAttempt(
                view=None,
                fallback_note=(
                    "Bucket-scoped Cost Explorer actuals require resource-level daily "
                    "granular data to be enabled for the archive bucket and available "
                    "through a chargeable billing view."
                ),
            )
        return _BucketActualsAttempt(
            view=None,
            fallback_note=(
                "Bucket-scoped Cost Explorer actuals could not be resolved through "
                "GetCostAndUsageWithResources, so Riverhog fell back to broader AWS "
                "billing attribution."
            ),
        )

    if not matched_resource:
        return _BucketActualsAttempt(
            view=None,
            fallback_note=(
                "Bucket-scoped Cost Explorer actuals were not present in the AWS "
                "resource-level results for the archive bucket, so Riverhog fell back "
                "to broader AWS billing attribution."
            ),
        )

    periods = tuple(_periods_from_resource_totals(periods_by_range))
    return _BucketActualsAttempt(
        view=GlacierBillingActualsView(
            source=_AWS_COST_EXPLORER_RESOURCE_SOURCE,
            scope="bucket",
            filter_label=config.glacier_bucket,
            service=_S3_SERVICE_NAME,
            billing_view_arn=billing_view_arn,
            granularity="DAILY",
            periods=periods,
            notes=tuple(notes),
        )
    )


def _resolve_billing_view_arn(config: RuntimeConfig) -> str | None:
    if config.glacier_billing_view_arn:
        return config.glacier_billing_view_arn
    try:
        response = _create_billing_client(config).list_billing_views(
            billingViewTypes=["PRIMARY"],
            maxResults=10,
        )
    except Exception:
        return None
    for item in response.get("billingViews", []):
        if not isinstance(item, dict):
            continue
        arn = _optional_str(item.get("arn"))
        if arn:
            return arn
    return None


def _bucket_resource_ids(config: RuntimeConfig) -> tuple[str, ...]:
    return (
        config.glacier_bucket,
        f"arn:aws:s3:::{config.glacier_bucket}",
    )


def _matching_resource_groups(
    groups: object,
    *,
    resource_ids: set[str],
) -> list[dict[str, object]]:
    if not isinstance(groups, list):
        return []
    matches: list[dict[str, object]] = []
    for group in groups:
        if not isinstance(group, dict):
            continue
        keys = group.get("Keys", [])
        if not isinstance(keys, list):
            continue
        if any(str(key) in resource_ids for key in keys):
            matches.append(group)
    return matches


def _time_period_key(item: dict[str, object]) -> tuple[str, str, bool]:
    time_period = item.get("TimePeriod", {})
    if not isinstance(time_period, dict):
        time_period = {}
    return (
        str(time_period.get("Start", "")),
        str(time_period.get("End", "")),
        bool(item.get("Estimated", False)),
    )


def _accumulate_group_metrics(aggregate: dict[str, object], group: dict[str, object]) -> None:
    metrics = group.get("Metrics", {})
    if not isinstance(metrics, dict):
        metrics = {}
    cost_metric = metrics.get("UnblendedCost", {})
    usage_metric = metrics.get("UsageQuantity", {})
    aggregate["cost"] = Decimal(str(aggregate["cost"])) + _metric_decimal(cost_metric)
    quantity = _metric_decimal(usage_metric)
    if quantity != Decimal("0"):
        aggregate["quantity"] = Decimal(str(aggregate["quantity"])) + quantity
        aggregate["quantity_seen"] = True
    unit = _metric_unit(usage_metric)
    current_unit = aggregate.get("unit")
    if current_unit in (None, ""):
        aggregate["unit"] = unit
    elif unit not in (None, "", current_unit):
        aggregate["unit"] = "mixed"


def _periods_from_resource_totals(
    totals: dict[tuple[str, str, bool], dict[str, object]],
) -> list[GlacierBillingActual]:
    periods: list[GlacierBillingActual] = []
    for start, end, estimated in sorted(totals):
        values = totals[(start, end, estimated)]
        quantity_seen = bool(values.get("quantity_seen", False))
        periods.append(
            GlacierBillingActual(
                start=start,
                end=end,
                estimated=estimated,
                unblended_cost_usd=float(Decimal(str(values["cost"]))),
                usage_quantity=(
                    float(Decimal(str(values["quantity"]))) if quantity_seen else None
                ),
                usage_unit=(
                    str(values["unit"]) if values.get("unit") not in (None, "") else None
                ),
            )
        )
    return periods


def _resolve_scope_actuals(
    client: Any,
    *,
    config: RuntimeConfig,
    fallback_note: str | None,
) -> GlacierBillingActualsView:
    scope = _billing_scope(config)
    periods = _load_actual_costs(client, config=config, scope=scope)
    notes = list(scope.notes)
    if fallback_note:
        notes.insert(0, fallback_note)
    return GlacierBillingActualsView(
        source=_AWS_COST_EXPLORER_SOURCE,
        scope=scope.name,
        filter_label=scope.label,
        service=_S3_SERVICE_NAME,
        granularity="MONTHLY",
        periods=periods,
        notes=tuple(notes),
    )


def _resolve_forecast(
    client: Any,
    *,
    config: RuntimeConfig,
    actuals: GlacierBillingActualsView,
) -> GlacierBillingForecastView:
    scope = _billing_scope(config)
    periods = _load_cost_forecast(client, config=config, scope=scope)
    notes = list(scope.notes)
    if actuals.scope == "bucket":
        notes.insert(
            0,
            "AWS Cost Explorer forecast does not expose bucket-resource forecasting, "
            "so Riverhog falls back to tag-scoped or service-scoped forecast data.",
        )
    return GlacierBillingForecastView(
        source=_AWS_COST_EXPLORER_SOURCE,
        scope=scope.name,
        filter_label=scope.label,
        service=_S3_SERVICE_NAME,
        currency_code=config.glacier_billing_currency_code,
        granularity="MONTHLY",
        periods=periods,
        notes=tuple(notes),
    )


def _load_actual_costs(
    client: Any,
    *,
    config: RuntimeConfig,
    scope: _CostExplorerScope,
) -> tuple[GlacierBillingActual, ...]:
    start = _month_start(_add_months(date.today(), -(config.glacier_billing_lookback_months - 1)))
    end = _month_start(_add_months(date.today(), 1))
    response = client.get_cost_and_usage(
        TimePeriod={"Start": start.isoformat(), "End": end.isoformat()},
        Granularity="MONTHLY",
        Metrics=["UnblendedCost", "UsageQuantity"],
        Filter=scope.expression,
    )
    return tuple(_map_actual_period(item) for item in response.get("ResultsByTime", []))


def _load_cost_forecast(
    client: Any,
    *,
    config: RuntimeConfig,
    scope: _CostExplorerScope,
) -> tuple[GlacierBillingForecast, ...]:
    start = _month_start(_add_months(date.today(), 1))
    end = _month_start(_add_months(start, config.glacier_billing_forecast_months))
    response = client.get_cost_forecast(
        TimePeriod={"Start": start.isoformat(), "End": end.isoformat()},
        Granularity="MONTHLY",
        Metric="UNBLENDED_COST",
        PredictionIntervalLevel=80,
        Filter=scope.expression,
    )
    return tuple(
        _map_forecast_period(item, currency_code=config.glacier_billing_currency_code)
        for item in response.get("ForecastResultsByTime", [])
    )


def _resolve_billing_exports(config: RuntimeConfig) -> GlacierBillingExportView:
    export_location = _billing_export_location(config)
    if export_location is None:
        return _unavailable_exports_view(
            "Set ARC_GLACIER_BILLING_EXPORT_BUCKET and ARC_GLACIER_BILLING_EXPORT_PREFIX "
            "to inspect CUR or Data Exports billing detail."
        )
    export_bucket, export_prefix = export_location

    try:
        s3_client = _create_billing_s3_client(config)
        manifest = _resolve_export_manifest(
            s3_client,
            config=config,
            export_bucket=export_bucket,
            export_prefix=export_prefix,
        )
        if manifest is None:
            return _unavailable_exports_view(
                "No CUR or Data Exports manifest was found under the configured billing "
                "export location."
            )
        rows_scanned, breakdowns, source = _aggregate_export_breakdowns(
            s3_client,
            config=config,
            manifest=manifest,
        )
    except Exception:
        return _unavailable_exports_view(
            "CUR or Data Exports billing detail could not be resolved for this runtime."
        )

    filter_label = (
        f"{config.glacier_billing_tag_key}={config.glacier_billing_tag_value}"
        if config.glacier_billing_tag_key and config.glacier_billing_tag_value
        else config.glacier_bucket
    )
    scope = (
        "tag"
        if config.glacier_billing_tag_key and config.glacier_billing_tag_value
        else "bucket"
    )
    if not breakdowns:
        return GlacierBillingExportView(
            source=_UNAVAILABLE_SOURCE,
            scope=scope,
            filter_label=filter_label,
            service=_S3_SERVICE_NAME,
            export_arn=manifest.export_arn,
            export_name=manifest.export_name,
            execution_id=manifest.execution_id,
            manifest_key=manifest.manifest_key,
            billing_period=manifest.billing_period,
            bucket=manifest.bucket,
            prefix=manifest.prefix,
            object_key=manifest.object_keys[0] if manifest.object_keys else None,
            exported_at=manifest.exported_at,
            currency_code=config.glacier_billing_currency_code,
            files_read=len(manifest.object_keys),
            rows_scanned=rows_scanned,
            breakdowns=(),
            notes=manifest.notes
            + (
                (
                    "The selected CUR or Data Exports manifest did not contain "
                    "archive-matching rows. Use cost-allocation tags or resource IDs "
                    "in the export to make Glacier drill-down precise."
                ),
            ),
        )
    return GlacierBillingExportView(
        source=source,
        scope=scope,
        filter_label=filter_label,
        service=_S3_SERVICE_NAME,
        export_arn=manifest.export_arn,
        export_name=manifest.export_name,
        execution_id=manifest.execution_id,
        manifest_key=manifest.manifest_key,
        billing_period=manifest.billing_period,
        bucket=manifest.bucket,
        prefix=manifest.prefix,
        object_key=manifest.object_keys[0] if len(manifest.object_keys) == 1 else None,
        exported_at=manifest.exported_at,
        currency_code=config.glacier_billing_currency_code,
        files_read=len(manifest.object_keys),
        rows_scanned=rows_scanned,
        breakdowns=breakdowns,
        notes=manifest.notes,
    )


def _resolve_export_manifest(
    s3_client: Any,
    *,
    config: RuntimeConfig,
    export_bucket: str,
    export_prefix: str,
) -> _ManifestSelection | None:
    if config.glacier_billing_export_arn:
        manifest = _resolve_data_exports_manifest(
            s3_client,
            config=config,
            export_bucket=export_bucket,
            export_prefix=export_prefix,
        )
        if manifest is not None:
            return manifest
    return _resolve_manifest_from_prefix(
        s3_client,
        config=config,
        export_bucket=export_bucket,
        export_prefix=export_prefix,
    )


def _resolve_data_exports_manifest(
    s3_client: Any,
    *,
    config: RuntimeConfig,
    export_bucket: str,
    export_prefix: str,
) -> _ManifestSelection | None:
    client = _create_data_exports_client(config)
    response = client.list_executions(
        ExportArn=config.glacier_billing_export_arn,
        MaxResults=20,
    )
    execution = _latest_successful_execution(response.get("Executions", []))
    if execution is None:
        return None
    execution_id, exported_at = execution
    execution_response = client.get_execution(
        ExportArn=config.glacier_billing_export_arn,
        ExecutionId=execution_id,
    )
    export = execution_response.get("Export", {})
    if not isinstance(export, dict):
        export = {}
    export_name = _extract_export_name(export, config=config)
    metadata_prefix = _data_exports_metadata_prefix(
        export_prefix=export_prefix,
        export_name=export_name,
    )
    manifest_key = _select_manifest_key(
        s3_client,
        bucket=export_bucket,
        prefix=metadata_prefix,
        prefer_execution_id=execution_id,
    )
    if manifest_key is None:
        return None
    manifest = _load_manifest_json(
        s3_client,
        bucket=export_bucket,
        key=manifest_key,
    )
    object_keys = _manifest_object_keys(manifest, manifest_key=manifest_key)
    return _ManifestSelection(
        source=_AWS_DATA_EXPORTS_SOURCE,
        bucket=export_bucket,
        prefix=export_prefix,
        export_arn=config.glacier_billing_export_arn,
        export_name=export_name,
        execution_id=execution_id,
        manifest_key=manifest_key,
        exported_at=exported_at,
        billing_period=_manifest_billing_period(manifest_key, manifest=manifest),
        object_keys=object_keys,
        notes=(
            "Riverhog selected the AWS Data Exports manifest for the latest successful execution.",
        ),
    )


def _resolve_manifest_from_prefix(
    s3_client: Any,
    *,
    config: RuntimeConfig,
    export_bucket: str,
    export_prefix: str,
) -> _ManifestSelection | None:
    manifest_key = _select_manifest_key(
        s3_client,
        bucket=export_bucket,
        prefix=export_prefix,
        prefer_execution_id=None,
    )
    if manifest_key is None:
        return None
    exported_at = _object_last_modified(
        s3_client,
        bucket=export_bucket,
        prefix=export_prefix,
        key=manifest_key,
    )
    manifest = _load_manifest_json(
        s3_client,
        bucket=export_bucket,
        key=manifest_key,
    )
    return _ManifestSelection(
        source=_manifest_source_from_key(manifest_key),
        bucket=export_bucket,
        prefix=export_prefix,
        export_name=_manifest_export_name(manifest_key, export_prefix=export_prefix),
        manifest_key=manifest_key,
        exported_at=exported_at,
        billing_period=_manifest_billing_period(manifest_key, manifest=manifest),
        object_keys=_manifest_object_keys(manifest, manifest_key=manifest_key),
        notes=(
            "Riverhog selected the latest manifest under the configured billing "
            "export prefix.",
        ),
    )


def _latest_successful_execution(executions: object) -> tuple[str, str | None] | None:
    if not isinstance(executions, list):
        return None
    latest: tuple[str, datetime | None] | None = None
    for item in executions:
        if not isinstance(item, dict):
            continue
        status = item.get("ExecutionStatus", {})
        if not isinstance(status, dict):
            status = {}
        if status.get("StatusCode") != "DELIVERY_SUCCESS":
            continue
        execution_id = _optional_str(item.get("ExecutionId"))
        if execution_id is None:
            continue
        completed = _object_datetime(status.get("CompletedAt"))
        if latest is None or (completed or datetime.min.replace(tzinfo=UTC)) > (
            latest[1] or datetime.min.replace(tzinfo=UTC)
        ):
            latest = (execution_id, completed)
    if latest is None:
        return None
    return latest[0], _datetime_to_utc_iso(latest[1]) if latest[1] else None


def _extract_export_name(export: dict[str, object], *, config: RuntimeConfig) -> str | None:
    name = _optional_str(export.get("Name"))
    if name:
        return name
    arn = config.glacier_billing_export_arn or ""
    if "/" in arn:
        return arn.rsplit("/", 1)[-1]
    return None


def _data_exports_metadata_prefix(
    *,
    export_prefix: str,
    export_name: str | None,
) -> str:
    prefix = export_prefix.strip("/")
    if export_name:
        return "/".join(part for part in (prefix, export_name, "metadata") if part)
    return "/".join(part for part in (prefix, "metadata") if part)


def _select_manifest_key(
    s3_client: Any,
    *,
    bucket: str,
    prefix: str | None,
    prefer_execution_id: str | None,
) -> str | None:
    prefix_value = (prefix or "").strip("/")
    latest: tuple[str, datetime] | None = None
    preferred_latest: tuple[str, datetime] | None = None
    paginator = s3_client.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix_value):
        for entry in page.get("Contents", []):
            key = entry.get("Key")
            modified = entry.get("LastModified")
            if not isinstance(key, str) or modified is None or not _is_manifest_key(key):
                continue
            candidate = (key, modified)
            if latest is None or modified > latest[1]:
                latest = candidate
            if prefer_execution_id and prefer_execution_id in key:
                if preferred_latest is None or modified > preferred_latest[1]:
                    preferred_latest = candidate
    if preferred_latest is not None:
        return preferred_latest[0]
    if latest is not None:
        return latest[0]
    return None


def _is_manifest_key(key: str) -> bool:
    normalized = key.casefold()
    return normalized.endswith("manifest.json")


def _object_last_modified(
    s3_client: Any,
    *,
    bucket: str,
    prefix: str,
    key: str,
) -> str | None:
    paginator = s3_client.get_paginator("list_objects_v2")
    prefix_value = prefix.strip("/")
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix_value):
        for entry in page.get("Contents", []):
            if entry.get("Key") != key:
                continue
            modified = _object_datetime(entry.get("LastModified"))
            return _datetime_to_utc_iso(modified) if modified else None
    return None


def _load_manifest_json(s3_client: Any, *, bucket: str, key: str) -> dict[str, object]:
    payload = s3_client.get_object(Bucket=bucket, Key=key)["Body"].read()
    data = json.loads(payload.decode("utf-8"))
    if not isinstance(data, dict):
        raise RuntimeError("billing export manifest must be a JSON object")
    return data


def _manifest_object_keys(
    manifest: dict[str, object],
    *,
    manifest_key: str,
) -> tuple[str, ...]:
    raw_keys: list[str] = []
    report_keys = manifest.get("reportKeys")
    if isinstance(report_keys, list):
        for item in report_keys:
            if isinstance(item, str):
                raw_keys.append(item)
    report_files = manifest.get("reportFiles")
    if isinstance(report_files, list):
        for item in report_files:
            if isinstance(item, str):
                raw_keys.append(item)
                continue
            if not isinstance(item, dict):
                continue
            for name in ("filePath", "key", "s3Key", "dataFileS3Key"):
                value = item.get(name)
                if isinstance(value, str):
                    raw_keys.append(value)
                    break
    file_paths = manifest.get("filePaths")
    if isinstance(file_paths, list):
        for item in file_paths:
            if isinstance(item, str):
                raw_keys.append(item)

    manifest_dir = manifest_key.rsplit("/", 1)[0] if "/" in manifest_key else ""
    normalized: list[str] = []
    for key in raw_keys:
        normalized_key = _normalize_manifest_object_key(key, manifest_dir=manifest_dir)
        if normalized_key not in normalized:
            normalized.append(normalized_key)
    return tuple(normalized)


def _normalize_manifest_object_key(key: str, *, manifest_dir: str) -> str:
    if key.startswith("s3://"):
        without_scheme = key.removeprefix("s3://")
        if "/" not in without_scheme:
            return without_scheme
        return without_scheme.split("/", 1)[1]
    normalized = key.lstrip("/")
    if "/" not in normalized and manifest_dir:
        return f"{manifest_dir}/{normalized}"
    return normalized


def _manifest_source_from_key(key: str) -> str:
    if "/metadata/" in key:
        return _AWS_DATA_EXPORTS_SOURCE
    return _AWS_CUR_SOURCE


def _manifest_export_name(key: str, *, export_prefix: str) -> str | None:
    parts = [part for part in key.split("/") if part]
    prefix_parts = [part for part in export_prefix.split("/") if part]
    if len(parts) <= len(prefix_parts):
        return None
    return parts[len(prefix_parts)]


def _manifest_billing_period(
    manifest_key: str,
    *,
    manifest: dict[str, object],
) -> str | None:
    billing_period = manifest.get("billingPeriod", {})
    if isinstance(billing_period, dict):
        start = _optional_str(billing_period.get("start"))
        end = _optional_str(billing_period.get("end"))
        if start and end:
            return f"{start}..{end}"
    for part in manifest_key.split("/"):
        if part.startswith("BILLING_PERIOD="):
            return part.removeprefix("BILLING_PERIOD=")
        if len(part) == 17 and part[8] == "-":
            return part
    return None


def _aggregate_export_breakdowns(
    s3_client: Any,
    *,
    config: RuntimeConfig,
    manifest: _ManifestSelection,
) -> tuple[int, tuple[GlacierBillingExportBreakdown, ...], str]:
    source = manifest.source
    rows_scanned = 0
    breakdown_key = tuple[str | None, str | None, str | None, str | None]
    aggregates: dict[breakdown_key, _ExportBreakdownTotals] = {}
    for key in manifest.object_keys:
        payload = s3_client.get_object(Bucket=manifest.bucket, Key=key)["Body"].read()
        for text in _decode_export_object_parts(key, payload):
            file_rows, file_source, file_aggregates = _parse_export_rows(text, config=config)
            rows_scanned += file_rows
            source = file_source
            for breakdown_key, values in file_aggregates.items():
                current = aggregates.get(breakdown_key, _ExportBreakdownTotals())
                current_unit = current.unit
                next_unit = values.unit
                merged_unit = current_unit
                if merged_unit in (None, ""):
                    merged_unit = next_unit
                elif next_unit not in (None, "", merged_unit):
                    merged_unit = "mixed"
                aggregates[breakdown_key] = _ExportBreakdownTotals(
                    cost=current.cost + values.cost,
                    quantity=current.quantity + values.quantity,
                    quantity_seen=current.quantity_seen or values.quantity_seen,
                    unit=merged_unit,
                )
    breakdowns = [
        GlacierBillingExportBreakdown(
            usage_type=usage_type,
            operation=operation,
            resource_id=resource_id,
            tag_value=tag_value,
            unblended_cost_usd=float(values.cost),
            usage_quantity=float(values.quantity) if values.quantity_seen else None,
            usage_unit=values.unit,
        )
        for (usage_type, operation, resource_id, tag_value), values in aggregates.items()
    ]
    breakdowns.sort(key=lambda current: current.unblended_cost_usd, reverse=True)
    return rows_scanned, tuple(breakdowns[: config.glacier_billing_export_max_items]), source


def _decode_export_object_parts(key: str, payload: bytes) -> tuple[str, ...]:
    normalized = key.casefold()
    if normalized.endswith(".zip"):
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            parts = tuple(
                archive.read(name).decode("utf-8-sig")
                for name in archive.namelist()
                if not name.endswith("/")
            )
        return parts
    if normalized.endswith(".gz"):
        payload = gzip.decompress(payload)
    return (payload.decode("utf-8-sig"),)


def _parse_export_rows(
    payload: str,
    *,
    config: RuntimeConfig,
) -> tuple[
    int,
    str,
    dict[tuple[str | None, str | None, str | None, str | None], _ExportBreakdownTotals],
]:
    reader = csv.DictReader(io.StringIO(payload))
    rows_scanned = 0
    source = _AWS_DATA_EXPORTS_SOURCE
    breakdown_key = tuple[str | None, str | None, str | None, str | None]
    aggregates: dict[breakdown_key, _ExportBreakdownTotals] = {}
    for row in reader:
        rows_scanned += 1
        if not row:
            continue
        if any(name.startswith("line_item_") for name in row):
            source = _AWS_CUR_SOURCE
        if not _row_matches_archive_scope(row, config=config):
            continue
        usage_type = _row_value(row, _USAGE_TYPE_COLUMN_CANDIDATES)
        operation = _row_value(row, _OPERATION_COLUMN_CANDIDATES)
        resource_id = _row_value(row, _RESOURCE_ID_COLUMN_CANDIDATES)
        tag_value = _row_tag_value(row, config=config)
        key = (usage_type, operation, resource_id, tag_value)
        current = aggregates.get(key, _ExportBreakdownTotals())
        quantity = _row_decimal(row, _USAGE_QUANTITY_COLUMN_CANDIDATES)
        quantity_seen = current.quantity_seen or quantity != Decimal("0")
        unit = _row_value(row, _USAGE_UNIT_COLUMN_CANDIDATES)
        merged_unit = current.unit
        if merged_unit in (None, ""):
            merged_unit = unit
        elif unit not in (None, "", merged_unit):
            merged_unit = "mixed"
        aggregates[key] = _ExportBreakdownTotals(
            cost=current.cost + _row_decimal(row, _COST_COLUMN_CANDIDATES),
            quantity=current.quantity + quantity,
            quantity_seen=quantity_seen,
            unit=merged_unit,
        )
    return rows_scanned, source, aggregates


def _row_matches_archive_scope(row: dict[str, str], *, config: RuntimeConfig) -> bool:
    service = (_row_value(row, _SERVICE_COLUMN_CANDIDATES) or "").casefold()
    if service and "s3" not in service and "simple storage" not in service:
        return False
    if config.glacier_billing_tag_key and config.glacier_billing_tag_value:
        return _row_tag_value(row, config=config) == config.glacier_billing_tag_value
    resource_id = _row_value(row, _RESOURCE_ID_COLUMN_CANDIDATES)
    if resource_id is None:
        return False
    return resource_id in set(_bucket_resource_ids(config))


def _row_tag_value(row: dict[str, str], *, config: RuntimeConfig) -> str | None:
    if not config.glacier_billing_tag_key:
        return None
    sanitized = config.glacier_billing_tag_key.replace("-", "_").replace(":", "_")
    candidates = (
        f"resource_tags_user_{config.glacier_billing_tag_key}",
        f"resource_tags_user_{sanitized}",
        f"resourceTags/user:{config.glacier_billing_tag_key}",
        f"resource_tags/{config.glacier_billing_tag_key}",
    )
    return _row_value(row, candidates)


def _row_value(row: dict[str, str], candidates: tuple[str, ...]) -> str | None:
    for name in candidates:
        value = row.get(name)
        if value not in (None, ""):
            return str(value)
    lowered = {key.casefold(): value for key, value in row.items()}
    for name in candidates:
        value = lowered.get(name.casefold())
        if value not in (None, ""):
            return str(value)
    return None


def _row_decimal(row: dict[str, str], candidates: tuple[str, ...]) -> Decimal:
    value = _row_value(row, candidates)
    if value is None or value == "":
        return Decimal("0")
    return Decimal(value)


def _billing_export_location(config: RuntimeConfig) -> tuple[str, str] | None:
    bucket = config.glacier_billing_export_bucket
    prefix = config.glacier_billing_export_prefix
    if not bucket or not prefix:
        return None
    return bucket, prefix


def _resolve_invoices(config: RuntimeConfig) -> GlacierBillingInvoicesView:
    try:
        account_id = config.glacier_billing_invoice_account_id or _resolve_account_id(config)
    except Exception:
        return _unavailable_invoices_view(
            "AWS account identity is unavailable for invoice summary lookup."
        )

    try:
        client = _create_invoicing_client(config)
        response = client.list_invoice_summaries(
            Selector={"ResourceType": "ACCOUNT_ID", "Value": account_id},
            MaxResults=config.glacier_billing_invoice_max_items,
        )
    except Exception:
        return _unavailable_invoices_view(
            "AWS invoice summaries could not be resolved for this runtime."
        )

    return GlacierBillingInvoicesView(
        source=_AWS_INVOICING_SOURCE,
        scope="account",
        account_id=account_id,
        invoices=tuple(
            _map_invoice_summary(item) for item in response.get("InvoiceSummaries", [])
        ),
        notes=(
            (
                "AWS invoice summaries are account-level totals and do not "
                "attribute cost to a single Glacier bucket."
            ),
        ),
    )


def _resolve_account_id(config: RuntimeConfig) -> str:
    response = _create_sts_client(config).get_caller_identity()
    account = response.get("Account")
    if account in (None, ""):
        raise RuntimeError("missing AWS account id")
    return str(account)


def _resource_level_unavailable(exc: Exception) -> bool:
    normalized = f"{exc.__class__.__name__}: {exc}".casefold()
    return (
        "dataunavailable" in normalized
        or "resource-level" in normalized
        or "resource id" in normalized
        or "14 days" in normalized
        or "billingview" in normalized
        or "granular data" in normalized
        or "chargeable data" in normalized
    )


def _map_actual_period(payload: dict[str, object]) -> GlacierBillingActual:
    metrics = payload.get("Total", {})
    if not isinstance(metrics, dict):
        metrics = {}
    cost_metric = metrics.get("UnblendedCost", {})
    usage_metric = metrics.get("UsageQuantity", {})
    cost_amount = _metric_amount(cost_metric)
    usage_amount = _metric_amount(usage_metric)
    usage_unit = _metric_unit(usage_metric)
    time_period = payload.get("TimePeriod", {})
    if not isinstance(time_period, dict):
        time_period = {}
    return GlacierBillingActual(
        start=str(time_period.get("Start", "")),
        end=str(time_period.get("End", "")),
        estimated=bool(payload.get("Estimated", False)),
        unblended_cost_usd=cost_amount,
        usage_quantity=usage_amount,
        usage_unit=usage_unit,
    )


def _map_forecast_period(
    payload: dict[str, object],
    *,
    currency_code: str,
) -> GlacierBillingForecast:
    time_period = payload.get("TimePeriod", {})
    if not isinstance(time_period, dict):
        time_period = {}
    return GlacierBillingForecast(
        start=str(time_period.get("Start", "")),
        end=str(time_period.get("End", "")),
        mean_cost_usd=_decimal_to_float(payload.get("MeanValue")),
        lower_bound_cost_usd=_optional_decimal_to_float(
            payload.get("PredictionIntervalLowerBound")
        ),
        upper_bound_cost_usd=_optional_decimal_to_float(
            payload.get("PredictionIntervalUpperBound")
        ),
        currency_code=currency_code,
    )


def _map_invoice_summary(payload: dict[str, object]) -> GlacierBillingInvoiceSummary:
    billing_period = payload.get("BillingPeriod", {})
    if not isinstance(billing_period, dict):
        billing_period = {}
    month = int(billing_period.get("Month", 1) or 1)
    year = int(billing_period.get("Year", 1970) or 1970)
    start = date(year, month, 1)
    end = _add_months(start, 1)
    entity = payload.get("Entity", {})
    if not isinstance(entity, dict):
        entity = {}
    base_amount = payload.get("BaseCurrencyAmount", {})
    if not isinstance(base_amount, dict):
        base_amount = {}
    payment_amount = payload.get("PaymentCurrencyAmount", {})
    if not isinstance(payment_amount, dict):
        payment_amount = {}
    return GlacierBillingInvoiceSummary(
        invoice_id=_optional_str(payload.get("InvoiceId")),
        account_id=_optional_str(payload.get("AccountId")),
        billing_period_start=start.isoformat(),
        billing_period_end=end.isoformat(),
        invoice_type=_optional_str(payload.get("InvoiceType")),
        invoicing_entity=_optional_str(entity.get("InvoicingEntity")),
        issued_at=_timestamp_to_iso(payload.get("IssuedDate")),
        due_at=_timestamp_to_iso(payload.get("DueDate")),
        base_currency_code=_optional_str(base_amount.get("CurrencyCode")),
        base_total_amount=_optional_decimal_to_float(base_amount.get("TotalAmount")),
        payment_currency_code=_optional_str(payment_amount.get("CurrencyCode")),
        payment_total_amount=_optional_decimal_to_float(payment_amount.get("TotalAmount")),
        original_invoice_id=_optional_str(payload.get("OriginalInvoiceId")),
    )


def _metric_amount(metric: object) -> float:
    return float(_metric_decimal(metric))


def _metric_decimal(metric: object) -> Decimal:
    if not isinstance(metric, dict):
        return Decimal("0")
    amount = metric.get("Amount")
    if amount in (None, ""):
        return Decimal("0")
    return Decimal(str(amount))


def _metric_unit(metric: object) -> str | None:
    if not isinstance(metric, dict):
        return None
    unit = metric.get("Unit")
    return str(unit) if unit not in (None, "") else None


def _dimension_expression(key: str, values: list[str]) -> dict[str, object]:
    return {"Dimensions": {"Key": key, "Values": values}}


def _month_start(value: date) -> date:
    return value.replace(day=1)


def _add_months(value: date, months: int) -> date:
    month_index = (value.year * 12 + (value.month - 1)) + months
    year = month_index // 12
    month = month_index % 12 + 1
    return date(year, month, 1)


def _timestamp_to_iso(value: object) -> str | None:
    if value in (None, ""):
        return None
    timestamp = float(Decimal(str(value)))
    if timestamp > 10_000_000_000:
        timestamp /= 1000.0
    return datetime.fromtimestamp(timestamp, tz=UTC).isoformat().replace("+00:00", "Z")


def _object_datetime(value: object) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    if value in (None, ""):
        return None
    timestamp = float(Decimal(str(value)))
    if timestamp > 10_000_000_000:
        timestamp /= 1000.0
    return datetime.fromtimestamp(timestamp, tz=UTC)


def _datetime_to_utc_iso(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _optional_str(value: object) -> str | None:
    if value in (None, ""):
        return None
    return str(value)


def _decimal_to_float(value: object) -> float:
    return float(Decimal(str(value)))


def _optional_decimal_to_float(value: object) -> float | None:
    if value in (None, ""):
        return None
    return _decimal_to_float(value)
