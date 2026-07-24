from __future__ import annotations

from riverhog_api.app import create_app


def test_openapi_describes_custody_catalog_and_retrieval_boundaries() -> None:
    paths = create_app().openapi()["paths"]

    assert {
        "/.well-known/resourcesync",
        "/resourcesync/resourcelist.xml",
        "/resourcesync/changelist.xml",
        "/v1/archive",
        "/v1/archive/copies",
        "/v1/apps",
        "/v1/apps/{app}/keys",
        "/v1/apps/{app}/keys/{key_id}/collection-grants",
        "/v1/apps/{app}/keys/{key_id}/download-quota",
        "/v1/apps/{app}/keys/{key_id}/revoke",
        "/v1/apps/{app}/keys/{key_id}/rotate",
        "/v1/catalog/collections/{collection_id}/manifest",
        "/v1/collections",
        "/v1/collections/{collection_id}",
        "/v1/collections/{collection_id}/deletion-plan",
        "/v1/collections/{collection_id}/delete",
        "/v1/retrieval-plans",
        "/v1/retrieval-jobs",
        "/v1/retrieval-jobs/{job_id}",
        "/v1/retrieval-jobs/{job_id}/content",
        "/v1/retrieval-jobs/{job_id}/objects/{object_id}/content",
        "/v1/retrieval-jobs/{job_id}/ack",
        "/v1/download-quota",
        "/v1/download-quotas",
        "/v1/search",
    }.issubset(paths)
    assert "delete" in paths["/v1/retrieval-jobs/{job_id}"]


def test_retrieval_plan_and_job_schemas_bind_exact_versions() -> None:
    schemas = create_app().openapi()["components"]["schemas"]

    assert set(schemas["RetrievalPlanOut"]["required"]) == {
        "format",
        "lease_seconds",
        "files",
        "objects",
        "etag",
    }
    assert {"id", "state", "plan_etag", "files", "objects"} <= set(
        schemas["RetrievalJobOut"]["required"]
    )


def test_collection_file_preflight_requires_client_side_encryption() -> None:
    schemas = create_app().openapi()["components"]["schemas"]
    encryption = schemas["CollectionUploadEncryptionOut"]
    response = schemas["CollectionUploadSessionFileUploadOut"]

    assert encryption["properties"]["format"]["const"] == "age-v1-scrypt-resumable"
    assert encryption["properties"]["passphrase"]["writeOnly"] is True
    assert {"encryption", "length", "offset", "archive_store"} <= set(response["required"])
