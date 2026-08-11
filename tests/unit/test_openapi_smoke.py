from __future__ import annotations

from riverhog_api.app import create_app
from riverhog_api.mappers import map_collection
from riverhog_core.domain.models import CollectionSummary
from riverhog_core.domain.types import CollectionId


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
        "files",
        "objects",
        "etag",
    }
    assert {"id", "state", "plan_etag", "restore_requested_at", "files", "objects"} <= set(
        schemas["RetrievalJobOut"]["required"]
    )


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
            files=1,
            bytes=10,
        )
    )
    assert mapped["created_at"] == "2026-07-26T20:00:00.000000Z"
