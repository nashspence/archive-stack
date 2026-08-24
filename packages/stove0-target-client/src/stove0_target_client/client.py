"""Black-box HTTP client for independently deployed stove0 targets."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar

import httpx
from http_api_contracts import parse_error_payload, safe_http_base_url
from pydantic import BaseModel
from stove0_target_protocol import (
    TargetCancelRequest,
    TargetContract,
    TargetJobRequest,
    TargetJobStatus,
    TargetPreflightRequest,
    TargetPreflightResponse,
)

ModelT = TypeVar("ModelT", bound=BaseModel)


class TargetProtocolError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        code: str = "target_client_error",
        observed_status: int | None = None,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        if observed_status is not None and not 400 <= observed_status <= 599:
            raise ValueError("observed HTTP status must be a 4xx or 5xx response")
        self.message = message
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
        return self._request("POST", "/v1/preflight", TargetPreflightResponse, request)

    def put_job(self, request: TargetJobRequest) -> TargetJobStatus:
        return self._request(
            "PUT",
            f"/v1/jobs/{request.declaration.job_id}",
            TargetJobStatus,
            request,
        )

    def status(self, job_id: str) -> TargetJobStatus:
        return self._request("GET", f"/v1/jobs/{job_id}", TargetJobStatus)

    def cancel(self, job_id: str, request: TargetCancelRequest | None = None) -> TargetJobStatus:
        return self._request(
            "POST",
            f"/v1/jobs/{job_id}/cancel",
            TargetJobStatus,
            request or TargetCancelRequest(),
        )

    def _request(
        self,
        method: str,
        path: str,
        model: type[ModelT],
        payload: BaseModel | None = None,
    ) -> ModelT:
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
            raise TargetProtocolError(
                f"target request failed: {exc}",
                code="target_transport_error",
            ) from exc
        if response.status_code >= 400:
            try:
                payload = response.json()
            except ValueError:
                payload = None
            code, message, details = parse_error_payload(
                payload,
                fallback_message=response.text or f"HTTP {response.status_code}",
            )
            raise TargetProtocolError(
                message,
                code=code,
                observed_status=response.status_code,
                details=details,
            )
        try:
            return model.model_validate(response.json())
        except (TypeError, ValueError) as exc:
            raise TargetProtocolError(
                "target returned an invalid protocol response",
                code="invalid_target_response",
            ) from exc


__all__ = ["TargetClient", "TargetProtocolError"]
