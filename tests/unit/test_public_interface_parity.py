from __future__ import annotations

import ast
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
import riverhog_api_client
import riverhog_api_client.client as riverhog_client_module
import riverhog_core.services.access_groups as access_group_service_module
import riverhog_core.services.app_keys as app_key_service_module
import riverhog_core.services.archive_copies as archive_copy_service_module
import riverhog_core.services.archive_stores as archive_store_service_module
import riverhog_core.services.collection_uploads as upload_service_module
import riverhog_core.services.collection_workflows as workflow_service_module
import riverhog_core.services.collections as collection_service_module
import riverhog_core.services.download_allowances as quota_service_module
import riverhog_core.services.provenance as provenance_service_module
import riverhog_core.services.retrieval as retrieval_service_module
import riverhog_core.services.search as search_service_module
import stove0_api_client.client as stove0_client_module
import stove0_core.persistence as stove0_persistence_module
from fastapi import FastAPI
from http_api_contracts import (
    ERROR_STATUS_BY_CODE,
    JSON_SEQUENCE_MEDIA_TYPE,
    MAX_BROWSE_QUERY_CHARACTERS,
    MAX_BROWSE_TOKEN_BYTES,
    closed_literal_values,
    safe_http_base_url,
)
from http_api_contracts import (
    HealthResponse as CanonicalHealthResponse,
)
from pydantic import TypeAdapter, ValidationError
from riverhog_api.app import create_app as create_riverhog_app
from riverhog_api.browse import canonical_selectors
from riverhog_api.error_contracts import RIVERHOG_OPERATION_ERROR_CODES
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
from riverhog_api_client import workflows as riverhog_workflow_client_module
from riverhog_api_client.client import ApiClient
from riverhog_application_access import (
    ApplicationPermission as CanonicalApplicationPermission,
)
from riverhog_application_access import (
    ApplicationResource as CanonicalApplicationResource,
)
from riverhog_cli import main as riverhog_cli
from riverhog_cli import upload_progress as riverhog_upload_progress
from riverhog_core.services.archive_copy_states import ARCHIVE_COPY_STATES
from riverhog_ftp_adapter_api_client import (
    HealthResponse as FtpAdapterHealthResponse,
)
from riverhog_ftp_adapter_api_client import (
    RiverhogFtpAdapterClient,
)
from riverhog_protocol import (
    ApplicationAccessSort,
    ApplicationKeySort,
    ApplicationSort,
    ArchiveCopySort,
    ArchiveCopyState,
    ArchiveStoreSort,
    ClaimState,
    CollectionAccessGroupSort,
    CollectionAccessGroupStatus,
    CollectionRootIdentity,
    CollectionRootIdentityDocument,
    CollectionSort,
    CollectionUploadSort,
    CollectionUploadState,
    DownloadQuotaSort,
    ProcessingClaimSort,
    ProvenanceSort,
    ProvenanceStatus,
    RetrievalCacheProtection,
    RetrievalCacheSort,
    RetrievalCacheState,
    RetrievalFileReferenceDocument,
    SearchSort,
    SortOrder,
)
from riverhog_protocol.errors import BadRequest
from stove0_api.error_contracts import STOVE0_OPERATION_ERROR_CODES
from stove0_api_client import HealthResponse as Stove0HealthResponse
from stove0_api_client import Stove0ApiClient
from stove0_operator_contracts import (
    EvaluationPhase,
    EvaluationSort,
    WorkPhase,
    WorkSort,
)
from stove0_operator_contracts import (
    SortOrder as Stove0SortOrder,
)
from stove0_protocol import CollectionRootRef
from stove0_target_client import TargetCallbackClient
from stove0_target_protocol import OutputCollectionRef

from scripts.operation_qualification import (
    create_adapter_contract_app,
    create_stove0_contract_app,
)

HTTP_METHODS = {"delete", "get", "patch", "post", "put"}
REPO_ROOT = Path(__file__).resolve().parents[2]
OPERATION_ERROR_CODES = {
    "riverhog": RIVERHOG_OPERATION_ERROR_CODES,
    "stove0": STOVE0_OPERATION_ERROR_CODES,
}
SUPPORTED_CLIENT_HELPERS = {
    "riverhog": {
        "close",
        "collection_provenance_journal_metadata",
        "download_collection_provenance_journal",
        "spawn",
        "stream_retrieval_file",
        "upload_collection_upload_session_provenance_journal",
    },
    "stove0": {"close", "health_live", "health_ready", "iter_inputs"},
    "riverhog-ftp-adapter": {
        "close",
        "ftp_adapter_health_live",
        "ftp_adapter_health_ready",
    },
}


@pytest.mark.parametrize(
    ("model", "payload"),
    (
        (
            CollectionRootIdentityDocument,
            {
                "collection_id": "1",
                "archive_root_sha256": "a" * 64,
                "content_identity": "b" * 64,
            },
        ),
        (
            RetrievalFileReferenceDocument,
            {"collection_id": "1", "path": "camera/clip.mp4"},
        ),
        (
            CollectionRootRef,
            {
                "collection_id": "1",
                "archive_root_sha256": "a" * 64,
                "content_identity": "b" * 64,
            },
        ),
        (
            OutputCollectionRef,
            {
                "collection_id": "1",
                "archive_root_sha256": "a" * 64,
                "content_identity": "b" * 64,
                "derivation_sha256": "c" * 64,
            },
        ),
    ),
)
def test_public_collection_identity_mirrors_use_the_canonical_scalar(
    model: type[Any],
    payload: dict[str, object],
) -> None:
    with pytest.raises(ValidationError):
        model.model_validate(payload)


def test_public_collection_identity_dataclass_uses_the_canonical_scalar() -> None:
    with pytest.raises(ValueError):
        CollectionRootIdentity(
            collection_id="1",  # type: ignore[arg-type]
            archive_root_sha256="a" * 64,
            content_identity="b" * 64,
        )


def test_riverhog_client_exports_the_complete_public_error_hierarchy() -> None:
    assert {
        "BadRequest",
        "Conflict",
        "DownloadAllowanceExceeded",
        "Forbidden",
        "HashMismatch",
        "InvalidPath",
        "InvalidRange",
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
        ("stove0", create_stove0_contract_app, (Stove0ApiClient, TargetCallbackClient)),
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
        ("stove0", create_stove0_contract_app, (Stove0ApiClient, TargetCallbackClient)),
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
        expected_codes = {"bad_request", "unauthorized", "forbidden", "internal_error"}
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


READ_COLLECTION_OPERATIONS = {
    "riverhog": {
        "mutable-browse": {
            "list_app_key_access",
            "list_app_keys",
            "list_apps",
            "list_archive_copy_jobs",
            "list_archive_stores",
            "list_collection_provenance",
            "list_collection_provenance_journal_agents",
            "list_collection_archive_copies",
            "list_collection_access_group_members",
            "list_collection_access_groups",
            "list_collection_access_groups_for_collection",
            "list_collection_upload_session_files",
            "list_collection_upload_sessions",
            "list_collections",
            "list_download_quotas",
            "list_processing_claims",
            "list_retrieval_cache_objects",
            "search",
            "trace_collection_file_provenance",
        },
        "cursor-feed": {"list_catalog_sync_changes", "list_lifecycle_events"},
        "exact-set-page": {"get_portable_collection_inventory"},
        "exact-authority-page": {
            "list_retrieval_plan_files",
            "list_processing_claim_artifacts",
            "list_processing_claim_disposition_outputs",
            "list_processing_claim_dispositions",
            "list_processing_claim_inputs",
            "list_processing_claim_outcomes",
            "list_catalog_sync_collections",
        },
    },
    "stove0": {
        "mutable-browse": {
            "list_evaluations",
            "list_work",
        },
        "cursor-feed": {"list_events"},
        "exact-set-page": set(),
        "exact-authority-page": {
            "get_artifact_selection",
            "get_target_execution_inputs",
        },
    },
}

EXACT_RESOURCE_STREAM_OPERATIONS = {
    "riverhog": {
        "stream_collection_provenance_journal",
    },
    "stove0": set(),
}

# Every public query selector is intentional and frozen here.  This is broader
# than the page/stream classification above: exact-resource operations and
# cursor feeds also have selectors whose meaning must not appear or drift
# without an explicit contract decision.
PUBLIC_QUERY_SELECTORS = {
    "riverhog": {
        "acquire_collection_upload_session_work": {"limit"},
        "download_retrieval_file": {"collection_id", "path"},
        "list_app_key_access": {
            "active",
            "app",
            "key",
            "order",
            "page_size",
            "page_token",
            "permission",
            "q",
            "resource",
            "sort",
        },
        "list_app_keys": {"active", "order", "page_size", "page_token", "q", "sort"},
        "list_apps": {"active", "order", "page_size", "page_token", "q", "sort"},
        "list_archive_copy_jobs": {"order", "page_size", "page_token", "q", "sort", "state"},
        "list_archive_stores": {"order", "page_size", "page_token", "q", "sort"},
        "list_collection_provenance": {
            "order",
            "page_size",
            "page_token",
            "q",
            "sort",
            "status",
        },
        "list_collection_provenance_journal_agents": {"page_size", "page_token"},
        "list_collection_archive_copies": {"page_size", "page_token"},
        "get_portable_collection_inventory": {"cursor", "limit"},
        "list_collection_access_group_members": {"page_size", "page_token"},
        "list_collection_access_groups": {
            "order",
            "page_size",
            "page_token",
            "q",
            "sort",
            "status",
        },
        "list_collection_access_groups_for_collection": {"page_size", "page_token"},
        "list_collection_upload_session_files": {"page_size", "page_token"},
        "list_collection_upload_sessions": {
            "order",
            "page_size",
            "page_token",
            "q",
            "sort",
            "state",
        },
        "list_collections": {
            "encryption_format",
            "order",
            "page_size",
            "passphrase_id",
            "page_token",
            "q",
            "sort",
        },
        "list_download_quotas": {
            "active",
            "app",
            "order",
            "page_size",
            "page_token",
            "q",
            "sort",
        },
        "list_lifecycle_events": {"after", "limit"},
        "list_processing_claims": {"order", "page_size", "page_token", "sort", "state"},
        "list_processing_claim_artifacts": {"authority_sha256", "start_ordinal"},
        "list_processing_claim_inputs": {"authority_sha256", "start_ordinal"},
        "list_processing_claim_outcomes": {"authority_sha256", "start_ordinal"},
        "list_processing_claim_dispositions": {"authority_sha256", "start_ordinal"},
        "list_processing_claim_disposition_outputs": {
            "authority_sha256",
            "start_ordinal",
        },
        "list_retrieval_cache_objects": {
            "cache_store",
            "collection_id",
            "expires_after",
            "expires_before",
            "order",
            "page_size",
            "page_token",
            "protection",
            "q",
            "sort",
            "source_store",
            "state",
        },
        "list_retrieval_plan_files": {"page_size", "start_ordinal"},
        "plan_collection_deletion": {"retirement_claim_id"},
        "list_catalog_sync_changes": {"cursor", "limit"},
        "list_catalog_sync_collections": {"cursor", "limit"},
        "search": {"collection", "order", "page_size", "page_token", "q", "sort"},
        "trace_collection_file_provenance": {"page_size", "page_token"},
    },
    "stove0": {
        "get_artifact_selection": {"continuation"},
        "get_target_execution_inputs": {"continuation"},
        "get_recipe": {"revision"},
        "list_evaluations": {"order", "page_size", "page_token", "phase", "q", "sort"},
        "list_events": {"after", "limit"},
        "list_work": {"order", "page_size", "page_token", "phase", "q", "sort"},
    },
    "riverhog-ftp-adapter": {},
}

NAMED_ENUM_QUERY_SELECTOR_TYPES = {
    "ApplicationAccessSort": ApplicationAccessSort,
    "ApplicationKeySort": ApplicationKeySort,
    "ApplicationPermission": CanonicalApplicationPermission,
    "ApplicationSort": ApplicationSort,
    "ArchiveCopySort": ArchiveCopySort,
    "ArchiveCopyState": ArchiveCopyState,
    "ArchiveStoreSort": ArchiveStoreSort,
    "CollectionAccessGroupSort": CollectionAccessGroupSort,
    "CollectionAccessGroupStatus": CollectionAccessGroupStatus,
    "CollectionSort": CollectionSort,
    "CollectionUploadSort": CollectionUploadSort,
    "CollectionUploadState": CollectionUploadState,
    "DownloadQuotaSort": DownloadQuotaSort,
    "ProvenanceStatus": ProvenanceStatus,
    "RetrievalCacheProtection": RetrievalCacheProtection,
    "RetrievalCacheSort": RetrievalCacheSort,
    "RetrievalCacheState": RetrievalCacheState,
    "SearchSort": SearchSort,
    "SortOrder": SortOrder,
}
INLINE_ENUM_QUERY_SELECTOR_TYPES = {
    ("riverhog", "list_collection_provenance", "sort"): ProvenanceSort,
    ("riverhog", "list_collection_provenance", "order"): SortOrder,
    ("riverhog", "list_processing_claims", "state"): ClaimState,
    ("riverhog", "list_processing_claims", "sort"): ProcessingClaimSort,
    ("riverhog", "list_processing_claims", "order"): SortOrder,
    ("stove0", "list_work", "phase"): WorkPhase,
    ("stove0", "list_work", "sort"): WorkSort,
    ("stove0", "list_work", "order"): Stove0SortOrder,
    ("stove0", "list_evaluations", "phase"): EvaluationPhase,
    ("stove0", "list_evaluations", "sort"): EvaluationSort,
    ("stove0", "list_evaluations", "order"): Stove0SortOrder,
}


def _http_operations(schema: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        operation["operationId"]: operation
        for path_item in schema["paths"].values()
        for method, operation in path_item.items()
        if method in HTTP_METHODS
    }


def _schema_variants(
    schema: dict[str, Any],
    components: dict[str, dict[str, Any]],
) -> tuple[dict[str, Any], ...]:
    reference = schema.get("$ref")
    if reference is not None:
        return _schema_variants(components[str(reference).rsplit("/", 1)[-1]], components)
    alternatives = schema.get("oneOf") or schema.get("anyOf")
    if alternatives is not None:
        return tuple(
            variant
            for alternative in alternatives
            if alternative.get("type") != "null"
            for variant in _schema_variants(alternative, components)
        )
    return (schema,)


def _parameters(operation: dict[str, Any], *, exclude: set[str] | None = None) -> dict[str, Any]:
    omitted = exclude or set()
    return {
        f"{parameter['in']}:{parameter['name']}": parameter
        for parameter in operation.get("parameters", [])
        if parameter["name"] not in omitted
    }


@pytest.mark.parametrize(
    ("application", "app_factory"),
    (
        ("riverhog", create_riverhog_app),
        ("stove0", create_stove0_contract_app),
    ),
)
def test_public_read_collection_selectors_are_bounded_and_frozen(
    application: str,
    app_factory: Callable[[], FastAPI],
) -> None:
    schema = app_factory().openapi()
    operations = _http_operations(schema)
    classified: dict[str, set[str]] = {
        "mutable-browse": set(),
        "cursor-feed": set(),
        "exact-set-page": set(),
        "exact-authority-page": set(),
    }
    exact_resource_streams: set[str] = set()

    for operation_id, operation in operations.items():
        parameter_names = {item["name"] for item in operation.get("parameters", [])}
        assert "all" not in parameter_names, operation_id
        classification = operation.get("x-riverhog-read-collection")
        if classification is None:
            assert not {"page_size", "page_token"} <= parameter_names, operation_id
            assert "after" not in parameter_names, operation_id
            success_media = set(operation.get("responses", {}).get("200", {}).get("content", {}))
            if operation_id.startswith("stream_") and JSON_SEQUENCE_MEDIA_TYPE in success_media:
                exact_resource_streams.add(operation_id)
            continue
        kind = classification["kind"]
        classified[kind].add(operation_id)
        if kind == "cursor-feed":
            assert classification["cursor_parameter"] in parameter_names
            limit = classification.get("limit_parameter")
            if limit is not None:
                parameter = next(item for item in operation["parameters"] if item["name"] == limit)
                assert parameter["schema"]["maximum"] >= 1
            else:
                assert classification["fixed_limit"] >= 1
            continue
        if kind == "exact-set-page":
            assert classification["cursor_parameter"] in parameter_names
            limit_name = classification["limit_parameter"]
            limit = next(item for item in operation["parameters"] if item["name"] == limit_name)
            assert limit["schema"]["maximum"] >= 1
            validator = classification["validator_header"]
            assert any(
                item["in"] == "header" and item["name"] == validator
                for item in operation["parameters"]
            )
            assert classification["authority"] == "portable-collection-inventory"
            continue
        if kind == "exact-authority-page":
            assert classification["cursor_parameter"] in parameter_names
            authority_parameter = classification.get("authority_parameter")
            if authority_parameter is not None:
                assert authority_parameter in parameter_names
            limit_name = classification.get("limit_parameter")
            if limit_name is None:
                assert classification["fixed_limit"] >= 1
            else:
                limit = next(item for item in operation["parameters"] if item["name"] == limit_name)
                assert limit["schema"]["maximum"] >= 1
            assert classification["authority"]
            continue
        if kind == "mutable-browse":
            assert {"page_size", "page_token"} <= parameter_names
            page_size = next(
                item for item in operation["parameters"] if item["name"] == "page_size"
            )
            assert page_size["schema"]["maximum"] >= 1
            page_token = next(
                item for item in operation["parameters"] if item["name"] == "page_token"
            )
            token_reference = next(
                option["$ref"] for option in page_token["schema"]["anyOf"] if "$ref" in option
            )
            token_schema = schema["components"]["schemas"][token_reference.rsplit("/", 1)[-1]]
            assert token_schema["minLength"] == 1
            assert token_schema["maxLength"] == MAX_BROWSE_TOKEN_BYTES
            query = next(
                (item for item in operation["parameters"] if item["name"] == "q"),
                None,
            )
            if query is not None:
                query_reference = next(
                    option["$ref"] for option in query["schema"]["anyOf"] if "$ref" in option
                )
                query_schema = schema["components"]["schemas"][query_reference.rsplit("/", 1)[-1]]
                assert query_schema["minLength"] == 1
                assert query_schema["maxLength"] == MAX_BROWSE_QUERY_CHARACTERS
            response_schema = operation["responses"]["200"]["content"]["application/json"]["schema"]
            for response_variant in _schema_variants(
                response_schema, schema["components"]["schemas"]
            ):
                assert "next_page_token" in response_variant["required"]
                next_token_options = response_variant["properties"]["next_page_token"]["anyOf"]
                assert any(option.get("type") == "null" for option in next_token_options)
                next_token_reference = next(
                    option["$ref"] for option in next_token_options if "$ref" in option
                )
                assert next_token_reference == token_reference
            continue
        raise AssertionError(f"unsupported read-collection classification: {kind}")

    assert classified == READ_COLLECTION_OPERATIONS[application]
    assert exact_resource_streams == EXACT_RESOURCE_STREAM_OPERATIONS[application]


def test_no_public_http_operation_exposes_an_all_selector() -> None:
    for app_factory in (
        create_riverhog_app,
        create_stove0_contract_app,
        create_adapter_contract_app,
    ):
        for operation_id, operation in _http_operations(app_factory().openapi()).items():
            assert "all" not in {
                parameter["name"] for parameter in operation.get("parameters", [])
            }, operation_id


def test_browse_selector_identity_preserves_non_ascii_query_meaning() -> None:
    assert canonical_selectors(q="Straße") == {"q": "Straße"}
    assert canonical_selectors(q="Straße") != canonical_selectors(q="STRASSE")


def test_official_client_never_drops_a_supplied_query_by_truthiness() -> None:
    tree = ast.parse(Path(riverhog_client_module.__file__).read_text(encoding="utf-8"))
    offenders = [
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.If)
        and isinstance(node.test, ast.Name)
        and node.test.id in {"q", "query"}
    ]
    assert offenders == []


def test_public_read_services_do_not_use_sql_offsets() -> None:
    modules = (
        access_group_service_module,
        app_key_service_module,
        archive_copy_service_module,
        archive_store_service_module,
        upload_service_module,
        workflow_service_module,
        collection_service_module,
        quota_service_module,
        provenance_service_module,
        retrieval_service_module,
        search_service_module,
        stove0_persistence_module,
    )
    observed: set[tuple[str, str]] = set()
    for module in modules:
        path = Path(module.__file__ or "")
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for class_node in (node for node in tree.body if isinstance(node, ast.ClassDef)):
            for function in (
                node
                for node in class_node.body
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            ):
                if any(
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "offset"
                    for node in ast.walk(function)
                ):
                    observed.add((module.__name__, f"{class_node.name}.{function.name}"))

    assert observed == set()


@pytest.mark.parametrize(
    ("application", "app_factory"),
    (
        ("riverhog", create_riverhog_app),
        ("stove0", create_stove0_contract_app),
        ("riverhog-ftp-adapter", create_adapter_contract_app),
    ),
)
def test_every_public_query_selector_is_intentionally_frozen(
    application: str,
    app_factory: Callable[[], FastAPI],
) -> None:
    actual = {
        operation_id: {
            parameter["name"]
            for parameter in operation.get("parameters", [])
            if parameter["in"] == "query"
        }
        for operation_id, operation in _http_operations(app_factory().openapi()).items()
        if any(parameter["in"] == "query" for parameter in operation.get("parameters", []))
    }

    assert actual == PUBLIC_QUERY_SELECTORS[application]


@pytest.mark.parametrize(
    ("application", "app_factory"),
    (
        ("riverhog", create_riverhog_app),
        ("stove0", create_stove0_contract_app),
        ("riverhog-ftp-adapter", create_adapter_contract_app),
    ),
)
def test_every_closed_query_selector_has_one_exact_public_vocabulary(
    application: str,
    app_factory: Callable[[], FastAPI],
) -> None:
    schema = app_factory().openapi()
    components = schema["components"]["schemas"]
    observed_inline: set[tuple[str, str, str]] = set()
    observed_named: set[str] = set()
    for operation_id, operation in _http_operations(schema).items():
        for parameter in operation.get("parameters", []):
            if parameter["in"] != "query":
                continue
            parameter_schema = parameter["schema"]
            candidate = parameter_schema
            if "anyOf" in candidate:
                candidate = next(
                    (
                        option
                        for option in candidate["anyOf"]
                        if "$ref" in option or "enum" in option
                    ),
                    {},
                )
            reference = candidate.get("$ref")
            if reference is not None:
                component = str(reference).rsplit("/", 1)[-1]
                values = _parameter_enum(candidate, components)
                if not values:
                    continue
                assert component in NAMED_ENUM_QUERY_SELECTOR_TYPES, (
                    application,
                    operation_id,
                    parameter["name"],
                )
                observed_named.add(component)
                assert values == closed_literal_values(NAMED_ENUM_QUERY_SELECTOR_TYPES[component])
                continue
            values = _parameter_enum(candidate, components)
            if not values:
                continue
            key = (application, operation_id, parameter["name"])
            assert key in INLINE_ENUM_QUERY_SELECTOR_TYPES
            observed_inline.add(key)
            assert values == closed_literal_values(INLINE_ENUM_QUERY_SELECTOR_TYPES[key])

    assert observed_inline == {
        key for key in INLINE_ENUM_QUERY_SELECTOR_TYPES if key[0] == application
    }
    if application == "riverhog":
        assert observed_named == set(NAMED_ENUM_QUERY_SELECTOR_TYPES)
    else:
        assert observed_named == set()


def test_official_client_selector_validation_projects_public_vocabularies() -> None:
    riverhog_controls = {
        "_APPLICATION_ACCESS_SORTS": ApplicationAccessSort,
        "_APPLICATION_KEY_SORTS": ApplicationKeySort,
        "_APPLICATION_SORTS": ApplicationSort,
        "_ARCHIVE_COPY_SORTS": ArchiveCopySort,
        "_ARCHIVE_COPY_STATES": ArchiveCopyState,
        "_ARCHIVE_STORE_SORTS": ArchiveStoreSort,
        "_COLLECTION_ACCESS_GROUP_SORTS": CollectionAccessGroupSort,
        "_COLLECTION_SORTS": CollectionSort,
        "_COLLECTION_UPLOAD_SORTS": CollectionUploadSort,
        "_COLLECTION_UPLOAD_STATES": CollectionUploadState,
        "_DOWNLOAD_QUOTA_SORTS": DownloadQuotaSort,
        "_PROVENANCE_SORTS": ProvenanceSort,
        "_PROVENANCE_STATUSES": ProvenanceStatus,
        "_RETRIEVAL_CACHE_PROTECTIONS": RetrievalCacheProtection,
        "_RETRIEVAL_CACHE_SORTS": RetrievalCacheSort,
        "_RETRIEVAL_CACHE_STATES": RetrievalCacheState,
        "_SEARCH_SORTS": SearchSort,
        "_SORT_ORDERS": SortOrder,
    }
    for attribute, vocabulary in riverhog_controls.items():
        assert getattr(riverhog_client_module, attribute) == closed_literal_values(vocabulary)
    for attribute, vocabulary in {
        "_CLAIM_SORTS": ProcessingClaimSort,
        "_CLAIM_STATES": ClaimState,
        "_SORT_ORDERS": SortOrder,
    }.items():
        assert getattr(riverhog_workflow_client_module, attribute) == closed_literal_values(
            vocabulary
        )
    for attribute, vocabulary in {
        "_EVALUATION_PHASES": EvaluationPhase,
        "_EVALUATION_SORTS": EvaluationSort,
        "_SORT_ORDERS": Stove0SortOrder,
        "_WORK_PHASES": WorkPhase,
        "_WORK_SORTS": WorkSort,
    }.items():
        assert getattr(stove0_client_module, attribute) == closed_literal_values(vocabulary)


def test_service_selector_validation_projects_public_vocabularies() -> None:
    projections = (
        (collection_service_module, "_COLLECTION_SORT_FIELDS", CollectionSort),
        (collection_service_module, "_SORT_ORDERS", SortOrder),
        (upload_service_module, "_UPLOAD_SORT_FIELDS", CollectionUploadSort),
        (upload_service_module, "_UPLOAD_STATES", CollectionUploadState),
        (upload_service_module, "_SORT_ORDERS", SortOrder),
        (retrieval_service_module, "_CACHE_SORT_FIELDS", RetrievalCacheSort),
        (retrieval_service_module, "_CACHE_STATES", RetrievalCacheState),
        (
            retrieval_service_module,
            "_CACHE_PROTECTION_FILTERS",
            RetrievalCacheProtection,
        ),
        (retrieval_service_module, "_SORT_ORDERS", SortOrder),
        (search_service_module, "_SORT_FIELDS", SearchSort),
        (search_service_module, "_SORT_ORDERS", SortOrder),
        (provenance_service_module, "_SORT_FIELDS", ProvenanceSort),
        (provenance_service_module, "_STATUS_VALUES", ProvenanceStatus),
        (provenance_service_module, "_SORT_ORDERS", SortOrder),
        (archive_copy_service_module, "_SORT_FIELDS", ArchiveCopySort),
        (archive_copy_service_module, "_SORT_ORDERS", SortOrder),
        (archive_store_service_module, "_SORT_FIELDS", ArchiveStoreSort),
        (archive_store_service_module, "_SORT_ORDERS", SortOrder),
        (app_key_service_module, "_APP_SORT_FIELDS", ApplicationSort),
        (app_key_service_module, "_KEY_SORT_FIELDS", ApplicationKeySort),
        (app_key_service_module, "_ACCESS_SORT_FIELDS", ApplicationAccessSort),
        (app_key_service_module, "_SORT_ORDERS", SortOrder),
        (access_group_service_module, "_SORT_FIELDS", CollectionAccessGroupSort),
        (access_group_service_module, "_SORT_ORDERS", SortOrder),
        (quota_service_module, "_KEY_QUOTA_SORT_FIELDS", DownloadQuotaSort),
        (quota_service_module, "_SORT_ORDERS", SortOrder),
        (workflow_service_module, "_CLAIM_SORT_NAMES", ProcessingClaimSort),
        (workflow_service_module, "_CLAIM_STATES", ClaimState),
        (workflow_service_module, "_SORT_ORDERS", SortOrder),
        (stove0_persistence_module, "_WORK_PHASES", WorkPhase),
        (stove0_persistence_module, "_WORK_SORTS", WorkSort),
        (stove0_persistence_module, "_EVALUATION_PHASES", EvaluationPhase),
        (stove0_persistence_module, "_EVALUATION_SORTS", EvaluationSort),
        (stove0_persistence_module, "_SORT_ORDERS", Stove0SortOrder),
    )
    for module, attribute, vocabulary in projections:
        assert getattr(module, attribute) == closed_literal_values(vocabulary)


def test_non_cli_python_interfaces_do_not_reintroduce_an_all_selector() -> None:
    roots = (
        REPO_ROOT / "packages",
        REPO_ROOT / "riverhog" / "server",
        REPO_ROOT / "companions" / "stove0" / "server",
    )
    offenders: list[str] = []
    for root in roots:
        for source in root.rglob("*.py"):
            tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
            for node in ast.walk(tree):
                if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                parameters = (
                    list(node.args.posonlyargs) + list(node.args.args) + list(node.args.kwonlyargs)
                )
                for parameter in parameters:
                    if parameter.arg in {"all", "all_items"}:
                        offenders.append(
                            f"{source.relative_to(REPO_ROOT)}:{node.lineno}:{node.name}"
                        )
    assert offenders == []


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
    assert if_match["schema"]["type"] == "string"
    assert if_match["schema"]["pattern"] == '^"[0-9a-f]{64}"$'


def test_collection_upload_authority_and_binary_headers_are_exact() -> None:
    openapi = create_riverhog_app().openapi()
    paths = openapi["paths"]
    provenance_create = paths[
        "/v1/collection-upload-sessions/{collection_id}/provenance/journals/{journal_id}"
    ]["put"]
    provenance_append = paths[
        "/v1/collection-upload-sessions/{collection_id}/provenance/journals/{journal_id}"
    ]["patch"]
    upload_unit = paths[
        "/v1/collection-upload-sessions/{collection_id}/volumes/{volume_id}/units/{unit}"
    ]["put"]

    authority_ref = provenance_create["requestBody"]["content"]["application/json"]["schema"][
        "$ref"
    ]
    authority = openapi["components"]["schemas"][authority_ref.rsplit("/", 1)[-1]]
    assert set(authority["required"]) == {"bytes", "sha256"}
    assert authority["properties"]["sha256"]["pattern"] == "^[0-9a-f]{64}$"
    upload_offset = next(
        parameter
        for parameter in provenance_append["parameters"]
        if parameter["name"] == "Upload-Offset"
    )
    assert upload_offset["required"] is True
    assert upload_offset["schema"]["type"] == "integer"
    assert upload_offset["schema"]["minimum"] == 0
    upload_identity = next(
        parameter for parameter in upload_unit["parameters"] if parameter["name"] == "If-Match"
    )
    assert upload_identity["required"] is True
    assert upload_identity["schema"]["type"] == "string"
    assert upload_identity["schema"]["pattern"] == '^"[0-9a-f]{64}"$'


def test_official_clients_reject_invalid_crud_controls_and_access_group_identities() -> None:
    riverhog = ApiClient()
    stove0 = Stove0ApiClient()
    try:
        with pytest.raises(BadRequest, match="collection sort must be one of"):
            riverhog.list_collections(sort="newest")  # type: ignore[arg-type]
        with pytest.raises(BadRequest):
            riverhog.list_app_key_access(permission="unknown")  # type: ignore[arg-type]
        with pytest.raises(BadRequest):
            riverhog.list_app_key_access(resource="provider:internal")  # type: ignore[arg-type]
        with pytest.raises(BadRequest, match="processing-claim state must be one of"):
            riverhog.list_processing_claims(state="unknown")  # type: ignore[arg-type]
        with pytest.raises(BadRequest, match="lowercase SHA-256"):
            riverhog.get_collection_access_group("not-a-group")
        with pytest.raises(BadRequest, match="does not accept a scoped resource"):
            riverhog.add_app_key_access(
                "example",
                "0" * 16,
                permission="keys:manage",
                resource=f"group:{'a' * 64}",
            )
        with pytest.raises(BadRequest, match="lowercase SHA-256"):
            riverhog.get_processing_claim("not-a-claim")  # type: ignore[arg-type]
        with pytest.raises(ValueError, match="evaluation sort must be one of"):
            stove0.list_evaluations(sort="newest")  # type: ignore[arg-type]
        with pytest.raises(ValueError, match="work phase must be one of"):
            stove0.list_work(phase="unknown")  # type: ignore[arg-type]
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
    content_length = next(
        parameter for parameter in operation["parameters"] if parameter["name"] == "Content-Length"
    )
    assert content_length == {
        "name": "Content-Length",
        "in": "header",
        "required": True,
        "description": "Exact request-body length in bytes.",
        "schema": {"type": "integer", "minimum": 0},
    }
    assert "411" in operation["responses"]


def test_provenance_upload_openapi_describes_bounded_resumable_transfer() -> None:
    path = create_riverhog_app().openapi()["paths"][
        "/v1/collection-upload-sessions/{collection_id}/provenance/journals/{journal_id}"
    ]
    append = path["patch"]
    status = path["get"]

    assert set(append["requestBody"]["content"]) == {"application/json-seq"}
    assert append["requestBody"]["content"]["application/json-seq"]["schema"] == {
        "type": "string",
        "format": "binary",
    }
    content_length = next(
        parameter for parameter in append["parameters"] if parameter["name"] == "Content-Length"
    )
    assert content_length["required"] is True
    assert content_length["schema"] == {
        "type": "integer",
        "minimum": 1,
        "maximum": 1024 * 1024,
    }
    assert "411" in append["responses"]

    response = status["responses"]["200"]
    assert set(response["content"]) == {"application/json"}


@pytest.mark.parametrize(
    ("environment", "expected"),
    (
        ({}, 8),
        ({"RIVERHOG_UPLOAD_FILE_CONCURRENCY": "1"}, 1),
        ({"RIVERHOG_UPLOAD_FILE_CONCURRENCY": "256"}, 256),
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
        ({}, 256, 512),
        ({"RIVERHOG_UPLOAD_FILE_WINDOW": "1024"}, 256, 1024),
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
        ({"RIVERHOG_DOWNLOAD_FILE_CONCURRENCY": "256"}, 256),
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
        ({}, 256, 512),
        ({"RIVERHOG_DOWNLOAD_FILE_WINDOW": "1024"}, 256, 1024),
    ),
)
def test_shared_retrieval_download_window_contract(
    environment: dict[str, str],
    concurrency: int,
    expected: int,
) -> None:
    assert configured_download_window(environment, concurrency=concurrency) == expected
