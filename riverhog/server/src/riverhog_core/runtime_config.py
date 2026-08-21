from __future__ import annotations

import os
import re
import shlex
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from urllib.parse import urlsplit

from riverhog_storage_adapter_protocol import StorageProfile, StorageProfilePayload
from time_formats import parse_duration

_BYTES_RE = re.compile(r"^(\d+(?:_\d+)*)([kmgt]i?b?|b)?$", re.IGNORECASE)
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
DEV_ARCHIVE_PASSPHRASE = "riverhog-dev-archive-passphrase"
DEV_STORAGE_ADAPTER_HOSTS = frozenset(
    {"127.0.0.1", "localhost", "garage-storage-adapter"}
)
DEFAULT_DATABASE_URL = "postgresql+psycopg://riverhog:riverhog@127.0.0.1:5432/riverhog"
DEFAULT_ARCHIVE_MULTIPART_PART_BYTES = 64 * 1024 * 1024
DEFAULT_STORAGE_ADAPTER_MAX_CONNECTIONS = 32
DEFAULT_ARCHIVE_SCRYPT_WORK_FACTOR = 18
DEFAULT_LOG_LEVEL = "INFO"
DEV_STORAGE_PROFILE = StorageProfile.seal(
    StorageProfilePayload(
        profile_id="riverhog.garage-development/v1",
        read_mode="immediate",
        egress_accounting_id="riverhog-garage-development",
    )
)


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


def _normalize_name(value: str, *, kind: str) -> str:
    name = value.strip().casefold()
    if not name or not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", name):
        raise ValueError(
            f"invalid {kind} name {value!r}: expected lowercase letters, digits, and dashes"
        )
    return name


def _env_suffix(name: str) -> str:
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
    endpoint_url: str
    token_file: Path
    expected_profile_id: str
    expected_profile_read_mode: str
    expected_egress_accounting_id: str
    expected_profile_contract_sha256: str
    allow_insecure_http: bool = False
    expected_implementation_id: str | None = None

    def __post_init__(self) -> None:
        name = _normalize_name(self.name, kind="storage adapter")
        object.__setattr__(self, "name", name)
        endpoint = self.endpoint_url.strip().rstrip("/")
        parsed = urlsplit(endpoint)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError(
                f"storage adapter {name} endpoint must be an HTTP(S) URL without "
                "credentials, query, or fragment"
            )
        if parsed.scheme == "http" and not self.allow_insecure_http:
            raise ValueError(
                f"storage adapter {name} plaintext HTTP requires explicit insecure opt-in"
            )
        object.__setattr__(self, "endpoint_url", endpoint)
        if not str(self.token_file):
            raise ValueError(f"storage adapter {name} token file must not be empty")
        if not self.expected_profile_id.strip():
            raise ValueError(f"storage adapter {name} expected profile ID must not be blank")
        if self.expected_profile_read_mode not in {"immediate", "restore_required"}:
            raise ValueError(
                f"storage adapter {name} expected read mode must be immediate or restore_required"
            )
        if not self.expected_egress_accounting_id.strip():
            raise ValueError(
                f"storage adapter {name} expected egress accounting ID must not be blank"
            )
        if _SHA256_RE.fullmatch(self.expected_profile_contract_sha256) is None:
            raise ValueError(
                f"storage adapter {name} expected profile contract must be a lowercase SHA-256"
            )
        StorageProfile(
            protocol="riverhog-storage-adapter/v1",
            profile_id=self.expected_profile_id,
            read_mode=self.expected_profile_read_mode,  # type: ignore[arg-type]
            egress_accounting_id=self.expected_egress_accounting_id,
            profile_contract_sha256=self.expected_profile_contract_sha256,
        )
        if (
            self.expected_implementation_id is not None
            and not self.expected_implementation_id.strip()
        ):
            raise ValueError(
                f"storage adapter {name} expected implementation ID must not be blank"
            )


@dataclass(frozen=True, slots=True)
class ArchiveStoreConfig:
    name: str
    storage_adapter: str
    monthly_download_allowance_bytes: int | None = None
    download_safety_buffer_bytes: int = 0


@dataclass(frozen=True, slots=True)
class RetrievalCacheConfig:
    storage_adapter: str


def _is_development_adapter(adapter: StorageAdapterRegistration) -> bool:
    host = (urlsplit(adapter.endpoint_url).hostname or "").casefold()
    return (
        host in DEV_STORAGE_ADAPTER_HOSTS
        and adapter.expected_profile_id == DEV_STORAGE_PROFILE.profile_id
        and adapter.expected_profile_contract_sha256
        == DEV_STORAGE_PROFILE.profile_contract_sha256
    )


@dataclass(frozen=True, slots=True)
class RuntimeConfig:
    storage_adapter_max_connections: int = DEFAULT_STORAGE_ADAPTER_MAX_CONNECTIONS
    database_url: str = ""
    log_level: str = DEFAULT_LOG_LEVEL
    storage_adapters: Mapping[str, StorageAdapterRegistration] = field(
        default_factory=lambda: {
            "archive": StorageAdapterRegistration(
                name="archive",
                endpoint_url="http://127.0.0.1:8081",
                token_file=Path("/run/secrets/riverhog-storage-adapter-token"),
                expected_profile_id=DEV_STORAGE_PROFILE.profile_id,
                expected_profile_read_mode=DEV_STORAGE_PROFILE.read_mode,
                expected_egress_accounting_id=DEV_STORAGE_PROFILE.egress_accounting_id,
                expected_profile_contract_sha256=(
                    DEV_STORAGE_PROFILE.profile_contract_sha256
                ),
                allow_insecure_http=True,
                expected_implementation_id="riverhog.garage-storage-adapter/v1",
            )
        }
    )
    archive_write_store: str = "archive"
    archive_read_order: tuple[str, ...] = ("archive",)
    archive_stores: Mapping[str, ArchiveStoreConfig] = field(
        default_factory=lambda: {
            "archive": ArchiveStoreConfig(
                name="archive",
                storage_adapter="archive",
            )
        }
    )
    archive_multipart_part_bytes: int = DEFAULT_ARCHIVE_MULTIPART_PART_BYTES
    archive_multipart_max_age: timedelta = field(default_factory=lambda: timedelta(days=3))
    archive_multipart_sweep_interval: timedelta = field(default_factory=lambda: timedelta(hours=6))
    retrieval_cache: RetrievalCacheConfig | None = None
    retrieval_cache_new_archive_enabled: bool = True
    retrieval_cache_new_archive_lease: timedelta = field(
        default_factory=lambda: timedelta(hours=72)
    )
    retrieval_default_lease: timedelta = field(default_factory=lambda: timedelta(hours=24))
    retrieval_max_lease: timedelta = field(default_factory=lambda: timedelta(days=7))
    retrieval_pending_timeout: timedelta = field(default_factory=lambda: timedelta(hours=72))
    retrieval_cache_sweep_interval: timedelta = field(default_factory=lambda: timedelta(minutes=5))
    archive_passphrase: str = DEV_ARCHIVE_PASSPHRASE
    archive_require_explicit_passphrase: bool = False
    archive_scrypt_work_factor: int = DEFAULT_ARCHIVE_SCRYPT_WORK_FACTOR
    archive_upload_sweep_interval: timedelta = field(default_factory=lambda: timedelta(seconds=30))
    retrieval_restore_poll_interval: timedelta = field(default_factory=lambda: timedelta(minutes=5))
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
        for name, value in (
            ("RIVERHOG_EVENT_CONTEXT_RETENTION", self.event_context_retention),
            ("RIVERHOG_PROOF_MATURATION_RETRY_DELAY", self.proof_maturation_retry_delay),
            ("RIVERHOG_PROOF_MATURATION_SWEEP_INTERVAL", self.proof_maturation_sweep_interval),
            ("RIVERHOG_ARCHIVE_MULTIPART_MAX_AGE", self.archive_multipart_max_age),
            ("RIVERHOG_ARCHIVE_MULTIPART_SWEEP_INTERVAL", self.archive_multipart_sweep_interval),
            ("RIVERHOG_RETRIEVAL_CACHE_NEW_ARCHIVE_LEASE", self.retrieval_cache_new_archive_lease),
            ("RIVERHOG_RETRIEVAL_DEFAULT_LEASE", self.retrieval_default_lease),
            ("RIVERHOG_RETRIEVAL_PENDING_TIMEOUT", self.retrieval_pending_timeout),
            ("RIVERHOG_RETRIEVAL_CACHE_SWEEP_INTERVAL", self.retrieval_cache_sweep_interval),
            ("RIVERHOG_ARCHIVE_UPLOAD_SWEEP_INTERVAL", self.archive_upload_sweep_interval),
            ("RIVERHOG_RETRIEVAL_RESTORE_POLL_INTERVAL", self.retrieval_restore_poll_interval),
        ):
            if value.total_seconds() <= 0:
                raise ValueError(f"{name} must be > 0")
        if self.retrieval_max_lease < self.retrieval_default_lease:
            raise ValueError(
                "RIVERHOG_RETRIEVAL_MAX_LEASE must be at least RIVERHOG_RETRIEVAL_DEFAULT_LEASE"
            )
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
            parsed = urlsplit(public_base_url)
            if (
                parsed.scheme not in {"http", "https"}
                or not parsed.hostname
                or parsed.username is not None
                or parsed.password is not None
                or parsed.query
                or parsed.fragment
            ):
                raise ValueError(
                    "RIVERHOG_PUBLIC_BASE_URL must be an HTTP(S) URL without "
                    "credentials, query, or fragment"
                )
            object.__setattr__(self, "public_base_url", public_base_url)
        if self.storage_adapter_max_connections < 1:
            raise ValueError("RIVERHOG_STORAGE_ADAPTER_MAX_CONNECTIONS must be >= 1")
        if self.archive_multipart_part_bytes < 1:
            raise ValueError("RIVERHOG_ARCHIVE_MULTIPART_PART_BYTES must be >= 1")
        if self.archive_scrypt_work_factor < 1 or self.archive_scrypt_work_factor > 22:
            raise ValueError("RIVERHOG_ARCHIVE_SCRYPT_WORK_FACTOR must be in 1..22")
        if not self.archive_passphrase:
            raise ValueError("RIVERHOG_ARCHIVE_PASSPHRASE must be set")

        adapters: dict[str, StorageAdapterRegistration] = {}
        for raw_name, adapter in self.storage_adapters.items():
            name = _normalize_name(raw_name, kind="storage adapter")
            if adapter.name != name:
                raise ValueError(
                    f"storage-adapter mapping key must match its name: {raw_name!r}"
                )
            adapters[name] = adapter
        if not adapters:
            raise ValueError("at least one storage adapter must be configured")
        object.__setattr__(self, "storage_adapters", adapters)

        stores: dict[str, ArchiveStoreConfig] = {}
        for raw_name, store in self.archive_stores.items():
            name = _normalize_name(raw_name, kind="archive store")
            if store.name != name:
                raise ValueError(
                    f"archive-store mapping key must match its name: {raw_name!r}"
                )
            registration = _normalize_name(
                store.storage_adapter,
                kind="storage adapter",
            )
            if registration not in adapters:
                raise ValueError(
                    f"archive store {name} references an unknown storage adapter: {registration}"
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
            stores[name] = ArchiveStoreConfig(
                name=name,
                storage_adapter=registration,
                monthly_download_allowance_bytes=store.monthly_download_allowance_bytes,
                download_safety_buffer_bytes=store.download_safety_buffer_bytes,
            )
        write_store = _normalize_name(self.archive_write_store, kind="archive store")
        if write_store not in stores:
            raise ValueError(f"archive write store is not configured: {write_store}")
        read_order = tuple(
            dict.fromkeys(
                _normalize_name(name, kind="archive store")
                for name in self.archive_read_order
            )
        )
        unknown_read_stores = set(read_order) - set(stores)
        if unknown_read_stores:
            raise ValueError(
                "archive read order contains unconfigured stores: "
                + ", ".join(sorted(unknown_read_stores))
            )
        metered: dict[str, list[str]] = {}
        for name, store in stores.items():
            if store.monthly_download_allowance_bytes is not None:
                metered.setdefault(store.storage_adapter, []).append(name)
        duplicates = [names for names in metered.values() if len(names) > 1]
        if duplicates:
            raise ValueError(
                "a metered storage-adapter registration must have one archive-store name; "
                "duplicate aliases: " + ", ".join(sorted(duplicates[0]))
            )
        object.__setattr__(self, "archive_write_store", write_store)
        object.__setattr__(
            self,
            "archive_read_order",
            (*read_order, *[name for name in stores if name not in read_order]),
        )
        object.__setattr__(self, "archive_stores", stores)

        if self.retrieval_cache is not None:
            registration = _normalize_name(
                self.retrieval_cache.storage_adapter,
                kind="storage adapter",
            )
            if registration not in adapters:
                raise ValueError(
                    "retrieval cache references an unknown storage adapter: "
                    + registration
                )
            object.__setattr__(
                self,
                "retrieval_cache",
                RetrievalCacheConfig(storage_adapter=registration),
            )

        selected_adapter_names = {
            store.storage_adapter for store in stores.values()
        }
        if self.retrieval_cache is not None:
            selected_adapter_names.add(self.retrieval_cache.storage_adapter)
        non_development = sorted(
            name
            for name in selected_adapter_names
            if not _is_development_adapter(adapters[name])
        )
        if self.archive_passphrase == DEV_ARCHIVE_PASSPHRASE and non_development:
            raise ValueError(
                "RIVERHOG_ARCHIVE_PASSPHRASE must be explicitly set to a "
                "non-development secret for storage adapter(s): "
                + ", ".join(non_development)
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

    def storage_adapter(self, name: str) -> StorageAdapterRegistration:
        normalized = _normalize_name(name, kind="storage adapter")
        try:
            return self.storage_adapters[normalized]
        except KeyError as exc:
            raise ValueError(f"storage adapter is not configured: {normalized}") from exc

    def archive_store(self, name: str) -> ArchiveStoreConfig:
        normalized = _normalize_name(name, kind="archive store")
        try:
            return self.archive_stores[normalized]
        except KeyError as exc:
            raise ValueError(f"archive store is not configured: {normalized}") from exc


def _parse_storage_adapters(
    values: Mapping[str, str],
) -> dict[str, StorageAdapterRegistration]:
    names = tuple(
        dict.fromkeys(
            _normalize_name(raw, kind="storage adapter")
            for raw in values.get("RIVERHOG_STORAGE_ADAPTERS", "archive").split(",")
            if raw.strip()
        )
    )
    if not names:
        raise ValueError("RIVERHOG_STORAGE_ADAPTERS must configure at least one adapter")
    registrations: dict[str, StorageAdapterRegistration] = {}
    for name in names:
        prefix = f"RIVERHOG_STORAGE_ADAPTER_{_env_suffix(name)}_"
        endpoint = values.get(f"{prefix}ENDPOINT_URL", "http://127.0.0.1:8081").rstrip("/")
        endpoint_host = (urlsplit(endpoint).hostname or "").casefold()
        insecure_default = "true" if endpoint_host in DEV_STORAGE_ADAPTER_HOSTS else "false"
        implementation_default = (
            "riverhog.garage-storage-adapter/v1"
            if endpoint_host in DEV_STORAGE_ADAPTER_HOSTS
            else ""
        )
        registrations[name] = StorageAdapterRegistration(
            name=name,
            endpoint_url=endpoint,
            token_file=Path(
                values.get(
                    f"{prefix}TOKEN_FILE",
                    "/run/secrets/riverhog-storage-adapter-token",
                )
            ),
            allow_insecure_http=_parse_bool(
                values.get(f"{prefix}ALLOW_INSECURE_HTTP", insecure_default)
            ),
            expected_profile_id=values.get(
                f"{prefix}EXPECTED_PROFILE_ID",
                DEV_STORAGE_PROFILE.profile_id,
            ).strip(),
            expected_profile_read_mode=values.get(
                f"{prefix}EXPECTED_PROFILE_READ_MODE",
                DEV_STORAGE_PROFILE.read_mode,
            ).strip(),
            expected_egress_accounting_id=values.get(
                f"{prefix}EXPECTED_EGRESS_ACCOUNTING_ID",
                DEV_STORAGE_PROFILE.egress_accounting_id,
            ).strip(),
            expected_profile_contract_sha256=values.get(
                f"{prefix}EXPECTED_PROFILE_CONTRACT_SHA256",
                DEV_STORAGE_PROFILE.profile_contract_sha256,
            ).strip(),
            expected_implementation_id=(
                values.get(
                    f"{prefix}EXPECTED_IMPLEMENTATION_ID",
                    implementation_default,
                ).strip()
                or None
            ),
        )
    return registrations


def _parse_archive_stores(
    values: Mapping[str, str],
) -> tuple[str, tuple[str, ...], dict[str, ArchiveStoreConfig]]:
    names = tuple(
        dict.fromkeys(
            _normalize_name(raw, kind="archive store")
            for raw in values.get("RIVERHOG_ARCHIVE_STORES", "archive").split(",")
            if raw.strip()
        )
    )
    if not names:
        raise ValueError("RIVERHOG_ARCHIVE_STORES must configure at least one store")
    write_store = _normalize_name(
        values.get("RIVERHOG_ARCHIVE_WRITE_STORE", names[0]),
        kind="archive store",
    )
    stores: dict[str, ArchiveStoreConfig] = {}
    for name in names:
        prefix = f"RIVERHOG_ARCHIVE_STORE_{_env_suffix(name)}_"
        allowance = values.get(
            f"{prefix}MONTHLY_DOWNLOAD_ALLOWANCE_BYTES",
            "",
        ).strip()
        buffer = values.get(
            f"{prefix}DOWNLOAD_SAFETY_BUFFER_BYTES",
            "",
        ).strip()
        stores[name] = ArchiveStoreConfig(
            name=name,
            storage_adapter=values.get(
                f"{prefix}STORAGE_ADAPTER",
                name,
            ).strip(),
            monthly_download_allowance_bytes=(
                _parse_bytes(
                    allowance,
                    name=f"{prefix}MONTHLY_DOWNLOAD_ALLOWANCE_BYTES",
                    minimum=1,
                )
                if allowance
                else None
            ),
            download_safety_buffer_bytes=(
                _parse_bytes(
                    buffer,
                    name=f"{prefix}DOWNLOAD_SAFETY_BUFFER_BYTES",
                )
                if buffer
                else 0
            ),
        )
    if write_store not in stores:
        raise ValueError(
            f"RIVERHOG_ARCHIVE_WRITE_STORE is not listed in RIVERHOG_ARCHIVE_STORES: "
            f"{write_store}"
        )
    read_order = tuple(
        dict.fromkeys(
            _normalize_name(raw, kind="archive store")
            for raw in values.get(
                "RIVERHOG_ARCHIVE_READ_ORDER",
                ",".join(names),
            ).split(",")
            if raw.strip()
        )
    )
    return write_store, read_order, stores


def load_runtime_config() -> RuntimeConfig:
    database_url = os.getenv("RIVERHOG_DATABASE_URL", "").strip() or DEFAULT_DATABASE_URL
    if _database_url_driver(database_url) != "postgresql":
        raise ValueError("RIVERHOG_DATABASE_URL must use postgresql")
    storage_adapters = _parse_storage_adapters(os.environ)
    archive_write_store, archive_read_order, archive_stores = _parse_archive_stores(
        os.environ
    )
    cache_registration = os.getenv(
        "RIVERHOG_RETRIEVAL_CACHE_STORAGE_ADAPTER",
        "",
    ).strip()
    archive_passphrase_supplied = "RIVERHOG_ARCHIVE_PASSPHRASE" in os.environ
    archive_passphrase = (
        os.getenv("RIVERHOG_ARCHIVE_PASSPHRASE", DEV_ARCHIVE_PASSPHRASE).strip()
        or DEV_ARCHIVE_PASSPHRASE
    )
    require_passphrase = _parse_bool(
        os.getenv("RIVERHOG_ARCHIVE_REQUIRE_EXPLICIT_PASSPHRASE", "false")
    )
    if require_passphrase and (
        not archive_passphrase_supplied or archive_passphrase == DEV_ARCHIVE_PASSPHRASE
    ):
        raise ValueError(
            "RIVERHOG_ARCHIVE_REQUIRE_EXPLICIT_PASSPHRASE requires "
            "RIVERHOG_ARCHIVE_PASSPHRASE to be explicitly set to a non-development secret"
        )
    work_factor = _parse_int(
        os.getenv(
            "RIVERHOG_ARCHIVE_SCRYPT_WORK_FACTOR",
            str(DEFAULT_ARCHIVE_SCRYPT_WORK_FACTOR),
        ),
        name="RIVERHOG_ARCHIVE_SCRYPT_WORK_FACTOR",
        minimum=1,
    )
    if work_factor > 22:
        raise ValueError("RIVERHOG_ARCHIVE_SCRYPT_WORK_FACTOR must be <= 22")
    secret_key = os.getenv("RIVERHOG_ATTESTATION_SECRET_KEY_FILE", "").strip()
    public_key = os.getenv("RIVERHOG_ATTESTATION_PUBLIC_KEY_FILE", "").strip()
    return RuntimeConfig(
        event_source=os.getenv("RIVERHOG_EVENT_SOURCE", "urn:riverhog").strip(),
        event_context_retention=parse_duration(
            os.getenv("RIVERHOG_EVENT_CONTEXT_RETENTION", "30d")
        ),
        storage_adapter_max_connections=_parse_int(
            os.getenv(
                "RIVERHOG_STORAGE_ADAPTER_MAX_CONNECTIONS",
                str(DEFAULT_STORAGE_ADAPTER_MAX_CONNECTIONS),
            ),
            name="RIVERHOG_STORAGE_ADAPTER_MAX_CONNECTIONS",
            minimum=1,
        ),
        database_url=database_url,
        log_level=os.getenv("RIVERHOG_LOG_LEVEL", DEFAULT_LOG_LEVEL).strip()
        or DEFAULT_LOG_LEVEL,
        storage_adapters=storage_adapters,
        archive_write_store=archive_write_store,
        archive_read_order=archive_read_order,
        archive_stores=archive_stores,
        archive_multipart_part_bytes=_parse_bytes(
            os.getenv("RIVERHOG_ARCHIVE_MULTIPART_PART_BYTES", "64MiB"),
            name="RIVERHOG_ARCHIVE_MULTIPART_PART_BYTES",
            minimum=1,
        ),
        archive_multipart_max_age=parse_duration(
            os.getenv("RIVERHOG_ARCHIVE_MULTIPART_MAX_AGE", "72h")
        ),
        archive_multipart_sweep_interval=parse_duration(
            os.getenv("RIVERHOG_ARCHIVE_MULTIPART_SWEEP_INTERVAL", "6h")
        ),
        retrieval_cache=(
            RetrievalCacheConfig(storage_adapter=cache_registration)
            if cache_registration
            else None
        ),
        retrieval_cache_new_archive_enabled=_parse_bool(
            os.getenv("RIVERHOG_RETRIEVAL_CACHE_NEW_ARCHIVE_ENABLED", "true")
        ),
        retrieval_cache_new_archive_lease=parse_duration(
            os.getenv("RIVERHOG_RETRIEVAL_CACHE_NEW_ARCHIVE_LEASE", "72h")
        ),
        retrieval_default_lease=parse_duration(
            os.getenv("RIVERHOG_RETRIEVAL_DEFAULT_LEASE", "24h")
        ),
        retrieval_max_lease=parse_duration(
            os.getenv("RIVERHOG_RETRIEVAL_MAX_LEASE", "7d")
        ),
        retrieval_pending_timeout=parse_duration(
            os.getenv("RIVERHOG_RETRIEVAL_PENDING_TIMEOUT", "72h")
        ),
        retrieval_cache_sweep_interval=parse_duration(
            os.getenv("RIVERHOG_RETRIEVAL_CACHE_SWEEP_INTERVAL", "5m")
        ),
        archive_passphrase=archive_passphrase,
        archive_require_explicit_passphrase=require_passphrase,
        archive_scrypt_work_factor=work_factor,
        archive_upload_sweep_interval=parse_duration(
            os.getenv("RIVERHOG_ARCHIVE_UPLOAD_SWEEP_INTERVAL", "30s")
        ),
        retrieval_restore_poll_interval=parse_duration(
            os.getenv("RIVERHOG_RETRIEVAL_RESTORE_POLL_INTERVAL", "5m")
        ),
        ots_stamp_command=_parse_command(
            os.getenv("RIVERHOG_OTS_STAMP_COMMAND", "ots"),
            name="RIVERHOG_OTS_STAMP_COMMAND",
        ),
        ots_verify_command=_parse_command(
            os.getenv(
                "RIVERHOG_OTS_VERIFY_COMMAND",
                "ots --no-bitcoin --no-default-whitelist",
            ),
            name="RIVERHOG_OTS_VERIFY_COMMAND",
        ),
        ots_upgrade_command=_parse_command(
            os.getenv("RIVERHOG_OTS_UPGRADE_COMMAND", "ots"),
            name="RIVERHOG_OTS_UPGRADE_COMMAND",
        ),
        attestation_secret_key_file=Path(secret_key) if secret_key else None,
        attestation_public_key_file=Path(public_key) if public_key else None,
        proof_maturation_retry_delay=parse_duration(
            os.getenv("RIVERHOG_PROOF_MATURATION_RETRY_DELAY", "6h")
        ),
        proof_maturation_sweep_interval=parse_duration(
            os.getenv("RIVERHOG_PROOF_MATURATION_SWEEP_INTERVAL", "1h")
        ),
        public_base_url=os.getenv("RIVERHOG_PUBLIC_BASE_URL", "").strip() or None,
    )
