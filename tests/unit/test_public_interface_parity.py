from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest
import riverhog_api_client
from application_access import (
    ApplicationPermission as CanonicalApplicationPermission,
)
from application_access import (
    ApplicationResource as CanonicalApplicationResource,
)
from fastapi import FastAPI
from http_api_contracts import (
    ERROR_STATUS_BY_CODE,
    safe_http_base_url,
)
from http_api_contracts import (
    HealthResponse as CanonicalHealthResponse,
)
from pydantic import TypeAdapter
from riverhog_api.app import create_app as create_riverhog_app
from riverhog_api_client import (
    ApplicationPermission,
    ApplicationResource,
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
from riverhog_ftp_adapter_api_client import (
    HealthResponse as FtpAdapterHealthResponse,
)
from riverhog_ftp_adapter_api_client import (
    RiverhogFtpAdapterClient,
)
from riverhog_protocol.errors import BadRequest
from stove0_api_client import HealthResponse as Stove0HealthResponse
from stove0_api_client import Stove0ApiClient

from scripts.operation_qualification import (
    create_adapter_contract_app,
    create_stove0_contract_app,
)

HTTP_METHODS = {"delete", "get", "patch", "post", "put"}
OPERATION_ERROR_CODES = {
    "riverhog": {
        "create_retrieval_job": {"download_allowance_exceeded"},
        "download_retrieval_file": {"download_allowance_exceeded"},
        "retire_archive_copy": {"service_unavailable"},
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
    "riverhog-ftp-adapter": {
        "close",
        "ftp_adapter_health_live",
        "ftp_adapter_health_ready",
    },
}


def test_riverhog_client_exports_the_complete_public_error_hierarchy() -> None:
    assert {
        "BadRequest",
        "Conflict",
        "DownloadAllowanceExceeded",
        "Forbidden",
        "HashMismatch",
        "InvalidPath",
        "InvalidState",
        "NotFound",
        "RiverhogError",
        "ServiceUnavailable",
        "Unauthorized",
    } <= set(riverhog_api_client.__all__)


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


def _parameter_enum(
    schema: dict[str, Any],
    components: dict[str, Any],
) -> set[str]:
    if "$ref" in schema:
        schema = components[str(schema["$ref"]).rsplit("/", 1)[-1]]
    if "enum" in schema:
        return {str(value) for value in schema["enum"]}
    variants = schema.get("anyOf", [])
    return {
        str(value)
        for variant in variants
        if variant.get("type") != "null"
        for value in _parameter_enum(variant, components)
    }


@pytest.mark.parametrize(
    ("application", "app_factory", "client_types"),
    (
        ("riverhog", create_riverhog_app, (ApiClient,)),
        ("stove0", create_stove0_contract_app, (Stove0ApiClient,)),
        (
            "riverhog-ftp-adapter",
            create_adapter_contract_app,
            (RiverhogFtpAdapterClient,),
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
            "riverhog-ftp-adapter",
            create_adapter_contract_app,
            (RiverhogFtpAdapterClient,),
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
        ("riverhog-ftp-adapter", create_adapter_contract_app),
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
        method, path = route.split(" ", 1)
        expected_codes = {"bad_request", "unauthorized", "forbidden", "internal_error"}
        if "{" in path:
            expected_codes.add("not_found")
        if method.casefold() in {"delete", "patch", "post", "put"}:
            expected_codes.add("conflict")
        expected_codes |= OPERATION_ERROR_CODES.get(application, {}).get(operation_id, set())
        actual_codes = {
            code
            for status, response in responses.items()
            if status.isdigit() and int(status) >= 400
            for code in response.get("x-riverhog-error-codes", [])
        }
        assert actual_codes == expected_codes, (
            f"{application} error codes do not match the implementing operation: {route}"
        )
        for status, response in responses.items():
            if not status.isdigit() or int(status) < 400:
                continue
            assert responses[status]["content"]["application/json"]["schema"] == {
                "$ref": "#/components/schemas/ErrorResponse"
            }
            assert {ERROR_STATUS_BY_CODE[code] for code in response["x-riverhog-error-codes"]} == {
                int(status)
            }


def test_official_client_health_models_project_the_exact_http_contract() -> None:
    expected = create_stove0_contract_app().openapi()["components"]["schemas"]["HealthResponse"]

    assert Stove0HealthResponse is CanonicalHealthResponse
    assert FtpAdapterHealthResponse is CanonicalHealthResponse
    assert Stove0HealthResponse.model_json_schema() == expected
    assert FtpAdapterHealthResponse.model_json_schema() == expected


def test_riverhog_client_exports_the_canonical_public_access_types() -> None:
    assert ApplicationPermission is CanonicalApplicationPermission
    assert ApplicationResource is CanonicalApplicationResource
    assert TypeAdapter(ApplicationPermission).json_schema()
    assert TypeAdapter(ApplicationResource).json_schema()


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
    openapi = create_riverhog_app().openapi()
    schemas = openapi["components"]["schemas"]
    state_schema = schemas["ArchiveCopyJobOut"]["properties"]["state"]

    assert _parameter_enum(state_schema, schemas) == ARCHIVE_COPY_STATES


@pytest.mark.parametrize(
    "app_factory",
    (create_riverhog_app, create_stove0_contract_app, create_adapter_contract_app),
)
def test_public_crud_control_parameters_have_closed_vocabularies(
    app_factory: Callable[[], FastAPI],
) -> None:
    components = app_factory().openapi()["components"]["schemas"]
    found = 0
    for path_item in app_factory().openapi()["paths"].values():
        for method, operation in path_item.items():
            if method not in HTTP_METHODS:
                continue
            for parameter in operation.get("parameters", []):
                if parameter["name"] not in {
                    "order",
                    "phase",
                    "protection",
                    "sort",
                    "state",
                    "status",
                }:
                    continue
                found += 1
                assert _parameter_enum(parameter["schema"], components), parameter
    if app_factory is not create_adapter_contract_app:
        assert found > 0


def test_retrieval_job_creation_requires_the_sealed_plan_precondition() -> None:
    operation = create_riverhog_app().openapi()["paths"]["/v1/retrieval-jobs"]["post"]
    if_match = next(
        parameter for parameter in operation["parameters"] if parameter["name"] == "If-Match"
    )

    assert if_match["in"] == "header"
    assert if_match["required"] is True


def test_official_clients_reject_invalid_crud_controls_and_noncanonical_tags() -> None:
    riverhog = ApiClient()
    stove0 = Stove0ApiClient()
    try:
        with pytest.raises(BadRequest, match="collection sort must be one of"):
            riverhog.list_collections(sort="newest")  # type: ignore[arg-type]
        with pytest.raises(BadRequest, match="tag must be canonical"):
            riverhog.create_tag("Not Canonical")
        with pytest.raises(BadRequest, match="does not accept a scoped resource"):
            riverhog.add_app_key_access(
                "example",
                "0" * 16,
                permission="keys:manage",
                resource="tag:incoming",
            )
        with pytest.raises(ValueError, match="evaluation sort must be one of"):
            stove0.list_evaluations(sort="newest")  # type: ignore[arg-type]
    finally:
        riverhog.close()
        stove0.close()


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
            RiverhogFtpAdapterClient,
            "RIVERHOG_FTP_ADAPTER_BASE_URL",
            "RIVERHOG_FTP_ADAPTER_TOKEN",
            "RIVERHOG_FTP_ADAPTER_HTTP2",
            "RIVERHOG_FTP_ADAPTER_HTTP_TIMEOUT_SECONDS",
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
        (RiverhogFtpAdapterClient, ValueError),
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
        (RiverhogFtpAdapterClient, "RIVERHOG_FTP_ADAPTER_ALLOW_INSECURE_HTTP"),
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


@pytest.mark.parametrize("client_type", (ApiClient, Stove0ApiClient, RiverhogFtpAdapterClient))
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
