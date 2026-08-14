from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path

import httpx
import jeb_api_client.client as jeb_client_module
import jeb_core.services.ingress as ingress_service_module
import pytest
from jeb_api.composition import JebServices, config_from_env, create_services
from jeb_api_client import JebApiError, JebIngressClient
from jeb_core.ingress import (
    JebIngressAuthenticationError,
    JebIngressError,
    JebLandingPublisher,
    PreparedTusUpload,
    incomplete_tus_upload_status,
    normalize_tus_upload_id,
    prepare_tus_upload,
)
from jeb_core.persistence.schema import upgrade_state
from riverhog_provenance import FileProvenanceBinding, build_portable_provenance_set


def jeb_env(tmp_path: Path) -> dict[str, str]:
    return {
        "JEB_API_TOKEN": "test-jeb-management-token",
        "JEB_LANDING_DIR": str(tmp_path / "landing"),
        "JEB_STATE_DIR": str(tmp_path / "state"),
        "JEB_MUNCHY_URL": "https://munchy.invalid",
    }


def services_for(env: dict[str, str]) -> JebServices:
    services = create_services(config_from_env(env))
    upgrade_state(services.config)
    services.sources.add_source(
        "phone",
        adapters=("tus",),
        target_config={"template_id": "phone-archive"},
        credential="phone-password",
        cadence="manual",
        stable_seconds=0,
    )
    return services


def basic_authorization(source: str, password: str) -> str:
    encoded = base64.b64encode(f"{source}:{password}".encode()).decode()
    return f"Basic {encoded}"


def stage_publication(
    services: JebServices,
    *,
    relative_path: str = "notes/note.txt",
    content: bytes = b"published payload",
) -> tuple[PreparedTusUpload, dict[str, object], str]:
    payload_sha256 = hashlib.sha256(content).hexdigest()
    binding = FileProvenanceBinding(
        path=relative_path,
        bytes=len(content),
        sha256=payload_sha256,
        status="omitted",
        omission_reason="test fixture intentionally omitted host provenance",
    )
    provenance = build_portable_provenance_set(bindings=(binding,), journals={})
    authorization = basic_authorization("phone", "phone-password")
    prepared = services.ingress.prepare(
        authorization=authorization,
        metadata={
            "path": relative_path,
            "sha256": payload_sha256,
            "provenance_sha256": hashlib.sha256(provenance).hexdigest(),
        },
        size=len(content),
    )
    services.ingress.put_binding(
        authorization=authorization,
        upload_id=prepared.upload_id,
        payload={
            "path": binding.path,
            "bytes": binding.bytes,
            "sha256": binding.sha256,
            "status": binding.status,
            "omission_reason": binding.omission_reason,
        },
    )
    ingress = services.config.ingress
    ingress.tus_staging_dir.mkdir(parents=True, exist_ok=True)
    source = ingress.tus_staging_dir / prepared.upload_id
    info = ingress.tus_staging_dir / f"{prepared.upload_id}.info"
    source.write_bytes(content)
    upload: dict[str, object] = {
        "ID": prepared.upload_id,
        "Size": len(content),
        "Offset": len(content),
        "MetaData": prepared.hook_metadata(),
        "Storage": {
            "Type": "filestore",
            "Path": str(source),
            "InfoPath": str(info),
        },
    }
    info.write_text(json.dumps(upload), encoding="utf-8")
    return prepared, upload, authorization


@pytest.mark.parametrize(
    "value",
    ("", "../upload", "a" * 31, "A" * 32, "not-an-upload-id"),
)
def test_jeb_tus_upload_ids_are_canonical_uuid_hex(value: str) -> None:
    with pytest.raises(JebIngressError, match="invalid ID"):
        normalize_tus_upload_id(value)


def test_jeb_tus_upload_id_normalization_returns_canonical_storage_segment() -> None:
    upload_id = "a" * 32

    assert normalize_tus_upload_id(upload_id) == upload_id


def write_upload(
    services: JebServices,
    *,
    size: int,
    offset: int,
    age_seconds: int,
    now: float,
) -> str:
    ingress = services.config.ingress
    prepared = prepare_tus_upload(
        ingress,
        services.source_registry,
        authorization=basic_authorization("phone", "phone-password"),
        metadata={
            "path": f"notes/{offset}.txt",
            "sha256": "0" * 64,
            "provenance_sha256": "1" * 64,
        },
        size=size,
    )
    ingress.tus_staging_dir.mkdir(parents=True, exist_ok=True)
    source = ingress.tus_staging_dir / prepared.upload_id
    info = ingress.tus_staging_dir / f"{prepared.upload_id}.info"
    source.write_bytes(b"x" * offset)
    info.write_text(
        json.dumps(
            {
                "ID": prepared.upload_id,
                "Size": size,
                "Offset": offset,
                "MetaData": prepared.hook_metadata(),
            }
        ),
        encoding="utf-8",
    )
    modified = now - age_seconds
    os.utime(source, (modified, modified))
    os.utime(info, (modified, modified))
    return prepared.upload_id


def test_jeb_reports_stale_incomplete_tus_uploads_from_signed_records(
    tmp_path: Path,
) -> None:
    now = 2_000_000_000.0
    services = services_for(
        {
            **jeb_env(tmp_path),
            "JEB_TUSD_BASE_URL": "http://tusd.test/files/",
            "JEB_TUS_INCOMPLETE_MAX_AGE": "14d",
        }
    )
    write_upload(
        services,
        size=10,
        offset=4,
        age_seconds=15 * 86_400,
        now=now,
    )
    write_upload(
        services,
        size=10,
        offset=3,
        age_seconds=1 * 86_400,
        now=now,
    )
    write_upload(
        services,
        size=2,
        offset=2,
        age_seconds=20 * 86_400,
        now=now,
    )
    invalid = services.config.ingress.tus_staging_dir / f"{'f' * 32}.info"
    invalid.write_text("{}", encoding="utf-8")

    status = incomplete_tus_upload_status(
        services.config.ingress,
        services.source_registry,
        now=now,
    )
    assert status == {
        "total": 2,
        "bytes": 7,
        "oldest_age_seconds": 15 * 86_400,
        "stale": 1,
        "stale_bytes": 4,
        "max_age_seconds": 14 * 86_400,
        "invalid_records": 1,
        "scan_error": None,
    }


def test_jeb_scheduler_reconciles_durable_ingress_publications(monkeypatch, tmp_path: Path) -> None:
    services = services_for(jeb_env(tmp_path))
    calls: list[object] = []

    def reconcile():
        calls.append("reconcile")
        return {
            "pending": 0,
            "accepted": 0,
            "rejected": 0,
            "failed": 0,
        }

    monkeypatch.setattr(services.ingress, "reconcile", reconcile)

    services.runtime.run_once()

    assert calls == ["reconcile", "reconcile"]


def test_jeb_tus_cleanup_defaults_are_bounded(tmp_path: Path) -> None:
    config = config_from_env(jeb_env(tmp_path))

    assert config.ingress.tusd_base_url == "http://jeb-tusd:1080/files/"
    assert config.ingress.tus_incomplete_max_age_seconds == 14 * 86_400
    with pytest.raises(ValueError, match="must be positive"):
        config_from_env({**jeb_env(tmp_path), "JEB_TUS_INCOMPLETE_MAX_AGE": "0s"})


def test_completed_publication_is_accepted_by_startup_reconciliation(
    tmp_path: Path,
) -> None:
    env = jeb_env(tmp_path)
    services = services_for(env)
    prepared, _upload, authorization = stage_publication(services)

    restarted = create_services(config_from_env(env))
    upgrade_state(restarted.config)
    restarted.runtime.initialize()

    receipt = restarted.ingress.receipt(prepared.upload_id, authorization=authorization)
    assert receipt["status"] == "accepted"
    assert receipt["path"] == "notes/note.txt"
    assert receipt["bytes"] == len(b"published payload")
    assert receipt["payload_sha256"] == hashlib.sha256(b"published payload").hexdigest()
    assert len(str(receipt["provenance_identity"])) == 64
    assert (tmp_path / "landing" / "phone" / "notes" / "note.txt").read_bytes() == (
        b"published payload"
    )
    assert not (restarted.config.ingress.tus_staging_dir / prepared.upload_id).exists()


def test_pending_partial_publication_is_invisible_until_logical_acceptance(
    tmp_path: Path,
) -> None:
    services = services_for(jeb_env(tmp_path))
    prepared, upload, authorization = stage_publication(services)
    source = services.config.ingress.tus_staging_dir / prepared.upload_id
    info = services.config.ingress.tus_staging_dir / f"{prepared.upload_id}.info"
    destination = tmp_path / "landing" / "phone" / "notes" / "note.txt"
    JebLandingPublisher(services.config.ingress).publish(
        upload_id=prepared.upload_id,
        source=source,
        info=info,
        destination=destination,
        size=len(b"published payload"),
    )

    configured = services.source_registry.get("phone")
    assert destination.is_file()
    assert services.sources.eligible_files(configured) == []
    assert services.ingress.receipt(prepared.upload_id, authorization=authorization)["status"] == (
        "pending"
    )

    receipt = services.ingress.publish(upload)
    assert receipt["status"] == "accepted"
    assert services.store.ingress_publication_counts() == {
        "pending": 0,
        "accepted": 1,
        "rejected": 0,
    }


def test_payload_link_crash_before_provenance_resumes_the_same_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    services = services_for(jeb_env(tmp_path))
    prepared, upload, authorization = stage_publication(services)
    destination = tmp_path / "landing" / "phone" / "notes" / "note.txt"
    original_publish = ingress_service_module.publish_ingress_provenance

    def fail_provenance(_destination: Path, _value: object) -> Path:
        raise OSError("injected provenance publication interruption")

    monkeypatch.setattr(
        ingress_service_module,
        "publish_ingress_provenance",
        fail_provenance,
    )
    with pytest.raises(OSError, match="injected provenance publication interruption"):
        services.ingress.publish(upload)

    assert destination.read_bytes() == b"published payload"
    assert (
        services.ingress.receipt(prepared.upload_id, authorization=authorization)["status"]
        == "pending"
    )
    assert services.sources.eligible_files(services.source_registry.get("phone")) == []

    monkeypatch.setattr(
        ingress_service_module,
        "publish_ingress_provenance",
        original_publish,
    )
    receipt = services.ingress.publish(upload)

    assert receipt["status"] == "accepted"
    assert destination.read_bytes() == b"published payload"


def test_provenance_write_crash_before_receipt_resumes_without_identity_change(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    services = services_for(jeb_env(tmp_path))
    prepared, upload, authorization = stage_publication(services)
    destination = tmp_path / "landing" / "phone" / "notes" / "note.txt"
    original_accept = services.store.accept_ingress_publication

    def fail_accept(
        _upload_id: str,
        *,
        destination_path: str,
        provenance_identity: str,
    ) -> object:
        _ = destination_path, provenance_identity
        raise RuntimeError("injected receipt commit interruption")

    monkeypatch.setattr(services.store, "accept_ingress_publication", fail_accept)
    with pytest.raises(RuntimeError, match="injected receipt commit interruption"):
        services.ingress.publish(upload)

    provenance = destination.with_name(f".{destination.name}.riverhog-provenance")
    payload_before = destination.read_bytes()
    provenance_before = (provenance / "index.json").read_bytes()
    assert (
        services.ingress.receipt(prepared.upload_id, authorization=authorization)["status"]
        == "pending"
    )

    monkeypatch.setattr(services.store, "accept_ingress_publication", original_accept)
    receipt = services.ingress.publish(upload)

    assert receipt["status"] == "accepted"
    assert destination.read_bytes() == payload_before
    assert (provenance / "index.json").read_bytes() == provenance_before


def test_accepted_receipt_survives_interrupted_staging_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    services = services_for(jeb_env(tmp_path))
    prepared, upload, authorization = stage_publication(services)
    original_cleanup = JebLandingPublisher.cleanup_staging

    def fail_cleanup(_source: Path, _info: Path) -> None:
        raise OSError("injected terminal cleanup interruption")

    monkeypatch.setattr(JebLandingPublisher, "cleanup_staging", fail_cleanup)
    receipt = services.ingress.publish(upload)

    assert receipt["status"] == "accepted"
    assert (services.config.ingress.tus_staging_dir / prepared.upload_id).is_file()

    monkeypatch.setattr(JebLandingPublisher, "cleanup_staging", original_cleanup)
    services.ingress.reconcile()

    assert services.ingress.receipt(prepared.upload_id, authorization=authorization) == receipt
    assert not (services.config.ingress.tus_staging_dir / prepared.upload_id).exists()
    assert not (services.config.ingress.tus_staging_dir / f"{prepared.upload_id}.info").exists()


def test_payload_identity_rejection_never_reaches_the_landing_namespace(
    tmp_path: Path,
) -> None:
    services = services_for(jeb_env(tmp_path))
    prepared, upload, authorization = stage_publication(services)
    (services.config.ingress.tus_staging_dir / prepared.upload_id).write_bytes(b"tampered payload!")

    receipt = services.ingress.publish(upload)

    assert receipt["status"] == "rejected"
    assert receipt["error"]["code"] == "provenance_rejected"  # type: ignore[index]
    assert not (tmp_path / "landing" / "phone" / "notes" / "note.txt").exists()
    assert services.ingress.receipt(prepared.upload_id, authorization=authorization) == receipt


def test_destination_collision_rejects_without_altering_existing_payload_or_provenance(
    tmp_path: Path,
) -> None:
    services = services_for(jeb_env(tmp_path))
    destination = tmp_path / "landing" / "phone" / "notes" / "note.txt"
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(b"existing payload")
    provenance_root = destination.with_name(f".{destination.name}.riverhog-provenance")
    provenance_root.mkdir()
    existing_provenance = b"existing provenance sentinel"
    (provenance_root / "index.json").write_bytes(existing_provenance)
    prepared, upload, authorization = stage_publication(services)

    receipt = services.ingress.publish(upload)

    assert receipt["status"] == "rejected"
    assert receipt["error"]["code"] == "destination_collision"  # type: ignore[index]
    assert destination.read_bytes() == b"existing payload"
    assert (provenance_root / "index.json").read_bytes() == existing_provenance
    assert services.ingress.receipt(prepared.upload_id, authorization=authorization) == receipt


def test_publication_receipt_is_source_scoped(tmp_path: Path) -> None:
    services = services_for(jeb_env(tmp_path))
    prepared, _upload, authorization = stage_publication(services)
    services.sources.add_source(
        "camera",
        adapters=("tus",),
        target_config={"template_id": "camera-archive"},
        credential="camera-password",
        cadence="manual",
    )

    assert services.ingress.receipt(prepared.upload_id, authorization=authorization)["status"] == (
        "pending"
    )
    with pytest.raises(JebIngressAuthenticationError, match="invalid Jeb ingress publication"):
        services.ingress.receipt(
            prepared.upload_id,
            authorization=basic_authorization("camera", "camera-password"),
        )


def test_expired_incomplete_intent_reaches_durable_rejection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    services = services_for({**jeb_env(tmp_path), "JEB_TUS_INCOMPLETE_MAX_AGE": "1s"})
    authorization = basic_authorization("phone", "phone-password")
    prepared = services.ingress.prepare(
        authorization=authorization,
        metadata={
            "path": "notes/stale.txt",
            "sha256": "a" * 64,
            "provenance_sha256": "b" * 64,
        },
        size=10,
    )
    terminated: list[str] = []
    monkeypatch.setattr(
        ingress_service_module,
        "terminate_tus_upload",
        lambda _config, upload_id: terminated.append(upload_id),
    )

    result = services.ingress.reconcile(now=10**12)
    receipt = services.ingress.receipt(prepared.upload_id, authorization=authorization)

    assert result["rejected"] == 1
    assert terminated == [prepared.upload_id]
    assert receipt["status"] == "rejected"
    assert receipt["error"]["code"] == "upload_expired"  # type: ignore[index]


def test_expired_invalid_upload_record_reaches_durable_rejection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    services = services_for({**jeb_env(tmp_path), "JEB_TUS_INCOMPLETE_MAX_AGE": "1s"})
    authorization = basic_authorization("phone", "phone-password")
    prepared = services.ingress.prepare(
        authorization=authorization,
        metadata={
            "path": "notes/invalid.txt",
            "sha256": "a" * 64,
            "provenance_sha256": "b" * 64,
        },
        size=10,
    )
    staging = services.config.ingress.tus_staging_dir
    staging.mkdir(parents=True, exist_ok=True)
    (staging / f"{prepared.upload_id}.info").write_text("not-json", encoding="utf-8")
    terminated: list[str] = []
    monkeypatch.setattr(
        ingress_service_module,
        "terminate_tus_upload",
        lambda _config, upload_id: terminated.append(upload_id),
    )

    result = services.ingress.reconcile(now=10**12)
    receipt = services.ingress.receipt(prepared.upload_id, authorization=authorization)

    assert result == {"pending": 0, "accepted": 0, "rejected": 1, "failed": 0}
    assert terminated == [prepared.upload_id]
    assert receipt["status"] == "rejected"
    assert receipt["error"]["code"] == "invalid_upload_record"  # type: ignore[index]


def test_old_completed_publication_remains_pending_during_transient_storage_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    services = services_for({**jeb_env(tmp_path), "JEB_TUS_INCOMPLETE_MAX_AGE": "1s"})
    prepared, _upload, authorization = stage_publication(services)

    def fail_provenance(_destination: Path, _value: object) -> Path:
        raise OSError("injected transient storage failure")

    monkeypatch.setattr(
        ingress_service_module,
        "publish_ingress_provenance",
        fail_provenance,
    )

    result = services.ingress.reconcile(now=10**12)
    receipt = services.ingress.receipt(prepared.upload_id, authorization=authorization)

    assert result == {"pending": 0, "accepted": 0, "rejected": 0, "failed": 1}
    assert receipt["status"] == "pending"


def test_official_ingress_client_waits_through_lost_polls_for_terminal_receipt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = tmp_path / "note.txt"
    payload.write_bytes(b"client payload")
    digest = hashlib.sha256(payload.read_bytes()).hexdigest()
    binding = {
        "path": "notes/note.txt",
        "bytes": payload.stat().st_size,
        "sha256": digest,
        "status": "omitted",
        "omission_reason": "test fixture",
    }
    upload_id = "a" * 32
    polls = 0

    def handle(request: httpx.Request) -> httpx.Response:
        nonlocal polls
        if request.method == "POST":
            return httpx.Response(201, headers={"Location": f"/files/{upload_id}"})
        if request.method == "PUT":
            return httpx.Response(200, json={"status": "accepted"})
        if request.method == "HEAD":
            return httpx.Response(200, headers={"Upload-Offset": "0"})
        if request.method == "PATCH":
            return httpx.Response(204, headers={"Upload-Offset": str(payload.stat().st_size)})
        if request.method == "GET":
            polls += 1
            if polls == 1:
                raise httpx.ConnectError("lost receipt poll", request=request)
            if polls == 2:
                return httpx.Response(503)
            if polls == 3:
                return httpx.Response(
                    200,
                    json={
                        "format": "jeb-ingress-publication/v1",
                        "status": "pending",
                        "upload_id": upload_id,
                        "path": binding["path"],
                        "bytes": binding["bytes"],
                        "payload_sha256": digest,
                    },
                )
            return httpx.Response(
                200,
                json={
                    "format": "jeb-ingress-publication/v1",
                    "status": "accepted",
                    "upload_id": upload_id,
                    "path": binding["path"],
                    "bytes": binding["bytes"],
                    "payload_sha256": digest,
                    "provenance_identity": "c" * 64,
                },
            )
        return httpx.Response(404)

    client = JebIngressClient(
        source="phone",
        password="phone-password",
        base_url="https://jeb.example.test",
        transport=httpx.MockTransport(handle),
    )
    monkeypatch.setattr(jeb_client_module.time, "sleep", lambda _seconds: None)
    try:
        result = client.upload_file(
            payload,
            relative_path="notes/note.txt",
            binding=binding,
            journals={},
        )
    finally:
        client.close()

    assert polls == 4
    assert result["status"] == "accepted"


def test_official_ingress_client_surfaces_durable_rejection() -> None:
    upload_id = "d" * 32

    def handle(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        return httpx.Response(
            200,
            json={
                "format": "jeb-ingress-publication/v1",
                "status": "rejected",
                "upload_id": upload_id,
                "path": "notes/rejected.txt",
                "bytes": 7,
                "payload_sha256": "a" * 64,
                "error": {
                    "code": "destination_collision",
                    "message": "landing path already exists",
                },
            },
        )

    client = JebIngressClient(
        source="phone",
        password="phone-password",
        base_url="https://jeb.example.test",
        transport=httpx.MockTransport(handle),
    )
    try:
        with pytest.raises(JebApiError, match="landing path already exists") as raised:
            client.wait_for_publication(upload_id, interval=0.001)
    finally:
        client.close()

    assert raised.value.code == "destination_collision"


def test_jeb_tus_publication_resolves_destination_under_its_source_root(
    tmp_path: Path,
) -> None:
    services = services_for(jeb_env(tmp_path))
    _prepared, upload, _authorization = stage_publication(services, content=b"notes")
    outside = tmp_path / "outside"
    outside.mkdir()
    source_landing = services.config.ingress.landing_dir / "phone"
    source_landing.mkdir(parents=True)
    (source_landing / "notes").symlink_to(outside, target_is_directory=True)

    receipt = services.ingress.publish(upload)

    assert receipt["status"] == "rejected"
    assert receipt["error"]["code"] == "invalid_upload"  # type: ignore[index]
    assert list(outside.iterdir()) == []


def test_jeb_landing_publisher_resolves_every_mutated_path_under_its_root(
    tmp_path: Path,
) -> None:
    services = services_for(jeb_env(tmp_path))
    ingress = services.config.ingress
    ingress.tus_staging_dir.mkdir(parents=True)
    ingress.landing_dir.mkdir(parents=True, exist_ok=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    outside_source = outside / "upload"
    outside_source.write_bytes(b"notes")
    source = ingress.tus_staging_dir / ("a" * 32)
    source.symlink_to(outside_source)
    info = ingress.tus_staging_dir / f"{'a' * 32}.info"
    info.write_text("{}", encoding="utf-8")

    with pytest.raises(JebIngressError, match="source escaped its configured root"):
        JebLandingPublisher(ingress).publish(
            upload_id="a" * 32,
            source=source,
            info=info,
            destination=ingress.landing_dir / "phone" / "note.txt",
            size=5,
        )

    assert outside_source.read_bytes() == b"notes"
