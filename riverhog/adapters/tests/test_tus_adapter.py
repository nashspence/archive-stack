from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path

import pytest
from riverhog_adapters.config import AdapterConfig, SourceConfig
from riverhog_adapters.landing import FinalizedReceiptAdapter
from riverhog_adapters.tus import TusPublicationService
from riverhog_api_client.producer import ProducedCollection
from riverhog_provenance import FileProvenanceBinding, build_portable_provenance_set


class _CompletedAdapter:
    def accept_completed_file(self, *_args: object, **_kwargs: object) -> ProducedCollection:
        return ProducedCollection(9, "a" * 64, "b" * 64, {"state": "finalized"})


class _Producer:
    calls: list[str] = []
    lose_response = False

    def __init__(self, _api: object, **_kwargs: object) -> None:
        pass

    def publish(self, _files: object, **kwargs: object) -> ProducedCollection:
        self.__class__.calls.append(str(kwargs["idempotency_key"]))
        if self.__class__.lose_response:
            self.__class__.lose_response = False
            raise ConnectionError("finalized response was lost")
        return ProducedCollection(9, "a" * 64, "b" * 64, {"state": "finalized"})


def test_tus_hook_binds_client_provenance_and_exact_finalized_receipt(
    monkeypatch,
    tmp_path: Path,
) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("CAMERA_TUS_PASSWORD", "correct horse")
    source = SourceConfig(
        id="camera-a",
        adapter="tus",
        root=tmp_path / "tus",
        ingest_source="tus:camera-a",
        tags=("camera-a", "intake"),
        provenance="omit",
        provenance_omission_reason="Camera cannot observe host provenance.",
        credential_env="CAMERA_TUS_PASSWORD",
    )
    config = AdapterConfig(
        host_id="urn:uuid:00000000-0000-4000-8000-000000000001",
        riverhog_base_url="https://riverhog.invalid",
        riverhog_token="riverhog-token",
        api_token="adapter-token",
        sources=(source,),
    )
    service = TusPublicationService(config, _CompletedAdapter())  # type: ignore[arg-type]
    payload = b"camera upload"
    digest = hashlib.sha256(payload).hexdigest()
    binding = FileProvenanceBinding(
        path="camera/upload.mp4",
        bytes=len(payload),
        sha256=digest,
        status="omitted",
        omission_reason="Camera cannot observe host provenance.",
    )
    portable = build_portable_provenance_set(bindings=(binding,), journals={})
    auth = "Basic " + base64.b64encode(b"camera-a:correct horse").decode()
    assert service.authenticate(auth) == "camera-a"

    prepared = service.prepare(
        authorization=auth,
        metadata={
            "path": binding.path,
            "sha256": binding.sha256,
            "provenance_sha256": hashlib.sha256(portable).hexdigest(),
        },
        size=binding.bytes,
    )
    service.put_binding(
        prepared.upload_id,
        {
            "path": binding.path,
            "bytes": binding.bytes,
            "sha256": binding.sha256,
            "status": "omitted",
            "omission_reason": binding.omission_reason,
        },
        authorization=auth,
    )
    uploads = source.root / "uploads"
    uploads.mkdir(parents=True)
    upload_path = uploads / prepared.upload_id
    info_path = uploads / f"{prepared.upload_id}.info"
    upload_path.write_bytes(payload)
    info_path.write_text(json.dumps({"ID": prepared.upload_id}), encoding="utf-8")
    upload = {
        "ID": prepared.upload_id,
        "Size": len(payload),
        "Offset": len(payload),
        "MetaData": prepared.metadata(),
        "Storage": {
            "Type": "filestore",
            "Path": str(upload_path),
            "InfoPath": str(info_path),
        },
    }

    receipt = service.publish(upload)

    assert receipt == {
        "format": "riverhog-tus-receipt/v1",
        "upload_id": prepared.upload_id,
        "status": "accepted",
        "path": binding.path,
        "bytes": len(payload),
        "payload_sha256": digest,
        "provenance_sha256": hashlib.sha256(portable).hexdigest(),
        "collection_id": 9,
        "manifest_sha256": "a" * 64,
        "content_etag": "b" * 64,
    }
    assert service.publish(upload) == receipt


def _publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[TusPublicationService, dict[str, object], str]:
    monkeypatch.setenv("CAMERA_TUS_PASSWORD", "correct horse")
    source = SourceConfig(
        id="camera-a",
        adapter="tus",
        root=tmp_path / "tus",
        ingest_source="tus:camera-a",
        tags=("camera-a", "intake"),
        provenance="omit",
        provenance_omission_reason="Camera cannot observe host provenance.",
        credential_env="CAMERA_TUS_PASSWORD",
    )
    config = AdapterConfig(
        host_id="urn:uuid:00000000-0000-4000-8000-000000000001",
        riverhog_base_url="https://riverhog.invalid",
        riverhog_token="riverhog-token",
        api_token="adapter-token",
        sources=(source,),
    )
    service = TusPublicationService(
        config,
        FinalizedReceiptAdapter(object(), config),  # type: ignore[arg-type]
    )
    payload = b"camera upload"
    digest = hashlib.sha256(payload).hexdigest()
    binding = FileProvenanceBinding(
        path="camera/upload.mp4",
        bytes=len(payload),
        sha256=digest,
        status="omitted",
        omission_reason="Camera cannot observe host provenance.",
    )
    portable = build_portable_provenance_set(bindings=(binding,), journals={})
    authorization = "Basic " + base64.b64encode(b"camera-a:correct horse").decode()
    prepared = service.prepare(
        authorization=authorization,
        metadata={
            "path": binding.path,
            "sha256": binding.sha256,
            "provenance_sha256": hashlib.sha256(portable).hexdigest(),
        },
        size=binding.bytes,
    )
    service.put_binding(
        prepared.upload_id,
        {
            "path": binding.path,
            "bytes": binding.bytes,
            "sha256": binding.sha256,
            "status": "omitted",
            "omission_reason": binding.omission_reason,
        },
        authorization=authorization,
    )
    uploads = source.root / "uploads"
    uploads.mkdir(parents=True)
    upload_path = uploads / prepared.upload_id
    info_path = uploads / f"{prepared.upload_id}.info"
    upload_path.write_bytes(payload)
    info_path.write_text(json.dumps({"ID": prepared.upload_id}), encoding="utf-8")
    return (
        service,
        {
            "ID": prepared.upload_id,
            "Size": len(payload),
            "Offset": len(payload),
            "MetaData": prepared.metadata(),
            "Storage": {
                "Type": "filestore",
                "Path": str(upload_path),
                "InfoPath": str(info_path),
            },
        },
        authorization,
    )


def test_tus_publication_reconciles_lost_riverhog_response_from_claimed_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("riverhog_adapters.landing.CollectionProducer", _Producer)
    service, upload, authorization = _publication(tmp_path, monkeypatch)
    _Producer.calls = []
    _Producer.lose_response = True

    with pytest.raises(ConnectionError, match="response was lost"):
        service.publish(upload)
    assert not Path(str(upload["Storage"]["Path"])).exists()  # type: ignore[index]

    receipt = service.publish(upload)

    assert receipt["status"] == "accepted"
    assert len(_Producer.calls) == 2
    assert _Producer.calls[0] == _Producer.calls[1]
    assert service.receipt(str(upload["ID"]), authorization=authorization) == receipt


def test_tus_publication_recovers_crash_after_finalized_adapter_receipt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("riverhog_adapters.landing.CollectionProducer", _Producer)
    service, upload, authorization = _publication(tmp_path, monkeypatch)
    _Producer.calls = []
    _Producer.lose_response = False
    from riverhog_adapters import tus as tus_module

    write_json = tus_module._write_json
    fail_receipt_once = True

    def interrupted_write(path: Path, payload: dict[str, object]) -> None:
        nonlocal fail_receipt_once
        if path.name == "receipt.json" and fail_receipt_once:
            fail_receipt_once = False
            raise OSError("simulated process loss before TUS receipt commit")
        write_json(path, payload)

    monkeypatch.setattr(tus_module, "_write_json", interrupted_write)
    with pytest.raises(OSError, match="process loss"):
        service.publish(upload)

    receipt = service.publish(upload)

    assert receipt["status"] == "accepted"
    assert len(_Producer.calls) == 1
    assert service.receipt(str(upload["ID"]), authorization=authorization) == receipt
