from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import httpx

from arc_core.domain.errors import ArcError, BadRequest, Conflict, HashMismatch, InvalidState, InvalidTarget, NotFound, NotYetImplemented


class ApiClient:
    def __init__(self, base_url: str | None = None, token: str | None = None) -> None:
        self.base_url = (base_url or os.getenv("ARC_BASE_URL") or "http://127.0.0.1:8000").rstrip("/")
        self.token = token or os.getenv("ARC_TOKEN")

    def _client(self) -> httpx.Client:
        headers = {"Accept": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return httpx.Client(base_url=self.base_url, headers=headers, timeout=60.0)

    def _raise_for_error(self, response: httpx.Response) -> None:
        if response.is_success:
            return
        try:
            data = response.json()
        except Exception:  # pragma: no cover
            response.raise_for_status()
        error = data.get("error", {}) if isinstance(data, Mapping) else {}
        code = error.get("code", "bad_request")
        message = error.get("message", response.text)
        exc_map: dict[str, type[ArcError]] = {
            "bad_request": BadRequest,
            "invalid_target": InvalidTarget,
            "not_found": NotFound,
            "conflict": Conflict,
            "invalid_state": InvalidState,
            "hash_mismatch": HashMismatch,
            "not_implemented": NotYetImplemented,
        }
        raise exc_map.get(code, ArcError)(str(message))

    def _json(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        with self._client() as client:
            response = client.request(method, path, **kwargs)
        self._raise_for_error(response)
        return response.json()

    def close_collection(self, path: str) -> dict[str, Any]:
        return self._json("POST", "/v1/collections/close", json={"path": path})

    def search(self, query: str, limit: int = 25) -> dict[str, Any]:
        return self._json("GET", "/v1/search", params={"q": query, "limit": limit})

    def get_collection(self, collection_id: str) -> dict[str, Any]:
        return self._json("GET", f"/v1/collections/{collection_id}")

    def get_plan(self) -> dict[str, Any]:
        return self._json("GET", "/v1/plan")

    def get_image(self, image_id: str) -> dict[str, Any]:
        return self._json("GET", f"/v1/images/{image_id}")

    def download_iso(self, image_id: str, output: Path | None = None) -> bytes:
        with self._client() as client:
            response = client.get(f"/v1/images/{image_id}/iso")
        self._raise_for_error(response)
        content = response.content
        if output is not None:
            output.write_bytes(content)
        return content

    def register_copy(self, image_id: str, copy_id: str, location: str) -> dict[str, Any]:
        return self._json("POST", f"/v1/images/{image_id}/copies", json={"id": copy_id, "location": location})

    def pin(self, target: str) -> dict[str, Any]:
        return self._json("POST", "/v1/pin", json={"target": target})

    def release(self, target: str) -> dict[str, Any]:
        return self._json("POST", "/v1/release", json={"target": target})

    def list_pins(self) -> dict[str, Any]:
        return self._json("GET", "/v1/pins")

    def get_fetch(self, fetch_id: str) -> dict[str, Any]:
        return self._json("GET", f"/v1/fetches/{fetch_id}")

    def get_fetch_manifest(self, fetch_id: str) -> dict[str, Any]:
        return self._json("GET", f"/v1/fetches/{fetch_id}/manifest")

    def upload_fetch_entry(self, fetch_id: str, entry_id: str, sha256: str, content: bytes) -> dict[str, Any]:
        return self._json(
            "PUT",
            f"/v1/fetches/{fetch_id}/files/{entry_id}",
            headers={"X-Sha256": sha256, "Content-Type": "application/octet-stream"},
            content=content,
        )

    def complete_fetch(self, fetch_id: str) -> dict[str, Any]:
        return self._json("POST", f"/v1/fetches/{fetch_id}/complete")
