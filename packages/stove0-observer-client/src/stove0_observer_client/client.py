"""Small HTTP client for independently maintained content observers."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Literal, TypeVar

import httpx
from http_api_contracts import (
    http_operation_for_request,
    parse_declared_error_payload,
    safe_http_base_url,
)
from pydantic import BaseModel
from stove0_observer_protocol import (
    OBSERVER_HTTP_OPERATIONS,
    ObservationInvocation,
    ObservationResult,
    ObserverDescriptor,
    SemanticValidatorProvider,
    accept_observation_result,
    require_semantic_validators,
)

ModelT = TypeVar("ModelT", bound=BaseModel)


class ObserverProtocolError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        failure_kind: Literal[
            "remote_rejection", "transport", "invalid_response", "unsupported_semantics"
        ],
        code: str | None = None,
        observed_status: int | None = None,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        if failure_kind == "remote_rejection":
            if code is None or observed_status is None or not 400 <= observed_status <= 599:
                raise ValueError("remote rejection requires a declared code and error status")
        elif code is not None or observed_status is not None:
            raise ValueError("client-local failure must not claim a remote code or status")
        self.message = message
        self.failure_kind = failure_kind
        self.code = code
        self.observed_status = observed_status
        self.details = dict(details or {})


class ContentObserverClient:
    """The complete v1 HTTP binding: descriptor plus synchronous observation."""

    def __init__(
        self,
        base_url: str,
        *,
        token: str | None = None,
        timeout: float | None = 300.0,
        allow_insecure_http: bool = False,
        semantic_validators: SemanticValidatorProvider | None = None,
    ) -> None:
        self.base_url = safe_http_base_url(
            base_url,
            setting="content observer base URL",
            allow_insecure_http=allow_insecure_http,
        )
        self.timeout = timeout
        self.token = token.strip() if token and token.strip() else None
        self.semantic_validators = semantic_validators

    def descriptor(self) -> ObserverDescriptor:
        descriptor = self._request("GET", "/v1/observer", ObserverDescriptor)
        try:
            require_semantic_validators(self.semantic_validators, descriptor)
        except ValueError as exc:
            raise ObserverProtocolError(
                "observer semantic acceptance profile is not enabled",
                failure_kind="unsupported_semantics",
            ) from exc
        return descriptor

    def observe(
        self,
        invocation: ObservationInvocation,
        *,
        descriptor: ObserverDescriptor,
    ) -> ObservationResult:
        result = self._request(
            "POST",
            "/v1/observe",
            ObservationResult,
            invocation,
        )
        try:
            accept_observation_result(
                result,
                invocation.request,
                descriptor,
                self.semantic_validators,
            )
        except (TypeError, ValueError) as exc:
            raise ObserverProtocolError(
                "observer returned a response inconsistent with the invocation",
                failure_kind="invalid_response",
            ) from exc
        return result

    def _request(
        self,
        method: str,
        path: str,
        model: type[ModelT],
        payload: BaseModel | None = None,
    ) -> ModelT:
        operation = http_operation_for_request(OBSERVER_HTTP_OPERATIONS, method, path)
        if operation is None:
            raise ValueError("observer client request is absent from its HTTP contract")
        try:
            with httpx.Client(timeout=self.timeout) as client:
                response = client.request(
                    method,
                    f"{self.base_url}{path}",
                    headers=({"Authorization": f"Bearer {self.token}"} if self.token else None),
                    json=(
                        payload.model_dump(mode="json", by_alias=True, exclude_none=True)
                        if payload is not None
                        else None
                    ),
                )
        except httpx.HTTPError as exc:
            raise ObserverProtocolError(
                f"observer request failed: {exc}",
                failure_kind="transport",
            ) from exc
        if response.status_code >= 400:
            try:
                code, message, details = parse_declared_error_payload(
                    operation,
                    status=response.status_code,
                    payload=response.json(),
                )
            except (TypeError, ValueError) as exc:
                raise ObserverProtocolError(
                    "observer returned an undeclared or invalid error response",
                    failure_kind="invalid_response",
                ) from exc
            raise ObserverProtocolError(
                message,
                failure_kind="remote_rejection",
                code=code,
                observed_status=response.status_code,
                details=details,
            )
        try:
            return model.model_validate(response.json())
        except (TypeError, ValueError) as exc:
            raise ObserverProtocolError(
                "observer returned an invalid protocol response",
                failure_kind="invalid_response",
            ) from exc


__all__ = ["ContentObserverClient", "ObserverProtocolError"]
