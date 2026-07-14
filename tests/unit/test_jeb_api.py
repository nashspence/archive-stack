from __future__ import annotations

from typing import Any

import pytest
from fastapi import Response

from riverhog_api.routers import jeb as jeb_router
from riverhog_core.domain.errors import BadRequest, InvalidState, ServiceUnavailable


def test_jeb_api_forwards_attempt_query_to_service(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: list[tuple[str, str, dict[str, Any] | None, dict[str, Any] | None]] = []

    def fake_request(
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        captured.append((method, path, params, json_payload))
        return {"attempts": [], "page": params["page"] if params else 1}

    monkeypatch.setattr(jeb_router, "_request_jeb_service", fake_request)

    payload = jeb_router.list_jeb_attempts(
        page=2,
        per_page=50,
        sort="created_at",
        order="asc",
        terminal="all",
        state="failed",
        account="camera",
        collection_slug="weekly",
        target="archive",
        q="metadata",
    )

    assert payload == {"attempts": [], "page": 2}
    assert captured == [
        (
            "GET",
            "/v1/jeb/attempts",
            {
                "account": "camera",
                "collection_slug": "weekly",
                "order": "asc",
                "page": 2,
                "per_page": 50,
                "q": "metadata",
                "sort": "created_at",
                "state": "failed",
                "target": "archive",
                "terminal": "all",
            },
            None,
        )
    ]


def test_jeb_api_forwards_action_payloads_to_service(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: list[tuple[str, str, dict[str, Any] | None, dict[str, Any] | None]] = []

    def fake_request(
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        captured.append((method, path, params, json_payload))
        return {"status": "started", "batch_id": "batch-1"}

    monkeypatch.setattr(jeb_router, "_request_jeb_service", fake_request)

    request = jeb_router.JebArchiveNowRequest(account="camera", process=False)
    response = Response()
    assert jeb_router.archive_jeb_now(request, response) == {
        "status": "started",
        "batch_id": "batch-1",
    }

    assert captured == [
        (
            "POST",
            "/v1/jeb/archive-now",
            None,
            {"account": "camera", "process": False, "dry_run": False},
        )
    ]


def test_jeb_api_forwards_dry_run_and_returns_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: list[tuple[str, str, dict[str, Any] | None, dict[str, Any] | None]] = []

    def fake_request(
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        captured.append((method, path, params, json_payload))
        return {"status": "would_process", "dry_run": True}

    monkeypatch.setattr(jeb_router, "_request_jeb_service", fake_request)

    request = jeb_router.JebArchiveNowRequest(account="camera", dry_run=True)
    response = Response()

    assert jeb_router.archive_jeb_now(request, response) == {
        "status": "would_process",
        "dry_run": True,
    }
    assert response.status_code == 200
    assert captured == [
        (
            "POST",
            "/v1/jeb/archive-now",
            None,
            {"account": "camera", "process": True, "dry_run": True},
        )
    ]


@pytest.mark.parametrize(
    ("payload", "error_type"),
    [
        ({"error": {"code": "bad_request", "message": "bad input"}}, BadRequest),
        ({"error": {"code": "invalid_state", "message": "still running"}}, InvalidState),
        ({"error": {"code": "unknown", "message": "missing"}}, ServiceUnavailable),
    ],
)
def test_jeb_api_maps_service_errors(payload: dict[str, Any], error_type: type[Exception]) -> None:
    error = jeb_router._error_from_payload(payload, "fallback")

    assert isinstance(error, error_type)
