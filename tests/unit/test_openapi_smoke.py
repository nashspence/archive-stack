from __future__ import annotations

from riverhog_api.app import create_app
from riverhog_api.mappers import map_collection
from riverhog_api.schemas.workflows import (
    CollectionArtifactIdentityIn,
    CollectionRootIdentityIn,
    OperationIdentityIn,
    ProcessingClaimCreateIn,
    ProcessingClaimPlanSealIn,
)
from riverhog_core.domain.models import CollectionSummary
from riverhog_core.domain.types import CollectionId
from riverhog_protocol import COLLECTION_UPLOAD_FILE_BATCH_MAX, RETRIEVAL_FILE_BATCH_MAX


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
        "/v1/catalog/collections/{collection_id}/manifest",
        "/v1/collections",
        "/v1/collection-upload-sessions",
        "/v1/collection-upload-sessions/{collection_id}",
        "/v1/collection-upload-sessions/{collection_id}/files",
        "/v1/collection-upload-sessions/{collection_id}/complete",
        "/v1/collection-upload-sessions/{collection_id}/cancel",
        "/v1/collection-upload-sessions/{collection_id}/volumes",
        "/v1/collection-upload-sessions/{collection_id}/volumes/{volume_id}",
        "/v1/collection-upload-sessions/{collection_id}/volumes/{volume_id}/units/{unit}",
        "/v1/collections/{collection_id}",
        "/v1/collections/{collection_id}/provenance/files",
        "/v1/collections/{collection_id}/provenance/files/{path}",
        "/v1/collections/{collection_id}/provenance/trace/{path}",
        "/v1/collections/{collection_id}/provenance/journals/{journal_id}",
        "/v1/collections/{collection_id}/provenance/verify",
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
    assert "external_state_references" in schemas["CollectionFileProvenanceTraceOut"]["required"]
    assert "provenance_identity" in schemas["CollectionProvenanceVerificationOut"]["properties"]
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

    roots = [
        CollectionRootIdentityIn(
            collection_id=index,
            manifest_sha256="1" * 64,
            content_identity="2" * 64,
        )
        for index in range(1, 1002)
    ]
    claim = ProcessingClaimCreateIn(
        work_id="3" * 64,
        work_document={"format": "fixture-work/v1"},
        work_document_sha256="4" * 64,
        inputs=roots,
    )
    sealed = ProcessingClaimPlanSealIn(
        fence=1,
        execution_id="5" * 64,
        controller_evidence={"format": "fixture-controller-evidence/v1"},
        controller_evidence_sha256="6" * 64,
        operation=OperationIdentityIn(id="fixture.operation/v1", sha256="7" * 64),
        input_artifacts=[
            CollectionArtifactIdentityIn(
                collection=roots[0],
                path="fixture.bin",
                bytes=1,
                sha256="8" * 64,
            )
        ],
        output_tags=[f"fixture/tag-{index:03}" for index in range(101)],
    )

    assert len(claim.inputs) == 1001
    assert len(sealed.output_tags) == 101


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
        "page",
        "per_page",
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
        "all",
    } <= {parameter["name"] for parameter in operation["parameters"]}


def test_collection_upload_contract_exposes_server_planned_plaintext_units() -> None:
    schemas = create_app().openapi()["components"]["schemas"]
    unit = schemas["CollectionUploadUnitOut"]
    source = schemas["CollectionUploadUnitSourceOut"]

    assert set(unit["required"]) == {
        "unit",
        "payload_bytes",
        "plaintext_bytes",
        "sources",
        "state",
    }
    assert set(source["required"]) == {"path", "offset", "bytes", "sha256"}
    assert source["properties"]["sha256"]


def test_collection_contracts_expose_the_stable_creation_timestamp() -> None:
    schemas = create_app().openapi()["components"]["schemas"]

    assert "created_at" in schemas["CollectionSummaryOut"]["required"]
    assert "remote_storage_bytes" in schemas["CollectionSummaryOut"]["required"]
    assert "created_at" in schemas["CollectionUploadSessionOut"]["required"]

    mapped = map_collection(
        CollectionSummary(
            id=CollectionId(42),
            created_at="2026-07-26T20:00:00.000000Z",
            tags=("family",),
            content_identity="1" * 64,
            manifest_sha256="2" * 64,
            files=1,
            bytes=10,
        )
    )
    assert mapped["created_at"] == "2026-07-26T20:00:00.000000Z"
