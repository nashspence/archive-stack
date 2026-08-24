from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from http import HTTPStatus
from ipaddress import ip_address
from typing import Any, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter


class HttpApiModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ErrorBody(HttpApiModel):
    code: str = Field(min_length=1)
    message: str = Field(min_length=1)
    details: dict[str, Any] | None = None


class ErrorResponse(HttpApiModel):
    error: ErrorBody


class HealthResponse(HttpApiModel):
    service: str = Field(min_length=1)
    status: Literal["ok"]


HttpBodyKind = Literal["none", "json", "framed", "binary"]


@dataclass(frozen=True, slots=True)
class HttpOperationContract:
    """One exact method/path binding projected into a running OpenAPI document."""

    method: Literal["GET", "POST", "PUT", "DELETE", "PATCH"]
    path: str
    request_type: object | None = None
    response_type: object | None = None
    request_kind: HttpBodyKind = "none"
    response_kind: HttpBodyKind = "json"
    success_statuses: tuple[int, ...] = (200,)
    error_statuses: tuple[int, ...] = (400, 401, 500)

    def __post_init__(self) -> None:
        if not self.path.startswith("/v1/"):
            raise ValueError("HTTP operation path must be an absolute v1 path")
        if self.request_kind in {"json", "framed"} and self.request_type is None:
            raise ValueError("typed HTTP request body has no declaration model")
        if self.response_kind == "json" and self.response_type is None:
            raise ValueError("JSON HTTP response has no response model")
        if self.response_kind == "none" and self.response_type is not None:
            raise ValueError("empty HTTP response cannot have a response model")


def inline_type_schema(value: object) -> dict[str, Any]:
    """Return a self-contained JSON Schema for an OpenAPI request or response."""

    schema = TypeAdapter(value).json_schema(ref_template="#/$defs/{model}")
    definitions = schema.pop("$defs", {})

    def expand(current: object, trail: frozenset[str] = frozenset()) -> object:
        if isinstance(current, list):
            return [expand(item, trail) for item in current]
        if not isinstance(current, dict):
            return current
        reference = current.get("$ref")
        if isinstance(reference, str) and reference.startswith("#/$defs/"):
            name = reference.rsplit("/", 1)[-1]
            if name in trail:
                return current
            target = definitions.get(name)
            if isinstance(target, dict):
                return expand(target, trail | {name})
        expanded_items = {str(key): expand(item, trail) for key, item in current.items()}
        discriminator = expanded_items.get("discriminator")
        if isinstance(discriminator, dict) and "propertyName" in discriminator:
            expanded_items["discriminator"] = {"propertyName": discriminator["propertyName"]}
        return expanded_items

    expanded = expand(schema)
    if not isinstance(expanded, dict):  # pragma: no cover - TypeAdapter always returns an object
        raise TypeError("JSON Schema root is not an object")
    return expanded


def operation_openapi(
    contract: HttpOperationContract,
    *,
    error_type: object = ErrorResponse,
) -> dict[str, Any]:
    """Build FastAPI route metadata without changing framework-neutral dispatch."""

    responses: dict[int, dict[str, Any]] = {}
    for status in contract.success_statuses:
        response: dict[str, Any] = {"description": HTTPStatus(status).phrase}
        if contract.response_kind == "json":
            response["content"] = {
                "application/json": {"schema": inline_type_schema(contract.response_type)}
            }
        elif contract.response_kind == "binary":
            response["content"] = {
                "application/octet-stream": {"schema": {"type": "string", "format": "binary"}}
            }
        responses[status] = response
    for status in contract.error_statuses:
        responses[status] = {
            "description": HTTPStatus(status).phrase,
            "content": {"application/json": {"schema": inline_type_schema(error_type)}},
        }
    extra: dict[str, Any] = {}
    if contract.request_kind == "json":
        extra["requestBody"] = {
            "required": True,
            "content": {"application/json": {"schema": inline_type_schema(contract.request_type)}},
        }
    elif contract.request_kind == "framed":
        extra["requestBody"] = {
            "required": True,
            "content": {
                "application/octet-stream": {
                    "schema": {"type": "string", "format": "binary"},
                    "x-riverhog-framing-declaration": inline_type_schema(contract.request_type),
                }
            },
        }
    return {
        "status_code": contract.success_statuses[0],
        "response_model": None,
        "responses": responses,
        "openapi_extra": extra or None,
    }


ERROR_STATUS_BY_CODE: dict[str, int] = {
    "bad_request": 400,
    "invalid_path": 400,
    "invalid_target": 400,
    "unauthorized": 401,
    "forbidden": 403,
    "not_found": 404,
    "method_not_allowed": 405,
    "length_required": 411,
    "conflict": 409,
    "hash_mismatch": 409,
    "input_upload_storage_hint_invalid": 409,
    "invalid_state": 409,
    "job_template_revision_conflict": 409,
    "storage_hint_mismatch": 409,
    "submission_conflict": 409,
    "download_allowance_exceeded": 429,
    "too_many_active_input_uploads": 429,
    "ingress_failed": 500,
    "internal_error": 500,
    "service_unavailable": 503,
    "insufficient_storage": 507,
}
PUBLIC_ERROR_CODES = frozenset(ERROR_STATUS_BY_CODE)

_ERROR_CODE_BY_STATUS: dict[int, str] = {
    400: "bad_request",
    401: "unauthorized",
    403: "forbidden",
    404: "not_found",
    405: "method_not_allowed",
    411: "length_required",
    409: "conflict",
    429: "too_many_active_input_uploads",
    500: "internal_error",
    503: "service_unavailable",
    507: "insufficient_storage",
}


def error_responses(*codes: str) -> dict[int | str, dict[str, Any]]:
    """Declare operation-specific public error codes beside a FastAPI route."""

    unknown = set(codes) - PUBLIC_ERROR_CODES
    if unknown:
        raise ValueError(f"unknown public error codes: {', '.join(sorted(unknown))}")
    grouped: dict[int, list[str]] = {}
    for code in codes:
        grouped.setdefault(ERROR_STATUS_BY_CODE[code], []).append(code)
    return {
        status: {
            "model": ErrorResponse,
            "x-riverhog-error-codes": sorted(set(status_codes)),
        }
        for status, status_codes in grouped.items()
    }


OperationInterface = Literal[
    "human-cli+json",
    "client-only-primitive",
    "standard-tool/protocol",
    "service-internal",
]


def operation_interface(value: OperationInterface) -> dict[str, str]:
    """Declare a non-default operation audience in its generated OpenAPI contract."""

    return {"x-riverhog-interface": value}


def safe_http_base_url(
    value: str,
    *,
    setting: str = "base URL",
    allow_insecure_http: bool = False,
) -> str:
    normalized = value.strip().rstrip("/")
    parsed = urlsplit(normalized)
    if parsed.scheme not in {"http", "https"} or parsed.hostname is None:
        raise ValueError(f"{setting} must be an absolute HTTP or HTTPS URL")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError(f"{setting} must not contain credentials")
    if parsed.query or parsed.fragment:
        raise ValueError(f"{setting} must not contain a query or fragment")
    if parsed.scheme == "https" or allow_insecure_http or _is_loopback_host(parsed.hostname):
        return normalized
    raise ValueError(
        f"{setting} must use HTTPS unless it targets a loopback host "
        "or insecure HTTP is explicitly enabled"
    )


def _is_loopback_host(host: str) -> bool:
    candidate = host.rstrip(".").casefold()
    if candidate == "localhost":
        return True
    try:
        return ip_address(candidate).is_loopback
    except ValueError:
        return False


def error_code_for_status(status: int) -> str:
    return _ERROR_CODE_BY_STATUS.get(status, "internal_error" if status >= 500 else "bad_request")


def status_for_error_code(code: str, *, fallback: int = 500) -> int:
    return ERROR_STATUS_BY_CODE.get(code, fallback)


def _default_operation_error_codes(path: str, method: str) -> set[str]:
    codes = {"bad_request", "unauthorized", "forbidden", "internal_error"}
    if "{" in path:
        codes.add("not_found")
    if method in {"delete", "patch", "post", "put"}:
        codes.add("conflict")
    return codes


def error_payload(
    *,
    code: str,
    message: str,
    details: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    return ErrorResponse(
        error=ErrorBody(
            code=code,
            message=message,
            details=dict(details) if details else None,
        )
    ).model_dump(exclude_none=True)


def apply_openapi_error_contract(schema: dict[str, Any]) -> dict[str, Any]:
    error_schema = ErrorResponse.model_json_schema(ref_template="#/components/schemas/{model}")
    definitions = error_schema.pop("$defs", {})
    components = schema.setdefault("components", {}).setdefault("schemas", {})
    components.update(definitions)
    components["ErrorResponse"] = error_schema
    for path, path_item in schema.get("paths", {}).items():
        if not path.startswith("/v1") or not isinstance(path_item, Mapping):
            continue
        for method, operation in path_item.items():
            if method not in {"delete", "get", "patch", "post", "put"} or not isinstance(
                operation, dict
            ):
                continue
            responses = operation.setdefault("responses", {})
            responses.pop("422", None)
            codes = _default_operation_error_codes(path, method)
            for response in responses.values():
                if not isinstance(response, Mapping):
                    continue
                declared = response.get("x-riverhog-error-codes", [])
                if isinstance(declared, list):
                    codes.update(str(code) for code in declared)
            unknown = codes - PUBLIC_ERROR_CODES
            if unknown:
                raise ValueError(
                    "OpenAPI operation declares unknown public error codes: "
                    + ", ".join(sorted(unknown))
                )
            by_status: dict[int, list[str]] = {}
            for code in sorted(codes):
                by_status.setdefault(ERROR_STATUS_BY_CODE[code], []).append(code)
            existing_error_statuses = [
                status for status in responses if str(status).isdigit() and int(str(status)) >= 400
            ]
            for status in existing_error_statuses:
                responses.pop(status, None)
            for status, status_codes in by_status.items():
                status_text = str(status)
                responses[status_text] = {
                    "description": HTTPStatus(status).phrase,
                    "x-riverhog-error-codes": status_codes,
                    "content": {
                        "application/json": {
                            "schema": {"$ref": "#/components/schemas/ErrorResponse"}
                        }
                    },
                }
    return schema


def parse_error_payload(
    payload: object,
    *,
    fallback_message: str,
) -> tuple[str, str, dict[str, Any]]:
    error = payload.get("error") if isinstance(payload, Mapping) else None
    if not isinstance(error, Mapping):
        return "invalid_response", fallback_message, {}
    code = str(error.get("code") or "invalid_response")
    message = str(error.get("message") or fallback_message)
    raw_details = error.get("details")
    details = dict(raw_details) if isinstance(raw_details, Mapping) else {}
    return code, message, details


__all__ = [
    "ERROR_STATUS_BY_CODE",
    "PUBLIC_ERROR_CODES",
    "ErrorBody",
    "ErrorResponse",
    "HealthResponse",
    "HttpBodyKind",
    "HttpOperationContract",
    "OperationInterface",
    "apply_openapi_error_contract",
    "error_code_for_status",
    "error_payload",
    "error_responses",
    "operation_interface",
    "operation_openapi",
    "inline_type_schema",
    "parse_error_payload",
    "safe_http_base_url",
    "status_for_error_code",
]
