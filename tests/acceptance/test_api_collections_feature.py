from __future__ import annotations

from tests.fixtures.acceptance import AcceptanceSystem, acceptance_system
from tests.fixtures.data import PHOTOS_2024_FILE_COUNT, PHOTOS_2024_TOTAL_BYTES, PHOTOS_COLLECTION_ID, STAGING_PATH


def test_close_a_staged_collection(acceptance_system: AcceptanceSystem) -> None:
    acceptance_system.seed_staged_photos()

    response = acceptance_system.request("POST", "/v1/collections/close", json_body={"path": STAGING_PATH})

    assert response.status_code == 200
    assert response.json() == {
        "collection": {
            "id": PHOTOS_COLLECTION_ID,
            "files": PHOTOS_2024_FILE_COUNT,
            "bytes": PHOTOS_2024_TOTAL_BYTES,
            "hot_bytes": PHOTOS_2024_TOTAL_BYTES,
            "archived_bytes": 0,
            "pending_bytes": PHOTOS_2024_TOTAL_BYTES,
        }
    }


def test_reclosing_the_same_staged_path_fails(acceptance_system: AcceptanceSystem) -> None:
    acceptance_system.seed_staged_photos()
    first = acceptance_system.request("POST", "/v1/collections/close", json_body={"path": STAGING_PATH})
    assert first.status_code == 200

    response = acceptance_system.request("POST", "/v1/collections/close", json_body={"path": STAGING_PATH})

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "conflict"


def test_read_a_collection_summary(acceptance_system: AcceptanceSystem) -> None:
    acceptance_system.seed_staged_photos()
    acceptance_system.request("POST", "/v1/collections/close", json_body={"path": STAGING_PATH})

    response = acceptance_system.request("GET", f"/v1/collections/{PHOTOS_COLLECTION_ID}")

    assert response.status_code == 200
    payload = response.json()
    assert set(payload) == {"id", "files", "bytes", "hot_bytes", "archived_bytes", "pending_bytes"}
    assert payload["id"] == PHOTOS_COLLECTION_ID
    assert payload["files"] == PHOTOS_2024_FILE_COUNT
    assert payload["bytes"] == PHOTOS_2024_TOTAL_BYTES
    assert payload["pending_bytes"] == payload["bytes"] - payload["archived_bytes"]
    assert 0 <= payload["hot_bytes"] <= payload["bytes"]
    assert 0 <= payload["archived_bytes"] <= payload["bytes"]


def test_unknown_collection_returns_not_found(acceptance_system: AcceptanceSystem) -> None:
    response = acceptance_system.request("GET", "/v1/collections/missing")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"
