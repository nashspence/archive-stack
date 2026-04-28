from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path

_DURATION_RE = re.compile(r"^(?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s)?$")


def _parse_duration(value: str) -> timedelta:
    m = _DURATION_RE.match(value.strip())
    if not m or not any(m.groups()):
        raise ValueError(f"invalid duration {value!r}: expected format like '24h', '30m', '90s'")
    hours = int(m.group(1) or 0)
    minutes = int(m.group(2) or 0)
    seconds = int(m.group(3) or 0)
    return timedelta(hours=hours, minutes=minutes, seconds=seconds)


def _parse_bool(value: str) -> bool:
    normalized = value.strip().casefold()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"invalid boolean {value!r}")


def _parse_int(value: str, *, name: str, minimum: int = 0) -> int:
    parsed = int(value.strip())
    if parsed < minimum:
        raise ValueError(f"invalid {name} {value!r}: expected >= {minimum}")
    return parsed


def _parse_float(value: str, *, name: str, minimum: float = 0.0) -> float:
    parsed = float(value.strip())
    if parsed < minimum:
        raise ValueError(f"invalid {name} {value!r}: expected >= {minimum}")
    return parsed


def _parse_choice(value: str, *, name: str, allowed: set[str]) -> str:
    normalized = value.strip().casefold().replace("-", "_")
    if normalized not in allowed:
        expected = ", ".join(sorted(allowed))
        raise ValueError(f"invalid {name} {value!r}: expected one of {expected}")
    return normalized


def _normalize_prefix(value: str) -> str:
    parts = [part for part in value.strip().strip("/").split("/") if part]
    if not parts:
        raise ValueError("ARC_GLACIER_PREFIX must not be empty")
    return "/".join(parts)


@dataclass(frozen=True, slots=True)
class RuntimeConfig:
    object_store: str
    s3_endpoint_url: str
    s3_region: str
    s3_bucket: str
    s3_access_key_id: str
    s3_secret_access_key: str
    s3_force_path_style: bool
    tusd_base_url: str
    tusd_hook_secret: str
    sqlite_path: Path
    incomplete_upload_ttl: timedelta = field(default_factory=lambda: timedelta(hours=24))
    upload_expiry_sweep_interval: timedelta = field(default_factory=lambda: timedelta(seconds=30))
    glacier_endpoint_url: str = "http://127.0.0.1:9000"
    glacier_region: str = "us-east-1"
    glacier_bucket: str = "riverhog"
    glacier_access_key_id: str = "minioadmin"
    glacier_secret_access_key: str = "minioadmin"
    glacier_force_path_style: bool = True
    glacier_prefix: str = "glacier/finalized-images"
    glacier_backend: str = "s3"
    glacier_storage_class: str = "DEEP_ARCHIVE"
    glacier_upload_retry_limit: int = 3
    glacier_upload_retry_delay: timedelta = field(default_factory=lambda: timedelta(minutes=5))
    glacier_upload_sweep_interval: timedelta = field(default_factory=lambda: timedelta(seconds=30))
    glacier_failure_webhook_url: str | None = None
    glacier_recovery_sweep_interval: timedelta = field(
        default_factory=lambda: timedelta(seconds=30)
    )
    glacier_recovery_restore_latency: timedelta = field(
        default_factory=lambda: timedelta(hours=48)
    )
    glacier_recovery_ready_ttl: timedelta = field(default_factory=lambda: timedelta(hours=24))
    glacier_recovery_webhook_url: str | None = None
    glacier_recovery_webhook_retry_delay: timedelta = field(
        default_factory=lambda: timedelta(minutes=1)
    )
    glacier_recovery_webhook_reminder_interval: timedelta = field(
        default_factory=lambda: timedelta(hours=1)
    )
    glacier_recovery_retrieval_tier: str = "bulk"
    glacier_bulk_retrieval_rate_usd_per_gib: float = 0.0025
    glacier_bulk_request_rate_usd_per_1000: float = 0.025
    glacier_standard_retrieval_rate_usd_per_gib: float = 0.02
    glacier_standard_request_rate_usd_per_1000: float = 0.10
    glacier_pricing_label: str = "aws-s3-us-west-2-public"
    glacier_pricing_mode: str = "auto"
    glacier_pricing_api_region: str = "us-east-1"
    glacier_pricing_region_code: str = "us-west-2"
    glacier_pricing_currency_code: str = "USD"
    glacier_pricing_cache_ttl: timedelta = field(default_factory=lambda: timedelta(hours=24))
    glacier_billing_mode: str = "auto"
    glacier_billing_api_region: str = "us-east-1"
    glacier_billing_currency_code: str = "USD"
    glacier_billing_lookback_months: int = 3
    glacier_billing_forecast_months: int = 1
    glacier_billing_view_arn: str | None = None
    glacier_billing_tag_key: str | None = None
    glacier_billing_tag_value: str | None = None
    glacier_billing_export_arn: str | None = None
    glacier_billing_export_bucket: str | None = None
    glacier_billing_export_prefix: str | None = None
    glacier_billing_export_region: str = "us-east-1"
    glacier_billing_export_max_items: int = 10
    glacier_billing_invoice_account_id: str | None = None
    glacier_billing_invoice_max_items: int = 6
    glacier_storage_rate_usd_per_gib_month: float = 0.00099
    glacier_standard_rate_usd_per_gib_month: float = 0.023
    glacier_archived_metadata_bytes_per_object: int = 32 * 1024
    glacier_standard_metadata_bytes_per_object: int = 8 * 1024
    glacier_minimum_storage_duration_days: int = 180
    public_base_url: str | None = None


def load_runtime_config() -> RuntimeConfig:
    object_store = os.getenv("ARC_OBJECT_STORE", "s3").strip().casefold() or "s3"
    if object_store != "s3":
        raise ValueError(f"unsupported ARC_OBJECT_STORE {object_store!r}: expected 's3'")

    sqlite_path_raw = os.getenv("ARC_DB_PATH", ".arc/state.sqlite3")
    ttl_raw = os.getenv("INCOMPLETE_UPLOAD_TTL", "24h")
    sweep_raw = os.getenv("UPLOAD_EXPIRY_SWEEP_INTERVAL", "30s")

    sqlite_path = Path(sqlite_path_raw).expanduser().resolve()
    sqlite_path.parent.mkdir(parents=True, exist_ok=True)
    incomplete_upload_ttl = _parse_duration(ttl_raw)
    upload_expiry_sweep_interval = _parse_duration(sweep_raw)
    s3_endpoint_url = os.getenv("ARC_S3_ENDPOINT_URL", "http://127.0.0.1:9000").rstrip("/")
    s3_region = os.getenv("ARC_S3_REGION", "us-east-1")
    s3_bucket = os.getenv("ARC_S3_BUCKET", "riverhog")
    s3_access_key_id = os.getenv("ARC_S3_ACCESS_KEY_ID", "minioadmin")
    s3_secret_access_key = os.getenv("ARC_S3_SECRET_ACCESS_KEY", "minioadmin")
    s3_force_path_style = _parse_bool(os.getenv("ARC_S3_FORCE_PATH_STYLE", "true"))

    glacier_retry_limit = _parse_int(
        os.getenv("ARC_GLACIER_UPLOAD_RETRY_LIMIT", "3"),
        name="ARC_GLACIER_UPLOAD_RETRY_LIMIT",
        minimum=1,
    )
    glacier_retry_delay = _parse_duration(os.getenv("ARC_GLACIER_UPLOAD_RETRY_DELAY", "5m"))
    glacier_upload_sweep_interval = _parse_duration(
        os.getenv("ARC_GLACIER_UPLOAD_SWEEP_INTERVAL", "30s")
    )
    glacier_failure_webhook_url = os.getenv("ARC_GLACIER_FAILURE_WEBHOOK_URL", "").strip() or None
    glacier_recovery_sweep_interval = _parse_duration(
        os.getenv("ARC_GLACIER_RECOVERY_SWEEP_INTERVAL", "30s")
    )
    glacier_recovery_restore_latency = _parse_duration(
        os.getenv("ARC_GLACIER_RECOVERY_RESTORE_LATENCY", "48h")
    )
    glacier_recovery_ready_ttl = _parse_duration(
        os.getenv("ARC_GLACIER_RECOVERY_READY_TTL", "24h")
    )
    glacier_recovery_webhook_url = (
        os.getenv("ARC_GLACIER_RECOVERY_WEBHOOK_URL", "").strip() or None
    )
    glacier_recovery_webhook_retry_delay = _parse_duration(
        os.getenv("ARC_GLACIER_RECOVERY_WEBHOOK_RETRY_DELAY", "60s")
    )
    glacier_recovery_webhook_reminder_interval = _parse_duration(
        os.getenv("ARC_GLACIER_RECOVERY_WEBHOOK_REMINDER_INTERVAL", "1h")
    )
    glacier_recovery_retrieval_tier = _parse_choice(
        os.getenv("ARC_GLACIER_RECOVERY_RETRIEVAL_TIER", "bulk"),
        name="ARC_GLACIER_RECOVERY_RETRIEVAL_TIER",
        allowed={"bulk", "standard"},
    )
    glacier_bulk_retrieval_rate_usd_per_gib = _parse_float(
        os.getenv("ARC_GLACIER_BULK_RETRIEVAL_RATE_USD_PER_GIB", "0.0025"),
        name="ARC_GLACIER_BULK_RETRIEVAL_RATE_USD_PER_GIB",
    )
    glacier_bulk_request_rate_usd_per_1000 = _parse_float(
        os.getenv("ARC_GLACIER_BULK_REQUEST_RATE_USD_PER_1000", "0.025"),
        name="ARC_GLACIER_BULK_REQUEST_RATE_USD_PER_1000",
    )
    glacier_standard_retrieval_rate_usd_per_gib = _parse_float(
        os.getenv("ARC_GLACIER_STANDARD_RETRIEVAL_RATE_USD_PER_GIB", "0.02"),
        name="ARC_GLACIER_STANDARD_RETRIEVAL_RATE_USD_PER_GIB",
    )
    glacier_standard_request_rate_usd_per_1000 = _parse_float(
        os.getenv("ARC_GLACIER_STANDARD_REQUEST_RATE_USD_PER_1000", "0.10"),
        name="ARC_GLACIER_STANDARD_REQUEST_RATE_USD_PER_1000",
    )
    glacier_pricing_label = (
        os.getenv("ARC_GLACIER_PRICING_LABEL", "aws-s3-us-west-2-public").strip()
        or "aws-s3-us-west-2-public"
    )
    glacier_pricing_mode = _parse_choice(
        os.getenv("ARC_GLACIER_PRICING_MODE", "auto"),
        name="ARC_GLACIER_PRICING_MODE",
        allowed={"auto", "aws", "manual"},
    )
    glacier_pricing_api_region = (
        os.getenv("ARC_GLACIER_PRICING_API_REGION", "us-east-1").strip() or "us-east-1"
    )
    glacier_pricing_region_code = (
        os.getenv(
            "ARC_GLACIER_PRICING_REGION_CODE",
            os.getenv("ARC_GLACIER_REGION", s3_region),
        ).strip()
        or os.getenv("ARC_GLACIER_REGION", s3_region)
    )
    glacier_pricing_currency_code = (
        os.getenv("ARC_GLACIER_PRICING_CURRENCY_CODE", "USD").strip().upper() or "USD"
    )
    glacier_pricing_cache_ttl = _parse_duration(
        os.getenv("ARC_GLACIER_PRICING_CACHE_TTL", "24h")
    )
    glacier_billing_mode = _parse_choice(
        os.getenv("ARC_GLACIER_BILLING_MODE", "auto"),
        name="ARC_GLACIER_BILLING_MODE",
        allowed={"auto", "aws", "disabled"},
    )
    glacier_billing_api_region = (
        os.getenv("ARC_GLACIER_BILLING_API_REGION", "us-east-1").strip() or "us-east-1"
    )
    glacier_billing_currency_code = (
        os.getenv("ARC_GLACIER_BILLING_CURRENCY_CODE", "USD").strip().upper() or "USD"
    )
    glacier_billing_lookback_months = _parse_int(
        os.getenv("ARC_GLACIER_BILLING_LOOKBACK_MONTHS", "3"),
        name="ARC_GLACIER_BILLING_LOOKBACK_MONTHS",
        minimum=1,
    )
    glacier_billing_forecast_months = _parse_int(
        os.getenv("ARC_GLACIER_BILLING_FORECAST_MONTHS", "1"),
        name="ARC_GLACIER_BILLING_FORECAST_MONTHS",
        minimum=1,
    )
    glacier_billing_view_arn = os.getenv("ARC_GLACIER_BILLING_VIEW_ARN", "").strip() or None
    glacier_billing_tag_key = os.getenv("ARC_GLACIER_BILLING_TAG_KEY", "").strip() or None
    glacier_billing_tag_value = os.getenv("ARC_GLACIER_BILLING_TAG_VALUE", "").strip() or None
    glacier_billing_export_arn = (
        os.getenv("ARC_GLACIER_BILLING_EXPORT_ARN", "").strip() or None
    )
    glacier_billing_export_bucket = (
        os.getenv("ARC_GLACIER_BILLING_EXPORT_BUCKET", "").strip() or None
    )
    glacier_billing_export_prefix = (
        os.getenv("ARC_GLACIER_BILLING_EXPORT_PREFIX", "").strip().strip("/") or None
    )
    glacier_billing_export_region = (
        os.getenv("ARC_GLACIER_BILLING_EXPORT_REGION", "us-east-1").strip() or "us-east-1"
    )
    glacier_billing_export_max_items = _parse_int(
        os.getenv("ARC_GLACIER_BILLING_EXPORT_MAX_ITEMS", "10"),
        name="ARC_GLACIER_BILLING_EXPORT_MAX_ITEMS",
        minimum=1,
    )
    glacier_billing_invoice_account_id = (
        os.getenv("ARC_GLACIER_BILLING_INVOICE_ACCOUNT_ID", "").strip() or None
    )
    glacier_billing_invoice_max_items = _parse_int(
        os.getenv("ARC_GLACIER_BILLING_INVOICE_MAX_ITEMS", "6"),
        name="ARC_GLACIER_BILLING_INVOICE_MAX_ITEMS",
        minimum=1,
    )
    glacier_storage_rate_usd_per_gib_month = _parse_float(
        os.getenv("ARC_GLACIER_STORAGE_RATE_USD_PER_GIB_MONTH", "0.00099"),
        name="ARC_GLACIER_STORAGE_RATE_USD_PER_GIB_MONTH",
    )
    glacier_standard_rate_usd_per_gib_month = _parse_float(
        os.getenv("ARC_GLACIER_STANDARD_RATE_USD_PER_GIB_MONTH", "0.023"),
        name="ARC_GLACIER_STANDARD_RATE_USD_PER_GIB_MONTH",
    )
    glacier_archived_metadata_bytes_per_object = _parse_int(
        os.getenv("ARC_GLACIER_ARCHIVED_METADATA_BYTES_PER_OBJECT", str(32 * 1024)),
        name="ARC_GLACIER_ARCHIVED_METADATA_BYTES_PER_OBJECT",
    )
    glacier_standard_metadata_bytes_per_object = _parse_int(
        os.getenv("ARC_GLACIER_STANDARD_METADATA_BYTES_PER_OBJECT", str(8 * 1024)),
        name="ARC_GLACIER_STANDARD_METADATA_BYTES_PER_OBJECT",
    )
    glacier_minimum_storage_duration_days = _parse_int(
        os.getenv("ARC_GLACIER_MINIMUM_STORAGE_DURATION_DAYS", "180"),
        name="ARC_GLACIER_MINIMUM_STORAGE_DURATION_DAYS",
        minimum=1,
    )
    public_base_url = os.getenv("ARC_PUBLIC_BASE_URL", "").strip() or None

    return RuntimeConfig(
        object_store=object_store,
        s3_endpoint_url=s3_endpoint_url,
        s3_region=s3_region,
        s3_bucket=s3_bucket,
        s3_access_key_id=s3_access_key_id,
        s3_secret_access_key=s3_secret_access_key,
        s3_force_path_style=s3_force_path_style,
        tusd_base_url=os.getenv("ARC_TUSD_BASE_URL", "http://127.0.0.1:1080/files").rstrip("/"),
        tusd_hook_secret=os.getenv("ARC_TUSD_HOOK_SECRET", "dev-tusd-hook-secret"),
        sqlite_path=sqlite_path,
        incomplete_upload_ttl=incomplete_upload_ttl,
        upload_expiry_sweep_interval=upload_expiry_sweep_interval,
        glacier_endpoint_url=os.getenv("ARC_GLACIER_ENDPOINT_URL", s3_endpoint_url).rstrip("/"),
        glacier_region=os.getenv("ARC_GLACIER_REGION", s3_region),
        glacier_bucket=os.getenv("ARC_GLACIER_BUCKET", s3_bucket),
        glacier_access_key_id=os.getenv("ARC_GLACIER_ACCESS_KEY_ID", s3_access_key_id),
        glacier_secret_access_key=os.getenv(
            "ARC_GLACIER_SECRET_ACCESS_KEY",
            s3_secret_access_key,
        ),
        glacier_force_path_style=_parse_bool(
            os.getenv("ARC_GLACIER_FORCE_PATH_STYLE", str(s3_force_path_style).lower())
        ),
        glacier_prefix=_normalize_prefix(
            os.getenv("ARC_GLACIER_PREFIX", "glacier/finalized-images")
        ),
        glacier_backend=os.getenv("ARC_GLACIER_BACKEND", "s3").strip() or "s3",
        glacier_storage_class=os.getenv("ARC_GLACIER_STORAGE_CLASS", "DEEP_ARCHIVE").strip()
        or "DEEP_ARCHIVE",
        glacier_upload_retry_limit=glacier_retry_limit,
        glacier_upload_retry_delay=glacier_retry_delay,
        glacier_upload_sweep_interval=glacier_upload_sweep_interval,
        glacier_failure_webhook_url=glacier_failure_webhook_url,
        glacier_recovery_sweep_interval=glacier_recovery_sweep_interval,
        glacier_recovery_restore_latency=glacier_recovery_restore_latency,
        glacier_recovery_ready_ttl=glacier_recovery_ready_ttl,
        glacier_recovery_webhook_url=glacier_recovery_webhook_url,
        glacier_recovery_webhook_retry_delay=glacier_recovery_webhook_retry_delay,
        glacier_recovery_webhook_reminder_interval=glacier_recovery_webhook_reminder_interval,
        glacier_recovery_retrieval_tier=glacier_recovery_retrieval_tier,
        glacier_bulk_retrieval_rate_usd_per_gib=glacier_bulk_retrieval_rate_usd_per_gib,
        glacier_bulk_request_rate_usd_per_1000=glacier_bulk_request_rate_usd_per_1000,
        glacier_standard_retrieval_rate_usd_per_gib=glacier_standard_retrieval_rate_usd_per_gib,
        glacier_standard_request_rate_usd_per_1000=glacier_standard_request_rate_usd_per_1000,
        glacier_pricing_label=glacier_pricing_label,
        glacier_pricing_mode=glacier_pricing_mode,
        glacier_pricing_api_region=glacier_pricing_api_region,
        glacier_pricing_region_code=glacier_pricing_region_code,
        glacier_pricing_currency_code=glacier_pricing_currency_code,
        glacier_pricing_cache_ttl=glacier_pricing_cache_ttl,
        glacier_billing_mode=glacier_billing_mode,
        glacier_billing_api_region=glacier_billing_api_region,
        glacier_billing_currency_code=glacier_billing_currency_code,
        glacier_billing_lookback_months=glacier_billing_lookback_months,
        glacier_billing_forecast_months=glacier_billing_forecast_months,
        glacier_billing_view_arn=glacier_billing_view_arn,
        glacier_billing_tag_key=glacier_billing_tag_key,
        glacier_billing_tag_value=glacier_billing_tag_value,
        glacier_billing_export_arn=glacier_billing_export_arn,
        glacier_billing_export_bucket=glacier_billing_export_bucket,
        glacier_billing_export_prefix=glacier_billing_export_prefix,
        glacier_billing_export_region=glacier_billing_export_region,
        glacier_billing_export_max_items=glacier_billing_export_max_items,
        glacier_billing_invoice_account_id=glacier_billing_invoice_account_id,
        glacier_billing_invoice_max_items=glacier_billing_invoice_max_items,
        glacier_storage_rate_usd_per_gib_month=glacier_storage_rate_usd_per_gib_month,
        glacier_standard_rate_usd_per_gib_month=glacier_standard_rate_usd_per_gib_month,
        glacier_archived_metadata_bytes_per_object=glacier_archived_metadata_bytes_per_object,
        glacier_standard_metadata_bytes_per_object=glacier_standard_metadata_bytes_per_object,
        glacier_minimum_storage_duration_days=glacier_minimum_storage_duration_days,
        public_base_url=public_base_url,
    )
