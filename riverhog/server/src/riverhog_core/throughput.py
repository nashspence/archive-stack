from __future__ import annotations

import hashlib
import logging
import re
import threading
import time
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Literal

DEFAULT_UPLOAD_PREPARE_CONCURRENCY = 8
DEFAULT_S3_PART_CONCURRENCY = 4
DEFAULT_S3_UPLOAD_REQUEST_CONCURRENCY = 4
DEFAULT_S3_MAX_POOL_CONNECTIONS = 32
DEFAULT_UPLOAD_MAX_INFLIGHT_BYTES = 1280 * 1024 * 1024
DEFAULT_RETRIEVAL_REQUEST_CONCURRENCY = 8
DEFAULT_RETRIEVAL_MAX_INFLIGHT_BYTES = 1024 * 1024 * 1024
DEFAULT_RETRIEVAL_READ_CHUNK_BYTES = 8 * 1024 * 1024
DEFAULT_SOURCE_READ_CHUNK_BYTES = 8 * 1024 * 1024
DEFAULT_AGE_SESSION_CACHE_ENTRIES = 128
DEFAULT_AGE_DERIVATION_CONCURRENCY = 4
MAX_WORKER_CONCURRENCY = 128
_BYTES_RE = re.compile(r"^(\d+(?:_\d+)*)([kmgt]i?b?|b)?$", re.IGNORECASE)
_TRANSFER_LOG = logging.getLogger("riverhog.transfer")


@dataclass(frozen=True, slots=True)
class S3TransportTuning:
    max_pool_connections: int = DEFAULT_S3_MAX_POOL_CONNECTIONS
    connect_timeout_seconds: float = 10.0
    read_timeout_seconds: float = 300.0
    max_attempts: int = 8
    retry_mode: Literal["standard", "adaptive"] = "standard"
    tcp_keepalive: bool = True

    def __post_init__(self) -> None:
        _bounded_int(
            self.max_pool_connections,
            name="S3 maximum pool connections",
            minimum=1,
            maximum=4096,
        )
        if self.connect_timeout_seconds <= 0 or self.read_timeout_seconds <= 0:
            raise ValueError("S3 timeouts must be positive")
        _bounded_int(
            self.max_attempts,
            name="S3 maximum attempts",
            minimum=1,
            maximum=100,
        )
        if self.retry_mode not in {"standard", "adaptive"}:
            raise ValueError("S3 retry mode must be standard or adaptive")

    @classmethod
    def from_env(
        cls,
        values: Mapping[str, str],
        *,
        default_pool_connections: int = DEFAULT_S3_MAX_POOL_CONNECTIONS,
        store_name: str | None = None,
    ) -> S3TransportTuning:
        return cls(
            max_pool_connections=_scoped_env_int(
                values,
                "RIVERHOG_S3_MAX_POOL_CONNECTIONS",
                default_pool_connections,
                store_name=store_name,
            ),
            connect_timeout_seconds=_scoped_env_float(
                values,
                "RIVERHOG_S3_CONNECT_TIMEOUT_SECONDS",
                10.0,
                store_name=store_name,
            ),
            read_timeout_seconds=_scoped_env_float(
                values,
                "RIVERHOG_S3_READ_TIMEOUT_SECONDS",
                300.0,
                store_name=store_name,
            ),
            max_attempts=_scoped_env_int(
                values,
                "RIVERHOG_S3_MAX_ATTEMPTS",
                8,
                store_name=store_name,
            ),
            retry_mode=_s3_retry_mode(
                _scoped_env_value(
                    values,
                    "RIVERHOG_S3_RETRY_MODE",
                    "standard",
                    store_name=store_name,
                )
            ),
            tcp_keepalive=_scoped_env_bool(
                values,
                "RIVERHOG_S3_TCP_KEEPALIVE",
                True,
                store_name=store_name,
            ),
        )


@dataclass(frozen=True, slots=True)
class ArchiveThroughputTuning:
    """Runtime-only transfer controls.

    Layout-affecting values such as pack size and multipart plaintext size are intentionally
    not included here: those values belong in the immutable collection plan. Every field in
    this class can be changed between collections without changing archive semantics.
    """

    upload_prepare_concurrency: int = DEFAULT_UPLOAD_PREPARE_CONCURRENCY
    s3_part_concurrency: int = DEFAULT_S3_PART_CONCURRENCY
    s3_upload_request_concurrency: int = DEFAULT_S3_UPLOAD_REQUEST_CONCURRENCY
    upload_max_inflight_bytes: int = DEFAULT_UPLOAD_MAX_INFLIGHT_BYTES
    source_read_chunk_bytes: int = DEFAULT_SOURCE_READ_CHUNK_BYTES
    retrieval_request_concurrency: int = DEFAULT_RETRIEVAL_REQUEST_CONCURRENCY
    retrieval_max_inflight_bytes: int = DEFAULT_RETRIEVAL_MAX_INFLIGHT_BYTES
    retrieval_read_chunk_bytes: int = DEFAULT_RETRIEVAL_READ_CHUNK_BYTES
    age_session_cache_entries: int = DEFAULT_AGE_SESSION_CACHE_ENTRIES
    age_derivation_concurrency: int = DEFAULT_AGE_DERIVATION_CONCURRENCY

    def __post_init__(self) -> None:
        _bounded_int(
            self.upload_prepare_concurrency,
            name="archive preparation concurrency",
            minimum=1,
            maximum=MAX_WORKER_CONCURRENCY,
        )
        _bounded_int(
            self.s3_part_concurrency,
            name="S3 part concurrency",
            minimum=1,
            maximum=MAX_WORKER_CONCURRENCY,
        )
        _bounded_int(
            self.s3_upload_request_concurrency,
            name="S3 upload request concurrency",
            minimum=1,
            maximum=MAX_WORKER_CONCURRENCY,
        )
        for name, value in (
            ("upload maximum in-flight bytes", self.upload_max_inflight_bytes),
            ("source read chunk bytes", self.source_read_chunk_bytes),
            ("retrieval maximum in-flight bytes", self.retrieval_max_inflight_bytes),
            ("retrieval read chunk bytes", self.retrieval_read_chunk_bytes),
        ):
            _bounded_int(value, name=name, minimum=64 * 1024, maximum=1 << 50)
        _bounded_int(
            self.retrieval_request_concurrency,
            name="retrieval request concurrency",
            minimum=1,
            maximum=MAX_WORKER_CONCURRENCY,
        )
        _bounded_int(
            self.age_session_cache_entries,
            name="age session cache entries",
            minimum=0,
            maximum=4096,
        )
        _bounded_int(
            self.age_derivation_concurrency,
            name="age derivation concurrency",
            minimum=1,
            maximum=MAX_WORKER_CONCURRENCY,
        )

    @classmethod
    def from_env(cls, values: Mapping[str, str]) -> ArchiveThroughputTuning:
        """Load the complete operator tuning surface from environment-like values."""

        return cls(
            upload_prepare_concurrency=_env_int(
                values,
                "RIVERHOG_ARCHIVE_PREPARE_CONCURRENCY",
                DEFAULT_UPLOAD_PREPARE_CONCURRENCY,
            ),
            s3_part_concurrency=_env_int(
                values,
                "RIVERHOG_ARCHIVE_MULTIPART_CONCURRENCY",
                DEFAULT_S3_PART_CONCURRENCY,
            ),
            s3_upload_request_concurrency=_env_int(
                values,
                "RIVERHOG_ARCHIVE_UPLOAD_REQUEST_CONCURRENCY",
                DEFAULT_S3_UPLOAD_REQUEST_CONCURRENCY,
            ),
            upload_max_inflight_bytes=_env_bytes(
                values,
                "RIVERHOG_INGRESS_MAX_INFLIGHT_BYTES",
                DEFAULT_UPLOAD_MAX_INFLIGHT_BYTES,
            ),
            source_read_chunk_bytes=_env_bytes(
                values,
                "RIVERHOG_INGRESS_SOURCE_READ_CHUNK_BYTES",
                DEFAULT_SOURCE_READ_CHUNK_BYTES,
            ),
            retrieval_request_concurrency=_env_int(
                values,
                "RIVERHOG_RETRIEVAL_REQUEST_CONCURRENCY",
                DEFAULT_RETRIEVAL_REQUEST_CONCURRENCY,
            ),
            retrieval_max_inflight_bytes=_env_bytes(
                values,
                "RIVERHOG_RETRIEVAL_MAX_INFLIGHT_BYTES",
                DEFAULT_RETRIEVAL_MAX_INFLIGHT_BYTES,
            ),
            retrieval_read_chunk_bytes=_env_bytes(
                values,
                "RIVERHOG_RETRIEVAL_READ_CHUNK_BYTES",
                DEFAULT_RETRIEVAL_READ_CHUNK_BYTES,
            ),
            age_session_cache_entries=_env_int(
                values,
                "RIVERHOG_AGE_SESSION_CACHE_ENTRIES",
                DEFAULT_AGE_SESSION_CACHE_ENTRIES,
            ),
            age_derivation_concurrency=_env_int(
                values,
                "RIVERHOG_AGE_SESSION_DERIVATION_CONCURRENCY",
                DEFAULT_AGE_DERIVATION_CONCURRENCY,
            ),
        )


class TransferConcurrencyGate:
    """Process-local request gate shared across all collections and volumes."""

    def __init__(self, capacity: int) -> None:
        if capacity <= 0:
            raise ValueError("concurrency gate capacity must be positive")
        self._capacity = capacity
        self._semaphore = threading.BoundedSemaphore(capacity)

    @property
    def capacity(self) -> int:
        return self._capacity

    @contextmanager
    def reserve(self) -> Iterator[float]:
        started = time.perf_counter()
        self._semaphore.acquire()
        waited = time.perf_counter() - started
        try:
            yield waited
        finally:
            self._semaphore.release()


class WeightedByteSemaphore:
    """A fair-enough process-local byte budget for buffered transfer work.

    Part bodies are ciphertext, never plaintext-at-rest. The budget prevents the product of
    volume concurrency and part size from exhausting memory while still allowing operators
    with sufficient RAM to raise concurrency and fill high-bandwidth/high-latency links.
    """

    def __init__(self, capacity: int) -> None:
        if capacity <= 0:
            raise ValueError("byte semaphore capacity must be positive")
        self._capacity = capacity
        self._available = capacity
        self._condition = threading.Condition()

    @property
    def capacity(self) -> int:
        return self._capacity

    @property
    def available(self) -> int:
        with self._condition:
            return self._available

    def acquire(self, amount: int) -> float:
        if amount <= 0:
            raise ValueError("byte semaphore amount must be positive")
        if amount > self._capacity:
            raise ValueError(
                f"single transfer buffer ({amount} bytes) exceeds the configured "
                f"in-flight budget ({self._capacity} bytes)"
            )
        started = time.perf_counter()
        with self._condition:
            while self._available < amount:
                self._condition.wait()
            self._available -= amount
        return time.perf_counter() - started

    def try_acquire(self, amount: int) -> bool:
        if amount <= 0:
            raise ValueError("byte semaphore amount must be positive")
        if amount > self._capacity:
            raise ValueError(
                f"single transfer buffer ({amount} bytes) exceeds the configured "
                f"in-flight budget ({self._capacity} bytes)"
            )
        with self._condition:
            if self._available < amount:
                return False
            self._available -= amount
            return True

    def release(self, amount: int) -> None:
        if amount <= 0:
            raise ValueError("byte semaphore amount must be positive")
        with self._condition:
            self._available += amount
            if self._available > self._capacity:
                self._available -= amount
                raise RuntimeError("byte semaphore released more capacity than acquired")
            self._condition.notify_all()

    @contextmanager
    def reserve(self, amount: int) -> Iterator[float]:
        waited = self.acquire(amount)
        try:
            yield waited
        finally:
            self.release(amount)


@dataclass(slots=True)
class ArchiveTransferResources:
    """Shared process resources; construct once and inject into every reader/uploader."""

    upload_bytes: WeightedByteSemaphore
    retrieval_bytes: WeightedByteSemaphore
    upload_preparations: TransferConcurrencyGate
    upload_requests: TransferConcurrencyGate
    retrieval_requests: TransferConcurrencyGate
    age_derivations: TransferConcurrencyGate

    @classmethod
    def from_tuning(
        cls,
        tuning: ArchiveThroughputTuning,
    ) -> ArchiveTransferResources:
        return cls(
            upload_bytes=WeightedByteSemaphore(tuning.upload_max_inflight_bytes),
            retrieval_bytes=WeightedByteSemaphore(tuning.retrieval_max_inflight_bytes),
            upload_preparations=TransferConcurrencyGate(tuning.upload_prepare_concurrency),
            upload_requests=TransferConcurrencyGate(tuning.s3_upload_request_concurrency),
            retrieval_requests=TransferConcurrencyGate(tuning.retrieval_request_concurrency),
            age_derivations=TransferConcurrencyGate(tuning.age_derivation_concurrency),
        )


@dataclass(frozen=True, slots=True)
class TransferTiming:
    operation: str
    identity: str
    plaintext_bytes: int
    stored_bytes: int
    queue_wait_seconds: float
    source_seconds: float
    crypto_seconds: float
    remote_seconds: float
    checkpoint_seconds: float
    elapsed_seconds: float
    downstream_seconds: float = 0.0

    @property
    def plaintext_mib_per_second(self) -> float:
        if self.elapsed_seconds <= 0:
            return 0.0
        return self.plaintext_bytes / (1024 * 1024) / self.elapsed_seconds

    @property
    def remote_mib_per_second(self) -> float:
        if self.remote_seconds <= 0:
            return 0.0
        return self.stored_bytes / (1024 * 1024) / self.remote_seconds

    def likely_bottleneck(self) -> str:
        phases = {
            "queue": self.queue_wait_seconds,
            "source": self.source_seconds,
            "crypto": self.crypto_seconds,
            "remote": self.remote_seconds,
            "checkpoint": self.checkpoint_seconds,
            "downstream": self.downstream_seconds,
        }
        return max(phases, key=phases.__getitem__)


def log_transfer_timing(timing: TransferTiming) -> None:
    """Emit phase-separated, identity-safe evidence after one payload operation."""

    identity_sha256 = hashlib.sha256(timing.identity.encode()).hexdigest()
    _TRANSFER_LOG.info(
        "transfer operation=%s identity_sha256=%s plaintext_bytes=%d stored_bytes=%d "
        "queue_seconds=%.6f source_seconds=%.6f crypto_seconds=%.6f "
        "remote_seconds=%.6f checkpoint_seconds=%.6f downstream_seconds=%.6f "
        "elapsed_seconds=%.6f plaintext_mib_per_second=%.3f "
        "remote_mib_per_second=%.3f bottleneck=%s",
        timing.operation,
        identity_sha256,
        timing.plaintext_bytes,
        timing.stored_bytes,
        timing.queue_wait_seconds,
        timing.source_seconds,
        timing.crypto_seconds,
        timing.remote_seconds,
        timing.checkpoint_seconds,
        timing.downstream_seconds,
        timing.elapsed_seconds,
        timing.plaintext_mib_per_second,
        timing.remote_mib_per_second,
        timing.likely_bottleneck(),
    )


def _bounded_int(value: int, *, name: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or value < minimum or value > maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return value


def _store_scoped_env_name(global_name: str, store_name: str) -> str:
    normalized = store_name.strip().casefold()
    if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", normalized):
        raise ValueError("archive store name is invalid for a scoped tuning value")
    suffix = normalized.upper().replace("-", "_")
    setting = global_name.removeprefix("RIVERHOG_")
    return f"RIVERHOG_ARCHIVE_STORE_{suffix}_{setting}"


def _scoped_env_value(
    values: Mapping[str, str],
    global_name: str,
    default: str,
    *,
    store_name: str | None,
) -> str:
    if store_name is not None:
        scoped_name = _store_scoped_env_name(global_name, store_name)
        scoped = values.get(scoped_name)
        if scoped is not None and scoped.strip():
            return scoped
    global_value = values.get(global_name)
    return global_value if global_value is not None and global_value.strip() else default


def _scoped_env_int(
    values: Mapping[str, str],
    global_name: str,
    default: int,
    *,
    store_name: str | None,
) -> int:
    raw = _scoped_env_value(values, global_name, str(default), store_name=store_name)
    try:
        return int(raw.strip())
    except ValueError as exc:
        raise ValueError(f"{global_name} must be an integer") from exc


def _s3_retry_mode(value: str) -> Literal["standard", "adaptive"]:
    normalized = value.strip().casefold()
    if normalized == "standard":
        return "standard"
    if normalized == "adaptive":
        return "adaptive"
    raise ValueError("S3 retry mode must be standard or adaptive")


def _scoped_env_float(
    values: Mapping[str, str],
    global_name: str,
    default: float,
    *,
    store_name: str | None,
) -> float:
    raw = _scoped_env_value(values, global_name, str(default), store_name=store_name)
    try:
        return float(raw.strip())
    except ValueError as exc:
        raise ValueError(f"{global_name} must be a number") from exc


def _scoped_env_bool(
    values: Mapping[str, str],
    global_name: str,
    default: bool,
    *,
    store_name: str | None,
) -> bool:
    raw = _scoped_env_value(
        values,
        global_name,
        "true" if default else "false",
        store_name=store_name,
    )
    normalized = raw.strip().casefold()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{global_name} must be true or false")


def _env_float(values: Mapping[str, str], name: str, default: float) -> float:
    raw = values.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return float(raw.strip())
    except ValueError as exc:
        raise ValueError(f"{name} must be a number") from exc


def _env_bool(values: Mapping[str, str], name: str, default: bool) -> bool:
    raw = values.get(name)
    if raw is None or not raw.strip():
        return default
    normalized = raw.strip().casefold()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be true or false")


def _env_int(values: Mapping[str, str], name: str, default: int) -> int:
    raw = values.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw.strip())
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc


def _env_bytes(values: Mapping[str, str], name: str, default: int) -> int:
    raw = values.get(name)
    if raw is None or not raw.strip():
        return default
    candidate = raw.strip().replace(" ", "")
    match = _BYTES_RE.fullmatch(candidate)
    if match is None:
        raise ValueError(f"{name} must be a byte size such as 512MiB")
    amount = int(match.group(1).replace("_", ""))
    unit = (match.group(2) or "b").casefold()
    scale = {
        "b": 1,
        "k": 1000,
        "kb": 1000,
        "m": 1000**2,
        "mb": 1000**2,
        "g": 1000**3,
        "gb": 1000**3,
        "t": 1000**4,
        "tb": 1000**4,
        "kib": 1024,
        "mib": 1024**2,
        "gib": 1024**3,
        "tib": 1024**4,
    }[unit]
    return amount * scale
