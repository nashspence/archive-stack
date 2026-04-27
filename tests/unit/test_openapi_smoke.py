from __future__ import annotations

from fastapi.testclient import TestClient

import arc_api.app as arc_app
from arc_api.app import create_app

REQUIRED_PATHS = {
    "/v1/collection-uploads",
    "/v1/collection-uploads/{collection_id}",
    "/v1/collection-uploads/{collection_id}/files/{path}/upload",
    "/v1/collection-files/{collection_id}",
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


def test_collection_upload_resource_openapi_exposes_tus_methods() -> None:
    app = create_app()
    client = TestClient(app)
    data = client.get("/openapi.json").json()
    methods = set(data["paths"]["/v1/collection-uploads/{collection_id}/files/{path}/upload"])
    assert {"post", "patch", "head", "delete", "options"}.issubset(methods)


def test_fetch_upload_resource_openapi_exposes_tus_methods() -> None:
    app = create_app()
    client = TestClient(app)
    data = client.get("/openapi.json").json()
    methods = set(data["paths"]["/v1/fetches/{fetch_id}/entries/{entry_id}/upload"])
    assert {"post", "patch", "head", "delete", "options"}.issubset(methods)


def test_healthz_is_available_and_hidden_from_openapi() -> None:
    app = create_app()
    client = TestClient(app)

    health = client.get("/healthz")
    assert health.status_code == 200
    assert health.json()["status"] == "ok"

    data = client.get("/openapi.json").json()
    assert "/healthz" not in data["paths"]
    assert "/_test/restart" not in data["paths"]


def test_restart_control_route_is_disabled_by_default() -> None:
    app = create_app()
    client = TestClient(app)

    response = client.post("/_test/restart")
    assert response.status_code == 404


def test_restart_control_route_is_available_when_enabled(monkeypatch) -> None:
    called: list[str] = []

    monkeypatch.setenv("ARC_ENABLE_TEST_CONTROL", "1")
    monkeypatch.setattr(arc_app, "_terminate_for_restart", lambda: called.append("restart"))

    app = create_app()
    client = TestClient(app)

    response = client.post("/_test/restart")
    assert response.status_code == 202
    assert response.json()["status"] == "restarting"
    assert called == ["restart"]


def test_reset_control_route_is_available_when_enabled(monkeypatch) -> None:
    called: list[str] = []

    monkeypatch.setenv("ARC_ENABLE_TEST_CONTROL", "1")
    monkeypatch.setattr(arc_app, "_reset_runtime_state", lambda: called.append("reset"))

    app = create_app()
    client = TestClient(app)

    response = client.post("/_test/reset")
    assert response.status_code == 204
    assert called == ["reset"]
