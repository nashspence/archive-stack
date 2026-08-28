from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest
import riverhog_ftp_adapter.landing as landing
from riverhog_api_client.producer import ProducedCollection
from riverhog_ftp_adapter.config import FtpAdapterConfig, SourceConfig
from riverhog_ftp_adapter.landing import FtpAdapter

from tests.provenance_observer import native_provenance_observer

REPO_ROOT = Path(__file__).resolve().parents[5]


def _config(tmp_path: Path) -> FtpAdapterConfig:
    return FtpAdapterConfig(
        host_id="test-host",
        riverhog_base_url="https://riverhog.invalid",
        riverhog_token="riverhog-token",
        api_token="adapter-token",
        sources=(
            SourceConfig(
                id="camera-a",
                root=tmp_path / "landing",
                ingest_source="ftp:camera-a",
                tags=("camera-a", "intake"),
                stable_seconds=1,
                max_files=10,
                max_bytes=1024 * 1024,
                provenance="omit",
                provenance_omission_reason="Fixture intentionally has no host provenance.",
            ),
        ),
    )


class _Producer:
    calls: list[dict[str, Any]] = []
    fail_once = False

    def __init__(self, _api: object, **kwargs: object) -> None:
        self.kwargs = kwargs

    def publish(self, files: object, **kwargs: object) -> ProducedCollection:
        materialized = tuple(files)  # type: ignore[arg-type]
        self.__class__.calls.append(
            {
                "files": [
                    (item.path, item.source.read_bytes(), item.provenance) for item in materialized
                ],
                "kwargs": kwargs,
                "producer": self.kwargs,
            }
        )
        if self.__class__.fail_once:
            self.__class__.fail_once = False
            raise ConnectionError("finalized response was lost")
        return ProducedCollection(
            collection_id=41,
            archive_root_sha256="a" * 64,
            content_identity="b" * 64,
            receipt={"state": "finalized"},
        )


def test_v1_claim_fixture_retains_payload_and_portable_provenance_identity() -> None:
    fixture_root = REPO_ROOT / "tests/fixtures/state/v1_0001/riverhog-ftp-adapter"
    manifest = json.loads((fixture_root / "claim.json").read_text(encoding="utf-8"))
    payload = (fixture_root / "payload.bin").read_bytes()

    assert landing._read_manifest(fixture_root) == manifest
    assert hashlib.sha256(payload).hexdigest() == manifest["files"][0]["sha256"]
    assert landing._producer_provenance(manifest["files"][0]) == {
        "status": "omitted",
        "omission_reason": "Fixture intentionally has no host provenance.",
    }


def test_landing_adapter_reconciles_lost_response_without_releasing_custody(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    source = config.sources[0]
    payload = source.root / "camera" / "clip.mp4"
    payload.parent.mkdir(parents=True)
    payload.write_bytes(b"immutable camera payload")
    old = time.time() - 10
    os.utime(payload, (old, old))
    _Producer.calls = []
    _Producer.fail_once = True
    monkeypatch.setattr("riverhog_ftp_adapter.landing.CollectionProducer", _Producer)
    adapter = FtpAdapter(object(), config)  # type: ignore[arg-type]

    first = adapter.run_once()

    assert first["completed"] == 0
    assert len(first["failed"]) == 1  # type: ignore[arg-type]
    assert not payload.exists()
    claims = list((source.root / ".riverhog-ftp-adapter" / "claims").iterdir())
    assert len(claims) == 1
    assert (claims[0] / "payload" / "camera" / "clip.mp4").read_bytes() == (
        b"immutable camera payload"
    )

    second = adapter.run_once()

    assert second == {
        "format": "riverhog-ftp-adapter-pass/v1",
        "completed": 1,
        "failed": [],
        "sources": ["camera-a"],
    }
    assert not claims[0].exists()
    assert [call["kwargs"]["idempotency_key"] for call in _Producer.calls] == [
        _Producer.calls[0]["kwargs"]["idempotency_key"],
        _Producer.calls[0]["kwargs"]["idempotency_key"],
    ]
    assert all(
        call["producer"]["adapter_id"] == "ftp/v1"
        and call["files"][0][:2] == ("camera/clip.mp4", b"immutable camera payload")
        for call in _Producer.calls
    )


def test_explicit_flush_is_the_same_bounded_claim_and_receipt_path(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    base = _config(tmp_path)
    source = base.sources[0].model_copy(update={"close_mode": "explicit-flush"})
    config = base.model_copy(update={"sources": (source,)})
    payload = source.root / "current.bin"
    payload.parent.mkdir(parents=True)
    payload.write_bytes(b"current")
    _Producer.calls = []
    monkeypatch.setattr("riverhog_ftp_adapter.landing.CollectionProducer", _Producer)
    adapter = FtpAdapter(object(), config)  # type: ignore[arg-type]

    assert adapter.run_once()["completed"] == 0
    assert adapter.flush(source.id)["completed"] == 1
    assert _Producer.calls[0]["files"] == [
        (
            "current.bin",
            b"current",
            {
                "status": "omitted",
                "omission_reason": "Fixture intentionally has no host provenance.",
            },
        )
    ]


def test_explicit_flush_marker_and_pass_are_one_serialized_operation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    base = _config(tmp_path)
    source = base.sources[0].model_copy(update={"close_mode": "explicit-flush"})
    config = base.model_copy(update={"sources": (source,)})
    payload = source.root / "current.bin"
    payload.parent.mkdir(parents=True)
    payload.write_bytes(b"current")
    _Producer.calls = []
    monkeypatch.setattr(landing, "CollectionProducer", _Producer)
    adapter = FtpAdapter(object(), config)  # type: ignore[arg-type]

    marker_written = threading.Event()
    release_marker_write = threading.Event()
    polling_pass_entered = threading.Event()
    original_write_atomic = landing._write_atomic
    original_pass = adapter._run_once

    def blocking_marker_write(path: Path, content: bytes) -> None:
        original_write_atomic(path, content)
        if path.name == ".riverhog-ftp-flush":
            marker_written.set()
            assert release_marker_write.wait(timeout=2)

    def observed_pass(source_ids: object = None) -> dict[str, object]:
        if threading.current_thread().name == "polling-pass":
            polling_pass_entered.set()
        return original_pass(source_ids)  # type: ignore[arg-type]

    monkeypatch.setattr(landing, "_write_atomic", blocking_marker_write)
    monkeypatch.setattr(adapter, "_run_once", observed_pass)

    with ThreadPoolExecutor(max_workers=2) as executor:
        flush = executor.submit(adapter.flush, source.id)
        try:
            assert marker_written.wait(timeout=2)
            poll = executor.submit(adapter.run_once)
            polling_stole_marker = polling_pass_entered.wait(timeout=0.25)
        finally:
            release_marker_write.set()
        flush_result = flush.result(timeout=5)
        poll_result = poll.result(timeout=5)

    assert polling_stole_marker is False
    assert flush_result["completed"] == 1
    assert flush_result["failed"] == []
    assert poll_result["completed"] == 0


def test_captured_provenance_is_identity_checked_and_projected_for_the_producer(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    base = _config(tmp_path)
    source = base.sources[0].model_copy(
        update={
            "close_mode": "explicit-flush",
            "provenance": "capture",
            "provenance_omission_reason": None,
        }
    )
    config = base.model_copy(
        update={
            "host_id": "urn:uuid:00000000-0000-4000-8000-000000000522",
            "provenance_observer": "fixture-observer",
            "sources": (source,),
        }
    )
    payload = source.root / "captured.bin"
    payload.parent.mkdir(parents=True)
    payload.write_bytes(b"captured")
    _Producer.calls = []
    monkeypatch.setattr("riverhog_ftp_adapter.landing.CollectionProducer", _Producer)

    result = FtpAdapter(
        object(),  # type: ignore[arg-type]
        config,
        provenance_observer_factory=native_provenance_observer,
    ).flush(source.id)

    assert result["completed"] == 1
    provenance = _Producer.calls[0]["files"][0][2]
    assert provenance["status"] == "captured"
    assert set(provenance) == {"status", "journal_id", "current_state_id"}
    assert _Producer.calls[0]["kwargs"]["provenance_journals"]


def test_custody_passes_are_serialized_across_protocol_and_polling_entrypoints(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    source = config.sources[0]
    active = 0
    maximum_active = 0
    guard = threading.Lock()

    class BlockingProducer(_Producer):
        def publish(self, files: object, **kwargs: object) -> ProducedCollection:
            nonlocal active, maximum_active
            with guard:
                active += 1
                maximum_active = max(maximum_active, active)
            try:
                time.sleep(0.05)
                return super().publish(files, **kwargs)
            finally:
                with guard:
                    active -= 1

    BlockingProducer.calls = []
    monkeypatch.setattr("riverhog_ftp_adapter.landing.CollectionProducer", BlockingProducer)
    adapter = FtpAdapter(object(), config)  # type: ignore[arg-type]
    payloads = []
    for index in range(2):
        payload = source.root / f"protocol-{index}.bin"
        payload.parent.mkdir(parents=True, exist_ok=True)
        content = f"protocol-{index}".encode()
        payload.write_bytes(content)
        payloads.append((payload, content))

    def accept(index: int) -> ProducedCollection:
        payload, content = payloads[index]
        digest = hashlib.sha256(content).hexdigest()
        return adapter.accept_completed_file(
            source,
            payload,
            relative_path=payload.name,
            source_event_id=f"event-{index}",
            expected_bytes=len(content),
            expected_sha256=digest,
            provenance={
                "path": payload.name,
                "bytes": len(content),
                "sha256": digest,
                "status": "omitted",
                "omission_reason": "Fixture intentionally has no host provenance.",
            },
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        receipts = tuple(executor.map(accept, range(2)))

    assert [receipt.collection_id for receipt in receipts] == [41, 41]
    assert maximum_active == 1
    assert len(BlockingProducer.calls) == 2
