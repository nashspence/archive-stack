from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from arc_api.routers.internal import router


def _client() -> TestClient:
    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


def test_precreate_hook_assigns_custom_staging_upload_id(monkeypatch) -> None:
    monkeypatch.setenv("ARC_TUSD_HOOK_SECRET", "hook-secret")
    client = _client()

    response = client.post(
        "/internal/tusd/hooks",
        headers={"X-Arc-Tusd-Hook-Secret": "hook-secret"},
        json={
            "Type": "pre-create",
            "Event": {
                "Upload": {
                    "MetaData": {
                        "target_path": ".arc/uploads/recovery/fx-1/e1.enc",
                    }
                }
            },
        },
    )

    assert response.status_code == 200
    assert response.json() == {
        "ChangeFileInfo": {"ID": ".arc/uploads/recovery/fx-1/e1.enc"}
    }


def test_precreate_hook_rejects_committed_collection_target(monkeypatch) -> None:
    monkeypatch.setenv("ARC_TUSD_HOOK_SECRET", "hook-secret")
    client = _client()

    response = client.post(
        "/internal/tusd/hooks",
        headers={"X-Arc-Tusd-Hook-Secret": "hook-secret"},
        json={
            "Type": "pre-create",
            "Event": {
                "Upload": {
                    "MetaData": {
                        "target_path": "collections/docs/file.txt",
                    }
                }
            },
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["RejectUpload"] is True
    assert payload["HTTPResponse"]["StatusCode"] == 400
