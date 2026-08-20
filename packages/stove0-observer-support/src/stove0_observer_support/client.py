"""Small HTTP client for independently maintained content observers."""

from __future__ import annotations

from typing import TypeVar

import httpx
from http_api_contracts import safe_http_base_url
from pydantic import BaseModel
from stove0_observer_protocol import (
    ObservationInvocation,
    ObservationResult,
    ObserverDescriptor,
)

ModelT = TypeVar("ModelT", bound=BaseModel)


class ObserverProtocolError(RuntimeError):
    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class ContentObserverClient:
    """The complete v1 HTTP binding: descriptor plus synchronous observation."""

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
            setting="content observer base URL",
            allow_insecure_http=allow_insecure_http,
        )
        self.timeout = timeout
        self.token = token.strip() if token and token.strip() else None

    def descriptor(self) -> ObserverDescriptor:
        return self._request("GET", "/v1/observer", ObserverDescriptor)

    def observe(self, invocation: ObservationInvocation) -> ObservationResult:
        return self._request(
            "POST",
            "/v1/observe",
            ObservationResult,
            invocation,
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
            raise ObserverProtocolError(f"observer request failed: {exc}") from exc
        if response.status_code >= 400:
            raise ObserverProtocolError(
                f"observer returned {response.status_code}: {response.text}",
                status_code=response.status_code,
            )
        try:
            return model.model_validate(response.json())
        except (TypeError, ValueError) as exc:
            raise ObserverProtocolError("observer returned an invalid protocol response") from exc


__all__ = ["ContentObserverClient", "ObserverProtocolError"]
