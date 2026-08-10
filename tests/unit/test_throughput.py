from __future__ import annotations

import threading
import time

from riverhog_core.throughput import (
    ArchiveThroughputTuning,
    ArchiveTransferResources,
    S3TransportTuning,
    TransferConcurrencyGate,
    WeightedByteSemaphore,
)


def test_throughput_env_preserves_main_defaults_and_exposes_new_bounds() -> None:
    tuning = ArchiveThroughputTuning.from_env({})

    assert tuning.client_upload_concurrency == 8
    assert tuning.client_upload_window == 16
    assert tuning.client_upload_chunk_bytes == 50 * 1024 * 1024
    assert tuning.age_session_cache_entries == 128
    assert tuning.age_derivation_concurrency == 4
    assert tuning.upload_prepare_concurrency == 8
    assert tuning.s3_part_concurrency == 4
    assert tuning.s3_upload_request_concurrency == 4
    assert tuning.ingress_volume_concurrency == 8
    assert tuning.ingress_volume_window == 16
    assert tuning.upload_max_inflight_bytes == 1280 * 1024 * 1024
    assert tuning.s3_max_pool_connections == 32
    assert tuning.recommended_s3_pool_connections == 20
    assert not tuning.s3_pool_is_likely_constraining

    customized = ArchiveThroughputTuning.from_env(
        {
            "RIVERHOG_UPLOAD_FILE_CONCURRENCY": "16",
            "RIVERHOG_UPLOAD_FILE_WINDOW": "48",
            "RIVERHOG_INGRESS_VOLUME_CONCURRENCY": "8",
            "RIVERHOG_INGRESS_VOLUME_WINDOW": "24",
            "RIVERHOG_ARCHIVE_PREPARE_CONCURRENCY": "12",
            "RIVERHOG_ARCHIVE_MULTIPART_CONCURRENCY": "8",
            "RIVERHOG_ARCHIVE_UPLOAD_REQUEST_CONCURRENCY": "64",
            "RIVERHOG_S3_MAX_POOL_CONNECTIONS": "32",
            "RIVERHOG_INGRESS_MAX_INFLIGHT_BYTES": "2GiB",
            "RIVERHOG_RETRIEVAL_REQUEST_CONCURRENCY": "16",
            "RIVERHOG_RAW_VERIFICATION_MODE": "remote-reread",
        }
    )
    assert customized.upload_max_inflight_bytes == 2 * 1024**3
    assert customized.ingress_volume_window == 24
    assert customized.upload_prepare_concurrency == 12
    assert customized.recommended_s3_pool_connections == 88
    assert customized.s3_pool_is_likely_constraining
    assert customized.raw_verification_mode == "remote_reread"


def test_default_capacity_keeps_all_configured_upload_and_retrieval_workers_active() -> None:
    tuning = ArchiveThroughputTuning.from_env({})
    age_part_bytes = 64 * 1024 * 1024 + 32 * 1024

    capacity = tuning.assess_capacity(
        upload_part_stored_bytes=32 * 1024 * 1024,
        retrieval_request_stored_bytes=age_part_bytes,
        retrieval_request_plaintext_bytes=64 * 1024 * 1024,
        upload_parts_per_volume=1,
    )

    assert capacity.client_upload_slots == 8
    assert capacity.upload_worker_slots == 8
    assert capacity.upload_prepare_slots == 8
    assert capacity.effective_prepare_slots == 8
    assert capacity.upload_memory_slots >= 8
    assert capacity.upload_request_slots == 4
    assert capacity.upload_pool_slots == 16
    assert capacity.upload_buffer_slots == 4
    assert capacity.effective_upload_slots == 4
    assert capacity.pipeline_overlap_possible
    assert capacity.end_to_end_upload_slots == 4
    assert capacity.client_download_slots == 4
    assert capacity.retrieval_worker_slots == 8
    assert capacity.retrieval_memory_slots >= 8
    assert capacity.retrieval_pool_slots == 20
    assert capacity.effective_retrieval_slots == 8
    assert capacity.end_to_end_retrieval_slots == 4
    assert not capacity.constrained


def test_capacity_reports_memory_and_connection_pool_constraints() -> None:
    tuning = ArchiveThroughputTuning(
        ingress_volume_concurrency=8,
        ingress_volume_window=16,
        s3_part_concurrency=8,
        s3_upload_request_concurrency=16,
        s3_max_pool_connections=24,
        upload_max_inflight_bytes=128 * 1024 * 1024,
        retrieval_request_concurrency=16,
        retrieval_max_inflight_bytes=64 * 1024 * 1024,
    )

    capacity = tuning.assess_capacity(
        upload_part_stored_bytes=64 * 1024 * 1024,
        retrieval_request_stored_bytes=64 * 1024 * 1024,
        retrieval_request_plaintext_bytes=64 * 1024 * 1024,
    )

    assert capacity.effective_upload_slots < capacity.upload_worker_slots
    assert capacity.effective_retrieval_slots < capacity.retrieval_worker_slots
    assert capacity.constrained
    assert len(capacity.warnings) >= 4


def test_shared_transfer_resources_use_process_wide_tuning_limits() -> None:
    tuning = ArchiveThroughputTuning.from_env({})
    resources = ArchiveTransferResources.from_tuning(tuning)

    assert resources.upload_bytes.capacity == tuning.upload_max_inflight_bytes
    assert resources.retrieval_bytes.capacity == tuning.retrieval_max_inflight_bytes
    assert resources.upload_preparations.capacity == 8
    assert resources.upload_requests.capacity == 4
    assert resources.retrieval_requests.capacity == 8
    assert resources.age_derivations.capacity == 4


def test_s3_transport_tuning_has_operator_timeouts_retries_and_keepalive() -> None:
    tuning = S3TransportTuning.from_env(
        {
            "RIVERHOG_S3_MAX_POOL_CONNECTIONS": "96",
            "RIVERHOG_S3_CONNECT_TIMEOUT_SECONDS": "4.5",
            "RIVERHOG_S3_READ_TIMEOUT_SECONDS": "900",
            "RIVERHOG_S3_MAX_ATTEMPTS": "12",
            "RIVERHOG_S3_RETRY_MODE": "adaptive",
            "RIVERHOG_S3_TCP_KEEPALIVE": "false",
        }
    )

    assert tuning.max_pool_connections == 96
    assert tuning.connect_timeout_seconds == 4.5
    assert tuning.read_timeout_seconds == 900
    assert tuning.max_attempts == 12
    assert tuning.retry_mode == "adaptive"
    assert not tuning.tcp_keepalive

    scoped = S3TransportTuning.from_env(
        {
            "RIVERHOG_S3_MAX_POOL_CONNECTIONS": "32",
            "RIVERHOG_ARCHIVE_STORE_B2_S3_MAX_POOL_CONNECTIONS": "128",
            "RIVERHOG_ARCHIVE_STORE_B2_S3_READ_TIMEOUT_SECONDS": "1800",
        },
        store_name="b2",
    )
    assert scoped.max_pool_connections == 128
    assert scoped.read_timeout_seconds == 1800


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


def test_capacity_warns_when_two_upload_legs_cannot_overlap() -> None:
    tuning = ArchiveThroughputTuning(
        client_upload_concurrency=4,
        client_upload_window=8,
        s3_upload_request_concurrency=4,
    )

    capacity = tuning.assess_capacity(
        upload_part_stored_bytes=64 * 1024 * 1024,
        retrieval_request_stored_bytes=64 * 1024 * 1024,
        retrieval_request_plaintext_bytes=64 * 1024 * 1024,
    )

    assert not capacity.pipeline_overlap_possible
    assert any("phase-lock" in warning for warning in capacity.warnings)


def test_throughput_payload_has_canonical_identity() -> None:
    payload = ArchiveThroughputTuning.from_env({}).as_dict()
    assert payload["client_upload_concurrency"] == 8
    assert payload["s3_upload_request_concurrency"] == 4


def test_default_one_part_pack_pipeline_has_headroom_between_network_legs() -> None:
    default = ArchiveThroughputTuning.from_env({})
    pack_capacity = default.assess_capacity(
        upload_part_stored_bytes=32 * 1024 * 1024,
        retrieval_request_stored_bytes=16 * 1024 * 1024,
        upload_parts_per_volume=1,
    )

    assert pack_capacity.upload_worker_slots == 8
    assert pack_capacity.upload_buffer_slots == 4
    assert pack_capacity.pipeline_overlap_possible

    phase_locked = ArchiveThroughputTuning(
        client_upload_concurrency=4,
        client_upload_window=8,
        ingress_volume_concurrency=4,
        ingress_volume_window=8,
    )
    locked_capacity = phase_locked.assess_capacity(
        upload_part_stored_bytes=32 * 1024 * 1024,
        retrieval_request_stored_bytes=16 * 1024 * 1024,
        upload_parts_per_volume=1,
    )

    assert not locked_capacity.pipeline_overlap_possible


def test_capacity_uses_actual_workload_shape_for_a_single_pack() -> None:
    tuning = ArchiveThroughputTuning.from_env({})

    capacity = tuning.assess_capacity(
        upload_part_stored_bytes=32 * 1024 * 1024,
        retrieval_request_stored_bytes=16 * 1024 * 1024,
        upload_parts_per_volume=1,
        active_upload_volumes=1,
        active_client_uploads=1,
        active_retrieval_requests=1,
        active_client_downloads=1,
    )

    assert capacity.upload_worker_slots == 1
    assert capacity.client_upload_slots == 1
    assert capacity.effective_upload_slots == 1
    assert capacity.upload_buffer_slots == 0
    assert not capacity.pipeline_overlap_possible
    assert any("phase-lock" in warning for warning in capacity.warnings)
