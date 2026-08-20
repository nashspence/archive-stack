from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest
from fastapi import FastAPI
from http_api_contracts import safe_http_base_url
from riverhog_adapter_api_client import RiverhogAdapterClient, RiverhogTusClient
from riverhog_api.app import create_app as create_riverhog_app
from riverhog_api_client import (
    configured_download_concurrency,
    configured_download_window,
    configured_upload_concurrency,
    configured_upload_window,
    upload_collection_units,
)
from riverhog_api_client import producer as riverhog_producer
from riverhog_api_client.client import ApiClient
from riverhog_cli import main as riverhog_cli
from riverhog_cli import upload_progress as riverhog_upload_progress
from riverhog_core.services.archive_copy_states import ARCHIVE_COPY_STATES
from riverhog_protocol.errors import BadRequest
from stove0_api_client import Stove0ApiClient

from scripts.operation_qualification import (
    create_adapter_contract_app,
    create_stove0_contract_app,
)

HTTP_METHODS = {"delete", "get", "patch", "post", "put"}
PUBLIC_ERROR_STATUSES = {"400", "401", "403", "404", "409", "500", "503"}
OPERATION_ERROR_STATUSES = {
    "riverhog": {
        "create_retrieval_job": {"429"},
        "download_retrieval_file": {"429"},
    },
}
SUPPORTED_CLIENT_HELPERS = {
    "riverhog": {
        "catalog_changes",
        "close",
        "resourcesync_capabilities",
        "resourcesync_discovery",
        "resourcesync_resource_pages",
        "resourcesync_resources",
        "spawn",
        "stream_retrieval_file",
    },
    "stove0": {"close", "health_live", "health_ready"},
    "riverhog-adapters": {
        "close",
        "adapter_health_live",
        "adapter_health_ready",
        "upload_file",
        "wait_for_publication",
    },
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
        ("stove0", create_stove0_contract_app, (Stove0ApiClient,)),
        (
            "riverhog-adapters",
            create_adapter_contract_app,
            (RiverhogAdapterClient, RiverhogTusClient),
        ),
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
        ("stove0", create_stove0_contract_app, (Stove0ApiClient,)),
        (
            "riverhog-adapters",
            create_adapter_contract_app,
            (RiverhogAdapterClient, RiverhogTusClient),
        ),
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
        ("stove0", create_stove0_contract_app),
        ("riverhog-adapters", create_adapter_contract_app),
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
    (create_riverhog_app, create_stove0_contract_app),
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


def test_archive_copy_wire_states_match_the_service_state_machine() -> None:
    schema = create_riverhog_app().openapi()["components"]["schemas"]["ArchiveCopyJobOut"]

    assert set(schema["properties"]["state"]["enum"]) == ARCHIVE_COPY_STATES


@pytest.mark.parametrize(
    (
        "client_type",
        "base_url_env",
        "token_env",
        "http2_env",
        "timeout_env",
        "base_url",
    ),
    (
        (
            ApiClient,
            "RIVERHOG_BASE_URL",
            "RIVERHOG_TOKEN",
            "RIVERHOG_HTTP2",
            "RIVERHOG_HTTP_TIMEOUT_SECONDS",
            "https://riverhog.example.test",
        ),
        (
            Stove0ApiClient,
            "STOVE0_BASE_URL",
            "STOVE0_TOKEN",
            "STOVE0_HTTP2",
            "STOVE0_HTTP_TIMEOUT_SECONDS",
            "https://stove0.example.test",
        ),
        (
            RiverhogAdapterClient,
            "RIVERHOG_ADAPTERS_BASE_URL",
            "RIVERHOG_ADAPTERS_TOKEN",
            "RIVERHOG_ADAPTERS_HTTP2",
            "RIVERHOG_ADAPTERS_HTTP_TIMEOUT_SECONDS",
            "https://adapters.example.test",
        ),
    ),
)
def test_official_clients_share_transport_configuration(
    monkeypatch: pytest.MonkeyPatch,
    client_type: type[Any],
    base_url_env: str,
    token_env: str,
    http2_env: str,
    timeout_env: str,
    base_url: str,
) -> None:
    monkeypatch.setenv(base_url_env, f"{base_url}/")
    monkeypatch.setenv(token_env, "example-token")
    monkeypatch.setenv(http2_env, "false")
    monkeypatch.setenv(timeout_env, "17")

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
        (Stove0ApiClient, ValueError),
        (RiverhogAdapterClient, ValueError),
    ),
)
def test_official_clients_reject_remote_cleartext_transport(
    client_type: type[Any],
    error_type: type[Exception],
) -> None:
    with pytest.raises(error_type, match="must use HTTPS unless it targets a loopback host"):
        client_type(base_url="http://api.example.test")


@pytest.mark.parametrize(
    ("client_type", "allow_insecure_env"),
    (
        (ApiClient, "RIVERHOG_ALLOW_INSECURE_HTTP"),
        (Stove0ApiClient, "STOVE0_ALLOW_INSECURE_HTTP"),
        (RiverhogAdapterClient, "RIVERHOG_ADAPTERS_ALLOW_INSECURE_HTTP"),
    ),
)
def test_official_clients_allow_explicit_remote_cleartext_transport(
    monkeypatch: pytest.MonkeyPatch,
    client_type: type[Any],
    allow_insecure_env: str,
) -> None:
    monkeypatch.setenv(allow_insecure_env, "true")
    client = client_type(base_url="http://api.example.test")
    try:
        assert client.base_url == "http://api.example.test"
    finally:
        client.close()


@pytest.mark.parametrize("client_type", (ApiClient, Stove0ApiClient, RiverhogAdapterClient))
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
    assert riverhog_producer.upload_collection_units is upload_collection_units


@pytest.mark.parametrize(
    ("setting", "rich_enabled"),
    (("RIVERHOG_CLI_PLAIN", riverhog_upload_progress._rich_progress_available),),
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


@pytest.mark.parametrize(
    ("environment", "concurrency", "expected"),
    (
        ({}, 8, 16),
        ({}, 16, 32),
        ({"RIVERHOG_UPLOAD_FILE_WINDOW": "64"}, 16, 64),
    ),
)
def test_shared_direct_ingress_window_contract(
    environment: dict[str, str],
    concurrency: int,
    expected: int,
) -> None:
    assert configured_upload_window(environment, concurrency=concurrency) == expected


@pytest.mark.parametrize(
    ("environment", "expected"),
    (
        ({}, 4),
        ({"RIVERHOG_DOWNLOAD_FILE_CONCURRENCY": "1"}, 1),
        ({"RIVERHOG_DOWNLOAD_FILE_CONCURRENCY": "64"}, 64),
    ),
)
def test_shared_retrieval_download_concurrency_contract(
    environment: dict[str, str],
    expected: int,
) -> None:
    assert configured_download_concurrency(environment) == expected


@pytest.mark.parametrize(
    ("environment", "concurrency", "expected"),
    (
        ({}, 4, 8),
        ({}, 16, 32),
        ({"RIVERHOG_DOWNLOAD_FILE_WINDOW": "64"}, 16, 64),
    ),
)
def test_shared_retrieval_download_window_contract(
    environment: dict[str, str],
    concurrency: int,
    expected: int,
) -> None:
    assert configured_download_window(environment, concurrency=concurrency) == expected
