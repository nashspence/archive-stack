from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from riverhog_api.routers.internal import router
from riverhog_core.tusd_ids import tusd_upload_id_for_target_path


def _client() -> TestClient:
    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


def test_precreate_hook_assigns_custom_staging_upload_id(monkeypatch) -> None:
    monkeypatch.setenv("RIVERHOG_TUSD_HOOK_SECRET", "hook-secret")
    client = _client()

    response = client.post(
        "/internal/tusd/hooks",
        headers={"X-Riverhog-Tusd-Hook-Secret": "hook-secret"},
        json={
            "Type": "pre-create",
            "Event": {
                "Upload": {
                    "MetaData": {
                        "target_path": ".riverhog/uploads/recovery/fx-1/e1.enc",
                    }
                }
            },
        },
    )

    assert response.status_code == 200
    assert response.json() == {
        "ChangeFileInfo": {
            "ID": tusd_upload_id_for_target_path(".riverhog/uploads/recovery/fx-1/e1.enc")
        }
    }


def test_precreate_hook_assigns_url_safe_id_for_paths_with_spaces(monkeypatch) -> None:
    monkeypatch.setenv("RIVERHOG_TUSD_HOOK_SECRET", "hook-secret")
    client = _client()

    target_path = (
        ".riverhog/uploads/collections/2025/demo/"
        "1982 - 1220 Land Drive Christmas Tancy Ralph Cowboy.mp4"
    )
    response = client.post(
        "/internal/tusd/hooks",
        headers={"X-Riverhog-Tusd-Hook-Secret": "hook-secret"},
        json={
            "Type": "pre-create",
            "Event": {
                "Upload": {
                    "MetaData": {
                        "target_path": target_path,
                    }
                }
            },
        },
    )

    assert response.status_code == 200
    upload_id = response.json()["ChangeFileInfo"]["ID"]
    assert upload_id == tusd_upload_id_for_target_path(target_path)
    assert " " not in upload_id


def test_precreate_hook_rejects_committed_collection_target(monkeypatch) -> None:
    monkeypatch.setenv("RIVERHOG_TUSD_HOOK_SECRET", "hook-secret")
    client = _client()

    response = client.post(
        "/internal/tusd/hooks",
        headers={"X-Riverhog-Tusd-Hook-Secret": "hook-secret"},
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
