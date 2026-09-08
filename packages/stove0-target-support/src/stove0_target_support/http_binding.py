"""Framework-neutral HTTP binding for independently maintained targets."""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Literal, Protocol, TypeVar

from http_api_contracts import http_operation_for_request
from pydantic import BaseModel, ValidationError
from stove0_target_protocol import (
    TARGET_HTTP_OPERATIONS,
    TargetContract,
    TargetJobRequest,
    TargetJobStatus,
    TargetPreflightRequest,
    TargetPreflightResponse,
)

_JSON_CONTENT_TYPE = "application/json"
_LOG = logging.getLogger(__name__)
_DEFAULT_MAX_REQUEST_BYTES = 16 * 1024 * 1024
_JOB_PATH = re.compile(r"^/v1/jobs/([0-9a-f]{64})$")
_CANCEL_PATH = re.compile(r"^/v1/jobs/([0-9a-f]{64})/cancel$")

ModelT = TypeVar("ModelT", bound=BaseModel)

type TargetHttpErrorCode = Literal[
    "bad_request",
    "invalid_target_request",
    "job_identity_mismatch",
    "job_not_found",
    "job_request_mismatch",
    "method_not_allowed",
    "not_found",
    "operation_contract_mismatch",
    "request_too_large",
    "target_contract_mismatch",
    "target_failed",
    "target_protocol_mismatch",
    "target_runtime_mismatch",
    "unauthorized",
    "unsupported_operation",
]

_TARGET_HTTP_ERROR_STATUS: dict[str, int] = {
    "bad_request": 400,
    "invalid_target_request": 400,
    "job_identity_mismatch": 409,
    "job_not_found": 404,
    "job_request_mismatch": 409,
    "method_not_allowed": 405,
    "not_found": 404,
    "operation_contract_mismatch": 409,
    "request_too_large": 413,
    "target_contract_mismatch": 409,
    "target_failed": 500,
    "target_protocol_mismatch": 409,
    "target_runtime_mismatch": 409,
    "unauthorized": 401,
    "unsupported_operation": 400,
}


class TargetService(Protocol):
    """Server-side target lifecycle required by the v1 HTTP binding."""

    def contract(self) -> TargetContract: ...

    def preflight(self, request: TargetPreflightRequest) -> TargetPreflightResponse: ...

    def put_job(self, request: TargetJobRequest) -> TargetJobStatus: ...

    def get_job(self, job_id: str) -> TargetJobStatus: ...

    def cancel_job(self, job_id: str) -> TargetJobStatus: ...


class TargetServiceError(RuntimeError):
    """Expected target-service rejection rendered as a stable HTTP error."""

    def __init__(self, status: int, code: TargetHttpErrorCode, message: str) -> None:
        super().__init__(message)
        if _TARGET_HTTP_ERROR_STATUS[code] != status:
            raise ValueError("target service error code does not match its HTTP status")
        self.status = status
        self.code = code
        self.message = message


@dataclass(frozen=True, slots=True)
class TargetHttpResponse:
    status: int
    headers: tuple[tuple[str, str], ...]
    body: bytes


class TargetHttpBinding:
    """Translate the five target endpoints into a target service object."""

    def __init__(
        self,
        target: TargetService,
        *,
        maximum_request_bytes: int = _DEFAULT_MAX_REQUEST_BYTES,
    ) -> None:
        if maximum_request_bytes < 1:
            raise ValueError("target HTTP request limit must be positive")
        self.target = target
        self.maximum_request_bytes = maximum_request_bytes

    def handle(self, method: str, path: str, body: bytes = b"") -> TargetHttpResponse:
        normalized_method = method.upper()
        operation = http_operation_for_request(TARGET_HTTP_OPERATIONS, normalized_method, path)
        try:
            if normalized_method == "GET" and path == "/v1/target":
                if body:
                    return _error(400, "bad_request", "GET /v1/target must not include a body")
                return _model_response(self.target.contract())
            if normalized_method == "POST" and path == "/v1/preflight":
                preflight = self._parse(body, TargetPreflightRequest)
                return _model_response(self.target.preflight(preflight))
            job_match = _JOB_PATH.fullmatch(path)
            if job_match is not None and normalized_method == "PUT":
                job_request = self._parse(body, TargetJobRequest)
                job_id = job_match.group(1)
                if job_request.declaration.job_id != job_id:
                    return _error(
                        409,
                        "job_identity_mismatch",
                        "target job path differs from request",
                    )
                return _model_response(self.target.put_job(job_request))
            if job_match is not None and normalized_method == "GET":
                if body:
                    return _error(400, "bad_request", "GET target job must not include a body")
                return _model_response(self.target.get_job(job_match.group(1)))
            cancel_match = _CANCEL_PATH.fullmatch(path)
            if cancel_match is not None and normalized_method == "POST":
                if body:
                    return _error(400, "bad_request", "target cancellation must not include a body")
                return _model_response(self.target.cancel_job(cancel_match.group(1)))
            if path == "/v1/target" or path == "/v1/preflight" or job_match or cancel_match:
                return _error(405, "method_not_allowed", "target endpoint method is not allowed")
            return _error(404, "not_found", "target endpoint not found")
        except TargetServiceError as exc:
            if operation is None or not operation.accepts_error(status=exc.status, code=exc.code):
                _LOG.exception("target service emitted an undeclared error")
                return _error(500, "target_failed", "target execution failed")
            return _error(exc.status, exc.code, exc.message)
        except Exception:
            _LOG.exception("target execution failed")
            return _error(500, "target_failed", "target execution failed")

    def _parse(self, body: bytes, model: type[ModelT]) -> ModelT:
        if len(body) > self.maximum_request_bytes:
            raise TargetServiceError(
                413,
                "request_too_large",
                "target request exceeds its size limit",
            )
        try:
            return model.model_validate_json(body)
        except (ValidationError, ValueError) as exc:
            raise TargetServiceError(
                400,
                "invalid_target_request",
                str(exc),
            ) from exc


def _model_response(model: BaseModel) -> TargetHttpResponse:
    return TargetHttpResponse(
        status=200,
        headers=(("Content-Type", _JSON_CONTENT_TYPE),),
        body=model.model_dump_json(by_alias=True, exclude_none=True).encode("utf-8"),
    )


def _error(status: int, code: str, message: str) -> TargetHttpResponse:
    if _TARGET_HTTP_ERROR_STATUS.get(code) != status:
        raise ValueError("target HTTP binding emitted an undeclared error code/status")
    body = json.dumps(
        {"error": {"code": code, "message": message}},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return TargetHttpResponse(
        status=status,
        headers=(("Content-Type", _JSON_CONTENT_TYPE),),
        body=body,
    )


__all__ = [
    "TargetHttpBinding",
    "TARGET_HTTP_OPERATIONS",
    "TargetHttpResponse",
    "TargetServiceError",
    "TargetService",
]
