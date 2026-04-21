from __future__ import annotations

import json

from tests.fixtures.acceptance import AcceptanceSystem, acceptance_system
from tests.fixtures.data import INVOICE_TARGET


def test_arc_pin_emits_the_api_pin_payload(acceptance_system: AcceptanceSystem) -> None:
    acceptance_system.seed_docs_hot()

    result = acceptance_system.run_arc("pin", INVOICE_TARGET, "--json")
    expected = acceptance_system.request("POST", "/v1/pin", json_body={"target": INVOICE_TARGET})

    assert result.returncode == 0
    assert json.loads(result.stdout) == expected.json()


def test_arc_release_emits_the_api_release_payload(acceptance_system: AcceptanceSystem) -> None:
    acceptance_system.seed_docs_hot()

    result = acceptance_system.run_arc("release", INVOICE_TARGET, "--json")
    expected = acceptance_system.request("POST", "/v1/release", json_body={"target": INVOICE_TARGET})

    assert result.returncode == 0
    assert json.loads(result.stdout) == expected.json()


def test_arc_find_emits_the_api_search_payload(acceptance_system: AcceptanceSystem) -> None:
    acceptance_system.seed_search_fixtures()

    result = acceptance_system.run_arc("find", "invoice", "--json")
    expected = acceptance_system.request("GET", "/v1/search", params={"q": "invoice", "limit": 25})

    assert result.returncode == 0
    assert json.loads(result.stdout) == expected.json()


def test_arc_plan_emits_the_api_plan_payload(acceptance_system: AcceptanceSystem) -> None:
    acceptance_system.seed_planner_fixtures()

    result = acceptance_system.run_arc("plan", "--json")
    expected = acceptance_system.request("GET", "/v1/plan")

    assert result.returncode == 0
    assert json.loads(result.stdout) == expected.json()


def test_arc_pin_prints_fetch_guidance_when_recovery_is_needed(acceptance_system: AcceptanceSystem) -> None:
    acceptance_system.seed_docs_archive()
    acceptance_system.seed_fetch("fx-1", INVOICE_TARGET)

    result = acceptance_system.run_arc("pin", INVOICE_TARGET)

    assert result.returncode == 0
    assert INVOICE_TARGET in result.stdout
    assert "fx-1" in result.stdout
    assert "copy-docs-1" in result.stdout
