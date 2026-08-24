"""Official operator client for the Riverhog FTP adapter API."""

from __future__ import annotations

import math
import os
from typing import Any, Self
from urllib.parse import quote

import httpx
from http_api_contracts import (
    HealthResponse as HealthResponse,
)
from http_api_contracts import (
    parse_error_payload,
    safe_http_base_url,
)


class FtpAdapterApiError(RuntimeError):
    def __init__(
        self, message: str, *, code: str = "ftp_adapter_error", status: int | None = None
    ) -> None:
        super().__init__(message)
        self.code = code
        self.status = status


class RiverhogFtpAdapterClient:
    """Conventional authenticated client for one FTP adapter deployment."""

    def __init__(
        self,
        base_url: str | None = None,
        token: str | None = None,
        *,
        allow_insecure_http: bool | None = None,
        timeout_seconds: float | None = None,
        http2: bool | None = None,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        allow = (
            _bool_env("RIVERHOG_FTP_ADAPTER_ALLOW_INSECURE_HTTP", False)
            if allow_insecure_http is None
            else allow_insecure_http
        )
        self.base_url = safe_http_base_url(
            base_url or os.getenv("RIVERHOG_FTP_ADAPTER_BASE_URL") or "http://127.0.0.1:8082",
            setting="RIVERHOG_FTP_ADAPTER_BASE_URL",
            allow_insecure_http=allow,
        )
        self.allow_insecure_http = allow
        self.token = token or os.getenv("RIVERHOG_FTP_ADAPTER_TOKEN")
        self.http2 = _bool_env("RIVERHOG_FTP_ADAPTER_HTTP2", True) if http2 is None else http2
        self.timeout_seconds = (
            _timeout_env("RIVERHOG_FTP_ADAPTER_HTTP_TIMEOUT_SECONDS", 300.0)
            if timeout_seconds is None
            else _positive_seconds(timeout_seconds, "timeout_seconds")
        )
        headers = {"Accept": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        self._http = httpx.Client(
            base_url=self.base_url,
            headers=headers,
            http2=self.http2,
            timeout=self.timeout_seconds,
            transport=transport,
        )

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def close(self) -> None:
        self._http.close()

    def ftp_adapter_health_live(self) -> HealthResponse:
        return HealthResponse.model_validate(self._json("GET", "/health/live", authenticated=False))

    def ftp_adapter_health_ready(self) -> HealthResponse:
        return HealthResponse.model_validate(
            self._json("GET", "/health/ready", authenticated=False)
        )

    def get_ftp_adapter_status(self) -> dict[str, Any]:
        return self._json("GET", "/v1/status")

    def run_ftp_adapter_pass(self) -> dict[str, Any]:
        return self._json("POST", "/v1/run")

    def flush_ftp_adapter_source(self, source_id: str) -> dict[str, Any]:
        return self._json("POST", f"/v1/sources/{quote(source_id, safe='')}/flush")

    def _json(self, method: str, path: str, *, authenticated: bool = True) -> dict[str, Any]:
        if authenticated and not self.token:
            raise FtpAdapterApiError("RIVERHOG_FTP_ADAPTER_TOKEN is required", code="unauthorized")
        response = self._http.request(method, path)
        self._raise(response)
        payload = response.json()
        if not isinstance(payload, dict):
            raise FtpAdapterApiError(
                "FTP adapter returned a non-object response", code="invalid_response"
            )
        return payload

    @staticmethod
    def _raise(response: httpx.Response) -> None:
        if response.status_code < 400:
            return
        try:
            payload = response.json()
        except ValueError:
            payload = None
        code, message, _details = parse_error_payload(
            payload,
            fallback_message=response.text or f"HTTP {response.status_code}",
        )
        raise FtpAdapterApiError(message, code=code, status=response.status_code)


def _bool_env(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    normalized = raw.strip().casefold()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be true or false")


def _timeout_env(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        return _positive_seconds(float(raw), name)
    except ValueError as exc:
        raise ValueError(f"{name} must be a positive number of seconds") from exc


def _positive_seconds(value: float, name: str) -> float:
    if not math.isfinite(value) or value <= 0:
        raise ValueError(f"{name} must be a positive number of seconds")
    return value


__all__ = ["FtpAdapterApiError", "RiverhogFtpAdapterClient"]
