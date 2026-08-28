"""Authenticated client for a configured terminal review sampler."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Literal, Self

import httpx
from http_api_contracts import (
    http_operation_for_request,
    parse_declared_error_payload,
    safe_http_base_url,
)
from stove0_review_sampler_protocol import (
    SAMPLER_HTTP_OPERATIONS,
    SamplerDescriptor,
    SamplerRequest,
    SamplerResult,
    validate_result,
)


class SamplerProtocolError(RuntimeError):
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


class ReviewSamplerClient:
    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        allow_insecure_http: bool = False,
        timeout_seconds: float = 86400,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.base_url = safe_http_base_url(
            base_url,
            setting="sampler base URL",
            allow_insecure_http=allow_insecure_http,
        )
        credential = token.strip()
        if not credential:
            raise ValueError("sampler bearer token must be nonempty")
        self._http = httpx.Client(
            base_url=self.base_url,
            headers={"Authorization": f"Bearer {credential}", "Accept": "application/json"},
            timeout=timeout_seconds,
            transport=transport,
        )
        self._descriptor: SamplerDescriptor | None = None

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def close(self) -> None:
        self._http.close()

    def descriptor(self, *, refresh: bool = False) -> SamplerDescriptor:
        if self._descriptor is None or refresh:
            try:
                self._descriptor = SamplerDescriptor.model_validate(
                    self._json("GET", "/v1/sampler")
                )
            except SamplerProtocolError:
                raise
            except (TypeError, ValueError) as exc:
                raise SamplerProtocolError(
                    "sampler returned an invalid descriptor",
                    failure_kind="invalid_response",
                ) from exc
        return self._descriptor

    def sample(self, request: SamplerRequest) -> SamplerResult:
        descriptor = self.descriptor()
        try:
            result = SamplerResult.model_validate(
                self._json(
                    "POST",
                    "/v1/sample",
                    content=request.model_dump_json(exclude_none=True).encode(),
                )
            )
            validate_result(result, request, descriptor)
        except SamplerProtocolError:
            raise
        except (TypeError, ValueError) as exc:
            raise SamplerProtocolError(
                "sampler returned a response inconsistent with the request",
                failure_kind="invalid_response",
            ) from exc
        return result

    def _json(self, method: str, path: str, *, content: bytes | None = None) -> object:
        operation = http_operation_for_request(SAMPLER_HTTP_OPERATIONS, method, path)
        if operation is None:
            raise ValueError("sampler client request is absent from its HTTP contract")
        try:
            response = self._http.request(
                method,
                path,
                content=content,
                headers={"Content-Type": "application/json"} if content is not None else None,
            )
        except httpx.HTTPError as exc:
            raise SamplerProtocolError(
                f"sampler request failed: {exc}",
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
                raise SamplerProtocolError(
                    "sampler returned an undeclared or invalid error response",
                    failure_kind="invalid_response",
                ) from exc
            raise SamplerProtocolError(
                message,
                failure_kind="remote_rejection",
                code=code,
                observed_status=response.status_code,
                details=details,
            )
        try:
            return response.json()
        except ValueError as exc:
            raise SamplerProtocolError(
                "sampler returned an invalid protocol response",
                failure_kind="invalid_response",
            ) from exc


__all__ = ["ReviewSamplerClient", "SamplerProtocolError"]
