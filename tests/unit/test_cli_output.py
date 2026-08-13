from __future__ import annotations

from jeb_cli_support.output import format_archive_plan, format_attempts, format_status
from riverhog_cli.output import (
    format_archive_store,
    format_collection_summary,
    format_collection_upload,
    format_collection_upload_plan,
    format_collections,
    format_find,
    format_local_collections,
    format_retrieval_cache_status,
)


def test_collection_upload_output_reports_archive_progress() -> None:
    active = format_collection_upload(
        {
            "collection_id": 42,
            "state": "archiving",
            "files_uploaded": 2,
            "files_total": 2,
            "uploaded_bytes": 10,
            "bytes_total": 10,
            "archive_phase": "uploading",
        }
    )
    planned = format_collection_upload_plan(
        {
            "collection_id": 42,
            "files_total": 2,
            "bytes_total": 10,
        }
    )

    assert "archive phase: uploading" in active
    assert "files: 2" in planned


def test_collection_output_leads_with_archive_copy_state() -> None:
    rendered = format_collection_summary(
        {
            "id": 42,
            "created_at": "2026-07-26T18:43:00.000000Z",
            "tags": ["family", "sony-a6700"],
            "files": 2,
            "bytes": 100,
            "archive_copies": [
                {
                    "store": "deep",
                    "state": "uploaded",
                    "storage_class": "DEEP_ARCHIVE",
                }
            ],
        }
    )

    assert "collection 42" in rendered
    assert "created: 2026-07-26T18:43:00.000000Z" in rendered
    assert "tags: family, sony-a6700" in rendered
    assert "archive copies: deep=uploaded" in rendered


def test_retrieval_cache_status_reports_independent_effective_policy() -> None:
    rendered = format_retrieval_cache_status(
        {
            "configured": True,
            "new_archive_enabled": True,
            "objects": 2,
            "stored_bytes": 30,
            "protected_objects": 1,
            "unleased_objects": 1,
            "policy": {
                "new_archive_lease_seconds": 3600,
                "retrieval_default_lease_seconds": 7200,
                "retrieval_max_lease_seconds": 10800,
                "pending_timeout_seconds": 14400,
                "restore_hold_seconds": 18000,
                "sweep_interval_seconds": 30,
                "restore_poll_interval_seconds": 60,
            },
        }
    )

    assert "pending timeout: 14400s" in rendered
    assert "provider restore hold: 18000s" in rendered
    assert "sweep interval: 30s" in rendered
    assert "restore poll interval: 60s" in rendered


def test_list_and_search_output_use_immutable_logical_identity() -> None:
    collections = format_collections(
        {
            "page": 1,
            "pages": 1,
            "total": 1,
            "collections": [
                {
                    "id": 42,
                    "created_at": "2026-07-26T18:43:00.000000Z",
                    "tags": ["family"],
                    "files": 1,
                    "bytes": 10,
                    "archive_copies": [{"store": "deep", "state": "uploaded"}],
                }
            ],
        }
    )
    files = format_find(
        {
            "page": 1,
            "pages": 1,
            "total": 1,
            "files": [
                {
                    "file_ref": "42/a.txt",
                    "bytes": 10,
                }
            ],
        }
    )

    assert "archive=deep=uploaded" in collections
    assert "created=2026-07-26T18:43:00.000000Z" in collections
    assert "tags=family" in collections
    assert "42/a.txt" in files


def test_local_list_output_projects_its_json_model() -> None:
    rendered = format_local_collections(
        {
            "page": 1,
            "pages": 1,
            "total": 1,
            "collections": [
                {
                    "collection_id": 42,
                    "created_at": "2026-07-26T18:43:00.000000Z",
                    "tags": ["family", "sony-a6700"],
                    "status": "desired",
                    "files": 1,
                    "bytes": 10,
                }
            ],
        }
    )

    assert "local collections: 1 (page 1/1)" in rendered
    assert "- 42" in rendered
    assert "status=desired" in rendered
    assert "tags=family,sony-a6700" in rendered


def test_archive_store_output_uses_remote_storage_measurement() -> None:
    rendered = format_archive_store(
        {
            "store": "deep",
            "backend": "aws",
            "storage_class": "DEEP_ARCHIVE",
            "read_mode": "restore_required",
            "read_priority": 2,
            "write_target": False,
            "collections": 2,
            "objects": 14,
            "stored_bytes": 1200,
            "download_allowance": {
                "accounted_bytes": 25_000_000_000,
                "reserved_bytes": 5_000_000_000,
                "effective_limit_bytes": 950_000_000_000,
                "resets_at": "2026-08-01T00:00:00.000000Z",
            },
        }
    )

    assert "stored: 1.2 KB" in rendered
    assert "read priority: 2" in rendered
    assert "download allowance: 25.0 GB used + 5.0 GB reserved / 950.0 GB" in rendered
    assert "download allowance resets: 2026-08-01T00:00:00.000000Z" in rendered


def test_jeb_output_remains_concise() -> None:
    attempts = format_attempts(
        {
            "page": 1,
            "pages": 1,
            "total": 1,
            "attempts": [{"attempt_id": "ja-1", "source_id": "camera", "state": "complete"}],
        }
    )
    status = format_status({"sources": [{"id": "camera", "enabled": True}]})
    plan = format_archive_plan({"source_id": "camera", "file_count": 1})

    assert "ja-1" in attempts
    assert "camera" in status
    assert "Jeb archive plan" in plan
