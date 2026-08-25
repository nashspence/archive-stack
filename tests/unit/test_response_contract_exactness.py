from __future__ import annotations

from copy import deepcopy
from typing import Any

import pytest
from pydantic import ValidationError
from riverhog_api.app import create_app
from riverhog_api.schemas.apps import AppKeyOut
from riverhog_api.schemas.archive import ArchiveCopyJobOut, ArchiveCopyRetirementPlanOut
from riverhog_api.schemas.collections import CollectionDeletionPlanOut, CollectionUploadSessionOut
from riverhog_api.schemas.retrieval import RetrievalJobOut
from riverhog_api.schemas.tags import TagDeletionPlanOut


def _collection_deletion(status: str, challenge: str | None, blockers: list[str]) -> dict[str, Any]:
    return {
        "status": status,
        "collection_id": 1,
        "warning": "warning",
        "expires_at": "2026-08-25T00:00:00.000000Z",
        "challenge": challenge,
        "file_count": 1,
        "bytes": 1,
        "archive_copies": [],
        "archive_object_count": 0,
        "remote_storage_bytes": 0,
        "upload_file_count": 0,
        "record_etag": "etag",
        "metadata_rows": {},
        "blockers": blockers,
        "billing_note": "billing",
    }


def _tag_deletion(status: str, challenge: str | None, blockers: list[str]) -> dict[str, Any]:
    dependency = {"count": 0, "sample": [], "truncated": False}
    return {
        "status": status,
        "tag": "incoming",
        "warning": "warning",
        "expires_at": "2026-08-25T00:00:00.000000Z",
        "challenge": challenge,
        "dependencies": {
            "collections": dependency,
            "upload_sessions": dependency,
            "app_key_access": dependency,
            "metadata_publications": dependency,
        },
        "blockers": blockers,
    }


def _retirement(status: str, challenge: str | None, blockers: list[str]) -> dict[str, Any]:
    return {
        "status": status,
        "collection_id": 1,
        "store": "archive",
        "warning": "warning",
        "expires_at": "2026-08-25T00:00:00.000000Z",
        "challenge": challenge,
        "target_copy": {
            "store": "archive",
            "last_verified_at": "2026-08-25T00:00:00.000000Z",
            "remote_storage_bytes": 1,
            "object_count": 1,
        },
        "retained_copies": [],
        "retired_retrieval_job_count": 0,
        "blockers": blockers,
        "verification_note": "verification",
        "billing_note": "billing",
    }


@pytest.mark.parametrize(
    ("model", "payload"),
    (
        (CollectionDeletionPlanOut, _collection_deletion("ready", None, [])),
        (CollectionDeletionPlanOut, _collection_deletion("blocked", "challenge", ["busy"])),
        (TagDeletionPlanOut, _tag_deletion("ready", None, [])),
        (TagDeletionPlanOut, _tag_deletion("blocked", "challenge", ["busy"])),
        (ArchiveCopyRetirementPlanOut, _retirement("ready", None, [])),
        (ArchiveCopyRetirementPlanOut, _retirement("blocked", "challenge", ["busy"])),
    ),
)
def test_destructive_plans_reject_impossible_challenge_blocker_states(
    model: type[Any],
    payload: dict[str, Any],
) -> None:
    with pytest.raises(ValidationError):
        model.model_validate(payload)


def test_terminal_job_responses_require_their_evidence() -> None:
    archive_job = {
        "collection_id": 1,
        "source_store": "archive",
        "destination_store": "replica",
        "initiated_by_app": "operator",
        "initiated_by_key_id": None,
        "state": "completed",
        "requested_at": None,
        "ready_at": None,
        "expires_at": None,
        "completed_at": None,
        "failure": None,
    }
    retrieval_job = {
        "id": "job",
        "state": "failed",
        "plan_etag": "etag",
        "created_at": "2026-08-25T00:00:00.000000Z",
        "requested_at": None,
        "restore_requested_at": None,
        "ready_at": None,
        "expires_at": None,
        "completed_at": None,
        "canceled_at": None,
        "failure": None,
        "lease_seconds": 1,
        "restore_policy": "allow",
        "requires_restore": False,
        "files": [],
        "objects": [],
    }

    with pytest.raises(ValidationError, match="completed_at"):
        ArchiveCopyJobOut.model_validate(archive_job)
    with pytest.raises(ValidationError, match="failure evidence"):
        RetrievalJobOut.model_validate(retrieval_job)


def test_operational_responses_reject_contradictory_state_evidence() -> None:
    archive_job = {
        "collection_id": 1,
        "source_store": "archive",
        "destination_store": "replica",
        "initiated_by_app": "operator",
        "initiated_by_key_id": None,
        "state": "requested",
        "requested_at": "2026-08-25T00:00:00.000000Z",
        "ready_at": None,
        "expires_at": None,
        "completed_at": "2026-08-25T00:00:01.000000Z",
        "failure": None,
    }
    with pytest.raises(ValidationError, match="completed_at"):
        ArchiveCopyJobOut.model_validate(archive_job)

    retrieval_job = {
        "id": "job",
        "state": "ready",
        "plan_etag": "etag",
        "created_at": "2026-08-25T00:00:00.000000Z",
        "requested_at": "2026-08-25T00:00:00.000000Z",
        "restore_requested_at": None,
        "ready_at": "2026-08-25T00:00:01.000000Z",
        "expires_at": "2026-08-25T01:00:00.000000Z",
        "completed_at": None,
        "canceled_at": "2026-08-25T00:00:02.000000Z",
        "failure": None,
        "lease_seconds": 3600,
        "restore_policy": "allow",
        "requires_restore": False,
        "files": [],
        "objects": [],
    }
    with pytest.raises(ValidationError, match="canceled_at"):
        RetrievalJobOut.model_validate(retrieval_job)

    retryable = deepcopy(retrieval_job)
    retryable.update(
        {
            "state": "requested",
            "ready_at": None,
            "expires_at": None,
            "canceled_at": None,
            "failure": "restore provider is temporarily unavailable",
        }
    )
    assert RetrievalJobOut.model_validate(retryable).failure is not None

    with pytest.raises(ValidationError, match="revoked_at"):
        AppKeyOut.model_validate(
            {
                "id": "0123456789abcdef",
                "app": "review",
                "access": [],
                "monthly_download_quota_bytes": None,
                "status": "active",
                "created_at": "2026-08-25T00:00:00.000000Z",
                "expires_at": None,
                "revoked_at": "2026-08-25T00:00:01.000000Z",
                "last_used_at": None,
            }
        )


def test_finalized_and_failed_upload_sessions_require_terminal_evidence() -> None:
    payload = {
        "collection_id": 1,
        "created_at": "2026-08-25T00:00:00.000000Z",
        "tags": [],
        "ingest_source": None,
        "provenance_mode": "omitted",
        "provenance_identity": None,
        "content_identity": None,
        "archive_root_sha256": None,
        "archive_store": "archive",
        "encryption_format": "age-x25519/v1",
        "passphrase_id": "0123456789abcdef",
        "state": "finalized",
        "registration_constraints": None,
        "files_total": 0,
        "files_pending": 0,
        "files_partial": 0,
        "files_uploaded": 0,
        "bytes_total": 0,
        "uploaded_bytes": 0,
        "missing_bytes": 0,
        "upload_state_expires_at": None,
        "collection": None,
    }
    with pytest.raises(ValidationError, match="immutable collection evidence"):
        CollectionUploadSessionOut.model_validate(payload)

    failed = deepcopy(payload)
    failed.update(
        {
            "state": "failed",
            "registration_constraints": {
                "pack_member_bytes": 1,
                "raw_part_plaintext_bytes": 65536,
            },
        }
    )
    with pytest.raises(ValidationError, match="failure evidence"):
        CollectionUploadSessionOut.model_validate(failed)

    open_with_final_identity = deepcopy(failed)
    open_with_final_identity.update(
        {
            "state": "open",
            "content_identity": "a" * 64,
            "latest_failure": None,
        }
    )
    with pytest.raises(ValidationError, match="nonfinal"):
        CollectionUploadSessionOut.model_validate(open_with_final_identity)


def test_list_responses_expose_only_closed_sort_and_filter_contracts() -> None:
    schemas = create_app().openapi()["components"]["schemas"]
    list_schemas = {
        name: schema for name, schema in schemas.items() if "sort" in schema.get("properties", {})
    }

    assert list_schemas
    for name, schema in list_schemas.items():
        sort_schema = schema["properties"]["sort"]
        assert "enum" in sort_schema or "$ref" in sort_schema, name
        filters = schema["properties"].get("filters")
        if filters is not None:
            assert filters.get("additionalProperties") is not True, name
