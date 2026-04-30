from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from arc_core.domain.enums import GlacierState
from arc_core.domain.models import GlacierArchiveStatus
from arc_core.domain.types import ImageId
from arc_core.runtime_config import RuntimeConfig, load_runtime_config
from arc_core.stores.s3_archive_store import ISO_SHA256_METADATA, S3ArchiveStore
from arc_core.stores.s3_support import create_glacier_s3_client
from tests.fixtures.acceptance import AcceptanceSystem
from tests.fixtures.data import IMAGE_ID

_RESTORE_CONFIRM = "request-glacier-restore"
_LIVE_ARCHIVE_IMAGE_ID = "gated-glacier-restore-api-v2"


@dataclass(frozen=True)
class _LiveArchiveFixture:
    image_id: str
    session_id: str
    object_path: str
    expected_sha256: str


def _require_live_restore_confirmation() -> None:
    if os.environ.get("ARC_GLACIER_GATED_RESTORE_CONFIRM") == _RESTORE_CONFIRM:
        return
    pytest.skip(
        "set ARC_GLACIER_GATED_RESTORE_CONFIRM=request-glacier-restore to run live "
        "Glacier restore validation"
    )


def _config() -> RuntimeConfig:
    config = replace(load_runtime_config(), glacier_recovery_restore_mode="aws")
    endpoint = config.glacier_endpoint_url.casefold()
    if config.glacier_backend.casefold() != "aws" and "amazonaws.com" not in endpoint:
        pytest.skip("live Glacier restore validation requires an AWS S3 archive backend")
    return config


def _store(config: RuntimeConfig) -> S3ArchiveStore:
    return S3ArchiveStore(config)


def _gated_retrieval_tier() -> str:
    return os.environ.get("ARC_GLACIER_GATED_RETRIEVAL_TIER", "bulk")


def _gated_hold_days() -> int:
    return int(os.environ.get("ARC_GLACIER_GATED_HOLD_DAYS", "1"))


def _prepare_fake_backed_recovery_session(
    system: AcceptanceSystem,
    *,
    config: RuntimeConfig,
    store: S3ArchiveStore,
) -> _LiveArchiveFixture:
    system.seed_planner_fixtures()
    response = system.request("POST", f"/v1/plan/candidates/{IMAGE_ID}/finalize")
    assert response.status_code == 200, response.text
    image_id = response.json()["id"]
    system.wait_for_image_glacier_state(image_id, "uploaded")
    with system.state.lock:
        image = system.state.finalized_images_by_id[ImageId(image_id)]
        image_root = image.image_root
    receipt = store.upload_finalized_image(
        image_id=_LIVE_ARCHIVE_IMAGE_ID,
        filename=f"{_LIVE_ARCHIVE_IMAGE_ID}.iso",
        image_root=image_root,
    )
    expected_sha256 = _uploaded_iso_sha256(config, receipt.object_path)
    current_text = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    with system.state.lock:
        original = system.state.glacier_status(image_id)
        system.state.glacier_status_by_image[ImageId(image_id)] = GlacierArchiveStatus(
            state=GlacierState.UPLOADED,
            object_path=receipt.object_path,
            stored_bytes=receipt.stored_bytes or original.stored_bytes,
            backend="aws",
            storage_class=receipt.storage_class,
            last_uploaded_at=receipt.uploaded_at or original.last_uploaded_at or current_text,
            last_verified_at=receipt.verified_at or original.last_verified_at or current_text,
            failure=None,
        )

    for copy_id, state in ((f"{image_id}-1", "lost"), (f"{image_id}-2", "damaged")):
        response = system.request(
            "POST",
            f"/v1/images/{image_id}/copies",
            json_body={"copy_id": copy_id, "location": f"gated fixture shelf {copy_id}"},
        )
        assert response.status_code == 200, response.text
        response = system.request(
            "PATCH",
            f"/v1/images/{image_id}/copies/{copy_id}",
            json_body={"state": state},
        )
        assert response.status_code == 200, response.text
    session = system.recovery_sessions.get_for_image(image_id)
    return _LiveArchiveFixture(
        image_id=image_id,
        session_id=str(session.id),
        object_path=receipt.object_path,
        expected_sha256=expected_sha256,
    )


def _uploaded_iso_sha256(config: RuntimeConfig, object_path: str) -> str:
    client = create_glacier_s3_client(config)
    head = client.head_object(Bucket=config.glacier_bucket, Key=object_path)
    metadata = head.get("Metadata", {})
    if not isinstance(metadata, dict):
        raise AssertionError(f"uploaded Glacier object is missing metadata: {object_path}")
    normalized = {str(key).lower(): str(value) for key, value in metadata.items()}
    expected_sha256 = normalized.get(ISO_SHA256_METADATA)
    if expected_sha256 is None:
        raise AssertionError(f"uploaded Glacier object is missing sha256 metadata: {object_path}")
    return expected_sha256


def _enable_live_recovery_archive_store(
    system: AcceptanceSystem,
    *,
    store: S3ArchiveStore,
) -> None:
    system.enable_live_recovery_archive_store(
        store,
        retrieval_tier=_gated_retrieval_tier(),
        hold_days=_gated_hold_days(),
        poll_interval_seconds=0.0,
    )


def _download_recovered_iso_sha256(
    system: AcceptanceSystem,
    *,
    session_id: str,
    image_id: str,
) -> str:
    digest = hashlib.sha256()
    with httpx.Client(base_url=system.base_url, timeout=120.0) as client:
        with client.stream(
            "GET",
            f"/v1/recovery-sessions/{session_id}/images/{image_id}/iso",
        ) as response:
            assert response.status_code == 200, response.text
            for chunk in response.iter_bytes():
                digest.update(chunk)
    return digest.hexdigest()


def _request_restore(
    store: S3ArchiveStore,
    *,
    image_id: str,
    object_path: str,
) -> str:
    now = datetime.now(UTC)
    requested_at = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    estimated_ready_at = (now + timedelta(hours=48)).strftime("%Y-%m-%dT%H:%M:%SZ")
    status = store.request_finalized_image_restore(
        image_id=image_id,
        object_path=object_path,
        retrieval_tier=_gated_retrieval_tier(),
        hold_days=_gated_hold_days(),
        requested_at=requested_at,
        estimated_ready_at=estimated_ready_at,
    )
    assert status.state in {"requested", "ready"}
    return status.state


def _wait_for_ready_or_skip(system: AcceptanceSystem, *, session_id: str) -> None:
    try:
        system.wait_for_recovery_session_state(session_id, "ready", timeout=10.0)
    except AssertionError:
        pytest.skip(
            "live AWS restore was requested, but the uploaded archive object is not readable yet; "
            "rerun make gated-glacier-restore after AWS completes the restore"
        )


def test_live_aws_restore_request_reports_requested_or_ready(tmp_path: Path) -> None:
    _require_live_restore_confirmation()
    config = _config()
    store = _store(config)
    system = AcceptanceSystem.create(tmp_path / "acceptance-system")
    try:
        fixture = _prepare_fake_backed_recovery_session(
            system,
            config=config,
            store=store,
        )
        status = _request_restore(
            store,
            image_id=_LIVE_ARCHIVE_IMAGE_ID,
            object_path=fixture.object_path,
        )
    finally:
        system.close()

    assert status in {"requested", "ready"}


def test_live_aws_recovery_session_api_requests_restore_with_fake_backed_harness(
    tmp_path: Path,
) -> None:
    _require_live_restore_confirmation()
    config = _config()
    store = _store(config)
    system = AcceptanceSystem.create(tmp_path / "acceptance-system")
    try:
        fixture = _prepare_fake_backed_recovery_session(
            system,
            config=config,
            store=store,
        )
        _enable_live_recovery_archive_store(system, store=store)

        response = system.request("POST", f"/v1/recovery-sessions/{fixture.session_id}/approve")
        assert response.status_code == 200, response.text
        payload = response.json()
        assert payload["state"] == "restore_requested"
        assert payload["restore_requested_at"] is not None
        assert payload["images"][0]["id"] == fixture.image_id
        assert payload["images"][0]["glacier"]["object_path"] == fixture.object_path
    finally:
        system.close()


def test_live_aws_restored_iso_stream_matches_uploaded_sha256(tmp_path: Path) -> None:
    _require_live_restore_confirmation()
    config = _config()
    store = _store(config)
    system = AcceptanceSystem.create(tmp_path / "acceptance-system")
    try:
        fixture = _prepare_fake_backed_recovery_session(
            system,
            config=config,
            store=store,
        )
        status = _request_restore(
            store,
            image_id=_LIVE_ARCHIVE_IMAGE_ID,
            object_path=fixture.object_path,
        )
        if status != "ready":
            pytest.skip(
                "live AWS restore was requested, but the uploaded archive object is not readable "
                "yet; rerun make gated-glacier-restore after AWS completes the restore"
            )

        digest = hashlib.sha256()
        for chunk in store.iter_restored_finalized_image(
            image_id=_LIVE_ARCHIVE_IMAGE_ID,
            object_path=fixture.object_path,
        ):
            digest.update(chunk)
    finally:
        system.close()

    assert digest.hexdigest() == fixture.expected_sha256


def test_live_aws_recovery_session_api_downloads_restored_iso_and_completes(
    tmp_path: Path,
) -> None:
    _require_live_restore_confirmation()
    config = _config()
    store = _store(config)
    system = AcceptanceSystem.create(tmp_path / "acceptance-system")
    try:
        fixture = _prepare_fake_backed_recovery_session(
            system,
            config=config,
            store=store,
        )
        _enable_live_recovery_archive_store(system, store=store)

        response = system.request("POST", f"/v1/recovery-sessions/{fixture.session_id}/approve")
        assert response.status_code == 200, response.text
        _wait_for_ready_or_skip(system, session_id=fixture.session_id)
        assert (
            _download_recovered_iso_sha256(
                system,
                session_id=fixture.session_id,
                image_id=fixture.image_id,
            )
            == fixture.expected_sha256
        )

        response = system.request("POST", f"/v1/recovery-sessions/{fixture.session_id}/complete")
        assert response.status_code == 200, response.text
        assert response.json()["state"] == "completed"
    finally:
        system.close()
