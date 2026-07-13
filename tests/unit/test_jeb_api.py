from __future__ import annotations

from typing import Any

import pytest

from riverhog_api.routers import jeb as jeb_router
from riverhog_core.domain.errors import BadRequest, InvalidState, ServiceUnavailable


def test_jeb_api_forwards_batch_query_to_service(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: list[tuple[str, str, dict[str, Any] | None, dict[str, Any] | None]] = []

    def fake_request(
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        captured.append((method, path, params, json_payload))
        return {"batches": [], "page": params["page"] if params else 1}

    monkeypatch.setattr(jeb_router, "_request_jeb_service", fake_request)

    payload = jeb_router.list_jeb_batches(
        page=2,
        per_page=50,
        sort="created_at",
        order="asc",
        terminal="all",
        state="failed",
        account="camera",
        collection="weekly",
        target="archive",
        q="metadata",
    )

    assert payload == {"batches": [], "page": 2}
    assert captured == [
        (
            "GET",
            "/v1/jeb/batches",
            {
                "account": "camera",
                "collection": "weekly",
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
    assert jeb_router.archive_jeb_now(request) == {"status": "started", "batch_id": "batch-1"}

    assert captured == [
        (
            "POST",
            "/v1/jeb/archive-now",
            None,
            {"account": "camera", "process": False},
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
