from __future__ import annotations

import base64
from types import SimpleNamespace
from urllib.parse import quote

from fastapi import FastAPI
from fastapi.testclient import TestClient

from riverhog_api.routers.internal import router
from riverhog_core.stores.tusd_upload_store import TusdUploadStore
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


def test_precreate_hook_accepts_signature_safe_base64_target_metadata(monkeypatch) -> None:
    monkeypatch.setenv("RIVERHOG_TUSD_HOOK_SECRET", "hook-secret")
    client = _client()
    target_path = (
        ".riverhog/uploads/collections/2025/demo/Logos/Plane Tail -  logo2a.jpg"
    )
    target_path_b64 = base64.b64encode(target_path.encode("utf-8")).decode("ascii")

    response = client.post(
        "/internal/tusd/hooks",
        headers={"X-Riverhog-Tusd-Hook-Secret": "hook-secret"},
        json={
            "Type": "pre-create",
            "Event": {
                "Upload": {
                    "MetaData": {
                        "target_path_b64": target_path_b64,
                    }
                }
            },
        },
    )

    assert response.status_code == 200
    assert response.json() == {
        "ChangeFileInfo": {"ID": tusd_upload_id_for_target_path(target_path)}
    }


def test_post_finish_hook_syncs_finished_target(monkeypatch) -> None:
    monkeypatch.setenv("RIVERHOG_TUSD_HOOK_SECRET", "hook-secret")
    target_path = ".riverhog/uploads/collections/2026/demo/camera/a.webm"
    calls: list[str] = []

    class FakeCollections:
        def sync_finished_upload_target(self, value: str) -> None:
            calls.append(value)

    monkeypatch.setattr(
        "riverhog_api.routers.internal.default_container",
        lambda: SimpleNamespace(collections=FakeCollections()),
    )
    client = _client()

    response = client.post(
        "/internal/tusd/hooks",
        headers={"X-Riverhog-Tusd-Hook-Secret": "hook-secret"},
        json={
            "Type": "post-finish",
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
    assert response.json() == {}
    assert calls == [target_path]


def test_tusd_upload_store_sends_signature_safe_target_metadata() -> None:
    target_path = (
        ".riverhog/uploads/collections/2025/demo/Logos/Plane Tail -  logo2a.jpg"
    )
    header = TusdUploadStore._metadata_header(object(), target_path)
    name, encoded_value = header.split(" ", 1)
    decoded_value = base64.b64decode(encoded_value, validate=True).decode("ascii")

    assert name == "target_path_b64"
    assert base64.b64decode(decoded_value, validate=True).decode("utf-8") == target_path
    assert " " not in decoded_value


def test_tusd_data_plane_auth_allows_encoded_upload_id(monkeypatch) -> None:
    monkeypatch.setenv("RIVERHOG_API_TOKEN", "api-token")
    client = _client()
    upload_id = ".riverhog/uploads/by-target/" + "a" * 64
    response = client.get(
        "/internal/tusd/auth",
        headers={
            "Authorization": "Bearer api-token",
            "X-Original-Method": "PATCH",
            "X-Original-Uri": f"/files/{quote(upload_id, safe='')}",
        },
    )

    assert response.status_code == 204


def test_tusd_data_plane_auth_rejects_wrong_token(monkeypatch) -> None:
    monkeypatch.setenv("RIVERHOG_API_TOKEN", "api-token")
    client = _client()

    response = client.get(
        "/internal/tusd/auth",
        headers={
            "Authorization": "Bearer wrong",
            "X-Original-Method": "PATCH",
            "X-Original-Uri": "/files/.riverhog%2Fuploads%2Fby-target%2Fabc",
        },
    )

    assert response.status_code == 401


def test_tusd_data_plane_auth_rejects_non_upload_namespace(monkeypatch) -> None:
    monkeypatch.setenv("RIVERHOG_API_TOKEN", "api-token")
    client = _client()

    response = client.get(
        "/internal/tusd/auth",
        headers={
            "Authorization": "Bearer api-token",
            "X-Original-Method": "PATCH",
            "X-Original-Uri": "/files/collections/docs/report.txt",
        },
    )

    assert response.status_code == 403


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
