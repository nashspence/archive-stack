from __future__ import annotations

import hashlib
import os
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from arc_core.runtime_config import load_runtime_config
from arc_core.stores.s3_archive_store import S3ArchiveStore

_RESTORE_CONFIRM = "request-glacier-restore"


def _required_env(name: str, *, reason: str) -> str:
    value = os.environ.get(name)
    if value:
        return value
    pytest.skip(f"{name} is required for {reason}")


def _require_live_restore_confirmation() -> None:
    if os.environ.get("ARC_GLACIER_GATED_RESTORE_CONFIRM") == _RESTORE_CONFIRM:
        return
    pytest.skip(
        "set ARC_GLACIER_GATED_RESTORE_CONFIRM=request-glacier-restore to run live "
        "Glacier restore validation"
    )


def _store() -> S3ArchiveStore:
    config = replace(load_runtime_config(), glacier_recovery_restore_mode="aws")
    endpoint = config.glacier_endpoint_url.casefold()
    if config.glacier_backend.casefold() != "aws" and "amazonaws.com" not in endpoint:
        pytest.skip("live Glacier restore validation requires an AWS S3 archive backend")
    return S3ArchiveStore(config)


def test_live_aws_restore_request_reports_requested_or_ready() -> None:
    _require_live_restore_confirmation()
    object_path = _required_env(
        "ARC_GLACIER_GATED_OBJECT_PATH",
        reason="live Glacier restore request validation",
    )
    image_id = os.environ.get("ARC_GLACIER_GATED_IMAGE_ID", "gated-glacier-restore")
    now = datetime.now(UTC)
    requested_at = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    estimated_ready_at = (now + timedelta(hours=48)).strftime("%Y-%m-%dT%H:%M:%SZ")

    status = _store().request_finalized_image_restore(
        image_id=image_id,
        object_path=object_path,
        retrieval_tier=os.environ.get("ARC_GLACIER_GATED_RETRIEVAL_TIER", "bulk"),
        hold_days=int(os.environ.get("ARC_GLACIER_GATED_HOLD_DAYS", "1")),
        requested_at=requested_at,
        estimated_ready_at=estimated_ready_at,
    )

    assert status.state in {"requested", "ready"}


def test_live_aws_restored_iso_stream_matches_expected_sha256() -> None:
    object_path = _required_env(
        "ARC_GLACIER_GATED_RESTORED_OBJECT_PATH",
        reason="live restored ISO download validation",
    )
    expected_sha256 = _required_env(
        "ARC_GLACIER_GATED_RESTORED_SHA256",
        reason="live restored ISO download validation",
    )
    image_id = os.environ.get("ARC_GLACIER_GATED_IMAGE_ID", "gated-glacier-restore")

    digest = hashlib.sha256()
    for chunk in _store().iter_restored_finalized_image(
        image_id=image_id,
        object_path=object_path,
    ):
        digest.update(chunk)

    assert digest.hexdigest() == expected_sha256
