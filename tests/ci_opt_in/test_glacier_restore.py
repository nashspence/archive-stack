from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta

import pytest

from arc_core.collection_archives import (
    CollectionArchiveExpectedFile,
    CollectionArchiveFile,
    CollectionArchivePackage,
    build_collection_archive_package,
    verify_collection_archive_files,
    verify_collection_archive_manifest,
    verify_collection_archive_proof,
)
from arc_core.ports.archive_store import CollectionArchiveUploadReceipt
from arc_core.runtime_config import RuntimeConfig, load_runtime_config
from arc_core.stores.s3_archive_store import S3ArchiveStore
from tests.fixtures.data import DOCS_FILES

_RESTORE_CONFIRM = "request-glacier-restore"
_LIVE_COLLECTION_ID = "ci-opt-in-glacier-restore-collection-v1"
pytestmark = [
    pytest.mark.ci_opt_in,
    pytest.mark.requires_aws_s3,
    pytest.mark.requires_glacier_restore,
]


@dataclass(frozen=True)
class _LiveCollectionArchiveFixture:
    collection_id: str
    package: CollectionArchivePackage
    receipt: CollectionArchiveUploadReceipt


def _require_live_restore_confirmation() -> None:
    if os.environ.get("ARC_GLACIER_CI_OPT_IN_RESTORE_CONFIRM") == _RESTORE_CONFIRM:
        return
    pytest.skip(
        "set ARC_GLACIER_CI_OPT_IN_RESTORE_CONFIRM=request-glacier-restore to run live "
        "Glacier restore validation"
    )


def _config() -> RuntimeConfig:
    config = replace(load_runtime_config(), glacier_recovery_restore_mode="aws")
    endpoint = config.glacier_endpoint_url.casefold()
    if config.glacier_backend.casefold() != "aws" and "amazonaws.com" not in endpoint:
        pytest.skip("live Glacier restore validation requires an AWS S3 archive backend")
    return config


def _package() -> CollectionArchivePackage:
    return build_collection_archive_package(
        collection_id=_LIVE_COLLECTION_ID,
        files=tuple(
            CollectionArchiveFile(
                path=path,
                content=content,
                sha256=hashlib.sha256(content).hexdigest(),
            )
            for path, content in sorted(DOCS_FILES.items())
        ),
    )


def _expected_files() -> tuple[CollectionArchiveExpectedFile, ...]:
    return tuple(
        CollectionArchiveExpectedFile(
            path=path,
            bytes=len(content),
            sha256=hashlib.sha256(content).hexdigest(),
        )
        for path, content in sorted(DOCS_FILES.items())
    )


def _prepare_collection_archive(store: S3ArchiveStore) -> _LiveCollectionArchiveFixture:
    package = _package()
    receipt = store.upload_collection_archive_package(
        collection_id=_LIVE_COLLECTION_ID,
        package=package,
    )
    return _LiveCollectionArchiveFixture(
        collection_id=_LIVE_COLLECTION_ID,
        package=package,
        receipt=receipt,
    )


def _request_restore(
    store: S3ArchiveStore,
    fixture: _LiveCollectionArchiveFixture,
) -> str:
    now = datetime.now(UTC)
    requested_at = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    estimated_ready_at = (now + timedelta(hours=48)).strftime("%Y-%m-%dT%H:%M:%SZ")
    status = store.request_collection_archive_restore(
        collection_id=fixture.collection_id,
        object_path=fixture.receipt.archive.object_path,
        manifest_object_path=fixture.receipt.manifest.object_path,
        proof_object_path=fixture.receipt.proof.object_path,
        retrieval_tier=os.environ.get("ARC_GLACIER_CI_OPT_IN_RETRIEVAL_TIER", "bulk"),
        hold_days=int(os.environ.get("ARC_GLACIER_CI_OPT_IN_HOLD_DAYS", "1")),
        requested_at=requested_at,
        estimated_ready_at=estimated_ready_at,
    )
    assert status.state in {"requested", "ready"}
    return status.state


def test_live_aws_collection_restore_request_reports_requested_or_ready() -> None:
    _require_live_restore_confirmation()
    store = S3ArchiveStore(_config())
    fixture = _prepare_collection_archive(store)

    status = _request_restore(store, fixture)

    assert status in {"requested", "ready"}


def test_live_aws_restored_collection_archive_package_verifies() -> None:
    _require_live_restore_confirmation()
    store = S3ArchiveStore(_config())
    fixture = _prepare_collection_archive(store)
    status = _request_restore(store, fixture)
    if status != "ready":
        pytest.skip(
            "live AWS restore was requested, but the uploaded collection archive package "
            "is not readable yet; rerun make ci-opt-in-glacier-restore after AWS completes "
            "the restore"
        )

    manifest_bytes = store.read_restored_collection_archive_manifest(
        collection_id=fixture.collection_id,
        object_path=fixture.receipt.manifest.object_path,
    )
    verify_collection_archive_manifest(
        manifest_bytes=manifest_bytes,
        expected_sha256=fixture.package.manifest_sha256,
        collection_id=fixture.collection_id,
        files=_expected_files(),
    )
    verify_collection_archive_proof(
        proof_bytes=store.read_restored_collection_archive_proof(
            collection_id=fixture.collection_id,
            object_path=fixture.receipt.proof.object_path,
        ),
        expected_sha256=fixture.package.proof_sha256,
        manifest_bytes=manifest_bytes,
    )
    verify_collection_archive_files(
        chunks=store.iter_restored_collection_archive(
            collection_id=fixture.collection_id,
            object_path=fixture.receipt.archive.object_path,
        ),
        files=_expected_files(),
    )
