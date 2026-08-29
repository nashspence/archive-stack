from __future__ import annotations

from copy import deepcopy
from typing import Any

import pytest
from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError as JsonSchemaValidationError
from pydantic import ValidationError
from riverhog_api.app import create_app
from riverhog_api.schemas.apps import AppAccessSetOut, AppKeyOut
from riverhog_api.schemas.archive import (
    ArchiveCopyJobOut,
    ArchiveCopyOut,
    ArchiveCopyRetirementPlanOut,
)
from riverhog_api.schemas.collections import (
    CollectionDeletionPlanOut,
    CollectionUploadDiscardPlanOut,
    CollectionUploadFileOut,
    CollectionUploadListItemOut,
    CollectionUploadSessionFilesRegistrationOut,
    CollectionUploadSessionOut,
)
from riverhog_api.schemas.provenance import (
    CollectionFileProvenanceDetailOut,
    CollectionFileProvenanceTraceOut,
    CollectionProvenanceVerificationOut,
    ListCollectionFileProvenanceResponse,
)
from riverhog_api.schemas.retrieval import RetrievalJobOut, RetrievalPlanFileOut
from riverhog_api.schemas.search import SearchFileOut
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
        "inventory_identity": "etag",
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


def test_upload_discard_readiness_requires_orphaned_custody_but_orphans_may_be_blocked() -> None:
    payload = {
        "status": "ready",
        "collection_id": 1,
        "warning": "warning",
        "expires_at": "2026-08-25T00:00:00.000000Z",
        "challenge": "challenge",
        "state": "orphaned",
        "files": 1,
        "bytes": 0,
        "custody": {"state": "complete"},
        "archive_objects": 1,
        "blockers": [],
    }
    CollectionUploadDiscardPlanOut.model_validate(payload)

    blocked_orphan = deepcopy(payload)
    blocked_orphan.update(
        {
            "status": "blocked",
            "challenge": None,
            "blockers": ["owning processing claim remains active"],
        }
    )
    CollectionUploadDiscardPlanOut.model_validate(blocked_orphan)

    ready_open = deepcopy(payload)
    ready_open["state"] = "open"
    with pytest.raises(ValidationError, match="orphaned custody"):
        CollectionUploadDiscardPlanOut.model_validate(ready_open)


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
        "plan_etag": "a" * 64,
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
        "plan_etag": "a" * 64,
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
                "access": [{"permission": "catalog:read", "resource": "*"}],
                "monthly_download_quota_bytes": None,
                "status": "active",
                "created_at": "2026-08-25T00:00:00.000000Z",
                "expires_at": None,
                "revoked_at": "2026-08-25T00:00:01.000000Z",
                "last_used_at": None,
            }
        )


def test_finalized_upload_sessions_require_immutable_evidence() -> None:
    payload = {
        "collection_id": 1,
        "created_at": "2026-08-25T00:00:00.000000Z",
        "tag_count": 0,
        "ingest_source": None,
        "provenance_mode": "omitted",
        "provenance_identity": None,
        "content_identity": None,
        "archive_root_sha256": None,
        "archive_store": "archive",
        "encryption_format": "age-x25519/v1",
        "passphrase_id": "0123456789abcdef",
        "state": "finalized",
        "custody_mode": "producer-retained",
        "registration_constraints": None,
        "files_total": 0,
        "bytes_total": 0,
        "upload_state_expires_at": None,
        "custody": {"state": "complete"},
        "orphaned_at": None,
        "latest_failure": None,
        "archive_phase": "completed",
        "archive_phase_updated_at": "2026-08-25T00:00:00.000000Z",
        "archive_next_attempt_at": None,
        "collection": None,
    }
    schema_validator = Draft202012Validator(CollectionUploadSessionOut.model_json_schema())
    with pytest.raises(ValidationError, match="immutable collection evidence"):
        CollectionUploadSessionOut.model_validate(payload)
    with pytest.raises(JsonSchemaValidationError):
        schema_validator.validate(payload)

    open_with_final_identity = deepcopy(payload)
    open_with_final_identity.update(
        {
            "state": "open",
            "content_identity": "a" * 64,
            "registration_constraints": {
                "pack_member_bytes": 1,
                "raw_part_plaintext_bytes": 65536,
            },
        }
    )
    with pytest.raises(ValidationError, match="nonfinal"):
        CollectionUploadSessionOut.model_validate(open_with_final_identity)
    with pytest.raises(JsonSchemaValidationError):
        schema_validator.validate(open_with_final_identity)

    open_payload = deepcopy(open_with_final_identity)
    open_payload.update(
        {
            "content_identity": None,
            "archive_phase": "planning",
        }
    )
    CollectionUploadSessionOut.model_validate(open_payload)
    schema_validator.validate(open_payload)

    for state, archive_phase, next_attempt_at, custody_mode, lease in (
        (
            "closing",
            "uploading",
            None,
            "custody-transfer",
            "2026-08-25T00:05:00.000000Z",
        ),
        (
            "finalizing",
            "finalization_queued",
            "2026-08-25T00:00:01.000000Z",
            "producer-retained",
            None,
        ),
    ):
        construction_payload = deepcopy(open_payload)
        construction_payload.update(
            {
                "state": state,
                "archive_phase": archive_phase,
                "archive_next_attempt_at": next_attempt_at,
                "custody_mode": custody_mode,
                "upload_state_expires_at": lease,
            }
        )
        CollectionUploadSessionOut.model_validate(construction_payload)
        schema_validator.validate(construction_payload)

    incomplete_finalizing = deepcopy(open_payload)
    incomplete_finalizing.update(
        {
            "state": "finalizing",
            "archive_phase": "finalization_queued",
            "archive_next_attempt_at": "2026-08-25T00:00:01.000000Z",
            "files_total": 1,
            "bytes_total": 2,
            "custody": {"state": "pending", "files": 0, "bytes": 0},
        }
    )
    with pytest.raises(ValidationError, match="complete Riverhog custody"):
        CollectionUploadSessionOut.model_validate(incomplete_finalizing)

    incomplete_transferred_upload = deepcopy(open_payload)
    incomplete_transferred_upload.update(
        {
            "state": "uploading",
            "custody_mode": "custody-transfer",
            "archive_phase": "uploading",
            "files_total": 1,
            "bytes_total": 2,
            "custody": {"state": "pending", "files": 0, "bytes": 0},
        }
    )
    with pytest.raises(ValidationError, match="complete Riverhog custody"):
        CollectionUploadSessionOut.model_validate(incomplete_transferred_upload)

    producer_retained_upload = deepcopy(incomplete_transferred_upload)
    producer_retained_upload["custody_mode"] = "producer-retained"
    CollectionUploadSessionOut.model_validate(producer_retained_upload)

    closing_transfer = deepcopy(incomplete_transferred_upload)
    closing_transfer.update(
        {
            "state": "closing",
            "upload_state_expires_at": "2026-08-25T00:05:00.000000Z",
        }
    )
    CollectionUploadSessionOut.model_validate(closing_transfer)

    nonfinal_provenance_identity = deepcopy(open_payload)
    nonfinal_provenance_identity["provenance_identity"] = "c" * 64
    with pytest.raises(ValidationError, match="nonfinal.*provenance identity"):
        CollectionUploadSessionOut.model_validate(nonfinal_provenance_identity)
    with pytest.raises(JsonSchemaValidationError):
        schema_validator.validate(nonfinal_provenance_identity)

    nonfinal_mixed = deepcopy(open_payload)
    nonfinal_mixed["provenance_mode"] = "mixed"
    with pytest.raises(ValidationError, match="mixed provenance"):
        CollectionUploadSessionOut.model_validate(nonfinal_mixed)
    with pytest.raises(JsonSchemaValidationError):
        schema_validator.validate(nonfinal_mixed)

    finalized_payload = deepcopy(payload)
    finalized_payload.update(
        {
            "content_identity": "a" * 64,
            "archive_root_sha256": "b" * 64,
            "collection": {
                "id": 1,
                "created_at": "2026-08-25T00:00:00.000000Z",
                "tag_count": 0,
                "content_identity": "a" * 64,
                "archive_root_sha256": "b" * 64,
                "encryption_format": "age-x25519/v1",
                "passphrase_id": "0123456789abcdef",
                "files": 0,
                "bytes": 0,
                "remote_storage_bytes": 0,
                "archive_copy_count": 0,
            },
        }
    )
    CollectionUploadSessionOut.model_validate(finalized_payload)
    schema_validator.validate(finalized_payload)

    incomplete_finalized = deepcopy(finalized_payload)
    incomplete_finalized.update(
        {
            "files_total": 1,
            "bytes_total": 2,
            "custody": {"state": "pending", "files": 0, "bytes": 0},
        }
    )
    incomplete_collection = incomplete_finalized["collection"]
    assert isinstance(incomplete_collection, dict)
    incomplete_collection.update({"files": 1, "bytes": 2})
    with pytest.raises(ValidationError, match="complete Riverhog custody"):
        CollectionUploadSessionOut.model_validate(incomplete_finalized)
    with pytest.raises(JsonSchemaValidationError):
        schema_validator.validate(incomplete_finalized)

    complete_finalized = deepcopy(incomplete_finalized)
    complete_finalized["custody"] = {"state": "complete"}
    CollectionUploadSessionOut.model_validate(complete_finalized)
    schema_validator.validate(complete_finalized)

    for provenance_mode in ("captured", "mixed"):
        missing_provenance_identity = deepcopy(finalized_payload)
        missing_provenance_identity["provenance_mode"] = provenance_mode
        with pytest.raises(ValidationError, match="captured provenance requires its identity"):
            CollectionUploadSessionOut.model_validate(missing_provenance_identity)
        with pytest.raises(JsonSchemaValidationError):
            schema_validator.validate(missing_provenance_identity)

        captured_payload = deepcopy(missing_provenance_identity)
        captured_payload["provenance_identity"] = "c" * 64
        CollectionUploadSessionOut.model_validate(captured_payload)
        schema_validator.validate(captured_payload)

    omitted_with_identity = deepcopy(finalized_payload)
    omitted_with_identity["provenance_identity"] = "c" * 64
    with pytest.raises(ValidationError, match="omitted provenance cannot have an identity"):
        CollectionUploadSessionOut.model_validate(omitted_with_identity)
    with pytest.raises(JsonSchemaValidationError):
        schema_validator.validate(omitted_with_identity)

    for field in ("files_total", "bytes_total"):
        negative_count = deepcopy(open_payload)
        negative_count[field] = -1
        with pytest.raises(ValidationError):
            CollectionUploadSessionOut.model_validate(negative_count)
        with pytest.raises(JsonSchemaValidationError):
            schema_validator.validate(negative_count)

    negative_progress = deepcopy(open_payload)
    negative_progress["custody"] = {"state": "pending", "files": -1, "bytes": 0}
    with pytest.raises(ValidationError):
        CollectionUploadSessionOut.model_validate(negative_progress)
    with pytest.raises(JsonSchemaValidationError):
        schema_validator.validate(negative_progress)

    for progress in (
        {"state": "pending", "files": 2, "bytes": 0},
        {"state": "pending", "files": 1, "bytes": 3},
        {"state": "pending", "files": 0, "bytes": 1},
        {"state": "pending", "files": 1, "bytes": 2},
    ):
        impossible_progress = deepcopy(open_payload)
        impossible_progress.update({"files_total": 1, "bytes_total": 2, "custody": progress})
        with pytest.raises(ValidationError, match="custody"):
            CollectionUploadSessionOut.model_validate(impossible_progress)

    for totals, progress in (
        ((2, 0), {"state": "pending", "files": 1, "bytes": 0}),
        ((2, 10), {"state": "pending", "files": 2, "bytes": 5}),
        ((2, 10), {"state": "pending", "files": 1, "bytes": 10}),
    ):
        partial_progress = deepcopy(open_payload)
        partial_progress.update(
            {
                "files_total": totals[0],
                "bytes_total": totals[1],
                "custody": progress,
            }
        )
        CollectionUploadSessionOut.model_validate(partial_progress)

    invalid_phase = deepcopy(open_payload)
    invalid_phase["archive_phase"] = "completed"
    with pytest.raises(ValidationError, match="archive phase differs"):
        CollectionUploadSessionOut.model_validate(invalid_phase)
    with pytest.raises(JsonSchemaValidationError):
        schema_validator.validate(invalid_phase)

    invalid_retry = deepcopy(open_payload)
    invalid_retry.update({"state": "finalizing", "archive_phase": "retry_wait"})
    with pytest.raises(ValidationError, match="failure and retry schedule"):
        CollectionUploadSessionOut.model_validate(invalid_retry)
    with pytest.raises(JsonSchemaValidationError):
        schema_validator.validate(invalid_retry)

    noncanonical_timestamp = deepcopy(open_payload)
    noncanonical_timestamp["archive_phase_updated_at"] = "2026-08-25T00:00:00Z"
    with pytest.raises(ValidationError, match="pattern|canonical UTC"):
        CollectionUploadSessionOut.model_validate(noncanonical_timestamp)
    with pytest.raises(JsonSchemaValidationError):
        schema_validator.validate(noncanonical_timestamp)


@pytest.mark.parametrize(
    "changes",
    (
        {"custody_mode": "producer-retained", "upload_state_expires_at": "later"},
        {"custody_mode": "producer-retained", "state": "closing"},
        {
            "custody_mode": "producer-retained",
            "state": "orphaned",
            "orphaned_at": "now",
        },
        {"custody_mode": "custody-transfer", "upload_state_expires_at": None},
        {
            "custody_mode": "custody-transfer",
            "state": "orphaned",
            "upload_state_expires_at": "later",
            "orphaned_at": "now",
        },
        {
            "custody_mode": "custody-transfer",
            "state": "uploading",
            "upload_state_expires_at": "later",
        },
    ),
)
def test_upload_session_list_states_reject_impossible_custody_lifecycles(
    changes: dict[str, object],
) -> None:
    payload: dict[str, object] = {
        "collection_id": 1,
        "created_at": "2026-08-25T00:00:00.000000Z",
        "tag_count": 0,
        "ingest_source": None,
        "archive_store": "archive",
        "encryption_format": "age-x25519/v1",
        "passphrase_id": "0123456789abcdef",
        "state": "open",
        "custody_mode": "producer-retained",
        "files": 0,
        "bytes": 0,
        "custody": {"state": "complete"},
        "upload_state_expires_at": None,
        "orphaned_at": None,
    }

    schema_validator = Draft202012Validator(CollectionUploadListItemOut.model_json_schema())
    schema_validator.validate(payload)
    invalid = {**payload, **changes}
    with pytest.raises(ValidationError):
        CollectionUploadListItemOut.model_validate(invalid)
    with pytest.raises(JsonSchemaValidationError):
        schema_validator.validate(invalid)


def test_file_registration_response_has_one_reachable_state() -> None:
    payload = {
        "collection_id": 1,
        "ingest_source": None,
        "archive_store": "archive",
        "encryption_format": "age-x25519/v1",
        "passphrase_id": "0123456789abcdef",
        "state": "open",
        "files": [],
        "volumes": [],
    }
    schema_validator = Draft202012Validator(
        CollectionUploadSessionFilesRegistrationOut.model_json_schema()
    )
    CollectionUploadSessionFilesRegistrationOut.model_validate(payload)
    schema_validator.validate(payload)

    impossible = {**payload, "state": "uploading"}
    with pytest.raises(ValidationError):
        CollectionUploadSessionFilesRegistrationOut.model_validate(impossible)
    with pytest.raises(JsonSchemaValidationError):
        schema_validator.validate(impossible)


@pytest.mark.parametrize(
    "changes",
    (
        {},
        {"custody_mode": "custody-transfer", "upload_state_expires_at": "later"},
        {"custody_mode": "custody-transfer", "state": "uploading"},
        {
            "custody_mode": "custody-transfer",
            "state": "orphaned",
            "orphaned_at": "now",
        },
    ),
)
def test_upload_session_list_states_accept_reachable_custody_lifecycles(
    changes: dict[str, object],
) -> None:
    payload: dict[str, object] = {
        "collection_id": 1,
        "created_at": "2026-08-25T00:00:00.000000Z",
        "tag_count": 0,
        "ingest_source": None,
        "archive_store": "archive",
        "encryption_format": "age-x25519/v1",
        "passphrase_id": "0123456789abcdef",
        "state": "open",
        "custody_mode": "producer-retained",
        "files": 0,
        "bytes": 0,
        "custody": {"state": "complete"},
        "upload_state_expires_at": None,
        "orphaned_at": None,
    }
    current = {**payload, **changes}

    CollectionUploadListItemOut.model_validate(current)
    Draft202012Validator(CollectionUploadListItemOut.model_json_schema()).validate(current)


@pytest.mark.parametrize(
    "changes",
    (
        {"state": "finalizing", "custody_mode": "producer-retained"},
        {"state": "uploading", "custody_mode": "custody-transfer"},
    ),
)
def test_upload_session_list_complete_states_require_complete_custody(
    changes: dict[str, object],
) -> None:
    payload: dict[str, object] = {
        "collection_id": 1,
        "created_at": "2026-08-25T00:00:00.000000Z",
        "tag_count": 0,
        "ingest_source": None,
        "archive_store": "archive",
        "encryption_format": "age-x25519/v1",
        "passphrase_id": "0123456789abcdef",
        "state": "open",
        "custody_mode": "producer-retained",
        "files": 1,
        "bytes": 2,
        "custody": {"state": "pending", "files": 0, "bytes": 0},
        "upload_state_expires_at": None,
        "orphaned_at": None,
    }

    incomplete = {**payload, **changes}
    schema_validator = Draft202012Validator(CollectionUploadListItemOut.model_json_schema())
    with pytest.raises(ValidationError, match="complete Riverhog custody"):
        CollectionUploadListItemOut.model_validate(incomplete)
    with pytest.raises(JsonSchemaValidationError):
        schema_validator.validate(incomplete)

    complete = {
        **payload,
        **changes,
        "custody": {"state": "complete"},
    }
    CollectionUploadListItemOut.model_validate(complete)
    schema_validator.validate(complete)


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


def test_archive_copy_projection_accepts_each_reachable_evidence_state() -> None:
    root = {
        "object_path": "collections/1/archive-root.json",
        "sha256": "a" * 64,
        "proof_object_path": None,
        "proof_sha256": None,
        "proof_state": "pending",
    }
    copies = (
        {
            "store": "archive",
            "state": "uploading",
            "storage_prefix": "collections/1",
            "object_count": 1,
            "stored_bytes": 42,
            "last_uploaded_at": None,
            "last_verified_at": None,
            "failure": None,
            "archive_root": root,
        },
        {
            "store": "archive",
            "state": "uploaded",
            "storage_prefix": "collections/1",
            "object_count": 2,
            "stored_bytes": 84,
            "last_uploaded_at": "2026-08-25T00:00:00.000000Z",
            "last_verified_at": "2026-08-25T00:00:01.000000Z",
            "failure": None,
            "archive_root": {
                **root,
                "proof_object_path": "collections/1/archive-root.proof.json",
                "proof_sha256": "b" * 64,
                "proof_state": "uploaded",
            },
        },
        {
            "store": "archive",
            "state": "failed",
            "storage_prefix": "collections/1",
            "object_count": 1,
            "stored_bytes": 42,
            "last_uploaded_at": "2026-08-25T00:00:00.000000Z",
            "last_verified_at": None,
            "failure": "proof publication failed",
            "archive_root": {**root, "proof_state": "failed"},
        },
    )
    validator = Draft202012Validator(ArchiveCopyOut.model_json_schema())

    for payload in copies:
        assert ArchiveCopyOut.model_validate(payload).root.state == payload["state"]
        validator.validate(payload)

    impossible = {**copies[1], "archive_root": root}
    with pytest.raises(ValidationError):
        ArchiveCopyOut.model_validate(impossible)
    with pytest.raises(JsonSchemaValidationError):
        validator.validate(impossible)


def test_provenance_read_projections_preserve_captured_mixed_and_omitted_truth() -> None:
    journal_id = "urn:uuid:00000000-0000-4000-8000-000000000001"
    state_id = "urn:uuid:00000000-0000-4000-8000-000000000002"
    file_identity = {"collection_id": 1, "path": "camera/clip.mp4", "bytes": 42, "sha256": "a" * 64}
    captured = {
        **file_identity,
        "provenance": {
            "status": "captured",
            "journal_id": journal_id,
            "current_state_id": state_id,
        },
    }
    omitted = {
        **file_identity,
        "path": "camera/omitted.mp4",
        "provenance": {"status": "omitted", "omission_reason": "device did not provide it"},
    }
    page_base = {
        "page": 1,
        "per_page": 2,
        "total": 2,
        "pages": 1,
        "sort": "path",
        "order": "asc",
        "query": None,
        "status": None,
        "collection_id": 1,
    }
    pages = (
        {
            **page_base,
            "provenance_mode": "captured",
            "provenance_identity": "b" * 64,
            "files": [captured],
        },
        {
            **page_base,
            "provenance_mode": "mixed",
            "provenance_identity": "b" * 64,
            "files": [captured, omitted],
        },
        {
            **page_base,
            "provenance_mode": "omitted",
            "provenance_identity": None,
            "files": [omitted],
        },
    )

    for payload in pages:
        assert (
            ListCollectionFileProvenanceResponse.model_validate(payload).root.provenance_mode
            == payload["provenance_mode"]
        )

    journal = {
        "journal_id": journal_id,
        "bytes": 128,
        "sha256": "c" * 64,
        "entries": 1,
        "current_state_id": state_id,
        "current_path": "camera/clip.mp4",
        "current_bytes": 42,
        "current_sha256": "a" * 64,
        "agent_count": 1,
        "entity_counts": {"file": 1},
    }
    assert (
        CollectionFileProvenanceDetailOut.model_validate(
            {**captured, "journal": journal}
        ).root.provenance.status
        == "captured"
    )
    assert (
        CollectionFileProvenanceTraceOut.model_validate(
            {
                **captured,
                "journal": journal,
                "page": 1,
                "per_page": 25,
                "total": 1,
                "pages": 1,
                "items": [{"kind": "journal", "journal": journal}],
            }
        ).root.provenance.status
        == "captured"
    )
    omitted_detail = {**omitted, "journal": None}
    assert CollectionFileProvenanceDetailOut.model_validate(omitted_detail).root.journal is None
    assert (
        CollectionFileProvenanceTraceOut.model_validate(
            {
                **omitted_detail,
                "page": 1,
                "per_page": 25,
                "total": 0,
                "pages": 0,
                "items": [],
            }
        ).root.provenance.status
        == "omitted"
    )
    assert (
        CollectionProvenanceVerificationOut.model_validate(
            {
                "collection_id": 1,
                "valid": True,
                "provenance_mode": "omitted",
                "provenance_identity": None,
                "files": 1,
                "journals": 0,
                "entities": 0,
            }
        ).root.valid
        is True
    )
    assert (
        CollectionProvenanceVerificationOut.model_validate(
            {
                "collection_id": 1,
                "valid": True,
                "provenance_mode": "mixed",
                "provenance_identity": "b" * 64,
                "files": 2,
                "journals": 1,
                "entities": 1,
            }
        ).root.valid
        is True
    )


def test_file_and_access_set_responses_reuse_their_canonical_owners() -> None:
    identity = {"path": "camera/clip.mp4", "bytes": 42, "sha256": "a" * 64}
    assert (
        CollectionUploadFileOut.model_validate(
            {
                **identity,
                "provenance": {
                    "status": "omitted",
                    "omission_reason": "device did not provide it",
                },
            }
        ).path
        == identity["path"]
    )
    assert RetrievalPlanFileOut.model_validate({**identity, "collection_id": 1}).bytes == 42
    assert (
        SearchFileOut.model_validate(
            {**identity, "collection_id": 1, "file_ref": "1/camera/clip.mp4"}
        ).sha256
        == identity["sha256"]
    )
    assert (
        AppAccessSetOut.model_validate(
            {
                "app": "indexer",
                "key_id": "0123456789abcdef",
                "access": [{"permission": "catalog:read", "resource": "*"}],
            }
        )
        .access.root[0]
        .permission
        == "catalog:read"
    )

    upload_file_schema = create_app().openapi()["components"]["schemas"]["CollectionUploadFileOut"]
    assert set(upload_file_schema["properties"]) == {
        "path",
        "bytes",
        "sha256",
        "provenance",
        "custody_receipt",
    }
