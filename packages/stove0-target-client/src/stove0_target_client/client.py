"""Black-box HTTP client for independently deployed stove0 targets."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any, Literal, TypeVar

import httpx
from http_api_contracts import (
    http_operation_for_request,
    parse_declared_error_payload,
    safe_http_base_url,
)
from pydantic import BaseModel, TypeAdapter, ValidationError
from stove0_target_protocol import (
    TARGET_HTTP_OPERATIONS,
    AcceptedTargetJob,
    OperationContract,
    Sha256,
    TargetContract,
    TargetJobRequest,
    TargetJobStatus,
    TargetPreflightRequest,
    TargetPreflightResponse,
    validate_preflight_response_against_request,
    validate_status_against_request,
)

ModelT = TypeVar("ModelT", bound=BaseModel)
_JOB_ID = TypeAdapter(Sha256)


class TargetProtocolError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        failure_kind: Literal["remote_rejection", "transport", "invalid_response"],
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


class TargetClient:
    def __init__(
        self,
        base_url: str,
        *,
        token: str | None = None,
        timeout: float | None = 300.0,
        allow_insecure_http: bool = False,
    ) -> None:
        self.base_url = safe_http_base_url(
            base_url,
            setting="target base URL",
            allow_insecure_http=allow_insecure_http,
        )
        self.timeout = timeout
        self.token = token.strip() if token and token.strip() else None

    def contract(self) -> TargetContract:
        return self._request("GET", "/v1/target", TargetContract)

    def preflight(self, request: TargetPreflightRequest) -> TargetPreflightResponse:
        response = self._request("POST", "/v1/preflight", TargetPreflightResponse, request)
        self._validate(lambda: validate_preflight_response_against_request(response, request))
        return response

    def put_job(
        self,
        request: TargetJobRequest,
        *,
        operation: OperationContract,
    ) -> TargetJobStatus:
        status = self._request(
            "PUT",
            f"/v1/jobs/{request.declaration.job_id}",
            TargetJobStatus,
            request,
        )
        self._validate(lambda: validate_status_against_request(status, request, operation))
        return status

    def status(
        self,
        request: TargetJobRequest | AcceptedTargetJob,
        *,
        operation: OperationContract,
    ) -> TargetJobStatus:
        if not isinstance(request, (TargetJobRequest, AcceptedTargetJob)):
            raise ValueError("target status requires exact accepted-request context")
        status = self._request(
            "GET",
            f"/v1/jobs/{_job_id(request.declaration.job_id)}",
            TargetJobStatus,
        )
        self._validate(lambda: validate_status_against_request(status, request, operation))
        return status

    def cancel(
        self,
        request: TargetJobRequest | AcceptedTargetJob,
        *,
        operation: OperationContract,
    ) -> TargetJobStatus:
        if not isinstance(request, (TargetJobRequest, AcceptedTargetJob)):
            raise ValueError("target cancellation requires exact accepted-request context")
        status = self._request(
            "POST",
            f"/v1/jobs/{_job_id(request.declaration.job_id)}/cancel",
            TargetJobStatus,
        )
        self._validate(lambda: validate_status_against_request(status, request, operation))
        return status

    @staticmethod
    def _validate(validation: Callable[[], None]) -> None:
        try:
            validation()
        except (TypeError, ValueError) as exc:
            raise TargetProtocolError(
                "target returned a response inconsistent with the request",
                failure_kind="invalid_response",
            ) from exc

    def _request(
        self,
        method: str,
        path: str,
        model: type[ModelT],
        payload: BaseModel | None = None,
    ) -> ModelT:
        operation = http_operation_for_request(TARGET_HTTP_OPERATIONS, method, path)
        if operation is None:
            raise ValueError("target client request is absent from its HTTP contract")
        request_kwargs: dict[str, Any] = {}
        if payload is not None:
            request_kwargs["json"] = payload.model_dump(
                mode="json",
                by_alias=True,
                exclude_none=True,
            )
        try:
            with httpx.Client(timeout=self.timeout) as client:
                response = client.request(
                    method,
                    f"{self.base_url}{path}",
                    headers=({"Authorization": f"Bearer {self.token}"} if self.token else None),
                    **request_kwargs,
                )
        except httpx.HTTPError as exc:
            raise TargetProtocolError(
                f"target request failed: {exc}",
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
                raise TargetProtocolError(
                    "target returned an undeclared or invalid error response",
                    failure_kind="invalid_response",
                ) from exc
            raise TargetProtocolError(
                message,
                failure_kind="remote_rejection",
                code=code,
                observed_status=response.status_code,
                details=details,
            )
        try:
            return model.model_validate(response.json())
        except (TypeError, ValueError) as exc:
            raise TargetProtocolError(
                "target returned an invalid protocol response",
                failure_kind="invalid_response",
            ) from exc


def _job_id(value: str) -> str:
    try:
        return _JOB_ID.validate_python(value, strict=True)
    except ValidationError as exc:
        raise ValueError("target job ID must be a lowercase SHA-256") from exc


__all__ = ["TargetClient", "TargetProtocolError"]
