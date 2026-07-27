from __future__ import annotations

import base64
import json
import os
from pathlib import Path

import httpx
import pytest
from jeb import collector as collector_module
from jeb.collector import Collector, config_from_env
from jeb.ingress import (
    incomplete_tus_upload_status,
    prepare_tus_upload,
    reap_stale_incomplete_tus_uploads,
)


def jeb_env(tmp_path: Path) -> dict[str, str]:
    return {
        "JEB_LANDING_DIR": str(tmp_path / "landing"),
        "JEB_STATE_DIR": str(tmp_path / "state"),
        "JEB_MUNCHY_URL": "http://munchy.invalid",
    }


def collector_for(env: dict[str, str]) -> Collector:
    collector = Collector(config_from_env(env))
    collector.add_source(
        "phone",
        adapters=("tus",),
        target_config={"template_id": "phone-archive"},
        credential="phone-password",
        cadence="manual",
    )
    return collector


def basic_authorization(source: str, password: str) -> str:
    encoded = base64.b64encode(f"{source}:{password}".encode()).decode()
    return f"Basic {encoded}"


def write_upload(
    collector: Collector,
    *,
    size: int,
    offset: int,
    age_seconds: int,
    now: float,
) -> str:
    ingress = collector.config.ingress
    prepared = prepare_tus_upload(
        ingress,
        collector.source_registry,
        authorization=basic_authorization("phone", "phone-password"),
        metadata={"path": f"notes/{offset}.txt"},
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
    collector = collector_for(
        {
            **jeb_env(tmp_path),
            "JEB_TUSD_BASE_URL": "http://tusd.test/files/",
            "JEB_TUS_INCOMPLETE_MAX_AGE": "14d",
        }
    )
    old_id = write_upload(
        collector,
        size=10,
        offset=4,
        age_seconds=15 * 86_400,
        now=now,
    )
    write_upload(
        collector,
        size=10,
        offset=3,
        age_seconds=1 * 86_400,
        now=now,
    )
    write_upload(
        collector,
        size=2,
        offset=2,
        age_seconds=20 * 86_400,
        now=now,
    )
    invalid = collector.config.ingress.tus_staging_dir / f"{'f' * 32}.info"
    invalid.write_text("{}", encoding="utf-8")

    status = incomplete_tus_upload_status(
        collector.config.ingress,
        collector.source_registry,
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
        collector.config.ingress,
        collector.source_registry,
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
    collector = collector_for(jeb_env(tmp_path))
    calls: list[object] = []

    def reap(config, registry):
        calls.append((config, registry))
        return {
            "terminated": 0,
            "already_absent": 0,
            "failed": 0,
            "scan_error": None,
        }

    monkeypatch.setattr(collector_module, "reap_stale_incomplete_tus_uploads", reap)

    collector.run_once()

    assert calls == [(collector.config.ingress, collector.source_registry)]


def test_jeb_tus_cleanup_defaults_are_bounded(tmp_path: Path) -> None:
    config = config_from_env(jeb_env(tmp_path))

    assert config.ingress.tusd_base_url == "http://jeb-tusd:1080/files/"
    assert config.ingress.tus_incomplete_max_age_seconds == 14 * 86_400
    with pytest.raises(ValueError, match="must be positive"):
        config_from_env({**jeb_env(tmp_path), "JEB_TUS_INCOMPLETE_MAX_AGE": "0s"})
