from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest
from fastapi import FastAPI
from jeb_api.app import create_app as create_jeb_app
from jeb_api_client import JebApiClient
from munchy_api.app import app as munchy_app
from munchy_api_client.client import MunchyAdminClient, MunchyClient
from riverhog_api.app import create_app as create_riverhog_app
from riverhog_api_client.client import ApiClient

HTTP_METHODS = {"delete", "get", "patch", "post", "put"}
PUBLIC_ERROR_STATUSES = {"400", "401", "403", "404", "409", "500", "503"}


def public_operations(app: FastAPI) -> list[tuple[str, str]]:
    operations: list[tuple[str, str]] = []
    for path, path_item in app.openapi()["paths"].items():
        if not path.startswith("/v1"):
            continue
        for method, operation in path_item.items():
            if method not in HTTP_METHODS:
                continue
            operations.append((str(operation["operationId"]), f"{method.upper()} {path}"))
    return operations


@pytest.mark.parametrize(
    ("application", "app_factory", "client_types"),
    (
        ("riverhog", create_riverhog_app, (ApiClient,)),
        ("munchy", lambda: munchy_app, (MunchyClient, MunchyAdminClient)),
        ("jeb", create_jeb_app, (JebApiClient,)),
    ),
)
def test_every_public_api_operation_has_an_official_client_method(
    application: str,
    app_factory: Callable[[], FastAPI],
    client_types: tuple[type[Any], ...],
) -> None:
    operations = public_operations(app_factory())
    operation_ids = [operation_id for operation_id, _route in operations]
    uncovered = {
        operation_id: route
        for operation_id, route in operations
        if not any(
            callable(getattr(client_type, operation_id, None)) for client_type in client_types
        )
    }

    assert len(operation_ids) == len(set(operation_ids)), (
        f"{application} OpenAPI operation IDs must be unique: {operation_ids}"
    )
    assert uncovered == {}, f"{application} OpenAPI operations missing from its client: {uncovered}"


@pytest.mark.parametrize(
    ("application", "app_factory"),
    (
        ("riverhog", create_riverhog_app),
        ("munchy", lambda: munchy_app),
        ("jeb", create_jeb_app),
    ),
)
def test_public_http_health_and_error_schemas_are_conventional(
    application: str,
    app_factory: Callable[[], FastAPI],
) -> None:
    schema = app_factory().openapi()
    assert schema.get("servers") in (None, [])
    assert schema["components"]["schemas"]["HealthResponse"] == {
        "additionalProperties": False,
        "properties": {
            "service": {"minLength": 1, "title": "Service", "type": "string"},
            "status": {"const": "ok", "title": "Status", "type": "string"},
        },
        "required": ["service", "status"],
        "title": "HealthResponse",
        "type": "object",
    }
    for path in ("/health/live", "/health/ready"):
        response = schema["paths"][path]["get"]["responses"]["200"]
        assert response["content"]["application/json"]["schema"] == {
            "$ref": "#/components/schemas/HealthResponse"
        }
    assert schema["paths"]["/health/ready"]["get"]["responses"]["503"]["content"][
        "application/json"
    ]["schema"] == {"$ref": "#/components/schemas/ErrorResponse"}

    operations = {
        f"{method.upper()} {path}": operation
        for path, path_item in schema["paths"].items()
        if path.startswith("/v1")
        for method, operation in path_item.items()
        if method in HTTP_METHODS
    }
    assert operations
    for route, operation in operations.items():
        responses = operation["responses"]
        assert "422" not in responses, f"{application} exposes obsolete validation status: {route}"
        assert PUBLIC_ERROR_STATUSES <= responses.keys(), (
            f"{application} is missing conventional error responses: {route}"
        )
        for status in PUBLIC_ERROR_STATUSES:
            assert responses[status]["content"]["application/json"]["schema"] == {
                "$ref": "#/components/schemas/ErrorResponse"
            }


@pytest.mark.parametrize(
    "app_factory",
    (create_riverhog_app, lambda: munchy_app, create_jeb_app),
)
def test_paged_lists_use_the_shared_parameter_and_response_envelope(
    app_factory: Callable[[], FastAPI],
) -> None:
    schema = app_factory().openapi()
    components = schema["components"]["schemas"]
    paged_operations = 0
    for path, path_item in schema["paths"].items():
        if not path.startswith("/v1") or "get" not in path_item:
            continue
        operation = path_item["get"]
        response_schema = (
            operation["responses"]
            .get("200", {})
            .get("content", {})
            .get("application/json", {})
            .get("schema", {})
        )
        response_name = str(response_schema.get("$ref") or "").rsplit("/", 1)[-1]
        response_properties = components.get(response_name, {}).get("properties", {})
        if not {"page", "pages", "per_page", "total"} <= response_properties.keys():
            continue
        paged_operations += 1
        parameter_names = {parameter["name"] for parameter in operation.get("parameters", [])}
        assert {"page", "per_page", "all"} <= parameter_names, path
    assert paged_operations > 0


@pytest.mark.parametrize(
    ("client_type", "prefix", "base_url"),
    (
        (ApiClient, "RIVERHOG", "https://riverhog.example.test"),
        (MunchyClient, "MUNCHY", "https://munchy.example.test"),
        (JebApiClient, "JEB", "https://jeb.example.test"),
    ),
)
def test_official_clients_share_transport_configuration(
    monkeypatch: pytest.MonkeyPatch,
    client_type: type[Any],
    prefix: str,
    base_url: str,
) -> None:
    monkeypatch.setenv(f"{prefix}_BASE_URL", f"{base_url}/")
    monkeypatch.setenv(f"{prefix}_TOKEN", "example-token")
    monkeypatch.setenv(f"{prefix}_HTTP2", "false")
    monkeypatch.setenv(f"{prefix}_HTTP_TIMEOUT_SECONDS", "17")

    client = client_type()
    try:
        assert client.base_url == base_url
        assert client.token == "example-token"
        assert client.http2 is False
        assert client.timeout_seconds == 17
    finally:
        client.close()
