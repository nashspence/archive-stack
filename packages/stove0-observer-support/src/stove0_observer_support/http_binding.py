"""Framework-neutral HTTP binding for independently maintained observers."""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass

from http_api_contracts import HttpErrorContract, HttpOperationContract
from pydantic import BaseModel, ValidationError
from stove0_observer_protocol import (
    ObservationInvocation,
    ObservationResult,
    ObserverDescriptor,
    validate_observation_result,
)

from stove0_observer_support.runtime import ContentObserver, ObservationRuntime

OBSERVER_HTTP_OPERATIONS = (
    HttpOperationContract(
        "GET",
        "/v1/observer",
        response_type=ObserverDescriptor,
        errors=(
            HttpErrorContract("bad_request", 400),
            HttpErrorContract("unauthorized", 401),
            HttpErrorContract("observer_failed", 500),
        ),
    ),
    HttpOperationContract(
        "POST",
        "/v1/observe",
        ObservationInvocation,
        ObservationResult,
        "json",
        errors=(
            HttpErrorContract("invalid_observation_request", 400),
            HttpErrorContract("unauthorized", 401),
            HttpErrorContract("request_too_large", 413),
            HttpErrorContract("observer_failed", 500),
        ),
    ),
)

_JSON_CONTENT_TYPE = "application/json"
_DEFAULT_MAX_REQUEST_BYTES = 4 * 1024 * 1024
_OBSERVER_HTTP_ERROR_STATUS = {
    "bad_request": 400,
    "invalid_observation_request": 400,
    "method_not_allowed": 405,
    "not_found": 404,
    "observer_failed": 500,
    "request_too_large": 413,
    "unauthorized": 401,
}


@dataclass(frozen=True, slots=True)
class ObserverHttpResponse:
    status: int
    headers: tuple[tuple[str, str], ...]
    body: bytes


class ObserverHttpBinding:
    """Translate the two v1 observer endpoints into a content-observer object.

    The binding is deliberately independent of any web framework. External
    maintainers may adapt :meth:`handle` to ASGI, WSGI, aiohttp, Flask, FastAPI,
    or another server without importing stove0 core.
    """

    def __init__(
        self,
        observer: ContentObserver,
        *,
        maximum_request_bytes: int = _DEFAULT_MAX_REQUEST_BYTES,
        maximum_concurrency: int = 1,
    ) -> None:
        if maximum_request_bytes < 1:
            raise ValueError("observer HTTP request limit must be positive")
        if isinstance(maximum_concurrency, bool) or maximum_concurrency < 1:
            raise ValueError("observer execution concurrency must be positive")
        self.observer = observer
        self.maximum_request_bytes = maximum_request_bytes
        self.maximum_concurrency = maximum_concurrency
        self._execution_slots = threading.BoundedSemaphore(maximum_concurrency)

    def handle(self, method: str, path: str, body: bytes = b"") -> ObserverHttpResponse:
        normalized_method = method.upper()
        if normalized_method == "GET" and path == "/v1/observer":
            if body:
                return _error(400, "bad_request", "GET /v1/observer must not include a body")
            try:
                return _model_response(self.observer.descriptor())
            except Exception:
                return _error(500, "observer_failed", "content observer descriptor failed")
        if normalized_method == "POST" and path == "/v1/observe":
            if len(body) > self.maximum_request_bytes:
                return _error(413, "request_too_large", "observer request exceeds its size limit")
            try:
                invocation = ObservationInvocation.model_validate_json(body)
            except (ValidationError, ValueError) as exc:
                return _error(400, "invalid_observation_request", str(exc))
            try:
                descriptor = ObserverDescriptor.model_validate(self.observer.descriptor())
            except Exception:
                return _error(500, "observer_failed", "content observer descriptor failed")
            try:
                _validate_descriptor(invocation, descriptor)
            except ValueError as exc:
                return _error(400, "invalid_observation_request", str(exc))
            try:
                with self._execution_slots:
                    with ObservationRuntime.from_invocation(invocation) as runtime:
                        result = self.observer.observe(invocation.request, runtime)
                validate_observation_result(result, invocation.request, descriptor)
                return _model_response(result)
            except Exception:
                return _error(500, "observer_failed", "content observer execution failed")
        if path in {"/v1/observer", "/v1/observe"}:
            return _error(405, "method_not_allowed", "observer endpoint method is not allowed")
        return _error(404, "not_found", "observer endpoint not found")


def _validate_descriptor(
    invocation: ObservationInvocation,
    descriptor: ObserverDescriptor,
) -> None:
    request = invocation.request
    if descriptor.descriptor_sha256 != request.observer_descriptor_sha256:
        raise ValueError("observer descriptor differs from the sealed request")
    support = descriptor.support_for(request.observer_contract_id)
    if support.contract_sha256 != request.observer_contract_sha256:
        raise ValueError("observer contract differs from the sealed request")


def _model_response(model: BaseModel) -> ObserverHttpResponse:
    dump = getattr(model, "model_dump_json", None)
    if not callable(dump):
        raise TypeError("observer HTTP response is not a protocol model")
    return ObserverHttpResponse(
        status=200,
        headers=(("Content-Type", _JSON_CONTENT_TYPE),),
        body=str(dump(by_alias=True, exclude_none=True)).encode("utf-8"),
    )


def _error(status: int, code: str, message: str) -> ObserverHttpResponse:
    if _OBSERVER_HTTP_ERROR_STATUS.get(code) != status:
        raise ValueError("observer HTTP binding emitted an undeclared error code/status")
    body = json.dumps(
        {"error": {"code": code, "message": message}},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return ObserverHttpResponse(
        status=status,
        headers=(("Content-Type", _JSON_CONTENT_TYPE),),
        body=body,
    )


__all__ = ["OBSERVER_HTTP_OPERATIONS", "ObserverHttpBinding", "ObserverHttpResponse"]
