from __future__ import annotations

from tests.fixtures.acceptance import AcceptanceSystem, acceptance_system
from tests.fixtures.data import INVOICE_TARGET


def test_pin_a_cold_archived_file(acceptance_system: AcceptanceSystem) -> None:
    acceptance_system.seed_docs_archive()

    response = acceptance_system.request("POST", "/v1/pin", json_body={"target": INVOICE_TARGET})

    assert response.status_code == 200
    payload = response.json()
    assert payload["pin"] is True
    assert payload["hot"]["state"] == "waiting"
    assert payload["hot"]["missing_bytes"] > 0
    assert payload["fetch"]["id"].startswith("fx-")
    assert payload["fetch"]["state"] == "waiting_media"


def test_repeating_the_same_pin_reuses_the_active_fetch(acceptance_system: AcceptanceSystem) -> None:
    acceptance_system.seed_docs_archive()
    acceptance_system.seed_fetch("fx-existing", INVOICE_TARGET)

    response = acceptance_system.request("POST", "/v1/pin", json_body={"target": INVOICE_TARGET})

    assert response.status_code == 200
    assert response.json()["fetch"]["id"] == "fx-existing"


def test_read_a_fetch_summary(acceptance_system: AcceptanceSystem) -> None:
    acceptance_system.seed_docs_archive()
    acceptance_system.seed_fetch("fx-1", INVOICE_TARGET)

    response = acceptance_system.request("GET", "/v1/fetches/fx-1")

    assert response.status_code == 200
    assert set(response.json()) == {"id", "target", "state", "files", "bytes", "copies"}


def test_read_the_manifest_twice(acceptance_system: AcceptanceSystem) -> None:
    acceptance_system.seed_docs_archive()
    acceptance_system.seed_fetch("fx-1", INVOICE_TARGET)

    first = acceptance_system.request("GET", "/v1/fetches/fx-1/manifest")
    second = acceptance_system.request("GET", "/v1/fetches/fx-1/manifest")

    assert first.status_code == 200
    assert second.status_code == 200
    first_entries = first.json()["entries"]
    second_entries = second.json()["entries"]
    assert [entry["id"] for entry in first_entries] == [entry["id"] for entry in second_entries]
    assert [entry["path"] for entry in first_entries] == [entry["path"] for entry in second_entries]


def test_uploading_bytes_with_the_wrong_hash_fails(acceptance_system: AcceptanceSystem) -> None:
    acceptance_system.seed_docs_archive()
    acceptance_system.seed_fetch("fx-1", INVOICE_TARGET)

    response = acceptance_system.request(
        "PUT",
        "/v1/fetches/fx-1/files/e1",
        headers={"X-Sha256": "wrong-hash", "Content-Type": "application/octet-stream"},
        content=b"bad plaintext bytes\n",
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "hash_mismatch"


def test_completing_before_all_required_entries_are_present_fails(acceptance_system: AcceptanceSystem) -> None:
    acceptance_system.seed_docs_archive()
    acceptance_system.seed_fetch("fx-1", INVOICE_TARGET)

    response = acceptance_system.request("POST", "/v1/fetches/fx-1/complete")

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "invalid_state"


def test_completing_a_fully_uploaded_fetch_materializes_the_target(acceptance_system: AcceptanceSystem) -> None:
    acceptance_system.seed_docs_archive()
    acceptance_system.seed_pin(INVOICE_TARGET)
    acceptance_system.seed_fetch("fx-1", INVOICE_TARGET)
    acceptance_system.upload_required_entries("fx-1")

    response = acceptance_system.request("POST", "/v1/fetches/fx-1/complete")

    assert response.status_code == 200
    assert response.json()["state"] == "done"
    assert acceptance_system.state.is_hot(INVOICE_TARGET) is True
    assert INVOICE_TARGET in acceptance_system.pins_list()
