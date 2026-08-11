from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest
from fastapi import FastAPI
from http_api_contracts import safe_http_base_url
from jeb_api.app import create_app as create_jeb_app
from jeb_api_client import JebApiClient
from munchy_api.app import app as munchy_app
from munchy_api_client.client import MunchyAdminClient, MunchyClient
from munchy_cli import main as munchy_cli
from munchy_core.adapters import riverhog as munchy_riverhog
from riverhog_api.app import create_app as create_riverhog_app
from riverhog_api_client import configured_upload_concurrency, upload_collection_units
from riverhog_api_client.client import ApiClient
from riverhog_cli import main as riverhog_cli
from riverhog_cli import upload_progress as riverhog_upload_progress
from riverhog_protocol.errors import BadRequest

HTTP_METHODS = {"delete", "get", "patch", "post", "put"}
PUBLIC_ERROR_STATUSES = {"400", "401", "403", "404", "409", "500", "503"}
OPERATION_ERROR_STATUSES = {
    "riverhog": {
        "create_retrieval_job": {"429"},
        "download_retrieval_file": {"429"},
    },
    "munchy": {
        "create_submission": {"429", "507"},
        "preflight_submission": {"429", "507"},
    },
}
SUPPORTED_CLIENT_HELPERS = {
    "riverhog": {"catalog_changes", "close", "spawn"},
    "munchy": {
        "close",
        "json",
        "request",
        "upload_file",
        "upload_files",
        "wait_for_job",
    },
    "jeb": {"close", "wait_for_attempt", "wait_for_operation"},
}


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
    ("application", "app_factory", "client_types"),
    (
        ("riverhog", create_riverhog_app, (ApiClient,)),
        ("munchy", lambda: munchy_app, (MunchyClient, MunchyAdminClient)),
        ("jeb", create_jeb_app, (JebApiClient,)),
    ),
)
def test_every_official_client_method_is_current_or_a_supported_helper(
    application: str,
    app_factory: Callable[[], FastAPI],
    client_types: tuple[type[Any], ...],
) -> None:
    operation_ids = {operation_id for operation_id, _route in public_operations(app_factory())}
    client_methods = {
        name
        for client_type in client_types
        for name in dir(client_type)
        if not name.startswith("_") and callable(getattr(client_type, name))
    }

    assert client_methods - operation_ids == SUPPORTED_CLIENT_HELPERS[application]


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
        operation_id = str(operation["operationId"])
        expected = PUBLIC_ERROR_STATUSES | OPERATION_ERROR_STATUSES.get(application, {}).get(
            operation_id, set()
        )
        actual = {status for status in responses if status.isdigit() and int(status) >= 400}
        assert actual == expected, (
            f"{application} error responses do not match the implementing operation: {route}"
        )
        for status in expected:
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


@pytest.mark.parametrize(
    "base_url",
    (
        "https://api.example.test",
        "http://localhost:8000",
        "http://127.42.0.1:8000",
        "http://[::1]:8000",
    ),
)
def test_shared_transport_contract_accepts_https_and_loopback_http(base_url: str) -> None:
    assert safe_http_base_url(base_url) == base_url


def test_shared_transport_contract_accepts_explicit_remote_cleartext_opt_in() -> None:
    assert (
        safe_http_base_url(
            "http://api.example.test",
            allow_insecure_http=True,
        )
        == "http://api.example.test"
    )


@pytest.mark.parametrize(
    ("client_type", "error_type"),
    (
        (ApiClient, BadRequest),
        (MunchyClient, ValueError),
        (JebApiClient, ValueError),
    ),
)
def test_official_clients_reject_remote_cleartext_transport(
    client_type: type[Any],
    error_type: type[Exception],
) -> None:
    with pytest.raises(error_type, match="must use HTTPS unless it targets a loopback host"):
        client_type(base_url="http://api.example.test")


@pytest.mark.parametrize(
    ("client_type", "prefix"),
    (
        (ApiClient, "RIVERHOG"),
        (MunchyClient, "MUNCHY"),
        (JebApiClient, "JEB"),
    ),
)
def test_official_clients_allow_explicit_remote_cleartext_transport(
    monkeypatch: pytest.MonkeyPatch,
    client_type: type[Any],
    prefix: str,
) -> None:
    monkeypatch.setenv(f"{prefix}_ALLOW_INSECURE_HTTP", "true")
    client = client_type(base_url="http://api.example.test")
    try:
        assert client.base_url == "http://api.example.test"
    finally:
        client.close()


@pytest.mark.parametrize("client_type", (ApiClient, MunchyClient, JebApiClient))
def test_official_clients_accept_a_scoped_remote_cleartext_opt_in(
    client_type: type[Any],
) -> None:
    client = client_type(
        base_url="http://api.example.test",
        allow_insecure_http=True,
    )
    try:
        assert client.base_url == "http://api.example.test"
        assert client.allow_insecure_http is True
    finally:
        client.close()


def test_official_direct_ingress_callers_share_the_upload_runner() -> None:
    assert riverhog_cli.upload_collection_units is upload_collection_units
    assert munchy_riverhog.upload_collection_units is upload_collection_units


@pytest.mark.parametrize(
    ("setting", "rich_enabled"),
    (
        ("RIVERHOG_CLI_PLAIN", riverhog_upload_progress._rich_progress_available),
        ("MUNCHY_CLI_PLAIN", munchy_cli._rich_enabled),
    ),
)
def test_rich_clients_share_plain_output_selection(
    monkeypatch: pytest.MonkeyPatch,
    setting: str,
    rich_enabled: Callable[[], bool],
) -> None:
    monkeypatch.setenv(setting, "true")
    assert rich_enabled() is False


def test_direct_ingress_openapi_describes_the_binary_unit_body() -> None:
    operation = create_riverhog_app().openapi()["paths"][
        "/v1/collection-upload-sessions/{collection_id}/volumes/{volume_id}/units/{unit}"
    ]["put"]
    request_body = operation["requestBody"]

    assert request_body["required"] is True
    assert set(request_body["content"]) == {"application/octet-stream"}
    schema = request_body["content"]["application/octet-stream"]["schema"]
    assert schema["type"] == "string"
    assert schema["format"] == "binary"


@pytest.mark.parametrize(
    ("environment", "expected"),
    (
        ({}, 8),
        ({"RIVERHOG_UPLOAD_FILE_CONCURRENCY": "1"}, 1),
        ({"RIVERHOG_UPLOAD_FILE_CONCURRENCY": "64"}, 64),
    ),
)
def test_shared_direct_ingress_concurrency_contract(
    environment: dict[str, str],
    expected: int,
) -> None:
    assert configured_upload_concurrency(environment) == expected
