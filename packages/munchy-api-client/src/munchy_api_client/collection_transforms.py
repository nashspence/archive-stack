"""Official client for Munchy's collection transform surface."""

from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Any, Self

import httpx
from http_api_contracts import parse_error_payload, safe_http_base_url


class MunchyCollectionTransformError(RuntimeError):
    def __init__(self, *, status: int, code: str, message: str, details: Mapping[str, Any]) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.details = dict(details)


class MunchyCollectionTransformClient:
    def __init__(
        self,
        base_url: str | None = None,
        token: str | None = None,
        *,
        allow_insecure_http: bool = False,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.base_url = safe_http_base_url(
            base_url or os.getenv("MUNCHY_BASE_URL") or "http://127.0.0.1:8092",
            setting="MUNCHY_BASE_URL",
            allow_insecure_http=allow_insecure_http,
        )
        self.token = token or os.getenv("MUNCHY_TOKEN") or ""
        headers = {"Accept": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        self._client = httpx.Client(
            base_url=self.base_url,
            headers=headers,
            timeout=300,
            transport=transport,
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def create_or_resume_collection_transform(
        self,
        *,
        job_id: str,
        claim_id: str,
        fence: int,
        capability_token: str,
        intent: Mapping[str, object],
    ) -> dict[str, Any]:
        return self._json(
            "POST",
            "/v1/collection-transforms",
            json={
                "job_id": job_id,
                "claim_id": claim_id,
                "fence": fence,
                "capability_token": capability_token,
                "intent": dict(intent),
            },
        )

    def get_collection_transform(self, job_id: str) -> dict[str, Any]:
        return self._json("GET", f"/v1/collection-transforms/{job_id}")

    def _json(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        response = self._client.request(method, path, **kwargs)
        if not response.is_success:
            try:
                payload = response.json()
            except Exception:
                payload = None
            code, message, details = parse_error_payload(
                payload,
                fallback_message=response.text or f"HTTP {response.status_code}",
            )
            raise MunchyCollectionTransformError(
                status=response.status_code,
                code=str(code),
                message=str(message),
                details=details,
            )
        payload = response.json()
        if not isinstance(payload, dict):
            raise RuntimeError("Munchy collection transform API returned non-object JSON")
        return payload


__all__ = ["MunchyCollectionTransformClient", "MunchyCollectionTransformError"]
