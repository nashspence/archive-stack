from __future__ import annotations

import base64
import json
import os
from pathlib import Path

import httpx
import jeb_core.runtime.service as service_runtime
import pytest
from jeb_api.composition import JebServices, config_from_env, create_services
from jeb_core.ingress import (
    JebIngressError,
    JebLandingPublisher,
    incomplete_tus_upload_status,
    normalize_tus_upload_id,
    prepare_tus_upload,
    publish_tus_upload,
    reap_stale_incomplete_tus_uploads,
)
from jeb_core.persistence.schema import upgrade_state


def jeb_env(tmp_path: Path) -> dict[str, str]:
    return {
        "JEB_LANDING_DIR": str(tmp_path / "landing"),
        "JEB_STATE_DIR": str(tmp_path / "state"),
        "JEB_MUNCHY_URL": "http://munchy.invalid",
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
    )
    return services


def basic_authorization(source: str, password: str) -> str:
    encoded = base64.b64encode(f"{source}:{password}".encode()).decode()
    return f"Basic {encoded}"


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


def test_jeb_reports_and_terminates_only_stale_incomplete_tus_uploads(
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
    old_id = write_upload(
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
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.method == "DELETE"
        assert request.url == f"http://tusd.test/files/{old_id}"
        assert request.headers["Tus-Resumable"] == "1.0.0"
        return httpx.Response(204)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    result = reap_stale_incomplete_tus_uploads(
        services.config.ingress,
        services.source_registry,
        now=now,
        client=client,
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
    assert len(requests) == 1
    assert result == {
        "candidates": 1,
        "candidate_bytes": 4,
        "terminated": 1,
        "already_absent": 0,
        "failed": 0,
        "invalid_records": 1,
        "scan_error": None,
    }


def test_jeb_scheduler_runs_incomplete_tus_cleanup(monkeypatch, tmp_path: Path) -> None:
    services = services_for(jeb_env(tmp_path))
    calls: list[object] = []

    def reap(config, registry):
        calls.append((config, registry))
        return {
            "terminated": 0,
            "already_absent": 0,
            "failed": 0,
            "scan_error": None,
        }

    monkeypatch.setattr(service_runtime, "reap_stale_incomplete_tus_uploads", reap)

    services.runtime.run_once()

    assert calls == [(services.config.ingress, services.source_registry)]


def test_jeb_tus_cleanup_defaults_are_bounded(tmp_path: Path) -> None:
    config = config_from_env(jeb_env(tmp_path))

    assert config.ingress.tusd_base_url == "http://jeb-tusd:1080/files/"
    assert config.ingress.tus_incomplete_max_age_seconds == 14 * 86_400
    with pytest.raises(ValueError, match="must be positive"):
        config_from_env({**jeb_env(tmp_path), "JEB_TUS_INCOMPLETE_MAX_AGE": "0s"})


def test_jeb_tus_publication_resolves_destination_under_its_source_root(
    tmp_path: Path,
) -> None:
    services = services_for(jeb_env(tmp_path))
    prepared = prepare_tus_upload(
        services.config.ingress,
        services.source_registry,
        authorization=basic_authorization("phone", "phone-password"),
        metadata={
            "path": "notes/note.txt",
            "sha256": "0" * 64,
            "provenance_sha256": "1" * 64,
        },
        size=5,
    )
    staging = services.config.ingress.tus_staging_dir
    staging.mkdir(parents=True)
    source = staging / prepared.upload_id
    info = staging / f"{prepared.upload_id}.info"
    source.write_bytes(b"notes")
    info.write_text("{}", encoding="utf-8")
    outside = tmp_path / "outside"
    outside.mkdir()
    source_landing = services.config.ingress.landing_dir / "phone"
    source_landing.mkdir(parents=True)
    (source_landing / "notes").symlink_to(outside, target_is_directory=True)

    with pytest.raises(JebIngressError, match="escaped its source landing directory"):
        publish_tus_upload(
            services.config.ingress,
            services.source_registry,
            upload={
                "ID": prepared.upload_id,
                "Size": 5,
                "Offset": 5,
                "MetaData": prepared.hook_metadata(),
                "Storage": {
                    "Type": "filestore",
                    "Path": str(source),
                    "InfoPath": str(info),
                },
            },
        )

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
