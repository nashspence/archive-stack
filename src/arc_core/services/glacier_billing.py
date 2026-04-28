from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any

from arc_core.domain.models import (
    GlacierBillingActual,
    GlacierBillingForecast,
    GlacierBillingSummary,
)
from arc_core.runtime_config import RuntimeConfig
from arc_core.stores.s3_support import _require_boto3

_AWS_COST_EXPLORER_SOURCE = "aws_cost_explorer"
_UNAVAILABLE_SOURCE = "unavailable"
_S3_SERVICE_NAME = "Amazon Simple Storage Service"


@dataclass(frozen=True)
class _CostExplorerScope:
    name: str
    expression: dict[str, object]
    label: str
    notes: tuple[str, ...]


def resolve_glacier_billing(
    config: RuntimeConfig,
    *,
    include: bool,
) -> GlacierBillingSummary | None:
    if not include:
        return None
    if config.glacier_billing_mode == "disabled":
        return GlacierBillingSummary(
            source=_UNAVAILABLE_SOURCE,
            scope="disabled",
            notes=("AWS Cost Explorer billing queries are disabled for this runtime.",),
        )
    if config.glacier_billing_mode == "auto" and not _should_try_aws_billing(config):
        return GlacierBillingSummary(
            source=_UNAVAILABLE_SOURCE,
            scope="unavailable",
            notes=("AWS Cost Explorer billing is unavailable for this runtime.",),
        )

    try:
        scope = _billing_scope(config)
        client = _create_cost_explorer_client(config)
        actuals = _load_actual_costs(client, config=config, scope=scope)
        forecast = _load_cost_forecast(client, config=config, scope=scope)
    except Exception:
        if config.glacier_billing_mode == "aws":
            raise
        return GlacierBillingSummary(
            source=_UNAVAILABLE_SOURCE,
            scope="unavailable",
            notes=("AWS Cost Explorer billing could not be resolved for this runtime.",),
        )

    return GlacierBillingSummary(
        source=_AWS_COST_EXPLORER_SOURCE,
        scope=scope.name,
        filter_label=scope.label,
        service=_S3_SERVICE_NAME,
        currency_code=config.glacier_billing_currency_code,
        history_granularity="MONTHLY",
        forecast_granularity="MONTHLY",
        actuals=actuals,
        forecast=forecast,
        notes=scope.notes,
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
            "Billing is scoped to the Amazon S3 service in the configured Glacier region.",
            (
                "Set ARC_GLACIER_BILLING_TAG_KEY and ARC_GLACIER_BILLING_TAG_VALUE "
                "for archive-specific billing attribution."
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
        lower_bound_cost_usd=_optional_decimal_to_float(payload.get("PredictionIntervalLowerBound")),
        upper_bound_cost_usd=_optional_decimal_to_float(payload.get("PredictionIntervalUpperBound")),
        currency_code=currency_code,
    )


def _metric_amount(metric: object) -> float | None:
    if not isinstance(metric, dict):
        return None
    amount = metric.get("Amount")
    if amount in (None, ""):
        return None
    return _decimal_to_float(amount)


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


def _decimal_to_float(value: object) -> float:
    return float(Decimal(str(value)))


def _optional_decimal_to_float(value: object) -> float | None:
    if value in (None, ""):
        return None
    return _decimal_to_float(value)
