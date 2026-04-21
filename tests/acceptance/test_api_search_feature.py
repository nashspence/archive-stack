from __future__ import annotations

from tests.fixtures.acceptance import AcceptanceSystem, acceptance_system


def test_search_returns_file_and_collection_targets(acceptance_system: AcceptanceSystem) -> None:
    acceptance_system.seed_search_fixtures()

    response = acceptance_system.request("GET", "/v1/search", params={"q": "invoice", "limit": 25})

    assert response.status_code == 200
    payload = response.json()
    assert payload["query"] == "invoice"
    file_results = [item for item in payload["results"] if item["kind"] == "file"]
    assert file_results
    assert all(result["target"] for result in file_results)
    assert all("hot" in result for result in file_results)
    assert all("copies" in result for result in file_results)


def test_search_targets_are_directly_reusable(acceptance_system: AcceptanceSystem) -> None:
    acceptance_system.seed_search_fixtures()

    response = acceptance_system.request("GET", "/v1/search", params={"q": "japan", "limit": 25})

    assert response.status_code == 200
    targets = [item["target"] for item in response.json()["results"]]
    assert targets
    for target in targets:
        assert acceptance_system.request("POST", "/v1/pin", json_body={"target": target}).status_code == 200
        assert acceptance_system.request("POST", "/v1/release", json_body={"target": target}).status_code == 200


def test_search_honors_limit(acceptance_system: AcceptanceSystem) -> None:
    acceptance_system.seed_search_fixtures()

    response = acceptance_system.request("GET", "/v1/search", params={"q": "a", "limit": 1})

    assert response.status_code == 200
    assert len(response.json()["results"]) <= 1


def test_search_is_case_insensitive_substring_match(acceptance_system: AcceptanceSystem) -> None:
    acceptance_system.seed_search_fixtures()

    response = acceptance_system.request("GET", "/v1/search", params={"q": "INVOICE", "limit": 25})

    assert response.status_code == 200
    targets = [item["target"] for item in response.json()["results"]]
    assert "docs:/tax/2022/invoice-123.pdf" in targets
