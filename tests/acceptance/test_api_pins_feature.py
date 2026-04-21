from __future__ import annotations

import pytest

from tests.fixtures.acceptance import AcceptanceSystem, acceptance_system
from tests.fixtures.data import DOCS_COLLECTION_ID, INVOICE_TARGET


def test_pin_a_whole_collection_that_is_already_hot(acceptance_system: AcceptanceSystem) -> None:
    acceptance_system.seed_docs_hot()

    response = acceptance_system.request("POST", "/v1/pin", json_body={"target": DOCS_COLLECTION_ID})

    assert response.status_code == 200
    assert response.json() == {
        "target": DOCS_COLLECTION_ID,
        "pin": True,
        "hot": {"state": "ready", "present_bytes": 55, "missing_bytes": 0},
        "fetch": None,
    }


def test_pin_a_single_file_that_is_already_hot(acceptance_system: AcceptanceSystem) -> None:
    acceptance_system.seed_docs_hot()

    response = acceptance_system.request("POST", "/v1/pin", json_body={"target": INVOICE_TARGET})

    assert response.status_code == 200
    assert response.json() == {
        "target": INVOICE_TARGET,
        "pin": True,
        "hot": {"state": "ready", "present_bytes": 21, "missing_bytes": 0},
        "fetch": None,
    }


def test_repeating_the_same_pin_does_not_create_duplicates(acceptance_system: AcceptanceSystem) -> None:
    acceptance_system.seed_docs_hot()
    acceptance_system.seed_pin(DOCS_COLLECTION_ID)

    response = acceptance_system.request("POST", "/v1/pin", json_body={"target": DOCS_COLLECTION_ID})
    pins = acceptance_system.request("GET", "/v1/pins")

    assert response.status_code == 200
    assert pins.status_code == 200
    assert [item["target"] for item in pins.json()["pins"]] == [DOCS_COLLECTION_ID]


def test_releasing_a_broader_pin_leaves_the_narrower_pin_intact(acceptance_system: AcceptanceSystem) -> None:
    acceptance_system.seed_docs_hot()
    acceptance_system.seed_pin("docs:/tax/")
    acceptance_system.seed_pin(INVOICE_TARGET)

    response = acceptance_system.request("POST", "/v1/release", json_body={"target": "docs:/tax/"})
    pins = acceptance_system.request("GET", "/v1/pins")

    assert response.status_code == 200
    assert [item["target"] for item in pins.json()["pins"]] == [INVOICE_TARGET]
    assert acceptance_system.state.is_hot(INVOICE_TARGET) is True


def test_releasing_a_narrower_pin_leaves_the_broader_pin_intact(acceptance_system: AcceptanceSystem) -> None:
    acceptance_system.seed_docs_hot()
    acceptance_system.seed_pin("docs:/tax/")
    acceptance_system.seed_pin(INVOICE_TARGET)

    response = acceptance_system.request("POST", "/v1/release", json_body={"target": INVOICE_TARGET})
    pins = acceptance_system.request("GET", "/v1/pins")

    assert response.status_code == 200
    assert [item["target"] for item in pins.json()["pins"]] == ["docs:/tax/"]
    assert acceptance_system.state.is_hot(INVOICE_TARGET) is True


def test_releasing_a_missing_pin_is_a_successful_no_op(acceptance_system: AcceptanceSystem) -> None:
    acceptance_system.seed_docs_hot()

    response = acceptance_system.request("POST", "/v1/release", json_body={"target": "docs:/missing/"})

    assert response.status_code == 200
    assert response.json() == {"target": "docs:/missing/", "pin": False}


@pytest.mark.parametrize("target", ["docs:", "docs:raw/", "docs:/a/../b", "docs://raw/"])
def test_invalid_targets_are_rejected_for_pin(acceptance_system: AcceptanceSystem, target: str) -> None:
    response = acceptance_system.request("POST", "/v1/pin", json_body={"target": target})

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_target"


@pytest.mark.parametrize("target", ["docs:", "docs:raw/", "docs:/a/../b", "docs://raw/"])
def test_invalid_targets_are_rejected_for_release(acceptance_system: AcceptanceSystem, target: str) -> None:
    response = acceptance_system.request("POST", "/v1/release", json_body={"target": target})

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_target"
