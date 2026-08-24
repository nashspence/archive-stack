"""Authenticated client for a configured terminal review sampler."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Self

import httpx
from http_api_contracts import parse_error_payload, safe_http_base_url
from stove0_review_sampler_protocol import (
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
        code: str = "sampler_client_error",
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
            self._descriptor = SamplerDescriptor.model_validate(self._json("GET", "/v1/sampler"))
        return self._descriptor

    def sample(self, request: SamplerRequest) -> SamplerResult:
        descriptor = self.descriptor()
        result = SamplerResult.model_validate(
            self._json(
                "POST",
                "/v1/sample",
                content=request.model_dump_json(exclude_none=True).encode(),
            )
        )
        validate_result(result, request, descriptor)
        return result

    def _json(self, method: str, path: str, *, content: bytes | None = None) -> object:
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
                code="sampler_transport_error",
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
            raise SamplerProtocolError(
                message,
                code=code,
                observed_status=response.status_code,
                details=details,
            )
        return response.json()


__all__ = ["ReviewSamplerClient", "SamplerProtocolError"]
