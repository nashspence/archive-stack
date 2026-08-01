"""HTTP adapter for Munchy's optional GPU manager and execution target."""

from __future__ import annotations

import os
from typing import Any

import httpx


class HttpGpuPlatform:
    def __init__(self) -> None:
        self.manager_url = os.getenv("MUNCHY_GPU_MANAGER_URL", "http://127.0.0.1:8080").rstrip("/")
        self.target_url = os.getenv("MUNCHY_GPU_TARGET_URL", "http://127.0.0.1:8000").rstrip("/")

    def manager_request(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        with httpx.Client(timeout=None) as client:
            response = client.request(method, f"{self.manager_url}{path}", json=payload)
        return self._object_response(response, service="gpu manager")

    def target_request(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        with httpx.Client(timeout=60.0) as client:
            response = client.request(method, f"{self.target_url}{path}", json=payload)
        return self._object_response(response, service="gpu target")

    @staticmethod
    def _object_response(response: httpx.Response, *, service: str) -> dict[str, Any]:
        if response.status_code >= 400:
            raise RuntimeError(f"{service} returned {response.status_code}: {response.text}")
        data = response.json()
        if not isinstance(data, dict):
            raise RuntimeError(f"{service} returned non-object JSON")
        return data
