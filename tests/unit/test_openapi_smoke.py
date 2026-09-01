from __future__ import annotations

from riverhog_api.app import create_app
from riverhog_api.mappers import map_collection
from riverhog_api.schemas.workflows import (
    OperationIdentityIn,
    ProcessingClaimCreateIn,
    ProcessingClaimPlanSealIn,
)
from riverhog_core.domain.models import CollectionSummary
from riverhog_core.domain.types import CollectionId
from riverhog_protocol import (
    COLLECTION_UPLOAD_FILE_BATCH_MAX,
    RETRIEVAL_FILE_BATCH_MAX,
    canonical_json_sha256,
)
from riverhog_protocol.collection_workflow_transport import (
    WORK_DOCUMENT_MAX_BYTES,
    ExactSetAuthorityDocument,
)
from riverhog_protocol.lifecycle_events import RIVERHOG_EVENT_TYPES


def test_openapi_describes_archive_catalog_and_retrieval_boundaries() -> None:
    paths = create_app().openapi()["paths"]

    assert {
        "/.well-known/resourcesync",
        "/resourcesync/resourcelist.xml",
        "/resourcesync/changelist.xml",
        "/v1/archive/stores",
        "/v1/archive/stores/{store}",
        "/v1/archive/copies",
        "/v1/archive/copies/{collection_id}/{destination_store}",
        "/v1/apps",
        "/v1/apps/{app}/keys",
        "/v1/apps/{app}/keys/{key_id}/access",
        "/v1/apps/{app}/keys/{key_id}/download-quota",
        "/v1/apps/{app}/keys/{key_id}/revoke",
        "/v1/apps/{app}/keys/{key_id}/rotate",
        "/v1/catalog/collections/{collection_id}/inventory",
        "/v1/collections",
        "/v1/collection-upload-sessions",
        "/v1/collection-upload-sessions/{collection_id}",
        "/v1/collection-upload-sessions/{collection_id}/files",
        "/v1/collection-upload-sessions/{collection_id}/complete",
        "/v1/collection-upload-sessions/{collection_id}/cancel",
        "/v1/collection-upload-sessions/{collection_id}/work",
        "/v1/collection-upload-sessions/{collection_id}/volumes/{volume_id}/units/{unit}",
        "/v1/collections/{collection_id}",
        "/v1/collections/{collection_id}/provenance/files",
        "/v1/collections/{collection_id}/provenance/files/{path}",
        "/v1/collections/{collection_id}/provenance/trace/{path}",
        "/v1/collections/{collection_id}/provenance/journals/{journal_id}",
        "/v1/collections/{collection_id}/provenance/journals/{journal_id}/agents",
        "/v1/collections/{collection_id}/provenance/verification",
        "/v1/collections/{collection_id}/deletion-plan",
        "/v1/collections/{collection_id}/delete",
        "/v1/retrieval-plans",
        "/v1/retrieval-jobs",
        "/v1/retrieval-jobs/{job_id}",
        "/v1/retrieval-jobs/{job_id}/content",
        "/v1/retrieval-jobs/{job_id}/ack",
        "/v1/retrieval-jobs/{job_id}/renew",
        "/v1/retrieval-cache",
        "/v1/retrieval-cache/objects",
        "/v1/retrieval-cache/objects/{collection_id}/{source_store}/{object_id}",
        "/v1/download-quota",
        "/v1/download-quotas",
        "/v1/search",
        "/v1/tags",
        "/v1/tags/{tag}",
        "/v1/collections/{collection_id}/tags",
    }.issubset(paths)
    assert "delete" in paths["/v1/retrieval-jobs/{job_id}"]
    assert "delete" in paths["/v1/archive/copies/{collection_id}/{destination_store}"]


def test_retrieval_plan_and_job_schemas_bind_exact_versions() -> None:
    schemas = create_app().openapi()["components"]["schemas"]

    assert set(schemas["RetrievalPlanOut"]["required"]) == {
        "format",
        "lease_seconds",
        "restore_policy",
        "requires_restore",
        "files",
        "objects",
        "etag",
    }
    assert {"id", "state", "plan_etag", "restore_requested_at", "files", "objects"} <= set(
        schemas["RetrievalJobOut"]["required"]
    )
    assert {"lease_seconds", "restore_policy", "requires_restore"} <= set(
        schemas["RetrievalJobOut"]["required"]
    )


def test_provenance_reads_publish_typed_captured_or_omitted_contracts() -> None:
    openapi = create_app().openapi()
    schemas = openapi["components"]["schemas"]
    paths = openapi["paths"]

    assert set(schemas["CapturedFileProvenanceBinding"]["required"]) == {
        "status",
        "journal_id",
        "current_state_id",
    }
    assert set(schemas["OmittedFileProvenanceBinding"]["required"]) == {
        "status",
        "omission_reason",
    }
    assert schemas["CollectionFileProvenanceTraceOut"]["anyOf"] == [
        {"$ref": "#/components/schemas/CapturedCollectionFileProvenanceTraceOut"},
        {"$ref": "#/components/schemas/OmittedCollectionFileProvenanceTraceOut"},
    ]
    verification = schemas["CollectionProvenanceVerificationOut"]
    assert verification["discriminator"]["propertyName"] == "provenance_mode"
    assert {item["$ref"] for item in verification["oneOf"]} == {
        "#/components/schemas/CapturedCollectionProvenanceVerification",
        "#/components/schemas/OmittedCollectionProvenanceVerification",
    }
    listing = schemas["ListCollectionFileProvenanceResponse"]
    assert listing["discriminator"]["propertyName"] == "provenance_mode"
    assert (
        paths["/v1/collections/{collection_id}/provenance/files"]["get"]["responses"]["200"][
            "content"
        ]["application/json"]["schema"]["$ref"]
        == "#/components/schemas/ListCollectionFileProvenanceResponse"
    )
    assert (
        paths["/v1/collections/{collection_id}/provenance/trace/{path}"]["get"]["responses"]["200"][
            "content"
        ]["application/json"]["schema"]["$ref"]
        == "#/components/schemas/CollectionFileProvenanceTraceOut"
    )


def test_collection_upload_provenance_request_is_an_exact_choice() -> None:
    schema = create_app().openapi()["components"]["schemas"][
        "CreateOrResumeCollectionUploadSessionRequest"
    ]

    assert schema["properties"]["provenance_mode"]["enum"] == ["captured", "omitted"]
    assert schema["properties"]["provenance_mode"]["default"] == "captured"
    assert schema["oneOf"] == [
        {
            "properties": {
                "provenance_mode": {"const": "captured"},
                "provenance_omission_reason": {"type": "null"},
            }
        },
        {
            "properties": {
                "provenance_mode": {"const": "omitted"},
                "provenance_omission_reason": {"type": "string"},
            },
            "required": ["provenance_mode", "provenance_omission_reason"],
        },
    ]


def test_collection_deletion_plan_types_the_retirement_evidence_reference() -> None:
    schemas = create_app().openapi()["components"]["schemas"]

    assert schemas["CollectionDeletionPlanOut"]["properties"]["retirement_claim"] == {
        "anyOf": [
            {"$ref": "#/components/schemas/RetirementClaimReferenceDocument"},
            {"type": "null"},
        ]
    }
    reference = schemas["RetirementClaimReferenceDocument"]
    assert reference["additionalProperties"] is False
    assert set(reference["properties"]) == {
        "claim_id",
        "execution_id",
        "fence",
        "outcomes",
        "output_collection_id",
        "work_id",
    }


def test_wire_batches_are_bounded_without_limiting_workflow_cardinality() -> None:
    schemas = create_app().openapi()["components"]["schemas"]

    assert (
        schemas["RegisterCollectionUploadSessionFilesRequest"]["properties"]["files"]["maxItems"]
        == COLLECTION_UPLOAD_FILE_BATCH_MAX
    )
    assert (
        schemas["RetrievalPlanRequest"]["properties"]["files"]["maxItems"]
        == RETRIEVAL_FILE_BATCH_MAX
    )

    work_document = {"format": "fixture-work/v1"}
    claim = ProcessingClaimCreateIn(
        work_id="3" * 64,
        work_document=work_document,
        work_document_sha256=canonical_json_sha256(work_document),
    )
    controller_evidence = {"format": "fixture-controller-evidence/v1"}
    sealed = ProcessingClaimPlanSealIn(
        fence=1,
        execution_id="5" * 64,
        controller_evidence=controller_evidence,
        controller_evidence_sha256=canonical_json_sha256(controller_evidence),
        operation=OperationIdentityIn(id="fixture.operation/v1", sha256="7" * 64),
    )

    logical_authority = ExactSetAuthorityDocument(count=10**100, sha256="8" * 64)
    assert claim.work_id == "3" * 64
    assert sealed.operation.id == "fixture.operation/v1"
    assert logical_authority.count == 10**100


def test_collection_workflow_openapi_uses_exact_riverhog_contract_documents() -> None:
    schemas = create_app().openapi()["components"]["schemas"]

    create = schemas["ProcessingClaimCreateDocument"]["properties"]
    settle = schemas["ProcessingClaimSettleDocument"]["properties"]
    claim = schemas["ProcessingClaimDocument"]["properties"]
    assert "inputs" not in create
    assert create["work_document"] == {
        "additionalProperties": True,
        "type": "object",
        "title": "Work Document",
        "x-riverhog-encoded-bytes-max": WORK_DOCUMENT_MAX_BYTES,
        "x-riverhog-extent": {
            "policy": "contract_max",
            "reason": "bounded-work-document-envelope",
        },
    }
    assert settle["derivation"]["$ref"].endswith("/CollectionDerivationDocument")
    assert claim["plan"]["anyOf"][0]["$ref"].endswith("/ProcessingClaimPlanDocument")
    assert claim["inputs"]["$ref"].endswith("/ReceivingSetDocument")
    assert claim["outcomes"]["$ref"].endswith("/OutcomeSetDocument")
    plan = schemas["ProcessingClaimPlanDocument"]["properties"]
    assert plan["inputs"]["$ref"].endswith("/ExactSetAuthorityDocument")
    assert plan["artifacts"]["$ref"].endswith("/ArtifactSetAuthorityDocument")
    assert plan["output_tags"]["$ref"].endswith("/ExactSetAuthorityDocument")
    assert (
        "stove0"
        not in str(
            {
                name: schema
                for name, schema in schemas.items()
                if name.startswith("ProcessingClaim") or name.startswith("CollectionDerivation")
            }
        ).casefold()
    )


def test_lifecycle_event_openapi_exposes_the_complete_discriminated_vocabulary() -> None:
    document = create_app().openapi()
    schemas = document["components"]["schemas"]
    page = schemas["RiverhogEventPage"]
    event_items = page["properties"]["events"]["items"]
    event_union = schemas[event_items["$ref"].rsplit("/", 1)[-1]]
    operation = document["paths"]["/v1/events"]["get"]
    cursor_parameter = next(
        parameter for parameter in operation["parameters"] if parameter["name"] == "after"
    )
    cursor_reference = next(
        option["$ref"] for option in cursor_parameter["schema"]["anyOf"] if "$ref" in option
    )
    cursor = schemas[cursor_reference.rsplit("/", 1)[-1]]

    assert event_union["discriminator"]["propertyName"] == "type"
    assert set(event_union["discriminator"]["mapping"]) == RIVERHOG_EVENT_TYPES
    assert len(event_union["oneOf"]) == len(RIVERHOG_EVENT_TYPES)
    assert cursor == {
        "type": "string",
        "maxLength": 19,
        "minLength": 1,
        "pattern": r"^(?:0|[1-9][0-9]*)$",
    }
    assert page["required"] == ["events", "next_cursor", "has_more"]
    assert page["properties"]["next_cursor"] == {"$ref": cursor_reference}
    assert set(operation["responses"]["200"]["content"]) == {"application/json"}


def test_retrieval_cache_contract_exposes_indexer_state_and_filters() -> None:
    openapi = create_app().openapi()
    schema = openapi["components"]["schemas"]["RetrievalCacheObjectOut"]
    operation = openapi["paths"]["/v1/retrieval-cache/objects"]["get"]

    assert {
        "collection_id",
        "source_store",
        "object_id",
        "state",
        "stored_bytes",
        "cached_at",
        "verified_at",
        "protected_until",
        "new_archive_expires_at",
        "lease_categories",
    } <= set(schema["required"])
    assert {
        "page_size",
        "page_token",
        "q",
        "tag",
        "collection_id",
        "source_store",
        "state",
        "protection",
        "expires_before",
        "expires_after",
        "sort",
        "order",
    } <= {parameter["name"] for parameter in operation["parameters"]}


def test_collection_upload_contract_exposes_server_planned_plaintext_units() -> None:
    schemas = create_app().openapi()["components"]["schemas"]
    unit = schemas["CollectionUploadUnitWorkDocument"]
    source = schemas["CollectionUploadUnitSourceDocument"]

    assert set(unit["required"]) == {
        "unit",
        "payload_bytes",
        "plaintext_bytes",
        "sources",
        "state",
    }
    assert set(source["required"]) == {"path", "offset", "bytes", "artifact_sha256"}
    assert source["properties"]["artifact_sha256"]


def test_collection_contracts_expose_creation_and_encryption_identities() -> None:
    document = create_app().openapi()
    schemas = document["components"]["schemas"]

    assert "created_at" in schemas["CollectionSummaryOut"]["required"]
    assert "remote_storage_bytes" in schemas["CollectionSummaryOut"]["required"]
    assert "created_at" in schemas["CollectionUploadSessionOut"]["required"]
    assert {
        "archive_phase",
        "archive_phase_updated_at",
        "archive_next_attempt_at",
        "latest_failure",
    } <= set(schemas["CollectionUploadSessionOut"]["properties"])
    assert {
        "archive_phase",
        "archive_phase_updated_at",
        "archive_next_attempt_at",
        "latest_failure",
    } <= set(schemas["CollectionUploadSessionOut"]["required"])
    assert schemas["CollectionUploadSessionOut"]["properties"]["archive_phase"]["enum"] == [
        "planning",
        "uploading",
        "finalization_queued",
        "finalizing",
        "retry_wait",
        "completed",
        "canceled",
        "orphaned",
        "discarding",
    ]
    assert {
        "files_pending",
        "files_partial",
        "files_uploaded",
        "uploaded_bytes",
        "missing_bytes",
    }.isdisjoint(schemas["CollectionUploadSessionOut"]["properties"])
    assert "uploaded_bytes" not in schemas["CollectionUploadListItemOut"]["properties"]
    for schema in (
        "CollectionSummaryOut",
        "CollectionUploadListItemOut",
        "CollectionUploadSessionFilesRegistrationOut",
        "CollectionUploadSessionOut",
    ):
        assert {"encryption_format", "passphrase_id"} <= set(schemas[schema]["required"])
    list_parameters = {
        parameter["name"] for parameter in document["paths"]["/v1/collections"]["get"]["parameters"]
    }
    assert {"encryption_format", "passphrase_id"} <= list_parameters

    mapped = map_collection(
        CollectionSummary(
            id=CollectionId(42),
            created_at="2026-07-26T20:00:00.000000Z",
            tag_count=1,
            content_identity="1" * 64,
            tag_set_identity="3" * 64,
            archive_root_sha256="2" * 64,
            encryption_format="age-v1-scrypt",
            passphrase_id="openapi-test-key-v1",
            files=1,
            bytes=10,
        )
    )
    assert mapped["created_at"] == "2026-07-26T20:00:00.000000Z"


def test_riverhog_application_access_openapi_uses_the_public_permission_and_resource_grammar() -> (
    None
):
    document = create_app().openapi()
    access = document["components"]["schemas"]["ApplicationAccessGrant"]
    schemas = document["components"]["schemas"]
    permission = schemas[access["properties"]["permission"]["$ref"].rsplit("/", 1)[-1]]
    resource = schemas[access["properties"]["resource"]["$ref"].rsplit("/", 1)[-1]]

    assert "catalog:read" in permission["enum"]
    assert resource["pattern"].startswith("^")
    for path, path_item in document["paths"].items():
        if not path.startswith("/v1"):
            continue
        for method, operation in path_item.items():
            if method in {"delete", "get", "patch", "post", "put"}:
                assert operation["x-riverhog-permission-requirements"]
