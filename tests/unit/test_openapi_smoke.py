from __future__ import annotations

from riverhog_api.app import create_app


def test_openapi_describes_collection_archive_and_fetch_boundaries() -> None:
    document = create_app().openapi()
    paths = document["paths"]

    assert {
        "/v1/archive",
        "/v1/archive/copies",
        "/v1/archive-restores",
        "/v1/archive-restores/{archive_restore_id}",
        "/v1/collections",
        "/v1/collections/{collection_id}",
        "/v1/collections/{collection_id}/deletion-plan",
        "/v1/collections/{collection_id}/delete",
        "/v1/fetches",
        "/v1/fetches/{fetch_id}/start",
        "/v1/fetches/{fetch_id}/status",
        "/v1/hot/evict",
        "/v1/search",
    }.issubset(paths)


def test_fetch_start_is_a_single_state_transition() -> None:
    operation = create_app().openapi()["paths"]["/v1/fetches/{fetch_id}/start"]["post"]
    assert operation["responses"]["200"]["description"] == "Successful Response"


def test_archive_restore_schema_is_collection_materialization() -> None:
    schemas = create_app().openapi()["components"]["schemas"]
    restore = schemas["ArchiveRestoreOut"]

    assert set(restore["required"]) >= {
        "id",
        "state",
        "collections",
        "progress",
    }


def test_collection_upload_requests_retain_hot_storage_by_default() -> None:
    schemas = create_app().openapi()["components"]["schemas"]

    assert schemas["CreateOrResumeCollectionUploadRequest"]["properties"]["retain_hot"] == {
        "type": "boolean",
        "title": "Retain Hot",
        "default": True,
    }
    assert (
        schemas["CreateOrResumeCollectionUploadSessionRequest"]["properties"]["retain_hot"][
            "default"
        ]
        is True
    )


def test_collection_file_upload_response_reports_storage_policy() -> None:
    schemas = create_app().openapi()["components"]["schemas"]
    response = schemas["CollectionUploadSessionFileUploadOut"]

    assert response["properties"]["retain_hot"] == {
        "type": "boolean",
        "title": "Retain Hot",
    }
    assert response["properties"]["archive_store"] == {
        "type": "string",
        "title": "Archive Store",
    }
    assert {"retain_hot", "archive_store"} <= set(response["required"])
