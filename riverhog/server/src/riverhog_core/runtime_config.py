from __future__ import annotations

import json
import os
import re
import shlex
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from datetime import timedelta
from math import isfinite
from pathlib import Path
from urllib.parse import urlsplit

from http_api_contracts import safe_http_base_url
from riverhog_archive_contracts import (
    ARCHIVE_ENCRYPTION_FORMAT,
    CollectionEncryptionBinding,
    normalize_passphrase_id,
)
from time_formats import parse_duration

_BYTES_RE = re.compile(r"^(\d+(?:_\d+)*)([kmgt]i?b?|b)?$", re.IGNORECASE)
DEV_ARCHIVE_PASSPHRASE = "riverhog-dev-archive-passphrase"
DEV_ARCHIVE_PASSPHRASE_ID = "riverhog-dev-key-v1"
DEFAULT_DATABASE_URL = "postgresql+psycopg://riverhog:riverhog@127.0.0.1:5432/riverhog"
DEFAULT_ARCHIVE_MULTIPART_PART_BYTES = 64 * 1024 * 1024
DEFAULT_STORAGE_ADAPTER_MAX_CONNECTIONS = 32
DEFAULT_STORAGE_ADAPTER_TIMEOUT_SECONDS = 300.0
DEFAULT_ARCHIVE_SCRYPT_WORK_FACTOR = 18
DEFAULT_LOG_LEVEL = "INFO"


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
    if not isfinite(parsed) or parsed <= minimum:
        raise ValueError(f"invalid {name} {value!r}: expected > {minimum}")
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
class StorageAdapterRegistration:
    name: str
    base_url: str
    token_file: Path
    allow_insecure_http: bool = False
    maximum_connections: int = DEFAULT_STORAGE_ADAPTER_MAX_CONNECTIONS
    timeout_seconds: float = DEFAULT_STORAGE_ADAPTER_TIMEOUT_SECONDS
    monthly_download_allowance_bytes: int | None = None
    download_safety_buffer_bytes: int = 0


@dataclass(frozen=True, slots=True)
class RuntimeConfig:
    database_url: str = ""
    log_level: str = DEFAULT_LOG_LEVEL
    archive_write_store: str = "archive"
    archive_read_order: tuple[str, ...] = ("archive",)
    archive_stores: Mapping[str, StorageAdapterRegistration] = field(
        default_factory=lambda: {
            "archive": StorageAdapterRegistration(
                name="archive",
                base_url="http://127.0.0.1:9081",
                token_file=Path("/run/secrets/riverhog_archive_adapter_token"),
            )
        }
    )
    archive_multipart_part_bytes: int = DEFAULT_ARCHIVE_MULTIPART_PART_BYTES
    archive_multipart_max_age: timedelta = field(default_factory=lambda: timedelta(days=3))
    archive_multipart_sweep_interval: timedelta = field(default_factory=lambda: timedelta(hours=6))
    retrieval_cache: StorageAdapterRegistration | None = None
    retrieval_cache_new_archive_enabled: bool = True
    retrieval_cache_new_archive_lease: timedelta = field(
        default_factory=lambda: timedelta(hours=72)
    )
    retrieval_default_lease: timedelta = field(default_factory=lambda: timedelta(hours=24))
    retrieval_max_lease: timedelta = field(default_factory=lambda: timedelta(days=7))
    retrieval_pending_timeout: timedelta = field(default_factory=lambda: timedelta(hours=72))
    retrieval_cache_sweep_interval: timedelta = field(default_factory=lambda: timedelta(minutes=5))
    archive_passphrases: Mapping[str, str] = field(
        default_factory=lambda: {DEV_ARCHIVE_PASSPHRASE_ID: DEV_ARCHIVE_PASSPHRASE}
    )
    archive_active_passphrase_id: str = DEV_ARCHIVE_PASSPHRASE_ID
    archive_require_explicit_passphrases: bool = False
    archive_scrypt_work_factor: int = DEFAULT_ARCHIVE_SCRYPT_WORK_FACTOR
    archive_upload_sweep_interval: timedelta = field(default_factory=lambda: timedelta(seconds=30))
    retrieval_restore_poll_interval: timedelta = field(default_factory=lambda: timedelta(minutes=5))
    retrieval_estimated_latency: timedelta = field(default_factory=lambda: timedelta(hours=48))
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
        if self.public_base_url is not None:
            public_base_url = self.public_base_url.strip().rstrip("/")
            parsed_public_base_url = urlsplit(public_base_url)
            if (
                parsed_public_base_url.scheme not in {"http", "https"}
                or not parsed_public_base_url.hostname
                or parsed_public_base_url.username is not None
                or parsed_public_base_url.password is not None
                or parsed_public_base_url.query
                or parsed_public_base_url.fragment
            ):
                raise ValueError(
                    "RIVERHOG_PUBLIC_BASE_URL must be an HTTP(S) URL without "
                    "credentials, query, or fragment"
                )
            object.__setattr__(self, "public_base_url", public_base_url)
        archive_write_store = _normalize_archive_store_name(self.archive_write_store)
        normalized_archive_stores: dict[str, StorageAdapterRegistration] = {}
        for raw_name, store in self.archive_stores.items():
            name = _normalize_archive_store_name(raw_name)
            if store.name != name:
                raise ValueError(f"archive store mapping key must match its name: {raw_name!r}")
            store = replace(
                store,
                base_url=safe_http_base_url(
                    store.base_url,
                    setting=f"archive store {name} adapter URL",
                    allow_insecure_http=store.allow_insecure_http,
                ),
            )
            if str(store.token_file) == ".":
                raise ValueError(f"archive store {name} adapter token file must be set")
            if store.maximum_connections < 1:
                raise ValueError(
                    f"archive store {name} adapter maximum connections must be positive"
                )
            if store.timeout_seconds <= 0:
                raise ValueError(f"archive store {name} adapter timeout must be positive")
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
            normalized_archive_stores[name] = store
        metered_sources: dict[tuple[str, ...], list[str]] = {}
        for name, store in normalized_archive_stores.items():
            metered_sources.setdefault((store.base_url.casefold(),), []).append(name)
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
        if self.retrieval_cache is not None:
            cache = replace(
                self.retrieval_cache,
                base_url=safe_http_base_url(
                    self.retrieval_cache.base_url,
                    setting="retrieval cache adapter URL",
                    allow_insecure_http=self.retrieval_cache.allow_insecure_http,
                ),
            )
            if cache.name != "retrieval-cache":
                raise ValueError(
                    "retrieval cache adapter registration name must be retrieval-cache"
                )
            if str(cache.token_file) == ".":
                raise ValueError("retrieval cache adapter token file must be set")
            if cache.maximum_connections < 1:
                raise ValueError("retrieval cache adapter maximum connections must be positive")
            if cache.timeout_seconds <= 0:
                raise ValueError("retrieval cache adapter timeout must be positive")
            if (
                cache.monthly_download_allowance_bytes is not None
                or cache.download_safety_buffer_bytes != 0
            ):
                raise ValueError("retrieval cache adapter does not accept archive allowances")
            object.__setattr__(self, "retrieval_cache", cache)
        if self.archive_multipart_part_bytes < 1:
            raise ValueError("RIVERHOG_ARCHIVE_MULTIPART_PART_BYTES must be >= 1")
        if self.archive_multipart_max_age.total_seconds() <= 0.0:
            raise ValueError("RIVERHOG_ARCHIVE_MULTIPART_MAX_AGE must be > 0")
        if self.archive_multipart_sweep_interval.total_seconds() <= 0.0:
            raise ValueError("RIVERHOG_ARCHIVE_MULTIPART_SWEEP_INTERVAL must be > 0")
        if self.retrieval_cache_new_archive_lease.total_seconds() <= 0:
            raise ValueError("RIVERHOG_RETRIEVAL_CACHE_NEW_ARCHIVE_LEASE must be > 0")
        if self.retrieval_default_lease.total_seconds() <= 0:
            raise ValueError("RIVERHOG_RETRIEVAL_DEFAULT_LEASE must be > 0")
        if self.retrieval_max_lease < self.retrieval_default_lease:
            raise ValueError(
                "RIVERHOG_RETRIEVAL_MAX_LEASE must be at least RIVERHOG_RETRIEVAL_DEFAULT_LEASE"
            )
        if self.retrieval_pending_timeout.total_seconds() <= 0:
            raise ValueError("RIVERHOG_RETRIEVAL_PENDING_TIMEOUT must be > 0")
        if self.retrieval_cache_sweep_interval.total_seconds() <= 0:
            raise ValueError("RIVERHOG_RETRIEVAL_CACHE_SWEEP_INTERVAL must be > 0")
        if self.retrieval_restore_poll_interval.total_seconds() <= 0:
            raise ValueError("RIVERHOG_RETRIEVAL_RESTORE_POLL_INTERVAL must be > 0")
        if self.archive_scrypt_work_factor < 1 or self.archive_scrypt_work_factor > 22:
            raise ValueError("RIVERHOG_ARCHIVE_SCRYPT_WORK_FACTOR must be in 1..22")
        archive_passphrases: dict[str, str] = {}
        for passphrase_id, passphrase in self.archive_passphrases.items():
            try:
                normalized_id = normalize_passphrase_id(passphrase_id)
            except ValueError as exc:
                raise ValueError(f"invalid archive passphrase ID: {passphrase_id!r}") from exc
            if not isinstance(passphrase, str) or not passphrase:
                raise ValueError(f"archive passphrase {normalized_id!r} must not be empty")
            archive_passphrases[normalized_id] = passphrase
        if not archive_passphrases:
            raise ValueError("RIVERHOG_ARCHIVE_PASSPHRASES_JSON must define at least one key")
        if len(set(archive_passphrases.values())) != len(archive_passphrases):
            raise ValueError("archive passphrase IDs must identify distinct secrets")
        try:
            active_passphrase_id = normalize_passphrase_id(self.archive_active_passphrase_id)
        except ValueError as exc:
            raise ValueError("RIVERHOG_ARCHIVE_ACTIVE_PASSPHRASE_ID is invalid") from exc
        if active_passphrase_id not in archive_passphrases:
            raise ValueError(
                "RIVERHOG_ARCHIVE_ACTIVE_PASSPHRASE_ID is not present in "
                "RIVERHOG_ARCHIVE_PASSPHRASES_JSON"
            )
        object.__setattr__(self, "archive_passphrases", archive_passphrases)
        object.__setattr__(self, "archive_active_passphrase_id", active_passphrase_id)
        if self.archive_require_explicit_passphrases and (
            active_passphrase_id == DEV_ARCHIVE_PASSPHRASE_ID
            or DEV_ARCHIVE_PASSPHRASE in archive_passphrases.values()
        ):
            raise ValueError(
                "RIVERHOG_ARCHIVE_REQUIRE_EXPLICIT_PASSPHRASES rejects the development key"
            )

    def archive_store(self, name: str) -> StorageAdapterRegistration:
        normalized = _normalize_archive_store_name(name)
        try:
            return self.archive_stores[normalized]
        except KeyError as exc:
            raise ValueError(f"archive store is not configured: {normalized}") from exc

    @property
    def archive_active_encryption(self) -> CollectionEncryptionBinding:
        return CollectionEncryptionBinding(
            format=ARCHIVE_ENCRYPTION_FORMAT,
            passphrase_id=self.archive_active_passphrase_id,
        )

    def archive_passphrase_for(self, passphrase_id: str) -> str:
        normalized = normalize_passphrase_id(passphrase_id)
        try:
            return self.archive_passphrases[normalized]
        except KeyError as exc:
            raise ValueError(f"archive passphrase ID is not configured: {normalized}") from exc


def _parse_archive_stores(
    values: Mapping[str, str],
) -> tuple[str, tuple[str, ...], dict[str, StorageAdapterRegistration]]:
    named_store_configuration = "RIVERHOG_ARCHIVE_STORES" in values
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
    stores: dict[str, StorageAdapterRegistration] = {}
    for name in names:
        prefix = f"RIVERHOG_ARCHIVE_STORE_{_archive_store_env_suffix(name)}_"
        configured_url = values.get(f"{prefix}ADAPTER_URL", "").strip()
        configured_token_file = values.get(f"{prefix}ADAPTER_TOKEN_FILE", "").strip()
        if named_store_configuration or configured_url or configured_token_file:
            if not configured_url or not configured_token_file:
                raise ValueError(f"archive store {name} adapter connection is incomplete")
        adapter_url = configured_url or "http://127.0.0.1:9081"
        monthly_download_allowance_raw = values.get(
            f"{prefix}MONTHLY_DOWNLOAD_ALLOWANCE_BYTES", ""
        ).strip()
        download_safety_buffer_raw = values.get(f"{prefix}DOWNLOAD_SAFETY_BUFFER_BYTES", "").strip()
        stores[name] = StorageAdapterRegistration(
            name=name,
            base_url=adapter_url.rstrip("/"),
            token_file=Path(configured_token_file or "/run/secrets/riverhog_archive_adapter_token"),
            allow_insecure_http=_parse_bool(
                values.get(
                    f"{prefix}ADAPTER_ALLOW_INSECURE_HTTP",
                    "false",
                )
            ),
            maximum_connections=_parse_int(
                values.get(
                    f"{prefix}ADAPTER_MAX_CONNECTIONS",
                    str(DEFAULT_STORAGE_ADAPTER_MAX_CONNECTIONS),
                ),
                name=f"{prefix}ADAPTER_MAX_CONNECTIONS",
                minimum=1,
            ),
            timeout_seconds=_parse_float(
                values.get(
                    f"{prefix}ADAPTER_TIMEOUT_SECONDS",
                    str(DEFAULT_STORAGE_ADAPTER_TIMEOUT_SECONDS),
                ),
                name=f"{prefix}ADAPTER_TIMEOUT_SECONDS",
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
    log_level = os.getenv("RIVERHOG_LOG_LEVEL", DEFAULT_LOG_LEVEL).strip() or DEFAULT_LOG_LEVEL

    database_url = database_url_raw or DEFAULT_DATABASE_URL
    if _database_url_driver(database_url) != "postgresql":
        raise ValueError("RIVERHOG_DATABASE_URL must use postgresql")
    archive_multipart_part_bytes = _parse_bytes(
        os.getenv("RIVERHOG_ARCHIVE_MULTIPART_PART_BYTES", "64MiB"),
        name="RIVERHOG_ARCHIVE_MULTIPART_PART_BYTES",
        minimum=1,
    )
    archive_multipart_max_age = parse_duration(
        os.getenv("RIVERHOG_ARCHIVE_MULTIPART_MAX_AGE", "72h")
    )
    archive_multipart_sweep_interval = parse_duration(
        os.getenv("RIVERHOG_ARCHIVE_MULTIPART_SWEEP_INTERVAL", "6h")
    )
    archive_upload_sweep_interval = parse_duration(
        os.getenv("RIVERHOG_ARCHIVE_UPLOAD_SWEEP_INTERVAL", "30s")
    )
    retrieval_restore_poll_interval = parse_duration(
        os.getenv("RIVERHOG_RETRIEVAL_RESTORE_POLL_INTERVAL", "5m")
    )
    retrieval_estimated_latency = parse_duration(
        os.getenv("RIVERHOG_RETRIEVAL_ESTIMATED_LATENCY", "48h")
    )
    archive_write_store, archive_read_order, archive_stores = _parse_archive_stores(os.environ)
    cache_values = {
        "base_url": os.getenv("RIVERHOG_RETRIEVAL_CACHE_ADAPTER_URL", "").strip().rstrip("/"),
        "token_file": os.getenv("RIVERHOG_RETRIEVAL_CACHE_ADAPTER_TOKEN_FILE", "").strip(),
    }
    configured_cache_fields = [name for name, value in cache_values.items() if value]
    if configured_cache_fields and len(configured_cache_fields) != len(cache_values):
        raise ValueError("RIVERHOG_RETRIEVAL_CACHE_ADAPTER_* configuration is incomplete")
    retrieval_cache = (
        StorageAdapterRegistration(
            name="retrieval-cache",
            base_url=cache_values["base_url"],
            token_file=Path(cache_values["token_file"]),
            allow_insecure_http=_parse_bool(
                os.getenv("RIVERHOG_RETRIEVAL_CACHE_ADAPTER_ALLOW_INSECURE_HTTP", "false")
            ),
            maximum_connections=_parse_int(
                os.getenv(
                    "RIVERHOG_RETRIEVAL_CACHE_ADAPTER_MAX_CONNECTIONS",
                    str(DEFAULT_STORAGE_ADAPTER_MAX_CONNECTIONS),
                ),
                name="RIVERHOG_RETRIEVAL_CACHE_ADAPTER_MAX_CONNECTIONS",
                minimum=1,
            ),
            timeout_seconds=_parse_float(
                os.getenv(
                    "RIVERHOG_RETRIEVAL_CACHE_ADAPTER_TIMEOUT_SECONDS",
                    str(DEFAULT_STORAGE_ADAPTER_TIMEOUT_SECONDS),
                ),
                name="RIVERHOG_RETRIEVAL_CACHE_ADAPTER_TIMEOUT_SECONDS",
            ),
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
    configured_archive_passphrases = os.getenv("RIVERHOG_ARCHIVE_PASSPHRASES_JSON", "").strip()
    archive_passphrases_supplied = bool(configured_archive_passphrases)
    archive_passphrases_raw = configured_archive_passphrases or json.dumps(
        {DEV_ARCHIVE_PASSPHRASE_ID: DEV_ARCHIVE_PASSPHRASE}
    )
    try:
        archive_passphrases_value = json.loads(archive_passphrases_raw)
    except json.JSONDecodeError as exc:
        raise ValueError("RIVERHOG_ARCHIVE_PASSPHRASES_JSON must be valid JSON") from exc
    if not isinstance(archive_passphrases_value, dict) or not all(
        isinstance(key, str) and isinstance(value, str)
        for key, value in archive_passphrases_value.items()
    ):
        raise ValueError("RIVERHOG_ARCHIVE_PASSPHRASES_JSON must be a string-to-string object")
    archive_passphrases = dict(archive_passphrases_value)
    archive_active_passphrase_id = (
        os.getenv("RIVERHOG_ARCHIVE_ACTIVE_PASSPHRASE_ID", "").strip() or DEV_ARCHIVE_PASSPHRASE_ID
    )
    archive_require_explicit_passphrases = _parse_bool(
        os.getenv("RIVERHOG_ARCHIVE_REQUIRE_EXPLICIT_PASSPHRASES", "false")
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
    if archive_require_explicit_passphrases and not archive_passphrases_supplied:
        raise ValueError(
            "RIVERHOG_ARCHIVE_REQUIRE_EXPLICIT_PASSPHRASES requires "
            "RIVERHOG_ARCHIVE_PASSPHRASES_JSON"
        )
    return RuntimeConfig(
        event_source=os.getenv("RIVERHOG_EVENT_SOURCE", "urn:riverhog").strip(),
        event_context_retention=parse_duration(
            os.getenv("RIVERHOG_EVENT_CONTEXT_RETENTION", "30d")
        ),
        database_url=database_url,
        log_level=log_level,
        archive_write_store=archive_write_store,
        archive_read_order=archive_read_order,
        archive_stores=archive_stores,
        archive_multipart_part_bytes=archive_multipart_part_bytes,
        archive_multipart_max_age=archive_multipart_max_age,
        archive_multipart_sweep_interval=archive_multipart_sweep_interval,
        retrieval_cache=retrieval_cache,
        retrieval_cache_new_archive_enabled=_parse_bool(
            os.getenv("RIVERHOG_RETRIEVAL_CACHE_NEW_ARCHIVE_ENABLED", "true")
        ),
        retrieval_cache_new_archive_lease=parse_duration(
            os.getenv("RIVERHOG_RETRIEVAL_CACHE_NEW_ARCHIVE_LEASE", "72h")
        ),
        retrieval_default_lease=parse_duration(
            os.getenv("RIVERHOG_RETRIEVAL_DEFAULT_LEASE", "24h")
        ),
        retrieval_max_lease=parse_duration(os.getenv("RIVERHOG_RETRIEVAL_MAX_LEASE", "7d")),
        retrieval_pending_timeout=parse_duration(
            os.getenv("RIVERHOG_RETRIEVAL_PENDING_TIMEOUT", "72h")
        ),
        retrieval_cache_sweep_interval=parse_duration(
            os.getenv("RIVERHOG_RETRIEVAL_CACHE_SWEEP_INTERVAL", "5m")
        ),
        archive_passphrases=archive_passphrases,
        archive_active_passphrase_id=archive_active_passphrase_id,
        archive_require_explicit_passphrases=archive_require_explicit_passphrases,
        archive_scrypt_work_factor=archive_scrypt_work_factor,
        archive_upload_sweep_interval=archive_upload_sweep_interval,
        retrieval_restore_poll_interval=retrieval_restore_poll_interval,
        retrieval_estimated_latency=retrieval_estimated_latency,
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
