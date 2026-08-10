from __future__ import annotations

import re
import threading
import time
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Literal

DEFAULT_CLIENT_UPLOAD_CONCURRENCY = 8
DEFAULT_CLIENT_UPLOAD_WINDOW = 16
DEFAULT_CLIENT_UPLOAD_CHUNK_BYTES = 50 * 1024 * 1024
DEFAULT_CLIENT_DOWNLOAD_CONCURRENCY = 4
DEFAULT_CLIENT_DOWNLOAD_WINDOW = 8
DEFAULT_CLIENT_DOWNLOAD_CHUNK_BYTES = 8 * 1024 * 1024
DEFAULT_INGRESS_VOLUME_CONCURRENCY = 8
DEFAULT_INGRESS_VOLUME_WINDOW = 16
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
DEFAULT_S3_CONTROL_CONNECTION_MARGIN = 8
MAX_CLIENT_UPLOAD_CONCURRENCY = 64
MAX_CLIENT_UPLOAD_WINDOW = 256
MAX_WORKER_CONCURRENCY = 128
RawVerificationMode = Literal["part_manifest", "remote_reread"]
RAW_VERIFICATION_PART_MANIFEST: RawVerificationMode = "part_manifest"
RAW_VERIFICATION_REMOTE_REREAD: RawVerificationMode = "remote_reread"
_BYTES_RE = re.compile(r"^(\d+(?:_\d+)*)([kmgt]i?b?|b)?$", re.IGNORECASE)


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

    client_upload_concurrency: int = DEFAULT_CLIENT_UPLOAD_CONCURRENCY
    client_upload_window: int = DEFAULT_CLIENT_UPLOAD_WINDOW
    client_upload_chunk_bytes: int = DEFAULT_CLIENT_UPLOAD_CHUNK_BYTES
    client_download_concurrency: int = DEFAULT_CLIENT_DOWNLOAD_CONCURRENCY
    client_download_window: int = DEFAULT_CLIENT_DOWNLOAD_WINDOW
    client_download_chunk_bytes: int = DEFAULT_CLIENT_DOWNLOAD_CHUNK_BYTES
    ingress_volume_concurrency: int = DEFAULT_INGRESS_VOLUME_CONCURRENCY
    ingress_volume_window: int = DEFAULT_INGRESS_VOLUME_WINDOW
    upload_prepare_concurrency: int = DEFAULT_UPLOAD_PREPARE_CONCURRENCY
    s3_part_concurrency: int = DEFAULT_S3_PART_CONCURRENCY
    s3_upload_request_concurrency: int = DEFAULT_S3_UPLOAD_REQUEST_CONCURRENCY
    s3_max_pool_connections: int = DEFAULT_S3_MAX_POOL_CONNECTIONS
    upload_max_inflight_bytes: int = DEFAULT_UPLOAD_MAX_INFLIGHT_BYTES
    source_read_chunk_bytes: int = DEFAULT_SOURCE_READ_CHUNK_BYTES
    retrieval_request_concurrency: int = DEFAULT_RETRIEVAL_REQUEST_CONCURRENCY
    retrieval_max_inflight_bytes: int = DEFAULT_RETRIEVAL_MAX_INFLIGHT_BYTES
    retrieval_read_chunk_bytes: int = DEFAULT_RETRIEVAL_READ_CHUNK_BYTES
    raw_verification_mode: RawVerificationMode = RAW_VERIFICATION_PART_MANIFEST
    age_session_cache_entries: int = DEFAULT_AGE_SESSION_CACHE_ENTRIES
    age_derivation_concurrency: int = DEFAULT_AGE_DERIVATION_CONCURRENCY

    def __post_init__(self) -> None:
        _bounded_int(
            self.client_upload_concurrency,
            name="client upload concurrency",
            minimum=1,
            maximum=MAX_CLIENT_UPLOAD_CONCURRENCY,
        )
        _bounded_int(
            self.client_upload_window,
            name="client upload window",
            minimum=self.client_upload_concurrency,
            maximum=MAX_CLIENT_UPLOAD_WINDOW,
        )
        _bounded_int(
            self.client_download_concurrency,
            name="client download concurrency",
            minimum=1,
            maximum=MAX_CLIENT_UPLOAD_CONCURRENCY,
        )
        _bounded_int(
            self.client_download_window,
            name="client download window",
            minimum=self.client_download_concurrency,
            maximum=MAX_CLIENT_UPLOAD_WINDOW,
        )
        _bounded_int(
            self.ingress_volume_concurrency,
            name="ingress volume concurrency",
            minimum=1,
            maximum=MAX_WORKER_CONCURRENCY,
        )
        _bounded_int(
            self.ingress_volume_window,
            name="ingress volume window",
            minimum=self.ingress_volume_concurrency,
            maximum=512,
        )
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
        _bounded_int(
            self.s3_max_pool_connections,
            name="S3 maximum pool connections",
            minimum=1,
            maximum=4096,
        )
        for name, value in (
            ("client upload chunk bytes", self.client_upload_chunk_bytes),
            ("client download chunk bytes", self.client_download_chunk_bytes),
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
        if self.raw_verification_mode not in {
            RAW_VERIFICATION_PART_MANIFEST,
            RAW_VERIFICATION_REMOTE_REREAD,
        }:
            raise ValueError("raw verification mode is invalid")

    @property
    def recommended_s3_pool_connections(self) -> int:
        """Connections needed to avoid the botocore pool throttling configured workers."""

        return (
            self.s3_upload_request_concurrency
            + self.retrieval_request_concurrency
            + DEFAULT_S3_CONTROL_CONNECTION_MARGIN
        )

    @property
    def s3_pool_is_likely_constraining(self) -> bool:
        return self.s3_max_pool_connections < self.recommended_s3_pool_connections

    def assess_capacity(
        self,
        *,
        upload_part_stored_bytes: int,
        retrieval_request_stored_bytes: int,
        retrieval_request_plaintext_bytes: int | None = None,
        upload_parts_per_volume: int | None = None,
        active_upload_volumes: int | None = None,
        active_client_uploads: int | None = None,
        active_retrieval_requests: int | None = None,
        active_client_downloads: int | None = None,
        control_connection_margin: int = DEFAULT_S3_CONTROL_CONNECTION_MARGIN,
    ) -> ArchiveTransferCapacity:
        """Calculate the effective request parallelism before starting a transfer.

        The result is deliberately conservative: it assumes uploads and retrievals may be
        active together and reserves pool connections for control traffic. This makes a
        mis-sized botocore pool or byte budget visible instead of allowing silent queuing.
        """

        if upload_part_stored_bytes < 1 or retrieval_request_stored_bytes < 1:
            raise ValueError("capacity assessment transfer sizes must be positive")
        if retrieval_request_plaintext_bytes is None:
            retrieval_request_plaintext_bytes = retrieval_request_stored_bytes
        if retrieval_request_plaintext_bytes < 0:
            raise ValueError("retrieval plaintext bytes must be non-negative")
        if control_connection_margin < 0:
            raise ValueError("S3 control connection margin must be non-negative")
        if upload_parts_per_volume is None:
            upload_parts_per_volume = self.s3_part_concurrency
        if upload_parts_per_volume < 1 or upload_parts_per_volume > self.s3_part_concurrency:
            raise ValueError("upload parts per volume must be between 1 and part concurrency")
        active_upload_volumes = _active_slots(
            active_upload_volumes,
            configured=self.ingress_volume_concurrency,
            name="active upload volumes",
        )
        active_client_uploads = _active_slots(
            active_client_uploads,
            configured=self.client_upload_concurrency,
            name="active client uploads",
        )
        active_retrieval_requests = _active_slots(
            active_retrieval_requests,
            configured=self.retrieval_request_concurrency,
            name="active retrieval requests",
        )
        active_client_downloads = _active_slots(
            active_client_downloads,
            configured=self.client_download_concurrency,
            name="active client downloads",
        )

        upload_workers = active_upload_volumes * upload_parts_per_volume
        upload_prepare_slots = min(
            upload_workers,
            self.upload_prepare_concurrency,
        )
        upload_working_bytes = upload_part_stored_bytes + self.source_read_chunk_bytes + 64 * 1024
        retrieval_working_bytes = retrieval_request_plaintext_bytes + min(
            retrieval_request_stored_bytes,
            self.retrieval_read_chunk_bytes,
        )
        upload_memory_slots = self.upload_max_inflight_bytes // upload_working_bytes
        retrieval_memory_slots = self.retrieval_max_inflight_bytes // max(
            1, retrieval_working_bytes
        )
        upload_pool_slots = max(
            0,
            self.s3_max_pool_connections - active_retrieval_requests - control_connection_margin,
        )
        retrieval_pool_slots = max(
            0,
            self.s3_max_pool_connections
            - min(self.s3_upload_request_concurrency, upload_workers)
            - control_connection_margin,
        )
        effective_prepare = min(
            upload_prepare_slots,
            upload_memory_slots,
        )
        effective_upload = min(
            upload_workers,
            upload_memory_slots,
            self.s3_upload_request_concurrency,
            upload_pool_slots,
        )
        effective_retrieval = min(
            active_retrieval_requests,
            retrieval_memory_slots,
            retrieval_pool_slots,
        )
        warnings: list[str] = []
        if effective_prepare < min(
            upload_workers,
            self.s3_upload_request_concurrency,
        ):
            warnings.append(
                "archive preparation concurrency or upload byte budget may not feed the "
                "configured final-store request concurrency"
            )
        if upload_memory_slots < upload_workers:
            warnings.append(
                "upload byte budget permits fewer in-flight parts than the configured "
                "volume × part workers"
            )
        if upload_pool_slots < self.s3_upload_request_concurrency:
            warnings.append(
                "S3 connection pool permits fewer upload requests after retrieval/control "
                "reservations than the configured upload request gate"
            )
        if retrieval_memory_slots < active_retrieval_requests:
            warnings.append(
                "retrieval byte budget permits fewer in-flight ranges than the configured "
                "request workers"
            )
        if retrieval_pool_slots < active_retrieval_requests:
            warnings.append(
                "S3 connection pool permits fewer retrieval requests after upload/control "
                "reservations than the configured retrieval workers"
            )
        upload_buffer_slots = max(
            0,
            min(upload_workers, upload_memory_slots) - effective_upload,
        )
        pipeline_overlap_possible = (
            effective_upload > 0
            and active_client_uploads > effective_upload
            and upload_buffer_slots > 0
        )
        if not pipeline_overlap_possible:
            warnings.append(
                "client/server preparation capacity does not exceed final-store request "
                "capacity; the client-to-server and server-to-S3 legs may phase-lock "
                "instead of overlapping"
            )
        if self.age_session_cache_entries < self.ingress_volume_concurrency:
            warnings.append(
                "age session cache is smaller than volume concurrency and may repeat scrypt "
                "derivations under object churn"
            )
        return ArchiveTransferCapacity(
            client_upload_slots=active_client_uploads,
            upload_worker_slots=upload_workers,
            upload_prepare_slots=upload_prepare_slots,
            effective_prepare_slots=effective_prepare,
            upload_memory_slots=upload_memory_slots,
            upload_request_slots=self.s3_upload_request_concurrency,
            upload_pool_slots=upload_pool_slots,
            upload_buffer_slots=upload_buffer_slots,
            effective_upload_slots=effective_upload,
            pipeline_overlap_possible=pipeline_overlap_possible,
            end_to_end_upload_slots=min(
                active_client_uploads,
                effective_upload,
            ),
            client_download_slots=active_client_downloads,
            retrieval_worker_slots=active_retrieval_requests,
            retrieval_memory_slots=retrieval_memory_slots,
            retrieval_pool_slots=retrieval_pool_slots,
            effective_retrieval_slots=effective_retrieval,
            end_to_end_retrieval_slots=min(
                active_client_downloads,
                effective_retrieval,
            ),
            upload_working_bytes_per_request=upload_working_bytes,
            retrieval_working_bytes_per_request=retrieval_working_bytes,
            warnings=tuple(warnings),
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "client_upload_concurrency": self.client_upload_concurrency,
            "client_upload_window": self.client_upload_window,
            "client_upload_chunk_bytes": self.client_upload_chunk_bytes,
            "client_download_concurrency": self.client_download_concurrency,
            "client_download_window": self.client_download_window,
            "client_download_chunk_bytes": self.client_download_chunk_bytes,
            "ingress_volume_concurrency": self.ingress_volume_concurrency,
            "ingress_volume_window": self.ingress_volume_window,
            "upload_prepare_concurrency": self.upload_prepare_concurrency,
            "s3_part_concurrency": self.s3_part_concurrency,
            "s3_upload_request_concurrency": self.s3_upload_request_concurrency,
            "s3_max_pool_connections": self.s3_max_pool_connections,
            "upload_max_inflight_bytes": self.upload_max_inflight_bytes,
            "source_read_chunk_bytes": self.source_read_chunk_bytes,
            "retrieval_request_concurrency": self.retrieval_request_concurrency,
            "retrieval_max_inflight_bytes": self.retrieval_max_inflight_bytes,
            "retrieval_read_chunk_bytes": self.retrieval_read_chunk_bytes,
            "raw_verification_mode": self.raw_verification_mode,
            "age_session_cache_entries": self.age_session_cache_entries,
            "age_derivation_concurrency": self.age_derivation_concurrency,
            "recommended_s3_pool_connections": self.recommended_s3_pool_connections,
        }

    @classmethod
    def from_env(cls, values: Mapping[str, str]) -> ArchiveThroughputTuning:
        """Load the complete operator tuning surface from environment-like values."""

        client_concurrency = _env_int(
            values,
            "RIVERHOG_UPLOAD_FILE_CONCURRENCY",
            DEFAULT_CLIENT_UPLOAD_CONCURRENCY,
        )
        volume_concurrency = _env_int(
            values,
            "RIVERHOG_INGRESS_VOLUME_CONCURRENCY",
            DEFAULT_INGRESS_VOLUME_CONCURRENCY,
        )
        download_concurrency = _env_int(
            values,
            "RIVERHOG_DOWNLOAD_FILE_CONCURRENCY",
            DEFAULT_CLIENT_DOWNLOAD_CONCURRENCY,
        )
        return cls(
            client_upload_concurrency=client_concurrency,
            client_upload_window=_env_int(
                values,
                "RIVERHOG_UPLOAD_FILE_WINDOW",
                min(client_concurrency * 2, MAX_CLIENT_UPLOAD_WINDOW),
            ),
            client_upload_chunk_bytes=_env_bytes(
                values,
                "RIVERHOG_UPLOAD_CHUNK_BYTES",
                DEFAULT_CLIENT_UPLOAD_CHUNK_BYTES,
            ),
            client_download_concurrency=download_concurrency,
            client_download_window=_env_int(
                values,
                "RIVERHOG_DOWNLOAD_FILE_WINDOW",
                min(download_concurrency * 2, MAX_CLIENT_UPLOAD_WINDOW),
            ),
            client_download_chunk_bytes=_env_bytes(
                values,
                "RIVERHOG_DOWNLOAD_CHUNK_BYTES",
                DEFAULT_CLIENT_DOWNLOAD_CHUNK_BYTES,
            ),
            ingress_volume_concurrency=volume_concurrency,
            ingress_volume_window=_env_int(
                values,
                "RIVERHOG_INGRESS_VOLUME_WINDOW",
                min(volume_concurrency * 2, 512),
            ),
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
            s3_max_pool_connections=_env_int(
                values,
                "RIVERHOG_S3_MAX_POOL_CONNECTIONS",
                DEFAULT_S3_MAX_POOL_CONNECTIONS,
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
            raw_verification_mode=_raw_verification_mode(
                values.get(
                    "RIVERHOG_RAW_VERIFICATION_MODE",
                    RAW_VERIFICATION_PART_MANIFEST,
                )
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


@dataclass(frozen=True, slots=True)
class ArchiveTransferCapacity:
    client_upload_slots: int
    upload_worker_slots: int
    upload_prepare_slots: int
    effective_prepare_slots: int
    upload_memory_slots: int
    upload_request_slots: int
    upload_pool_slots: int
    upload_buffer_slots: int
    effective_upload_slots: int
    pipeline_overlap_possible: bool
    end_to_end_upload_slots: int
    client_download_slots: int
    retrieval_worker_slots: int
    retrieval_memory_slots: int
    retrieval_pool_slots: int
    effective_retrieval_slots: int
    end_to_end_retrieval_slots: int
    upload_working_bytes_per_request: int
    retrieval_working_bytes_per_request: int
    warnings: tuple[str, ...]

    @property
    def constrained(self) -> bool:
        return bool(self.warnings)

    def as_dict(self) -> dict[str, object]:
        return {
            "client_upload_slots": self.client_upload_slots,
            "upload_worker_slots": self.upload_worker_slots,
            "upload_prepare_slots": self.upload_prepare_slots,
            "effective_prepare_slots": self.effective_prepare_slots,
            "upload_memory_slots": self.upload_memory_slots,
            "upload_request_slots": self.upload_request_slots,
            "upload_pool_slots": self.upload_pool_slots,
            "upload_buffer_slots": self.upload_buffer_slots,
            "effective_upload_slots": self.effective_upload_slots,
            "pipeline_overlap_possible": self.pipeline_overlap_possible,
            "end_to_end_upload_slots": self.end_to_end_upload_slots,
            "client_download_slots": self.client_download_slots,
            "retrieval_worker_slots": self.retrieval_worker_slots,
            "retrieval_memory_slots": self.retrieval_memory_slots,
            "retrieval_pool_slots": self.retrieval_pool_slots,
            "effective_retrieval_slots": self.effective_retrieval_slots,
            "end_to_end_retrieval_slots": self.end_to_end_retrieval_slots,
            "upload_working_bytes_per_request": self.upload_working_bytes_per_request,
            "retrieval_working_bytes_per_request": (self.retrieval_working_bytes_per_request),
            "warnings": list(self.warnings),
        }


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


def _active_slots(value: int | None, *, configured: int, name: str) -> int:
    if value is None:
        return configured
    if isinstance(value, bool) or value < 1:
        raise ValueError(f"{name} must be positive")
    return min(value, configured)


def _raw_verification_mode(value: str) -> RawVerificationMode:
    normalized = value.strip().casefold().replace("-", "_")
    if normalized == RAW_VERIFICATION_PART_MANIFEST:
        return RAW_VERIFICATION_PART_MANIFEST
    if normalized == RAW_VERIFICATION_REMOTE_REREAD:
        return RAW_VERIFICATION_REMOTE_REREAD
    raise ValueError("RIVERHOG_RAW_VERIFICATION_MODE must be part_manifest or remote_reread")


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
