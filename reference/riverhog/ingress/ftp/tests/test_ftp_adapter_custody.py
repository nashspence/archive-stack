from __future__ import annotations

import hashlib
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest
from riverhog_api_client.producer import ProducedCollection
from riverhog_ftp_adapter.config import FtpAdapterConfig, SourceConfig
from riverhog_ftp_adapter.landing import FtpAdapter


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
            "sources": (source,),
        }
    )
    payload = source.root / "captured.bin"
    payload.parent.mkdir(parents=True)
    payload.write_bytes(b"captured")
    _Producer.calls = []
    monkeypatch.setattr("riverhog_ftp_adapter.landing.CollectionProducer", _Producer)

    result = FtpAdapter(object(), config).flush(source.id)  # type: ignore[arg-type]

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
