from __future__ import annotations

import os
import re
import shlex
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from datetime import timedelta
from pathlib import Path
from urllib.parse import urlsplit

from time_formats import parse_duration

_BYTES_RE = re.compile(r"^(\d+(?:_\d+)*)([kmgt]i?b?|b)?$", re.IGNORECASE)
DEV_ARCHIVE_PASSPHRASE = "riverhog-dev-archive-passphrase"
DEV_INGRESS_SECRET_KEY = "riverhog-development-ingress-secret-key"
DEV_ARCHIVE_ACCESS_KEY_IDS = frozenset(
    {
        "minioadmin",
        "GK000000000000000000000002",
    }
)
DEV_ARCHIVE_ENDPOINT_HOSTS = frozenset({"127.0.0.1", "localhost", "garage"})
DEFAULT_DATABASE_URL = "postgresql+psycopg://riverhog:riverhog@127.0.0.1:5432/riverhog"
DEFAULT_ARCHIVE_MULTIPART_PART_BYTES = 64 * 1024 * 1024
DEFAULT_ARCHIVE_MULTIPART_CONCURRENCY = 4
DEFAULT_ARCHIVE_OBJECT_CONCURRENCY = 4
DEFAULT_S3_MAX_POOL_CONNECTIONS = 32
DEFAULT_ARCHIVE_SCRYPT_WORK_FACTOR = 18
DEFAULT_LOG_LEVEL = "INFO"
SUPPORTED_ARCHIVE_BACKENDS = frozenset({"aws", "b2", "s3"})


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


def _parse_choice(value: str, *, name: str, allowed: set[str]) -> str:
    normalized = value.strip().casefold().replace("-", "_")
    if normalized not in allowed:
        expected = ", ".join(sorted(allowed))
        raise ValueError(f"invalid {name} {value!r}: expected one of {expected}")
    return normalized


def _normalize_prefix(value: str) -> str:
    parts = [part for part in value.strip().strip("/").split("/") if part]
    return "/".join(parts)


def _s3_namespaces_overlap(
    *,
    left_endpoint_url: str,
    left_bucket: str,
    left_prefix: str,
    right_endpoint_url: str,
    right_bucket: str,
    right_prefix: str,
) -> bool:
    if left_endpoint_url.rstrip("/").casefold() != right_endpoint_url.rstrip("/").casefold():
        return False
    if left_bucket.casefold() != right_bucket.casefold():
        return False
    left_parts = tuple(part for part in left_prefix.split("/") if part)
    right_parts = tuple(part for part in right_prefix.split("/") if part)
    common = min(len(left_parts), len(right_parts))
    return left_parts[:common] == right_parts[:common]


def _normalize_archive_store_name(value: str) -> str:
    name = value.strip().casefold()
    if not name or not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", name):
        raise ValueError(
            f"invalid archive store name {value!r}: expected lowercase letters, digits, and dashes"
        )
    return name


def _archive_store_env_suffix(name: str) -> str:
    return name.upper().replace("-", "_")


def _parse_command(value: str, *, name: str) -> tuple[str, ...]:
    command = tuple(shlex.split(value.strip()))
    if not command:
        raise ValueError(f"{name} must not be empty")
    return command


def _database_url_driver(database_url: str) -> str:
    return database_url.strip().split(":", 1)[0].split("+", 1)[0]


@dataclass(frozen=True, slots=True)
class ArchiveStoreConfig:
    name: str
    endpoint_url: str
    region: str
    bucket: str
    access_key_id: str
    secret_access_key: str
    force_path_style: bool
    prefix: str
    backend: str
    storage_class: str
    read_mode: str = "immediate"
    cloudfront_base_url: str | None = None
    cloudfront_public_key_id: str | None = None
    cloudfront_private_key_path: Path | None = None
    monthly_download_allowance_bytes: int | None = None
    download_safety_buffer_bytes: int = 0


def _download_source(store: ArchiveStoreConfig) -> tuple[str, ...]:
    if store.cloudfront_base_url is not None:
        return ("cloudfront", store.cloudfront_base_url.rstrip("/").casefold())
    return (
        store.backend.casefold(),
        store.endpoint_url.rstrip("/").casefold(),
        store.bucket.casefold(),
    )


@dataclass(frozen=True, slots=True)
class RetrievalCacheConfig:
    endpoint_url: str
    region: str
    bucket: str
    access_key_id: str
    secret_access_key: str
    force_path_style: bool = False
    prefix: str = ""


@dataclass(frozen=True, slots=True)
class IngressStoreConfig:
    endpoint_url: str
    region: str
    bucket: str
    access_key_id: str
    secret_access_key: str
    force_path_style: bool = False
    prefix: str = ""


def _is_development_archive_store(store: ArchiveStoreConfig) -> bool:
    endpoint_host = (urlsplit(store.endpoint_url).hostname or "").casefold()
    return (
        endpoint_host in DEV_ARCHIVE_ENDPOINT_HOSTS
        and store.access_key_id in DEV_ARCHIVE_ACCESS_KEY_IDS
    )


@dataclass(frozen=True, slots=True)
class RuntimeConfig:
    tusd_base_url: str = "http://127.0.0.1:1080/files"
    tusd_hook_secret: str = "dev-tusd-hook-secret"
    s3_max_pool_connections: int = DEFAULT_S3_MAX_POOL_CONNECTIONS
    tusd_public_base_url: str | None = None
    tusd_append_timeout: timedelta = field(default_factory=lambda: timedelta(seconds=60))
    database_url: str = ""
    upload_file_ttl: timedelta = field(default_factory=lambda: timedelta(hours=24))
    upload_session_idle_ttl: timedelta = field(default_factory=lambda: timedelta(days=7))
    upload_expiry_sweep_interval: timedelta = field(default_factory=lambda: timedelta(seconds=30))
    log_level: str = DEFAULT_LOG_LEVEL
    archive_write_store: str = "archive"
    archive_read_order: tuple[str, ...] = ("archive",)
    archive_stores: Mapping[str, ArchiveStoreConfig] = field(
        default_factory=lambda: {
            "archive": ArchiveStoreConfig(
                name="archive",
                endpoint_url="http://127.0.0.1:9000",
                region="us-east-1",
                bucket="riverhog",
                access_key_id="minioadmin",
                secret_access_key="minioadmin",
                force_path_style=True,
                prefix="archive",
                backend="s3",
                storage_class="STANDARD",
                read_mode="immediate",
            )
        }
    )
    archive_multipart_part_bytes: int = DEFAULT_ARCHIVE_MULTIPART_PART_BYTES
    archive_multipart_concurrency: int = DEFAULT_ARCHIVE_MULTIPART_CONCURRENCY
    archive_multipart_max_age: timedelta = field(default_factory=lambda: timedelta(days=3))
    archive_multipart_sweep_interval: timedelta = field(default_factory=lambda: timedelta(hours=6))
    archive_object_concurrency: int = DEFAULT_ARCHIVE_OBJECT_CONCURRENCY
    ingress_store: IngressStoreConfig = field(
        default_factory=lambda: IngressStoreConfig(
            endpoint_url="http://127.0.0.1:9000",
            region="us-east-1",
            bucket="riverhog-ingress",
            access_key_id="minioadmin",
            secret_access_key="minioadmin",
            force_path_style=True,
        )
    )
    ingress_secret_key: str = DEV_INGRESS_SECRET_KEY
    ingress_cleanup_concurrency: int = 8
    ingress_cleanup_retry_delay: timedelta = field(default_factory=lambda: timedelta(minutes=5))
    ingress_cleanup_sweep_interval: timedelta = field(default_factory=lambda: timedelta(seconds=10))
    retrieval_cache: RetrievalCacheConfig | None = None
    retrieval_cache_new_archive_lease: timedelta = field(default_factory=lambda: timedelta(days=30))
    retrieval_default_lease: timedelta = field(default_factory=lambda: timedelta(days=7))
    retrieval_max_lease: timedelta = field(default_factory=lambda: timedelta(days=30))
    retrieval_cache_sweep_interval: timedelta = field(default_factory=lambda: timedelta(hours=1))
    archive_passphrase: str = DEV_ARCHIVE_PASSPHRASE
    archive_require_explicit_passphrase: bool = False
    archive_scrypt_work_factor: int = DEFAULT_ARCHIVE_SCRYPT_WORK_FACTOR
    archive_upload_retry_delay: timedelta = field(default_factory=lambda: timedelta(minutes=5))
    archive_upload_sweep_interval: timedelta = field(default_factory=lambda: timedelta(seconds=30))
    retrieval_sweep_interval: timedelta = field(default_factory=lambda: timedelta(seconds=30))
    retrieval_estimated_latency: timedelta = field(default_factory=lambda: timedelta(hours=48))
    retrieval_tier: str = "bulk"
    ots_stamp_command: tuple[str, ...] = ("ots",)
    ots_verify_command: tuple[str, ...] = (
        "ots",
        "--no-bitcoin",
        "--no-default-whitelist",
    )
    ots_upgrade_command: tuple[str, ...] = ("ots",)
    attestation_secret_key_file: Path | None = None
    attestation_public_key_file: Path | None = None
    proof_maturation_retry_delay: timedelta = field(default_factory=lambda: timedelta(hours=6))
    proof_maturation_sweep_interval: timedelta = field(default_factory=lambda: timedelta(hours=1))
    public_base_url: str | None = None
    event_source: str = "urn:riverhog"
    event_context_retention: timedelta = field(default_factory=lambda: timedelta(days=30))

    def __post_init__(self) -> None:
        if not self.event_source.strip():
            raise ValueError("RIVERHOG_EVENT_SOURCE must not be blank")
        if self.event_context_retention.total_seconds() <= 0:
            raise ValueError("RIVERHOG_EVENT_CONTEXT_RETENTION must be > 0")
        if self.proof_maturation_retry_delay.total_seconds() <= 0:
            raise ValueError("RIVERHOG_PROOF_MATURATION_RETRY_DELAY must be > 0")
        if self.proof_maturation_sweep_interval.total_seconds() <= 0:
            raise ValueError("RIVERHOG_PROOF_MATURATION_SWEEP_INTERVAL must be > 0")
        if bool(self.attestation_secret_key_file) != bool(self.attestation_public_key_file):
            raise ValueError("Riverhog attestation key configuration is incomplete")
        if not self.database_url:
            object.__setattr__(self, "database_url", DEFAULT_DATABASE_URL)
        log_level = self.log_level.strip().upper()
        if log_level not in {"CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG"}:
            raise ValueError(
                "RIVERHOG_LOG_LEVEL must be one of CRITICAL, ERROR, WARNING, INFO, or DEBUG"
            )
        object.__setattr__(self, "log_level", log_level)
        archive_write_store = _normalize_archive_store_name(self.archive_write_store)
        normalized_archive_stores: dict[str, ArchiveStoreConfig] = {}
        for raw_name, store in self.archive_stores.items():
            name = _normalize_archive_store_name(raw_name)
            if store.name != name:
                raise ValueError(f"archive store mapping key must match its name: {raw_name!r}")
            backend = _parse_choice(
                store.backend,
                name=f"archive store {name} backend",
                allowed=set(SUPPORTED_ARCHIVE_BACKENDS),
            )
            read_mode = _parse_choice(
                store.read_mode,
                name=f"archive store {name} read mode",
                allowed={"immediate", "restore_required"},
            )
            store = replace(
                store,
                prefix=_normalize_prefix(store.prefix),
                backend=backend,
                read_mode=read_mode,
            )
            required_store_fields = {
                "endpoint_url": store.endpoint_url,
                "region": store.region,
                "bucket": store.bucket,
                "access_key_id": store.access_key_id,
                "secret_access_key": store.secret_access_key,
                "backend": store.backend,
                "storage_class": store.storage_class,
            }
            missing_fields = [
                field_name
                for field_name, field_value in required_store_fields.items()
                if not field_value.strip()
            ]
            if missing_fields:
                raise ValueError(
                    f"archive store {name} has blank required fields: " + ", ".join(missing_fields)
                )
            if store.read_mode == "restore_required" and store.backend != "aws":
                raise ValueError(
                    f"archive store {name} restore_required reads require the aws backend"
                )
            if store.monthly_download_allowance_bytes is not None:
                if store.monthly_download_allowance_bytes <= 0:
                    raise ValueError(
                        f"archive store {name} monthly download allowance must be positive"
                    )
                if store.download_safety_buffer_bytes >= store.monthly_download_allowance_bytes:
                    raise ValueError(
                        f"archive store {name} download safety buffer must be smaller than "
                        "its monthly download allowance"
                    )
            elif store.download_safety_buffer_bytes != 0:
                raise ValueError(
                    f"archive store {name} download safety buffer requires a monthly "
                    "download allowance"
                )
            cloudfront_fields = {
                "base URL": store.cloudfront_base_url,
                "public key id": store.cloudfront_public_key_id,
                "private key path": store.cloudfront_private_key_path,
            }
            configured_cloudfront_fields = [
                field_name for field_name, value in cloudfront_fields.items() if value is not None
            ]
            if configured_cloudfront_fields and len(configured_cloudfront_fields) != len(
                cloudfront_fields
            ):
                raise ValueError(
                    f"archive store {name} CloudFront download configuration must set "
                    "base URL, public key id, and private key path together"
                )
            if store.cloudfront_base_url is not None:
                if store.backend.casefold() != "aws":
                    raise ValueError(
                        f"archive store {name} CloudFront downloads require the aws backend"
                    )
                cloudfront_url = urlsplit(store.cloudfront_base_url)
                if (
                    cloudfront_url.scheme != "https"
                    or not cloudfront_url.hostname
                    or cloudfront_url.username is not None
                    or cloudfront_url.password is not None
                    or cloudfront_url.query
                    or cloudfront_url.fragment
                ):
                    raise ValueError(
                        f"archive store {name} CloudFront base URL must be an HTTPS URL "
                        "without credentials, query, or fragment"
                    )
            normalized_archive_stores[name] = store
        metered_sources: dict[tuple[str, ...], list[str]] = {}
        for name, store in normalized_archive_stores.items():
            metered_sources.setdefault(_download_source(store), []).append(name)
        duplicate_metered_sources = [
            names
            for names in metered_sources.values()
            if len(names) > 1
            and any(
                normalized_archive_stores[name].monthly_download_allowance_bytes is not None
                for name in names
            )
        ]
        if duplicate_metered_sources:
            aliases = ", ".join(sorted(duplicate_metered_sources[0]))
            raise ValueError(
                "a metered archive download source must have one store name; "
                f"duplicate aliases: {aliases}"
            )
        if archive_write_store not in normalized_archive_stores:
            raise ValueError(f"archive write store is not configured: {archive_write_store}")
        object.__setattr__(self, "archive_write_store", archive_write_store)
        read_order = tuple(
            dict.fromkeys(_normalize_archive_store_name(name) for name in self.archive_read_order)
        )
        unknown_read_stores = set(read_order) - set(normalized_archive_stores)
        if unknown_read_stores:
            raise ValueError(
                "archive read order contains unconfigured stores: "
                f"{', '.join(sorted(unknown_read_stores))}"
            )
        object.__setattr__(
            self,
            "archive_read_order",
            (*read_order, *[name for name in normalized_archive_stores if name not in read_order]),
        )
        object.__setattr__(self, "archive_stores", normalized_archive_stores)
        ingress_store = replace(
            self.ingress_store,
            prefix=_normalize_prefix(self.ingress_store.prefix),
        )
        missing_ingress_fields = [
            name
            for name, value in {
                "endpoint_url": ingress_store.endpoint_url,
                "region": ingress_store.region,
                "bucket": ingress_store.bucket,
                "access_key_id": ingress_store.access_key_id,
                "secret_access_key": ingress_store.secret_access_key,
            }.items()
            if not value.strip()
        ]
        if missing_ingress_fields:
            raise ValueError(
                "ingress staging store has blank required fields: "
                + ", ".join(missing_ingress_fields)
            )
        object.__setattr__(self, "ingress_store", ingress_store)
        if len(self.ingress_secret_key.encode("utf-8")) < 32:
            raise ValueError("RIVERHOG_INGRESS_SECRET_KEY must contain at least 32 bytes")
        ingress_development = (
            (urlsplit(ingress_store.endpoint_url).hostname or "").casefold()
            in DEV_ARCHIVE_ENDPOINT_HOSTS
            and ingress_store.access_key_id in DEV_ARCHIVE_ACCESS_KEY_IDS
        )
        if not ingress_development and self.ingress_secret_key == DEV_INGRESS_SECRET_KEY:
            raise ValueError(
                "RIVERHOG_INGRESS_SECRET_KEY must be explicitly set for a non-development "
                "ingress staging store"
            )
        restore_required_stores = [
            name
            for name, store in normalized_archive_stores.items()
            if store.read_mode == "restore_required"
        ]
        if restore_required_stores and self.retrieval_cache is None:
            raise ValueError(
                "RIVERHOG_RETRIEVAL_CACHE_* must be configured for restore-required "
                "archive stores: " + ", ".join(restore_required_stores)
            )
        if self.retrieval_cache is not None:
            cache = replace(
                self.retrieval_cache,
                prefix=_normalize_prefix(self.retrieval_cache.prefix),
            )
            missing_cache_fields = [
                name
                for name, value in {
                    "endpoint_url": cache.endpoint_url,
                    "region": cache.region,
                    "bucket": cache.bucket,
                    "access_key_id": cache.access_key_id,
                    "secret_access_key": cache.secret_access_key,
                }.items()
                if not value.strip()
            ]
            if missing_cache_fields:
                raise ValueError(
                    "retrieval cache has blank required fields: " + ", ".join(missing_cache_fields)
                )
            if _s3_namespaces_overlap(
                left_endpoint_url=ingress_store.endpoint_url,
                left_bucket=ingress_store.bucket,
                left_prefix=ingress_store.prefix,
                right_endpoint_url=cache.endpoint_url,
                right_bucket=cache.bucket,
                right_prefix=cache.prefix,
            ):
                raise ValueError(
                    "ingress staging store and retrieval cache must use non-overlapping "
                    "S3 namespaces"
                )
            object.__setattr__(self, "retrieval_cache", cache)
        if self.tusd_append_timeout.total_seconds() <= 0.0:
            raise ValueError("RIVERHOG_TUSD_APPEND_TIMEOUT must be > 0")
        if self.s3_max_pool_connections < 1:
            raise ValueError("RIVERHOG_S3_MAX_POOL_CONNECTIONS must be >= 1")
        if self.ingress_cleanup_concurrency < 1:
            raise ValueError("RIVERHOG_INGRESS_CLEANUP_CONCURRENCY must be >= 1")
        if self.ingress_cleanup_retry_delay.total_seconds() <= 0.0:
            raise ValueError("RIVERHOG_INGRESS_CLEANUP_RETRY_DELAY must be > 0")
        if self.ingress_cleanup_sweep_interval.total_seconds() <= 0.0:
            raise ValueError("RIVERHOG_INGRESS_CLEANUP_SWEEP_INTERVAL must be > 0")
        if self.upload_session_idle_ttl.total_seconds() <= 0.0:
            raise ValueError("RIVERHOG_UPLOAD_SESSION_IDLE_TTL must be > 0")
        if self.archive_multipart_part_bytes < 1:
            raise ValueError("RIVERHOG_ARCHIVE_MULTIPART_PART_BYTES must be >= 1")
        if self.archive_multipart_concurrency < 1:
            raise ValueError("RIVERHOG_ARCHIVE_MULTIPART_CONCURRENCY must be >= 1")
        if self.archive_multipart_max_age.total_seconds() <= 0.0:
            raise ValueError("RIVERHOG_ARCHIVE_MULTIPART_MAX_AGE must be > 0")
        if self.archive_multipart_sweep_interval.total_seconds() <= 0.0:
            raise ValueError("RIVERHOG_ARCHIVE_MULTIPART_SWEEP_INTERVAL must be > 0")
        if self.archive_object_concurrency < 1:
            raise ValueError("RIVERHOG_ARCHIVE_OBJECT_CONCURRENCY must be >= 1")
        if self.retrieval_cache_new_archive_lease.total_seconds() <= 0:
            raise ValueError("RIVERHOG_RETRIEVAL_CACHE_NEW_ARCHIVE_LEASE must be > 0")
        if self.retrieval_default_lease.total_seconds() <= 0:
            raise ValueError("RIVERHOG_RETRIEVAL_DEFAULT_LEASE must be > 0")
        if self.retrieval_max_lease < self.retrieval_default_lease:
            raise ValueError(
                "RIVERHOG_RETRIEVAL_MAX_LEASE must be at least RIVERHOG_RETRIEVAL_DEFAULT_LEASE"
            )
        if self.retrieval_cache_sweep_interval.total_seconds() <= 0:
            raise ValueError("RIVERHOG_RETRIEVAL_CACHE_SWEEP_INTERVAL must be > 0")
        if self.archive_scrypt_work_factor < 1 or self.archive_scrypt_work_factor > 22:
            raise ValueError("RIVERHOG_ARCHIVE_SCRYPT_WORK_FACTOR must be in 1..22")
        if not self.archive_passphrase:
            raise ValueError("RIVERHOG_ARCHIVE_PASSPHRASE must be set")
        non_development_stores = sorted(
            name
            for name, store in self.archive_stores.items()
            if not _is_development_archive_store(store)
        )
        if self.archive_passphrase == DEV_ARCHIVE_PASSPHRASE and non_development_stores:
            raise ValueError(
                "RIVERHOG_ARCHIVE_PASSPHRASE must be explicitly set to a "
                "non-development secret for archive store(s): " + ", ".join(non_development_stores)
            )
        if (
            self.archive_require_explicit_passphrase
            and self.archive_passphrase == DEV_ARCHIVE_PASSPHRASE
        ):
            raise ValueError(
                "RIVERHOG_ARCHIVE_REQUIRE_EXPLICIT_PASSPHRASE requires "
                "RIVERHOG_ARCHIVE_PASSPHRASE to be explicitly set to a "
                "non-development secret"
            )

    def archive_store(self, name: str) -> ArchiveStoreConfig:
        normalized = _normalize_archive_store_name(name)
        try:
            return self.archive_stores[normalized]
        except KeyError as exc:
            raise ValueError(f"archive store is not configured: {normalized}") from exc


def _parse_archive_stores(
    values: Mapping[str, str],
) -> tuple[str, tuple[str, ...], dict[str, ArchiveStoreConfig]]:
    names = tuple(
        dict.fromkeys(
            _normalize_archive_store_name(raw)
            for raw in values.get("RIVERHOG_ARCHIVE_STORES", "archive").split(",")
            if raw.strip()
        )
    )
    if not names:
        raise ValueError("RIVERHOG_ARCHIVE_STORES must configure at least one store")
    write_store = _normalize_archive_store_name(
        values.get("RIVERHOG_ARCHIVE_WRITE_STORE", names[0])
    )
    stores: dict[str, ArchiveStoreConfig] = {}
    for name in names:
        prefix = f"RIVERHOG_ARCHIVE_STORE_{_archive_store_env_suffix(name)}_"
        monthly_download_allowance_raw = values.get(
            f"{prefix}MONTHLY_DOWNLOAD_ALLOWANCE_BYTES", ""
        ).strip()
        download_safety_buffer_raw = values.get(f"{prefix}DOWNLOAD_SAFETY_BUFFER_BYTES", "").strip()
        cloudfront_base_url = (
            values.get(f"{prefix}CLOUDFRONT_BASE_URL", "").strip().rstrip("/") or None
        )
        cloudfront_public_key_id = (
            values.get(f"{prefix}CLOUDFRONT_PUBLIC_KEY_ID", "").strip() or None
        )
        cloudfront_private_key_path_raw = values.get(
            f"{prefix}CLOUDFRONT_PRIVATE_KEY_PATH", ""
        ).strip()
        stores[name] = ArchiveStoreConfig(
            name=name,
            endpoint_url=values.get(f"{prefix}ENDPOINT_URL", "http://127.0.0.1:9000").rstrip("/"),
            region=values.get(f"{prefix}REGION", "us-east-1"),
            bucket=values.get(f"{prefix}BUCKET", "riverhog-archive"),
            access_key_id=values.get(f"{prefix}ACCESS_KEY_ID", "minioadmin"),
            secret_access_key=values.get(f"{prefix}SECRET_ACCESS_KEY", "minioadmin"),
            force_path_style=_parse_bool(values.get(f"{prefix}FORCE_PATH_STYLE", "true")),
            prefix=_normalize_prefix(values.get(f"{prefix}PREFIX", "archive")),
            backend=values.get(f"{prefix}BACKEND", "s3").strip() or "s3",
            storage_class=values.get(f"{prefix}STORAGE_CLASS", "STANDARD").strip() or "STANDARD",
            read_mode=_parse_choice(
                values.get(f"{prefix}READ_MODE", "immediate"),
                name=f"{prefix}READ_MODE",
                allowed={"immediate", "restore_required"},
            ),
            cloudfront_base_url=cloudfront_base_url,
            cloudfront_public_key_id=cloudfront_public_key_id,
            cloudfront_private_key_path=(
                Path(cloudfront_private_key_path_raw) if cloudfront_private_key_path_raw else None
            ),
            monthly_download_allowance_bytes=(
                _parse_bytes(
                    monthly_download_allowance_raw,
                    name=f"{prefix}MONTHLY_DOWNLOAD_ALLOWANCE_BYTES",
                    minimum=1,
                )
                if monthly_download_allowance_raw
                else None
            ),
            download_safety_buffer_bytes=(
                _parse_bytes(
                    download_safety_buffer_raw,
                    name=f"{prefix}DOWNLOAD_SAFETY_BUFFER_BYTES",
                )
                if download_safety_buffer_raw
                else 0
            ),
        )
    if write_store not in stores:
        raise ValueError(
            f"RIVERHOG_ARCHIVE_WRITE_STORE is not listed in RIVERHOG_ARCHIVE_STORES: {write_store}"
        )
    read_order = tuple(
        dict.fromkeys(
            _normalize_archive_store_name(raw)
            for raw in values.get("RIVERHOG_ARCHIVE_READ_ORDER", ",".join(names)).split(",")
            if raw.strip()
        )
    )
    return write_store, read_order, stores


def load_runtime_config() -> RuntimeConfig:
    database_url_raw = os.getenv("RIVERHOG_DATABASE_URL", "").strip()
    ttl_raw = os.getenv("RIVERHOG_UPLOAD_FILE_TTL", "24h")
    session_idle_ttl_raw = os.getenv("RIVERHOG_UPLOAD_SESSION_IDLE_TTL", "168h")
    sweep_raw = os.getenv("RIVERHOG_UPLOAD_EXPIRY_SWEEP_INTERVAL", "30s")
    log_level = os.getenv("RIVERHOG_LOG_LEVEL", DEFAULT_LOG_LEVEL).strip() or DEFAULT_LOG_LEVEL

    database_url = database_url_raw or DEFAULT_DATABASE_URL
    if _database_url_driver(database_url) != "postgresql":
        raise ValueError("RIVERHOG_DATABASE_URL must use postgresql")
    upload_file_ttl = parse_duration(ttl_raw)
    upload_session_idle_ttl = parse_duration(session_idle_ttl_raw)
    upload_expiry_sweep_interval = parse_duration(sweep_raw)
    s3_max_pool_connections = _parse_int(
        os.getenv("RIVERHOG_S3_MAX_POOL_CONNECTIONS", str(DEFAULT_S3_MAX_POOL_CONNECTIONS)),
        name="RIVERHOG_S3_MAX_POOL_CONNECTIONS",
        minimum=1,
    )
    ingress_store = IngressStoreConfig(
        endpoint_url=os.getenv("RIVERHOG_INGRESS_ENDPOINT_URL", "http://127.0.0.1:9000")
        .strip()
        .rstrip("/"),
        region=os.getenv("RIVERHOG_INGRESS_REGION", "us-east-1").strip(),
        bucket=os.getenv("RIVERHOG_INGRESS_BUCKET", "riverhog-ingress").strip(),
        access_key_id=os.getenv("RIVERHOG_INGRESS_ACCESS_KEY_ID", "minioadmin").strip(),
        secret_access_key=os.getenv("RIVERHOG_INGRESS_SECRET_ACCESS_KEY", "minioadmin").strip(),
        force_path_style=_parse_bool(os.getenv("RIVERHOG_INGRESS_FORCE_PATH_STYLE", "true")),
        prefix=os.getenv("RIVERHOG_INGRESS_PREFIX", "").strip(),
    )

    archive_multipart_part_bytes = _parse_bytes(
        os.getenv("RIVERHOG_ARCHIVE_MULTIPART_PART_BYTES", "64MiB"),
        name="RIVERHOG_ARCHIVE_MULTIPART_PART_BYTES",
        minimum=1,
    )
    archive_multipart_concurrency = _parse_int(
        os.getenv(
            "RIVERHOG_ARCHIVE_MULTIPART_CONCURRENCY",
            str(DEFAULT_ARCHIVE_MULTIPART_CONCURRENCY),
        ),
        name="RIVERHOG_ARCHIVE_MULTIPART_CONCURRENCY",
        minimum=1,
    )
    archive_multipart_max_age = parse_duration(
        os.getenv("RIVERHOG_ARCHIVE_MULTIPART_MAX_AGE", "72h")
    )
    archive_multipart_sweep_interval = parse_duration(
        os.getenv("RIVERHOG_ARCHIVE_MULTIPART_SWEEP_INTERVAL", "6h")
    )
    archive_object_concurrency = _parse_int(
        os.getenv(
            "RIVERHOG_ARCHIVE_OBJECT_CONCURRENCY",
            str(DEFAULT_ARCHIVE_OBJECT_CONCURRENCY),
        ),
        name="RIVERHOG_ARCHIVE_OBJECT_CONCURRENCY",
        minimum=1,
    )
    archive_retry_delay = parse_duration(os.getenv("RIVERHOG_ARCHIVE_UPLOAD_RETRY_DELAY", "5m"))
    archive_upload_sweep_interval = parse_duration(
        os.getenv("RIVERHOG_ARCHIVE_UPLOAD_SWEEP_INTERVAL", "30s")
    )
    ingress_cleanup_concurrency = _parse_int(
        os.getenv("RIVERHOG_INGRESS_CLEANUP_CONCURRENCY", "8"),
        name="RIVERHOG_INGRESS_CLEANUP_CONCURRENCY",
        minimum=1,
    )
    ingress_cleanup_retry_delay = parse_duration(
        os.getenv("RIVERHOG_INGRESS_CLEANUP_RETRY_DELAY", "5m")
    )
    ingress_cleanup_sweep_interval = parse_duration(
        os.getenv("RIVERHOG_INGRESS_CLEANUP_SWEEP_INTERVAL", "10s")
    )
    retrieval_sweep_interval = parse_duration(os.getenv("RIVERHOG_RETRIEVAL_SWEEP_INTERVAL", "30s"))
    retrieval_estimated_latency = parse_duration(
        os.getenv("RIVERHOG_RETRIEVAL_ESTIMATED_LATENCY", "48h")
    )
    retrieval_tier = _parse_choice(
        os.getenv("RIVERHOG_RETRIEVAL_TIER", "bulk"),
        name="RIVERHOG_RETRIEVAL_TIER",
        allowed={"bulk", "standard"},
    )
    archive_write_store, archive_read_order, archive_stores = _parse_archive_stores(os.environ)
    cache_values = {
        "endpoint_url": os.getenv("RIVERHOG_RETRIEVAL_CACHE_ENDPOINT_URL", "").strip().rstrip("/"),
        "region": os.getenv("RIVERHOG_RETRIEVAL_CACHE_REGION", "").strip(),
        "bucket": os.getenv("RIVERHOG_RETRIEVAL_CACHE_BUCKET", "").strip(),
        "access_key_id": os.getenv("RIVERHOG_RETRIEVAL_CACHE_ACCESS_KEY_ID", "").strip(),
        "secret_access_key": os.getenv("RIVERHOG_RETRIEVAL_CACHE_SECRET_ACCESS_KEY", "").strip(),
    }
    configured_cache_fields = [name for name, value in cache_values.items() if value]
    if configured_cache_fields and len(configured_cache_fields) != len(cache_values):
        raise ValueError("RIVERHOG_RETRIEVAL_CACHE_* configuration is incomplete")
    retrieval_cache = (
        RetrievalCacheConfig(
            **cache_values,
            force_path_style=_parse_bool(
                os.getenv("RIVERHOG_RETRIEVAL_CACHE_FORCE_PATH_STYLE", "false")
            ),
            prefix=os.getenv("RIVERHOG_RETRIEVAL_CACHE_PREFIX", "").strip(),
        )
        if configured_cache_fields
        else None
    )
    public_base_url = os.getenv("RIVERHOG_PUBLIC_BASE_URL", "").strip() or None
    ots_stamp_command = _parse_command(
        os.getenv("RIVERHOG_OTS_STAMP_COMMAND", "ots"),
        name="RIVERHOG_OTS_STAMP_COMMAND",
    )
    ots_verify_command = _parse_command(
        os.getenv(
            "RIVERHOG_OTS_VERIFY_COMMAND",
            "ots --no-bitcoin --no-default-whitelist",
        ),
        name="RIVERHOG_OTS_VERIFY_COMMAND",
    )
    ots_upgrade_command = _parse_command(
        os.getenv("RIVERHOG_OTS_UPGRADE_COMMAND", "ots"),
        name="RIVERHOG_OTS_UPGRADE_COMMAND",
    )
    attestation_secret_key_file = (
        Path(value)
        if (value := os.getenv("RIVERHOG_ATTESTATION_SECRET_KEY_FILE", "").strip())
        else None
    )
    attestation_public_key_file = (
        Path(value)
        if (value := os.getenv("RIVERHOG_ATTESTATION_PUBLIC_KEY_FILE", "").strip())
        else None
    )
    archive_passphrase_supplied = "RIVERHOG_ARCHIVE_PASSPHRASE" in os.environ
    archive_passphrase = (
        os.getenv("RIVERHOG_ARCHIVE_PASSPHRASE", DEV_ARCHIVE_PASSPHRASE).strip()
        or DEV_ARCHIVE_PASSPHRASE
    )
    archive_require_explicit_passphrase = _parse_bool(
        os.getenv("RIVERHOG_ARCHIVE_REQUIRE_EXPLICIT_PASSPHRASE", "false")
    )
    archive_scrypt_work_factor = _parse_int(
        os.getenv(
            "RIVERHOG_ARCHIVE_SCRYPT_WORK_FACTOR",
            str(DEFAULT_ARCHIVE_SCRYPT_WORK_FACTOR),
        ),
        name="RIVERHOG_ARCHIVE_SCRYPT_WORK_FACTOR",
        minimum=1,
    )
    if archive_scrypt_work_factor > 22:
        raise ValueError("RIVERHOG_ARCHIVE_SCRYPT_WORK_FACTOR must be <= 22")
    if archive_require_explicit_passphrase and (
        not archive_passphrase_supplied or archive_passphrase == DEV_ARCHIVE_PASSPHRASE
    ):
        raise ValueError(
            "RIVERHOG_ARCHIVE_REQUIRE_EXPLICIT_PASSPHRASE requires "
            "RIVERHOG_ARCHIVE_PASSPHRASE to be explicitly set to a non-development secret"
        )
    return RuntimeConfig(
        event_source=os.getenv("RIVERHOG_EVENT_SOURCE", "urn:riverhog").strip(),
        event_context_retention=parse_duration(
            os.getenv("RIVERHOG_EVENT_CONTEXT_RETENTION", "30d")
        ),
        s3_max_pool_connections=s3_max_pool_connections,
        ingress_store=ingress_store,
        ingress_secret_key=os.getenv("RIVERHOG_INGRESS_SECRET_KEY", DEV_INGRESS_SECRET_KEY).strip(),
        ingress_cleanup_concurrency=ingress_cleanup_concurrency,
        ingress_cleanup_retry_delay=ingress_cleanup_retry_delay,
        ingress_cleanup_sweep_interval=ingress_cleanup_sweep_interval,
        tusd_base_url=os.getenv("RIVERHOG_TUSD_BASE_URL", "http://127.0.0.1:1080/files").rstrip(
            "/"
        ),
        tusd_public_base_url=(
            os.getenv("RIVERHOG_TUSD_PUBLIC_BASE_URL", "").strip().rstrip("/") or None
        ),
        tusd_hook_secret=os.getenv("RIVERHOG_TUSD_HOOK_SECRET", "dev-tusd-hook-secret"),
        tusd_append_timeout=parse_duration(os.getenv("RIVERHOG_TUSD_APPEND_TIMEOUT", "60s")),
        database_url=database_url,
        upload_file_ttl=upload_file_ttl,
        upload_session_idle_ttl=upload_session_idle_ttl,
        upload_expiry_sweep_interval=upload_expiry_sweep_interval,
        log_level=log_level,
        archive_write_store=archive_write_store,
        archive_read_order=archive_read_order,
        archive_stores=archive_stores,
        archive_multipart_part_bytes=archive_multipart_part_bytes,
        archive_multipart_concurrency=archive_multipart_concurrency,
        archive_multipart_max_age=archive_multipart_max_age,
        archive_multipart_sweep_interval=archive_multipart_sweep_interval,
        archive_object_concurrency=archive_object_concurrency,
        retrieval_cache=retrieval_cache,
        retrieval_cache_new_archive_lease=parse_duration(
            os.getenv("RIVERHOG_RETRIEVAL_CACHE_NEW_ARCHIVE_LEASE", "30d")
        ),
        retrieval_default_lease=parse_duration(os.getenv("RIVERHOG_RETRIEVAL_DEFAULT_LEASE", "7d")),
        retrieval_max_lease=parse_duration(os.getenv("RIVERHOG_RETRIEVAL_MAX_LEASE", "30d")),
        retrieval_cache_sweep_interval=parse_duration(
            os.getenv("RIVERHOG_RETRIEVAL_CACHE_SWEEP_INTERVAL", "1h")
        ),
        archive_passphrase=archive_passphrase,
        archive_require_explicit_passphrase=archive_require_explicit_passphrase,
        archive_scrypt_work_factor=archive_scrypt_work_factor,
        archive_upload_retry_delay=archive_retry_delay,
        archive_upload_sweep_interval=archive_upload_sweep_interval,
        retrieval_sweep_interval=retrieval_sweep_interval,
        retrieval_estimated_latency=retrieval_estimated_latency,
        retrieval_tier=retrieval_tier,
        ots_stamp_command=ots_stamp_command,
        ots_verify_command=ots_verify_command,
        ots_upgrade_command=ots_upgrade_command,
        attestation_secret_key_file=attestation_secret_key_file,
        attestation_public_key_file=attestation_public_key_file,
        proof_maturation_retry_delay=parse_duration(
            os.getenv("RIVERHOG_PROOF_MATURATION_RETRY_DELAY", "6h")
        ),
        proof_maturation_sweep_interval=parse_duration(
            os.getenv("RIVERHOG_PROOF_MATURATION_SWEEP_INTERVAL", "1h")
        ),
        public_base_url=public_base_url,
    )
