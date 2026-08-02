from __future__ import annotations

from collections.abc import Mapping
from http import HTTPStatus
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


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


ERROR_STATUS_BY_CODE: dict[str, int] = {
    "bad_request": 400,
    "invalid_path": 400,
    "invalid_target": 400,
    "unauthorized": 401,
    "forbidden": 403,
    "not_found": 404,
    "method_not_allowed": 405,
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
    "not_implemented": 501,
    "service_unavailable": 503,
    "insufficient_storage": 507,
}

_ERROR_CODE_BY_STATUS: dict[int, str] = {
    400: "bad_request",
    401: "unauthorized",
    403: "forbidden",
    404: "not_found",
    405: "method_not_allowed",
    409: "conflict",
    422: "bad_request",
    429: "too_many_active_input_uploads",
    500: "internal_error",
    501: "not_implemented",
    503: "service_unavailable",
    507: "insufficient_storage",
}

PUBLIC_ERROR_RESPONSES: dict[int, dict[str, Any]] = {
    status: {"model": ErrorResponse} for status in (400, 401, 403, 404, 409, 500, 503)
}


def error_code_for_status(status: int) -> str:
    return _ERROR_CODE_BY_STATUS.get(status, "internal_error" if status >= 500 else "bad_request")


def status_for_error_code(code: str, *, fallback: int = 500) -> int:
    return ERROR_STATUS_BY_CODE.get(code, fallback)


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
            for status in PUBLIC_ERROR_RESPONSES:
                status_text = str(status)
                existing = responses.get(status_text, {})
                responses[status_text] = {
                    "description": existing.get("description") or HTTPStatus(status).phrase,
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
    "ErrorBody",
    "ErrorResponse",
    "HealthResponse",
    "PUBLIC_ERROR_RESPONSES",
    "apply_openapi_error_contract",
    "error_code_for_status",
    "error_payload",
    "parse_error_payload",
    "status_for_error_code",
]
