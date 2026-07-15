from __future__ import annotations

from riverhog_cli.output import (
    format_archive_report,
    format_archive_restore,
    format_collection_summary,
    format_collection_upload,
    format_collection_upload_plan,
    format_collections,
    format_fetch,
    format_fetches,
    format_find,
    format_hot_evict,
    format_jeb_archive_plan,
    format_jeb_attempts,
    format_jeb_status,
)


def test_collection_upload_output_reports_hot_retention_choice() -> None:
    archive_only = format_collection_upload(
        {"collection_id": "2026/20260101T000000Z__docs", "retain_hot": False}
    )
    retained = format_collection_upload_plan(
        {"collection_id": "2026/20260101T000000Z__docs", "retain_hot": True}
    )

    assert "hot storage: archive only" in archive_only
    assert "hot storage: retained" in retained


def test_collection_output_leads_with_archive_and_hot_state() -> None:
    rendered = format_collection_summary(
        {
            "id": "2025/20250102T030405Z__docs",
            "files": 2,
            "bytes": 100,
            "hot_bytes": 60,
            "archive": {"state": "uploaded", "storage_class": "DEEP_ARCHIVE"},
        }
    )

    assert "collection 2025/20250102T030405Z__docs" in rendered
    assert "hot: 60 B" in rendered
    assert "archive: uploaded" in rendered


def test_list_and_search_output_use_logical_paths_and_collection_ids() -> None:
    collections = format_collections(
        {
            "page": 1,
            "pages": 1,
            "total": 1,
            "collections": [
                {
                    "id": "2025/20250102T030405Z__docs",
                    "files": 1,
                    "bytes": 10,
                    "hot_bytes": 10,
                    "archive": {"state": "uploaded"},
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
                    "logical_path": "2025/20250102T030405Z__docs/a.txt",
                    "bytes": 10,
                    "hot": True,
                }
            ],
        }
    )

    assert "2025/20250102T030405Z__docs" in collections and "archive=uploaded" in collections
    assert "2025/20250102T030405Z__docs/a.txt" in files and "hot=true" in files


def test_fetch_output_explains_archive_progress() -> None:
    payload = {
        "id": "fx-1",
        "name": "tax documents",
        "state": "restoring_archive",
        "files": 2,
        "bytes": 100,
        "hot_files": 1,
        "hot_bytes": 40,
        "missing_files": 1,
        "missing_bytes": 60,
        "collections": ["2025/20250102T030405Z__docs"],
        "next_action": {"action": "wait", "reason": "archive materialization is in progress"},
        "archive_restores": {"total": 1},
    }

    assert "restoring_archive" in format_fetch(payload)
    assert "archive materialization" in format_fetch(payload)
    assert "fx-1" in format_fetches({"page": 1, "pages": 1, "total": 1, "fetches": [payload]})


def test_archive_output_reports_remote_storage_and_materialization() -> None:
    report = format_archive_report(
        {
            "scope": "all",
            "totals": {
                "collections": 2,
                "uploaded_collections": 2,
                "measured_storage_bytes": 1200,
            },
        }
    )
    restore = format_archive_restore(
        {"id": "ar-docs-1", "state": "completed", "latest_message": "files materialized"}
    )

    assert "remote storage: 1.2 KB" in report
    assert "files materialized" in restore


def test_hot_evict_output_reports_selected_and_affected_bytes() -> None:
    rendered = format_hot_evict(
        {
            "status": "evicted",
            "files": 2,
            "bytes": 20,
            "would_evict_files": 1,
            "would_evict_bytes": 10,
        }
    )
    assert "selected: 2" in rendered
    assert "affected: 1" in rendered


def test_jeb_output_remains_concise() -> None:
    attempts = format_jeb_attempts(
        {
            "page": 1,
            "pages": 1,
            "total": 1,
            "attempts": [{"attempt_id": "ja-1", "account_id": "camera", "state": "complete"}],
        }
    )
    status = format_jeb_status({"accounts": [{"account_id": "camera", "state": "ready"}]})
    plan = format_jeb_archive_plan({"account_id": "camera", "collections_total": 1})

    assert "ja-1" in attempts
    assert "camera" in status
    assert "Jeb archive plan" in plan
