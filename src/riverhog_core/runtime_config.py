from __future__ import annotations

import os
import re
import shlex
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from riverhog_core.operator_reminders import (
    next_operator_reminder_at,
    normalize_reminder_time,
    reminder_zone,
)

_DURATION_RE = re.compile(r"^(?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s)?$")
_BYTES_RE = re.compile(r"^(\d+(?:_\d+)*)([kmgt]i?b?|b)?$", re.IGNORECASE)
DEV_RECOVERY_PAYLOAD_PASSPHRASE = "riverhog-dev-recovery-passphrase"
DEFAULT_DATABASE_URL = "postgresql+psycopg://riverhog:riverhog@127.0.0.1:5432/riverhog"
DEFAULT_PLANNER_DISC_TARGET_BYTES = 50_000_000_000
DEFAULT_PLANNER_MIN_FILL_RATIO = 0.99
DEFAULT_PLANNER_UNPLANNED_SATURATION_BYTES = 300_000_000_000
DEFAULT_UNBURNED_COLLECTION_BYTES_LIMIT = 500_000_000_000
DEFAULT_GLACIER_MULTIPART_PART_BYTES = 64 * 1024 * 1024
DEFAULT_GLACIER_MULTIPART_CONCURRENCY = 4
DEFAULT_HOT_PROMOTION_CONCURRENCY = 8
DEFAULT_HOT_SINGLE_PUT_MAX_BYTES = 64 * 1024 * 1024
DEFAULT_S3_MAX_POOL_CONNECTIONS = 32
DEFAULT_GLACIER_ARCHIVE_WORK_FACTOR = 18
DEFAULT_LOG_LEVEL = "INFO"
DEFAULT_RECOVERY_PAYLOAD_WORK_FACTOR = 12
DEFAULT_RECOVERY_PAYLOAD_MAX_WORK_FACTOR = 30


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


def _parse_bytes(value: str, *, name: str, minimum: int = 0) -> int:
    raw = value.strip().replace(" ", "")
    match = _BYTES_RE.fullmatch(raw)
    if match is None:
        raise ValueError(f"invalid {name} {value!r}: expected bytes like '500GB' or '536870912000'")
    amount = int(match.group(1).replace("_", ""))
    unit = (match.group(2) or "b").casefold()
    scale = {
        "b": 1,
        "kb": 1_000,
        "k": 1_000,
        "mb": 1_000_000,
        "m": 1_000_000,
        "gb": 1_000_000_000,
        "g": 1_000_000_000,
        "tb": 1_000_000_000_000,
        "t": 1_000_000_000_000,
        "kib": 1024,
        "mib": 1024**2,
        "gib": 1024**3,
        "tib": 1024**4,
    }[unit]
    parsed = amount * scale
    if parsed < minimum:
        raise ValueError(f"invalid {name} {value!r}: expected >= {minimum}")
    return parsed


def _parse_float(value: str, *, name: str, minimum: float = 0.0) -> float:
    parsed = float(value.strip())
    if parsed < minimum:
        raise ValueError(f"invalid {name} {value!r}: expected >= {minimum}")
    return parsed


def _parse_ratio(value: str, *, name: str) -> float:
    raw = value.strip()
    if raw.endswith("%"):
        parsed = float(raw[:-1].strip()) / 100.0
    else:
        parsed = float(raw)
    if parsed <= 0.0 or parsed > 1.0:
        raise ValueError(f"invalid {name} {value!r}: expected a ratio in (0, 1]")
    return parsed


def _parse_choice(value: str, *, name: str, allowed: set[str]) -> str:
    normalized = value.strip().casefold().replace("-", "_")
    if normalized not in allowed:
        expected = ", ".join(sorted(allowed))
        raise ValueError(f"invalid {name} {value!r}: expected one of {expected}")
    return normalized


_RECIPIENT_NAME_CHARS = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.-")


def _normalize_notify_recipient(value: str, *, name: str) -> str:
    recipient = value.strip()
    if not recipient:
        raise ValueError(f"{name} must not contain blank recipients")
    if any(ch not in _RECIPIENT_NAME_CHARS for ch in recipient):
        raise ValueError(
            f"{name} recipients may contain only letters, digits, dots, underscores, and dashes"
        )
    return recipient


def _parse_recipient_list(value: str, *, name: str) -> tuple[str, ...]:
    recipients: list[str] = []
    for raw in value.split(","):
        if not raw.strip():
            continue
        recipient = _normalize_notify_recipient(raw, name=name)
        if recipient not in recipients:
            recipients.append(recipient)
    return tuple(recipients)


def _env_recipient_suffix(recipient: str) -> str:
    return "".join(ch.upper() if ch.isalnum() else "_" for ch in recipient)


def parse_notify_webhook_map(values: Mapping[str, str]) -> dict[str, str]:
    import json

    webhooks: dict[str, str] = {}
    raw = values.get("RIVERHOG_NOTIFY_WEBHOOKS", "").strip()
    if raw:
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError("RIVERHOG_NOTIFY_WEBHOOKS must be a JSON object") from exc
        if not isinstance(payload, dict):
            raise ValueError("RIVERHOG_NOTIFY_WEBHOOKS must be a JSON object")
        for raw_recipient, raw_url in payload.items():
            recipient = _normalize_notify_recipient(
                str(raw_recipient),
                name="RIVERHOG_NOTIFY_WEBHOOKS",
            )
            if not isinstance(raw_url, str) or not raw_url.strip():
                raise ValueError("RIVERHOG_NOTIFY_WEBHOOKS values must be non-empty URLs")
            webhooks[recipient] = raw_url.strip()

    prefix = "RIVERHOG_NOTIFY_WEBHOOK_"
    for key, raw_url in values.items():
        if not key.startswith(prefix) or key == "RIVERHOG_NOTIFY_WEBHOOKS":
            continue
        suffix = key[len(prefix) :]
        if not suffix:
            continue
        env_recipient = suffix.lower().replace("_", "-")
        recipient = next(
            (name for name in webhooks if _env_recipient_suffix(name) == suffix),
            env_recipient,
        )
        recipient = _normalize_notify_recipient(recipient, name=key)
        if raw_url.strip():
            webhooks[recipient] = raw_url.strip()
    return webhooks


def _normalize_prefix(value: str) -> str:
    parts = [part for part in value.strip().strip("/").split("/") if part]
    if not parts:
        raise ValueError("RIVERHOG_GLACIER_PREFIX must not be empty")
    return "/".join(parts)


def _parse_command(value: str, *, name: str) -> tuple[str, ...]:
    command = tuple(shlex.split(value.strip()))
    if not command:
        raise ValueError(f"{name} must not be empty")
    return command


def _database_url_driver(database_url: str) -> str:
    return database_url.strip().split(":", 1)[0].split("+", 1)[0]


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
    s3_max_pool_connections: int = DEFAULT_S3_MAX_POOL_CONNECTIONS
    tusd_public_base_url: str | None = None
    tusd_public_signing_secret: str | None = None
    upload_staging_root: Path = field(default_factory=lambda: Path(".riverhog/uploads"))
    tusd_append_timeout_seconds: float = 60.0
    database_url: str = ""
    incomplete_upload_ttl: timedelta = field(default_factory=lambda: timedelta(hours=24))
    upload_session_idle_ttl: timedelta = field(default_factory=lambda: timedelta(days=7))
    upload_expiry_sweep_interval: timedelta = field(default_factory=lambda: timedelta(seconds=30))
    log_level: str = DEFAULT_LOG_LEVEL
    glacier_endpoint_url: str = "http://127.0.0.1:9000"
    glacier_region: str = "us-east-1"
    glacier_bucket: str = "riverhog"
    glacier_access_key_id: str = "minioadmin"
    glacier_secret_access_key: str = "minioadmin"
    glacier_force_path_style: bool = True
    glacier_prefix: str = "glacier"
    glacier_backend: str = "s3"
    glacier_storage_class: str = "DEEP_ARCHIVE"
    glacier_multipart_part_bytes: int = DEFAULT_GLACIER_MULTIPART_PART_BYTES
    glacier_multipart_concurrency: int = DEFAULT_GLACIER_MULTIPART_CONCURRENCY
    hot_promotion_concurrency: int = DEFAULT_HOT_PROMOTION_CONCURRENCY
    hot_single_put_max_bytes: int = DEFAULT_HOT_SINGLE_PUT_MAX_BYTES
    glacier_archive_encryption: str = "age_scrypt"
    glacier_archive_passphrase: str = DEV_RECOVERY_PAYLOAD_PASSPHRASE
    glacier_archive_require_explicit_passphrase: bool = False
    glacier_archive_work_factor: int = DEFAULT_GLACIER_ARCHIVE_WORK_FACTOR
    glacier_upload_retry_delay: timedelta = field(default_factory=lambda: timedelta(minutes=5))
    glacier_upload_sweep_interval: timedelta = field(default_factory=lambda: timedelta(seconds=30))
    operator_webhook_url: str | None = None
    notify_webhook_urls: Mapping[str, str] = field(default_factory=dict)
    notify_default_recipients: tuple[str, ...] = ()
    operator_webhook_timeout: timedelta = field(default_factory=lambda: timedelta(seconds=5))
    operator_webhook_retry_delay: timedelta = field(default_factory=lambda: timedelta(minutes=1))
    operator_webhook_reminder_interval: timedelta = field(
        default_factory=lambda: timedelta(hours=24)
    )
    operator_webhook_reminder_time: str | None = None
    operator_webhook_reminder_timezone: str = "UTC"
    glacier_recovery_sweep_interval: timedelta = field(
        default_factory=lambda: timedelta(seconds=30)
    )
    glacier_recovery_restore_latency: timedelta = field(default_factory=lambda: timedelta(hours=48))
    glacier_recovery_ready_ttl: timedelta = field(default_factory=lambda: timedelta(hours=24))
    glacier_recovery_retrieval_tier: str = "bulk"
    glacier_recovery_restore_mode: str = "auto"
    ots_stamp_command: tuple[str, ...] = ("ots",)
    ots_verify_command: tuple[str, ...] = ("ots",)
    recovery_payload_command: tuple[str, ...] = ("age",)
    recovery_payload_passphrase: str = DEV_RECOVERY_PAYLOAD_PASSPHRASE
    recovery_payload_require_explicit_passphrase: bool = False
    recovery_payload_work_factor: int = DEFAULT_RECOVERY_PAYLOAD_WORK_FACTOR
    recovery_payload_max_work_factor: int = DEFAULT_RECOVERY_PAYLOAD_MAX_WORK_FACTOR
    public_base_url: str | None = None
    planner_disc_target_bytes: int = DEFAULT_PLANNER_DISC_TARGET_BYTES
    planner_min_fill_ratio: float = DEFAULT_PLANNER_MIN_FILL_RATIO
    planner_min_fill_bytes: int = int(
        DEFAULT_PLANNER_DISC_TARGET_BYTES * DEFAULT_PLANNER_MIN_FILL_RATIO
    )
    planner_unplanned_saturation_bytes: int = DEFAULT_PLANNER_UNPLANNED_SATURATION_BYTES
    planner_image_root: Path = field(default_factory=lambda: Path(".riverhog/images"))
    planner_refresh_sweep_interval: timedelta = field(default_factory=lambda: timedelta(minutes=1))
    unburned_collection_bytes_limit: int = DEFAULT_UNBURNED_COLLECTION_BYTES_LIMIT

    def __post_init__(self) -> None:
        if not self.database_url:
            object.__setattr__(self, "database_url", DEFAULT_DATABASE_URL)
        log_level = self.log_level.strip().upper()
        if log_level not in {"CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG"}:
            raise ValueError(
                "RIVERHOG_LOG_LEVEL must be one of CRITICAL, ERROR, WARNING, INFO, or DEBUG"
            )
        object.__setattr__(self, "log_level", log_level)
        if self.tusd_append_timeout_seconds <= 0.0:
            raise ValueError("RIVERHOG_TUSD_APPEND_TIMEOUT_SECONDS must be > 0")
        if self.s3_max_pool_connections < 1:
            raise ValueError("RIVERHOG_S3_MAX_POOL_CONNECTIONS must be >= 1")
        if self.upload_session_idle_ttl.total_seconds() <= 0.0:
            raise ValueError("RIVERHOG_UPLOAD_SESSION_IDLE_TTL must be > 0")
        if self.glacier_multipart_part_bytes < 1:
            raise ValueError("RIVERHOG_GLACIER_MULTIPART_PART_BYTES must be >= 1")
        if self.glacier_multipart_concurrency < 1:
            raise ValueError("RIVERHOG_GLACIER_MULTIPART_CONCURRENCY must be >= 1")
        if self.hot_promotion_concurrency < 1:
            raise ValueError("RIVERHOG_HOT_PROMOTION_CONCURRENCY must be >= 1")
        if self.hot_single_put_max_bytes < 0:
            raise ValueError("RIVERHOG_HOT_SINGLE_PUT_MAX_BYTES must be >= 0")
        if self.glacier_archive_encryption != "age_scrypt":
            raise ValueError("RIVERHOG_GLACIER_ARCHIVE_ENCRYPTION must be age_scrypt")
        if self.glacier_archive_work_factor < 1 or self.glacier_archive_work_factor > 22:
            raise ValueError("RIVERHOG_GLACIER_ARCHIVE_WORK_FACTOR must be in 1..22")
        if not self.glacier_archive_passphrase:
            raise ValueError("RIVERHOG_GLACIER_ARCHIVE_PASSPHRASE must be set")
        if (
            self.glacier_archive_require_explicit_passphrase
            and self.glacier_archive_passphrase == DEV_RECOVERY_PAYLOAD_PASSPHRASE
        ):
            raise ValueError(
                "RIVERHOG_GLACIER_ARCHIVE_REQUIRE_EXPLICIT_PASSPHRASE requires "
                "RIVERHOG_GLACIER_ARCHIVE_PASSPHRASE to be explicitly set to a "
                "non-development secret"
            )
        if self.operator_webhook_url or self.notify_webhook_urls:
            minimum_ready_ttl = self.operator_webhook_timeout + self.operator_webhook_retry_delay
            if self.glacier_recovery_ready_ttl < minimum_ready_ttl:
                raise ValueError(
                    "invalid operator webhook timing: "
                    "RIVERHOG_GLACIER_RECOVERY_READY_TTL must be at least the outbound webhook "
                    "timeout plus RIVERHOG_OPERATOR_WEBHOOK_RETRY_DELAY when operator "
                    "notifications are configured"
                )
        normalized_webhooks: dict[str, str] = {}
        for recipient, url in self.notify_webhook_urls.items():
            normalized_recipient = _normalize_notify_recipient(
                str(recipient),
                name="RIVERHOG_NOTIFY_WEBHOOKS",
            )
            if not str(url).strip():
                raise ValueError("RIVERHOG_NOTIFY_WEBHOOKS values must be non-empty URLs")
            normalized_webhooks[normalized_recipient] = str(url).strip()
        object.__setattr__(self, "notify_webhook_urls", normalized_webhooks)
        object.__setattr__(
            self,
            "notify_default_recipients",
            _parse_recipient_list(
                ",".join(self.notify_default_recipients),
                name="notify_default_recipients",
            ),
        )
        object.__setattr__(
            self,
            "operator_webhook_reminder_time",
            normalize_reminder_time(self.operator_webhook_reminder_time),
        )
        reminder_zone(self.operator_webhook_reminder_timezone)
        if self.planner_disc_target_bytes < 1:
            raise ValueError("RIVERHOG_PLANNER_DISC_TARGET_BYTES must be >= 1")
        if self.planner_min_fill_bytes < 1:
            raise ValueError("RIVERHOG_PLANNER_MIN_FILL_BYTES must be >= 1")
        if self.planner_min_fill_ratio <= 0.0 or self.planner_min_fill_ratio > 1.0:
            raise ValueError("RIVERHOG_PLANNER_MIN_FILL_RATIO must be in (0, 1]")
        if self.planner_min_fill_bytes > self.planner_disc_target_bytes:
            raise ValueError(
                "RIVERHOG_PLANNER_MIN_FILL_BYTES must be <= RIVERHOG_PLANNER_DISC_TARGET_BYTES"
            )
        if self.planner_unplanned_saturation_bytes < 0:
            raise ValueError("RIVERHOG_PLANNER_UNPLANNED_SATURATION_BYTES must be >= 0")
        if self.planner_refresh_sweep_interval.total_seconds() <= 0.0:
            raise ValueError("RIVERHOG_PLANNER_REFRESH_SWEEP_INTERVAL must be > 0")
        if self.unburned_collection_bytes_limit < 0:
            raise ValueError("RIVERHOG_UNBURNED_COLLECTION_BYTES_LIMIT must be >= 0")
        object.__setattr__(
            self,
            "planner_image_root",
            self.planner_image_root.expanduser().resolve(),
        )
        object.__setattr__(
            self,
            "upload_staging_root",
            self.upload_staging_root.expanduser().resolve(),
        )

    def operator_webhook_next_reminder_at(self, current: datetime) -> datetime | None:
        return next_operator_reminder_at(
            current,
            interval=self.operator_webhook_reminder_interval,
            reminder_time=self.operator_webhook_reminder_time,
            reminder_timezone=self.operator_webhook_reminder_timezone,
        )


def load_runtime_config() -> RuntimeConfig:
    object_store = os.getenv("RIVERHOG_OBJECT_STORE", "s3").strip().casefold() or "s3"
    if object_store != "s3":
        raise ValueError(f"unsupported RIVERHOG_OBJECT_STORE {object_store!r}: expected 's3'")

    database_url_raw = os.getenv("RIVERHOG_DATABASE_URL", "").strip()
    sqlite_path_raw = os.getenv("RIVERHOG_DB_PATH", "").strip()
    if sqlite_path_raw:
        raise ValueError("RIVERHOG_DB_PATH has been removed; set RIVERHOG_DATABASE_URL")
    ttl_raw = os.getenv("INCOMPLETE_UPLOAD_TTL", "24h")
    session_idle_ttl_raw = os.getenv("RIVERHOG_UPLOAD_SESSION_IDLE_TTL", "168h")
    sweep_raw = os.getenv("UPLOAD_EXPIRY_SWEEP_INTERVAL", "30s")
    log_level = os.getenv("RIVERHOG_LOG_LEVEL", DEFAULT_LOG_LEVEL).strip() or DEFAULT_LOG_LEVEL

    database_url = database_url_raw or DEFAULT_DATABASE_URL
    if _database_url_driver(database_url) != "postgresql":
        raise ValueError("RIVERHOG_DATABASE_URL must use postgresql")
    incomplete_upload_ttl = _parse_duration(ttl_raw)
    upload_session_idle_ttl = _parse_duration(session_idle_ttl_raw)
    upload_expiry_sweep_interval = _parse_duration(sweep_raw)
    s3_endpoint_url = os.getenv("RIVERHOG_S3_ENDPOINT_URL", "http://127.0.0.1:9000").rstrip("/")
    s3_region = os.getenv("RIVERHOG_S3_REGION", "us-east-1")
    s3_bucket = os.getenv("RIVERHOG_S3_BUCKET", "riverhog")
    s3_access_key_id = os.getenv("RIVERHOG_S3_ACCESS_KEY_ID", "minioadmin")
    s3_secret_access_key = os.getenv("RIVERHOG_S3_SECRET_ACCESS_KEY", "minioadmin")
    s3_force_path_style = _parse_bool(os.getenv("RIVERHOG_S3_FORCE_PATH_STYLE", "true"))
    s3_max_pool_connections = _parse_int(
        os.getenv("RIVERHOG_S3_MAX_POOL_CONNECTIONS", str(DEFAULT_S3_MAX_POOL_CONNECTIONS)),
        name="RIVERHOG_S3_MAX_POOL_CONNECTIONS",
        minimum=1,
    )
    upload_staging_root = Path(
        os.getenv("RIVERHOG_UPLOAD_STAGING_ROOT", ".riverhog/uploads").strip()
        or ".riverhog/uploads"
    )

    glacier_multipart_part_bytes = _parse_bytes(
        os.getenv("RIVERHOG_GLACIER_MULTIPART_PART_BYTES", "64MiB"),
        name="RIVERHOG_GLACIER_MULTIPART_PART_BYTES",
        minimum=1,
    )
    glacier_multipart_concurrency = _parse_int(
        os.getenv(
            "RIVERHOG_GLACIER_MULTIPART_CONCURRENCY",
            str(DEFAULT_GLACIER_MULTIPART_CONCURRENCY),
        ),
        name="RIVERHOG_GLACIER_MULTIPART_CONCURRENCY",
        minimum=1,
    )
    hot_promotion_concurrency = _parse_int(
        os.getenv(
            "RIVERHOG_HOT_PROMOTION_CONCURRENCY",
            str(DEFAULT_HOT_PROMOTION_CONCURRENCY),
        ),
        name="RIVERHOG_HOT_PROMOTION_CONCURRENCY",
        minimum=1,
    )
    hot_single_put_max_bytes = _parse_bytes(
        os.getenv("RIVERHOG_HOT_SINGLE_PUT_MAX_BYTES", "64MiB"),
        name="RIVERHOG_HOT_SINGLE_PUT_MAX_BYTES",
        minimum=0,
    )
    glacier_archive_encryption = _parse_choice(
        os.getenv("RIVERHOG_GLACIER_ARCHIVE_ENCRYPTION", "age_scrypt"),
        name="RIVERHOG_GLACIER_ARCHIVE_ENCRYPTION",
        allowed={"age_scrypt"},
    )
    glacier_retry_delay = _parse_duration(os.getenv("RIVERHOG_GLACIER_UPLOAD_RETRY_DELAY", "5m"))
    glacier_upload_sweep_interval = _parse_duration(
        os.getenv("RIVERHOG_GLACIER_UPLOAD_SWEEP_INTERVAL", "30s")
    )
    operator_webhook_url = os.getenv("RIVERHOG_OPERATOR_WEBHOOK_URL", "").strip() or None
    notify_webhook_urls = parse_notify_webhook_map(os.environ)
    notify_default_recipients = _parse_recipient_list(
        os.getenv("RIVERHOG_NOTIFY_DEFAULT_RECIPIENTS", ""),
        name="RIVERHOG_NOTIFY_DEFAULT_RECIPIENTS",
    )
    operator_webhook_timeout = _parse_duration(os.getenv("RIVERHOG_OPERATOR_WEBHOOK_TIMEOUT", "5s"))
    operator_webhook_retry_delay = _parse_duration(
        os.getenv("RIVERHOG_OPERATOR_WEBHOOK_RETRY_DELAY", "60s")
    )
    operator_webhook_reminder_interval = _parse_duration(
        os.getenv("RIVERHOG_OPERATOR_WEBHOOK_REMINDER_INTERVAL", "24h")
    )
    operator_webhook_reminder_time = (
        os.getenv("RIVERHOG_OPERATOR_WEBHOOK_REMINDER_TIME", "").strip() or None
    )
    operator_webhook_reminder_timezone = (
        os.getenv("RIVERHOG_OPERATOR_WEBHOOK_REMINDER_TIMEZONE", "UTC").strip() or "UTC"
    )
    glacier_recovery_sweep_interval = _parse_duration(
        os.getenv("RIVERHOG_GLACIER_RECOVERY_SWEEP_INTERVAL", "30s")
    )
    glacier_recovery_restore_latency = _parse_duration(
        os.getenv("RIVERHOG_GLACIER_RECOVERY_RESTORE_LATENCY", "48h")
    )
    glacier_recovery_ready_ttl = _parse_duration(
        os.getenv("RIVERHOG_GLACIER_RECOVERY_READY_TTL", "24h")
    )
    glacier_recovery_retrieval_tier = _parse_choice(
        os.getenv("RIVERHOG_GLACIER_RECOVERY_RETRIEVAL_TIER", "bulk"),
        name="RIVERHOG_GLACIER_RECOVERY_RETRIEVAL_TIER",
        allowed={"bulk", "standard"},
    )
    glacier_recovery_restore_mode = _parse_choice(
        os.getenv("RIVERHOG_GLACIER_RECOVERY_RESTORE_MODE", "auto"),
        name="RIVERHOG_GLACIER_RECOVERY_RESTORE_MODE",
        allowed={"auto", "aws"},
    )
    public_base_url = os.getenv("RIVERHOG_PUBLIC_BASE_URL", "").strip() or None
    ots_stamp_command = _parse_command(
        os.getenv("RIVERHOG_OTS_STAMP_COMMAND", "ots"),
        name="RIVERHOG_OTS_STAMP_COMMAND",
    )
    ots_verify_command = _parse_command(
        os.getenv("RIVERHOG_OTS_VERIFY_COMMAND", "ots"),
        name="RIVERHOG_OTS_VERIFY_COMMAND",
    )
    recovery_payload_command = _parse_command(
        os.getenv("RIVERHOG_RECOVERY_PAYLOAD_COMMAND", "age"),
        name="RIVERHOG_RECOVERY_PAYLOAD_COMMAND",
    )
    recovery_payload_require_explicit_passphrase = _parse_bool(
        os.getenv("RIVERHOG_RECOVERY_PAYLOAD_REQUIRE_EXPLICIT_PASSPHRASE", "false")
    )
    recovery_payload_passphrase_supplied = "RIVERHOG_RECOVERY_PAYLOAD_PASSPHRASE" in os.environ
    recovery_payload_passphrase = (
        os.getenv(
            "RIVERHOG_RECOVERY_PAYLOAD_PASSPHRASE",
            DEV_RECOVERY_PAYLOAD_PASSPHRASE,
        ).strip()
        or DEV_RECOVERY_PAYLOAD_PASSPHRASE
    )
    glacier_archive_passphrase_supplied = "RIVERHOG_GLACIER_ARCHIVE_PASSPHRASE" in os.environ
    glacier_archive_passphrase = (
        os.getenv("RIVERHOG_GLACIER_ARCHIVE_PASSPHRASE", recovery_payload_passphrase).strip()
        or recovery_payload_passphrase
    )
    glacier_archive_require_explicit_passphrase = _parse_bool(
        os.getenv("RIVERHOG_GLACIER_ARCHIVE_REQUIRE_EXPLICIT_PASSPHRASE", "false")
    )
    glacier_archive_work_factor = _parse_int(
        os.getenv(
            "RIVERHOG_GLACIER_ARCHIVE_WORK_FACTOR",
            str(DEFAULT_GLACIER_ARCHIVE_WORK_FACTOR),
        ),
        name="RIVERHOG_GLACIER_ARCHIVE_WORK_FACTOR",
        minimum=1,
    )
    if glacier_archive_work_factor > 22:
        raise ValueError("RIVERHOG_GLACIER_ARCHIVE_WORK_FACTOR must be <= 22")
    if glacier_archive_require_explicit_passphrase and (
        not glacier_archive_passphrase_supplied
        or glacier_archive_passphrase == DEV_RECOVERY_PAYLOAD_PASSPHRASE
    ):
        raise ValueError(
            "RIVERHOG_GLACIER_ARCHIVE_REQUIRE_EXPLICIT_PASSPHRASE requires "
            "RIVERHOG_GLACIER_ARCHIVE_PASSPHRASE to be explicitly set to a non-development secret"
        )
    if recovery_payload_require_explicit_passphrase and (
        not recovery_payload_passphrase_supplied
        or recovery_payload_passphrase == DEV_RECOVERY_PAYLOAD_PASSPHRASE
    ):
        raise ValueError(
            "RIVERHOG_RECOVERY_PAYLOAD_REQUIRE_EXPLICIT_PASSPHRASE requires "
            "RIVERHOG_RECOVERY_PAYLOAD_PASSPHRASE to be explicitly set to a non-development secret"
        )
    recovery_payload_work_factor = _parse_int(
        os.getenv(
            "RIVERHOG_RECOVERY_PAYLOAD_WORK_FACTOR",
            str(DEFAULT_RECOVERY_PAYLOAD_WORK_FACTOR),
        ),
        name="RIVERHOG_RECOVERY_PAYLOAD_WORK_FACTOR",
        minimum=1,
    )
    recovery_payload_max_work_factor = _parse_int(
        os.getenv(
            "RIVERHOG_RECOVERY_PAYLOAD_MAX_WORK_FACTOR",
            str(DEFAULT_RECOVERY_PAYLOAD_MAX_WORK_FACTOR),
        ),
        name="RIVERHOG_RECOVERY_PAYLOAD_MAX_WORK_FACTOR",
        minimum=1,
    )
    if recovery_payload_work_factor > 30:
        raise ValueError("RIVERHOG_RECOVERY_PAYLOAD_WORK_FACTOR must be <= 30")
    if recovery_payload_max_work_factor > 30:
        raise ValueError("RIVERHOG_RECOVERY_PAYLOAD_MAX_WORK_FACTOR must be <= 30")

    planner_disc_target_bytes = _parse_bytes(
        os.getenv("RIVERHOG_PLANNER_DISC_TARGET_BYTES", str(DEFAULT_PLANNER_DISC_TARGET_BYTES)),
        name="RIVERHOG_PLANNER_DISC_TARGET_BYTES",
        minimum=1,
    )
    planner_min_fill_ratio = _parse_ratio(
        os.getenv("RIVERHOG_PLANNER_MIN_FILL_RATIO", str(DEFAULT_PLANNER_MIN_FILL_RATIO)),
        name="RIVERHOG_PLANNER_MIN_FILL_RATIO",
    )
    planner_min_fill_bytes_raw = os.getenv("RIVERHOG_PLANNER_MIN_FILL_BYTES", "").strip()
    planner_min_fill_bytes = (
        _parse_bytes(
            planner_min_fill_bytes_raw,
            name="RIVERHOG_PLANNER_MIN_FILL_BYTES",
            minimum=1,
        )
        if planner_min_fill_bytes_raw
        else int(planner_disc_target_bytes * planner_min_fill_ratio)
    )
    planner_unplanned_saturation_bytes = _parse_bytes(
        os.getenv(
            "RIVERHOG_PLANNER_UNPLANNED_SATURATION_BYTES",
            str(DEFAULT_PLANNER_UNPLANNED_SATURATION_BYTES),
        ),
        name="RIVERHOG_PLANNER_UNPLANNED_SATURATION_BYTES",
        minimum=0,
    )
    planner_image_root = Path(
        os.getenv("RIVERHOG_PLANNER_IMAGE_ROOT", ".riverhog/images").strip() or ".riverhog/images"
    )
    planner_refresh_sweep_interval = _parse_duration(
        os.getenv("RIVERHOG_PLANNER_REFRESH_SWEEP_INTERVAL", "60s")
    )
    unburned_collection_bytes_limit = _parse_bytes(
        os.getenv(
            "RIVERHOG_UNBURNED_COLLECTION_BYTES_LIMIT",
            str(DEFAULT_UNBURNED_COLLECTION_BYTES_LIMIT),
        ),
        name="RIVERHOG_UNBURNED_COLLECTION_BYTES_LIMIT",
        minimum=0,
    )

    return RuntimeConfig(
        object_store=object_store,
        s3_endpoint_url=s3_endpoint_url,
        s3_region=s3_region,
        s3_bucket=s3_bucket,
        s3_access_key_id=s3_access_key_id,
        s3_secret_access_key=s3_secret_access_key,
        s3_force_path_style=s3_force_path_style,
        s3_max_pool_connections=s3_max_pool_connections,
        upload_staging_root=upload_staging_root,
        tusd_base_url=os.getenv("RIVERHOG_TUSD_BASE_URL", "http://127.0.0.1:1080/files").rstrip(
            "/"
        ),
        tusd_public_base_url=(
            os.getenv("RIVERHOG_TUSD_PUBLIC_BASE_URL", "").strip().rstrip("/") or None
        ),
        tusd_public_signing_secret=(
            os.getenv("RIVERHOG_TUSD_PUBLIC_SIGNING_SECRET", "").strip() or None
        ),
        tusd_hook_secret=os.getenv("RIVERHOG_TUSD_HOOK_SECRET", "dev-tusd-hook-secret"),
        tusd_append_timeout_seconds=_parse_float(
            os.getenv("RIVERHOG_TUSD_APPEND_TIMEOUT_SECONDS", "60"),
            name="RIVERHOG_TUSD_APPEND_TIMEOUT_SECONDS",
            minimum=0.0,
        ),
        database_url=database_url,
        incomplete_upload_ttl=incomplete_upload_ttl,
        upload_session_idle_ttl=upload_session_idle_ttl,
        upload_expiry_sweep_interval=upload_expiry_sweep_interval,
        log_level=log_level,
        glacier_endpoint_url=os.getenv("RIVERHOG_GLACIER_ENDPOINT_URL", s3_endpoint_url).rstrip(
            "/"
        ),
        glacier_region=os.getenv("RIVERHOG_GLACIER_REGION", s3_region),
        glacier_bucket=os.getenv("RIVERHOG_GLACIER_BUCKET", s3_bucket),
        glacier_access_key_id=os.getenv("RIVERHOG_GLACIER_ACCESS_KEY_ID", s3_access_key_id),
        glacier_secret_access_key=os.getenv(
            "RIVERHOG_GLACIER_SECRET_ACCESS_KEY",
            s3_secret_access_key,
        ),
        glacier_force_path_style=_parse_bool(
            os.getenv("RIVERHOG_GLACIER_FORCE_PATH_STYLE", str(s3_force_path_style).lower())
        ),
        glacier_prefix=_normalize_prefix(os.getenv("RIVERHOG_GLACIER_PREFIX", "glacier")),
        glacier_backend=os.getenv("RIVERHOG_GLACIER_BACKEND", "s3").strip() or "s3",
        glacier_storage_class=os.getenv("RIVERHOG_GLACIER_STORAGE_CLASS", "DEEP_ARCHIVE").strip()
        or "DEEP_ARCHIVE",
        glacier_multipart_part_bytes=glacier_multipart_part_bytes,
        glacier_multipart_concurrency=glacier_multipart_concurrency,
        hot_promotion_concurrency=hot_promotion_concurrency,
        hot_single_put_max_bytes=hot_single_put_max_bytes,
        glacier_archive_encryption=glacier_archive_encryption,
        glacier_archive_passphrase=glacier_archive_passphrase,
        glacier_archive_require_explicit_passphrase=glacier_archive_require_explicit_passphrase,
        glacier_archive_work_factor=glacier_archive_work_factor,
        glacier_upload_retry_delay=glacier_retry_delay,
        glacier_upload_sweep_interval=glacier_upload_sweep_interval,
        operator_webhook_url=operator_webhook_url,
        notify_webhook_urls=notify_webhook_urls,
        notify_default_recipients=notify_default_recipients,
        operator_webhook_timeout=operator_webhook_timeout,
        operator_webhook_retry_delay=operator_webhook_retry_delay,
        operator_webhook_reminder_interval=operator_webhook_reminder_interval,
        operator_webhook_reminder_time=operator_webhook_reminder_time,
        operator_webhook_reminder_timezone=operator_webhook_reminder_timezone,
        glacier_recovery_sweep_interval=glacier_recovery_sweep_interval,
        glacier_recovery_restore_latency=glacier_recovery_restore_latency,
        glacier_recovery_ready_ttl=glacier_recovery_ready_ttl,
        glacier_recovery_retrieval_tier=glacier_recovery_retrieval_tier,
        glacier_recovery_restore_mode=glacier_recovery_restore_mode,
        ots_stamp_command=ots_stamp_command,
        ots_verify_command=ots_verify_command,
        recovery_payload_command=recovery_payload_command,
        recovery_payload_passphrase=recovery_payload_passphrase,
        recovery_payload_require_explicit_passphrase=(recovery_payload_require_explicit_passphrase),
        recovery_payload_work_factor=recovery_payload_work_factor,
        recovery_payload_max_work_factor=recovery_payload_max_work_factor,
        public_base_url=public_base_url,
        planner_disc_target_bytes=planner_disc_target_bytes,
        planner_min_fill_ratio=planner_min_fill_ratio,
        planner_min_fill_bytes=planner_min_fill_bytes,
        planner_unplanned_saturation_bytes=planner_unplanned_saturation_bytes,
        planner_image_root=planner_image_root,
        planner_refresh_sweep_interval=planner_refresh_sweep_interval,
        unburned_collection_bytes_limit=unburned_collection_bytes_limit,
    )
