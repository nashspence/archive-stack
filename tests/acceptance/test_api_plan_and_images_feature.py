from __future__ import annotations

from tests.fixtures.acceptance import AcceptanceSystem, acceptance_system
from tests.fixtures.data import DOCS_COLLECTION_ID, IMAGE_ID, TARGET_BYTES


def test_read_the_current_plan(acceptance_system: AcceptanceSystem) -> None:
    acceptance_system.seed_planner_fixtures()

    response = acceptance_system.request("GET", "/v1/plan")

    assert response.status_code == 200
    payload = response.json()
    assert set(payload) == {"ready", "target_bytes", "min_fill_bytes", "images", "unplanned_bytes", "note"}
    assert payload["ready"] is True
    assert payload["target_bytes"] == TARGET_BYTES
    assert payload["images"]
    fills = []
    for image in payload["images"]:
        assert set(image) == {"id", "bytes", "fill", "files", "collections", "iso_ready"}
        assert image["fill"] == image["bytes"] / payload["target_bytes"]
        fills.append(image["fill"])
    assert fills == sorted(fills, reverse=True)


def test_read_one_image_summary(acceptance_system: AcceptanceSystem) -> None:
    acceptance_system.seed_planner_fixtures()

    response = acceptance_system.request("GET", f"/v1/images/{IMAGE_ID}")

    assert response.status_code == 200
    payload = response.json()
    assert payload["id"] == IMAGE_ID
    assert set(payload) == {"id", "bytes", "fill", "files", "collections", "iso_ready"}


def test_download_an_iso_for_a_ready_image(acceptance_system: AcceptanceSystem) -> None:
    acceptance_system.seed_planner_fixtures()

    response = acceptance_system.request("GET", f"/v1/images/{IMAGE_ID}/iso")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/octet-stream")
    assert response.headers["content-disposition"].endswith(f'"{IMAGE_ID}.iso"')
    assert response.content


def test_register_a_physical_copy(acceptance_system: AcceptanceSystem) -> None:
    acceptance_system.seed_planner_fixtures()
    before = acceptance_system.request("GET", f"/v1/collections/{DOCS_COLLECTION_ID}").json()

    response = acceptance_system.request(
        "POST",
        f"/v1/images/{IMAGE_ID}/copies",
        json_body={"id": "BR-021-A", "location": "Shelf B1"},
    )

    after = acceptance_system.request("GET", f"/v1/collections/{DOCS_COLLECTION_ID}").json()
    assert response.status_code == 200
    assert response.json()["copy"] == {
        "id": "BR-021-A",
        "image": IMAGE_ID,
        "location": "Shelf B1",
        "created_at": "2026-04-20T12:00:00Z",
    }
    assert after["archived_bytes"] > before["archived_bytes"]
    assert after["pending_bytes"] < before["pending_bytes"]


def test_reusing_a_copy_id_fails(acceptance_system: AcceptanceSystem) -> None:
    acceptance_system.seed_planner_fixtures()
    first = acceptance_system.request(
        "POST",
        f"/v1/images/{IMAGE_ID}/copies",
        json_body={"id": "BR-021-A", "location": "Shelf B1"},
    )
    assert first.status_code == 200

    response = acceptance_system.request(
        "POST",
        f"/v1/images/{IMAGE_ID}/copies",
        json_body={"id": "BR-021-A", "location": "Shelf B2"},
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "conflict"
