from __future__ import annotations

from fastapi.testclient import TestClient

from arc_api.app import create_app

REQUIRED_PATHS = {
    "/v1/collections/close",
    "/v1/collections/{collection_id}",
    "/v1/search",
    "/v1/plan",
    "/v1/images",
    "/v1/images/{image_id}",
    "/v1/plan/candidates/{candidate_id}/finalize",
    "/v1/images/{image_id}/iso",
    "/v1/images/{image_id}/copies",
    "/v1/pin",
    "/v1/release",
    "/v1/pins",
    "/v1/fetches/{fetch_id}",
    "/v1/fetches/{fetch_id}/manifest",
    "/v1/fetches/{fetch_id}/entries/{entry_id}/upload",
    "/v1/fetches/{fetch_id}/complete",
}


def test_openapi_contains_required_paths() -> None:
    app = create_app()
    client = TestClient(app)
    data = client.get("/openapi.json").json()
    assert REQUIRED_PATHS.issubset(set(data["paths"].keys()))
