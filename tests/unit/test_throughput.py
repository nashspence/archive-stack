from __future__ import annotations

import logging
import threading
import time

from riverhog_core.throughput import (
    ArchiveThroughputTuning,
    ArchiveTransferResources,
    TransferConcurrencyGate,
    TransferTiming,
    WeightedByteSemaphore,
    log_transfer_timing,
)


def test_archive_throughput_tuning_exposes_only_effective_runtime_controls() -> None:
    tuning = ArchiveThroughputTuning.from_env({})

    assert tuning.upload_prepare_concurrency == 8
    assert tuning.write_concurrency == 4
    assert tuning.upload_request_concurrency == 4
    assert tuning.upload_max_inflight_bytes == 1280 * 1024 * 1024
    assert tuning.source_read_chunk_bytes == 8 * 1024 * 1024
    assert tuning.retrieval_request_concurrency == 8
    assert tuning.retrieval_max_inflight_bytes == 1024 * 1024 * 1024
    assert tuning.retrieval_read_chunk_bytes == 8 * 1024 * 1024
    assert tuning.age_session_cache_entries == 128
    assert tuning.age_derivation_concurrency == 4

    customized = ArchiveThroughputTuning.from_env(
        {
            "RIVERHOG_ARCHIVE_PREPARE_CONCURRENCY": "12",
            "RIVERHOG_ARCHIVE_WRITE_CONCURRENCY": "8",
            "RIVERHOG_ARCHIVE_UPLOAD_REQUEST_CONCURRENCY": "16",
            "RIVERHOG_INGRESS_MAX_INFLIGHT_BYTES": "2GiB",
            "RIVERHOG_INGRESS_SOURCE_READ_CHUNK_BYTES": "16MiB",
            "RIVERHOG_RETRIEVAL_REQUEST_CONCURRENCY": "16",
            "RIVERHOG_RETRIEVAL_MAX_INFLIGHT_BYTES": "3GiB",
            "RIVERHOG_RETRIEVAL_READ_CHUNK_BYTES": "4MiB",
            "RIVERHOG_AGE_SESSION_CACHE_ENTRIES": "256",
            "RIVERHOG_AGE_SESSION_DERIVATION_CONCURRENCY": "6",
        }
    )

    assert customized.upload_prepare_concurrency == 12
    assert customized.write_concurrency == 8
    assert customized.upload_request_concurrency == 16
    assert customized.upload_max_inflight_bytes == 2 * 1024**3
    assert customized.source_read_chunk_bytes == 16 * 1024**2
    assert customized.retrieval_request_concurrency == 16
    assert customized.retrieval_max_inflight_bytes == 3 * 1024**3
    assert customized.retrieval_read_chunk_bytes == 4 * 1024**2
    assert customized.age_session_cache_entries == 256
    assert customized.age_derivation_concurrency == 6

    scaled = ArchiveThroughputTuning.from_env(
        {
            "RIVERHOG_ARCHIVE_PREPARE_CONCURRENCY": "256",
            "RIVERHOG_AGE_SESSION_CACHE_ENTRIES": "8192",
        }
    )
    assert scaled.upload_prepare_concurrency == 256
    assert scaled.age_session_cache_entries == 8192


def test_shared_transfer_resources_use_process_wide_tuning_limits() -> None:
    tuning = ArchiveThroughputTuning.from_env({})
    resources = ArchiveTransferResources.from_tuning(tuning)

    assert resources.upload_bytes.capacity == tuning.upload_max_inflight_bytes
    assert resources.retrieval_bytes.capacity == tuning.retrieval_max_inflight_bytes
    assert resources.upload_preparations.capacity == tuning.upload_prepare_concurrency
    assert resources.upload_requests.capacity == tuning.upload_request_concurrency
    assert resources.retrieval_requests.capacity == tuning.retrieval_request_concurrency
    assert resources.age_derivations.capacity == tuning.age_derivation_concurrency


def test_transfer_timing_log_separates_phases_without_raw_identity(
    caplog,
) -> None:
    caplog.set_level(logging.INFO, logger="riverhog.transfer")

    log_transfer_timing(
        TransferTiming(
            operation="pack_write_segment",
            identity="private/path.bin",
            plaintext_bytes=1024,
            stored_bytes=1056,
            queue_wait_seconds=0.1,
            source_seconds=0.2,
            crypto_seconds=0.3,
            remote_seconds=0.4,
            checkpoint_seconds=0.5,
            downstream_seconds=0.6,
            integrity_seconds=0.7,
            processing_seconds=0.8,
            elapsed_seconds=2.1,
        )
    )

    message = caplog.messages[-1]
    assert "operation=pack_write_segment" in message
    assert "identity_sha256=" in message
    assert "source_seconds=0.200000" in message
    assert "integrity_seconds=0.700000" in message
    assert "crypto_seconds=0.300000" in message
    assert "processing_seconds=0.800000" in message
    assert "remote_seconds=0.400000" in message
    assert "checkpoint_seconds=0.500000" in message
    assert "downstream_seconds=0.600000" in message
    assert "private/path.bin" not in message


def test_weighted_byte_budget_blocks_only_until_capacity_is_released() -> None:
    budget = WeightedByteSemaphore(10)
    entered = threading.Event()
    released = threading.Event()

    def waiter() -> None:
        with budget.reserve(6):
            entered.set()
        released.set()

    with budget.reserve(6):
        thread = threading.Thread(target=waiter)
        thread.start()
        time.sleep(0.03)
        assert not entered.is_set()
    assert entered.wait(1)
    assert released.wait(1)
    thread.join()


def test_request_gate_reports_queue_wait_and_releases_capacity() -> None:
    gate = TransferConcurrencyGate(1)
    entered = threading.Event()
    waited: list[float] = []

    def waiter() -> None:
        with gate.reserve() as seconds:
            waited.append(seconds)
            entered.set()

    with gate.reserve():
        thread = threading.Thread(target=waiter)
        thread.start()
        time.sleep(0.03)
        assert not entered.is_set()
    assert entered.wait(1)
    thread.join()
    assert waited[0] >= 0.02
