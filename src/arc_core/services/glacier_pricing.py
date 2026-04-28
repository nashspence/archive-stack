from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import Decimal
from threading import Lock
from time import monotonic
from typing import Any
from urllib.parse import urlparse
from urllib.request import urlopen

from arc_core.domain.models import GlacierPricingBasis
from arc_core.runtime_config import RuntimeConfig
from arc_core.stores.s3_support import _require_boto3
from arc_core.webhooks import utcnow

_PRICE_LIST_FORMAT = "json"
_S3_SERVICE_CODE = "AmazonS3"
_GLACIER_RATE_FALLBACK_VOLUME_TYPE = "IntelligentTieringDeepArchiveAccess"
_STANDARD_RATE_VOLUME_TYPE = "Standard"
_STANDARD_RATE_STORAGE_CLASS = "General Purpose"
_GLACIER_RATE_SOURCE = "aws_price_list_bulk_api"
_MANUAL_RATE_SOURCE = "manual"
_MANUAL_FALLBACK_SOURCE = "manual_fallback"


@dataclass(frozen=True)
class _AwsResolvedPricing:
    glacier_storage_rate_usd_per_gib_month: float
    standard_storage_rate_usd_per_gib_month: float
    currency_code: str
    region_code: str
    effective_at: str | None
    price_list_arn: str
    label: str


@dataclass(frozen=True)
class _CachedPricing:
    resolved: _AwsResolvedPricing
    expires_at_monotonic: float


_PRICING_CACHE: dict[tuple[str, str, str], _CachedPricing] = {}
_PRICING_CACHE_LOCK = Lock()


def resolve_glacier_pricing(config: RuntimeConfig) -> GlacierPricingBasis:
    manual_basis = _manual_glacier_pricing_basis(config, source=_MANUAL_RATE_SOURCE)
    if config.glacier_pricing_mode == "manual":
        return manual_basis
    if config.glacier_pricing_mode == "auto" and not _should_try_aws_pricing(config):
        return manual_basis

    try:
        resolved = _resolve_aws_pricing(config)
    except Exception:
        if config.glacier_pricing_mode == "aws":
            raise
        return _manual_glacier_pricing_basis(config, source=_MANUAL_FALLBACK_SOURCE)

    return GlacierPricingBasis(
        label=resolved.label,
        source=_GLACIER_RATE_SOURCE,
        storage_class=config.glacier_storage_class,
        currency_code=resolved.currency_code,
        region_code=resolved.region_code,
        effective_at=resolved.effective_at,
        price_list_arn=resolved.price_list_arn,
        glacier_storage_rate_usd_per_gib_month=resolved.glacier_storage_rate_usd_per_gib_month,
        standard_storage_rate_usd_per_gib_month=resolved.standard_storage_rate_usd_per_gib_month,
        archived_metadata_bytes_per_object=config.glacier_archived_metadata_bytes_per_object,
        standard_metadata_bytes_per_object=config.glacier_standard_metadata_bytes_per_object,
        minimum_storage_duration_days=config.glacier_minimum_storage_duration_days,
    )


def _manual_glacier_pricing_basis(config: RuntimeConfig, *, source: str) -> GlacierPricingBasis:
    return GlacierPricingBasis(
        label=config.glacier_pricing_label,
        source=source,
        storage_class=config.glacier_storage_class,
        currency_code=config.glacier_pricing_currency_code,
        region_code=config.glacier_pricing_region_code,
        effective_at=None,
        price_list_arn=None,
        glacier_storage_rate_usd_per_gib_month=config.glacier_storage_rate_usd_per_gib_month,
        standard_storage_rate_usd_per_gib_month=config.glacier_standard_rate_usd_per_gib_month,
        archived_metadata_bytes_per_object=config.glacier_archived_metadata_bytes_per_object,
        standard_metadata_bytes_per_object=config.glacier_standard_metadata_bytes_per_object,
        minimum_storage_duration_days=config.glacier_minimum_storage_duration_days,
    )


def _should_try_aws_pricing(config: RuntimeConfig) -> bool:
    if config.glacier_pricing_mode == "aws":
        return True
    if config.glacier_backend.casefold() == "aws":
        return True
    endpoint_url = config.glacier_endpoint_url.strip()
    if not endpoint_url:
        return True
    parsed = urlparse(endpoint_url)
    host = (parsed.netloc or parsed.path).casefold()
    return "amazonaws.com" in host


def _resolve_aws_pricing(config: RuntimeConfig) -> _AwsResolvedPricing:
    cache_ttl_seconds = max(config.glacier_pricing_cache_ttl.total_seconds(), 0.0)
    cache_key = (
        config.glacier_pricing_api_region,
        config.glacier_pricing_region_code,
        config.glacier_pricing_currency_code,
    )
    if cache_ttl_seconds > 0:
        with _PRICING_CACHE_LOCK:
            cached = _PRICING_CACHE.get(cache_key)
            if cached is not None and cached.expires_at_monotonic >= monotonic():
                return cached.resolved

    pricing_client = _create_pricing_client(config)
    price_list_ref = _latest_price_list_reference(
        pricing_client,
        region_code=config.glacier_pricing_region_code,
        currency_code=config.glacier_pricing_currency_code,
    )
    price_list_url = pricing_client.get_price_list_file_url(
        PriceListArn=price_list_ref["PriceListArn"],
        FileFormat=_PRICE_LIST_FORMAT,
    )["Url"]
    price_list_document = _download_price_list_json(price_list_url)
    standard_rate, standard_effective_at = _find_standard_storage_rate(
        price_list_document,
        region_code=config.glacier_pricing_region_code,
    )
    glacier_rate, glacier_effective_at = _find_glacier_storage_rate(
        price_list_document,
        region_code=config.glacier_pricing_region_code,
    )
    effective_at = max(
        [value for value in (standard_effective_at, glacier_effective_at) if value],
        default=None,
    )
    resolved = _AwsResolvedPricing(
        glacier_storage_rate_usd_per_gib_month=glacier_rate,
        standard_storage_rate_usd_per_gib_month=standard_rate,
        currency_code=config.glacier_pricing_currency_code,
        region_code=config.glacier_pricing_region_code,
        effective_at=effective_at,
        price_list_arn=price_list_ref["PriceListArn"],
        label=(
            f"aws-price-list-bulk-api:{config.glacier_pricing_currency_code}:"
            f"{config.glacier_pricing_region_code}"
        ),
    )
    if cache_ttl_seconds > 0:
        with _PRICING_CACHE_LOCK:
            _PRICING_CACHE[cache_key] = _CachedPricing(
                resolved=resolved,
                expires_at_monotonic=monotonic() + cache_ttl_seconds,
            )
    return resolved


def _create_pricing_client(config: RuntimeConfig) -> Any:
    boto3, Config = _require_boto3()
    return boto3.client(
        "pricing",
        region_name=config.glacier_pricing_api_region,
        config=Config(retries={"max_attempts": 3, "mode": "standard"}),
    )


def _latest_price_list_reference(
    pricing_client: Any,
    *,
    region_code: str,
    currency_code: str,
) -> dict[str, Any]:
    response = pricing_client.list_price_lists(
        ServiceCode=_S3_SERVICE_CODE,
        CurrencyCode=currency_code,
        RegionCode=region_code,
        EffectiveDate=utcnow(),
    )
    price_lists = response.get("PriceLists", [])
    if not price_lists:
        raise ValueError(
            f"no AWS price list found for {_S3_SERVICE_CODE} {currency_code} {region_code}"
        )
    json_lists = [
        entry for entry in price_lists if _PRICE_LIST_FORMAT in entry.get("FileFormats", [])
    ]
    if not json_lists:
        raise ValueError(
            f"no JSON AWS price list found for {_S3_SERVICE_CODE} {currency_code} {region_code}"
        )
    return sorted(json_lists, key=lambda entry: entry["PriceListArn"], reverse=True)[0]


def _download_price_list_json(url: str) -> dict[str, Any]:
    with urlopen(url, timeout=30) as response:
        return json.load(response)


def _find_standard_storage_rate(
    price_list_document: dict[str, Any],
    *,
    region_code: str,
) -> tuple[float, str | None]:
    candidates = [
        candidate
        for candidate in _iter_storage_rate_candidates(price_list_document, region_code=region_code)
        if candidate["product_family"] == "Storage"
        and candidate["storage_class"] == _STANDARD_RATE_STORAGE_CLASS
        and candidate["volume_type"] == _STANDARD_RATE_VOLUME_TYPE
        and candidate["begin_range"] == Decimal("0")
    ]
    if not candidates:
        raise ValueError(f"missing AWS S3 Standard storage rate for region {region_code}")
    chosen = sorted(candidates, key=lambda candidate: candidate["price"])[0]
    return float(chosen["price"]), chosen["effective_at"]


def _find_glacier_storage_rate(
    price_list_document: dict[str, Any],
    *,
    region_code: str,
) -> tuple[float, str | None]:
    candidates = [
        candidate
        for candidate in _iter_storage_rate_candidates(price_list_document, region_code=region_code)
        if candidate["product_family"] == "Storage"
    ]
    direct_candidates = [
        candidate
        for candidate in candidates
        if candidate["volume_type"] == "Glacier Deep Archive"
        and "staging" not in candidate["usagetype"].casefold()
        and "staging" not in candidate["description"].casefold()
    ]
    if direct_candidates:
        chosen = sorted(direct_candidates, key=lambda candidate: candidate["begin_range"])[0]
        return float(chosen["price"]), chosen["effective_at"]

    inferred_candidates = [
        candidate
        for candidate in candidates
        if candidate["volume_type"] == _GLACIER_RATE_FALLBACK_VOLUME_TYPE
        and candidate["begin_range"] == Decimal("0")
    ]
    if inferred_candidates:
        chosen = sorted(inferred_candidates, key=lambda candidate: candidate["price"])[0]
        return float(chosen["price"]), chosen["effective_at"]

    raise ValueError(f"missing AWS Glacier Deep Archive storage rate for region {region_code}")


def _iter_storage_rate_candidates(
    price_list_document: dict[str, Any],
    *,
    region_code: str,
) -> list[dict[str, Any]]:
    products = price_list_document.get("products", {})
    on_demand_terms = price_list_document.get("terms", {}).get("OnDemand", {})
    candidates: list[dict[str, Any]] = []

    for sku, product in products.items():
        attributes = product.get("attributes", {})
        if attributes.get("regionCode") != region_code:
            continue
        for term in on_demand_terms.get(sku, {}).values():
            effective_at = term.get("effectiveDate")
            for dimension in term.get("priceDimensions", {}).values():
                if dimension.get("unit") != "GB-Mo":
                    continue
                price_text = dimension.get("pricePerUnit", {}).get("USD", "")
                if not price_text:
                    continue
                candidates.append(
                    {
                        "product_family": product.get("productFamily"),
                        "storage_class": attributes.get("storageClass", ""),
                        "volume_type": attributes.get("volumeType", ""),
                        "usagetype": attributes.get("usagetype", ""),
                        "description": dimension.get("description", ""),
                        "begin_range": Decimal(dimension.get("beginRange", "0")),
                        "price": Decimal(price_text),
                        "effective_at": effective_at,
                    }
                )
    return candidates
