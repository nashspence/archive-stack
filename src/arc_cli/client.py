from __future__ import annotations

import base64
import hashlib
import os
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx

from arc_core.domain.errors import (
    ArcError,
    BadRequest,
    Conflict,
    HashMismatch,
    InvalidState,
    InvalidTarget,
    NotFound,
    NotYetImplemented,
)


class ApiClient:
    def __init__(self, base_url: str | None = None, token: str | None = None) -> None:
        self.base_url = (base_url or os.getenv("ARC_BASE_URL") or "http://127.0.0.1:8000").rstrip(
            "/"
        )
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

    def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        with self._client() as client:
            response = client.request(method, path, **kwargs)
        self._raise_for_error(response)
        return response

    def _json(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        payload = self._request(method, path, **kwargs).json()
        if not isinstance(payload, dict):
            raise BadRequest("API returned a non-object JSON payload")
        return payload

    def create_or_resume_collection_upload(
        self,
        collection_id: str,
        files: Sequence[Mapping[str, Any]],
        *,
        ingest_source: str | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "collection_id": collection_id,
            "files": [dict(file) for file in files],
        }
        if ingest_source is not None:
            payload["ingest_source"] = ingest_source
        return self._json("POST", "/v1/collection-uploads", json=payload)

    def get_collection_upload(self, collection_id: str) -> dict[str, Any]:
        return self._json("GET", f"/v1/collection-uploads/{quote(collection_id, safe='/')}")

    def create_or_resume_collection_file_upload(
        self, collection_id: str, path: str
    ) -> dict[str, Any]:
        return self._json(
            "POST",
            f"/v1/collection-uploads/{quote(collection_id, safe='/')}/files/"
            f"{quote(path, safe='/')}/upload",
        )

    def search(self, query: str, limit: int = 25) -> dict[str, Any]:
        return self._json("GET", "/v1/search", params={"q": query, "limit": limit})

    def get_collection(self, collection_id: str) -> dict[str, Any]:
        return self._json("GET", f"/v1/collections/{quote(collection_id, safe='/')}")

    def list_collections(
        self,
        *,
        page: int = 1,
        per_page: int = 25,
        q: str | None = None,
        protection_state: str | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "page": page,
            "per_page": per_page,
        }
        if q:
            params["q"] = q
        if protection_state:
            params["protection_state"] = protection_state
        return self._json("GET", "/v1/collections", params=params)

    def get_plan(
        self,
        *,
        page: int = 1,
        per_page: int = 25,
        sort: str = "fill",
        order: str = "desc",
        query: str | None = None,
        collection: str | None = None,
        iso_ready: bool | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "page": page,
            "per_page": per_page,
            "sort": sort,
            "order": order,
        }
        if query:
            params["q"] = query
        if collection:
            params["collection"] = collection
        if iso_ready is not None:
            params["iso_ready"] = iso_ready
        return self._json("GET", "/v1/plan", params=params)

    def list_images(
        self,
        *,
        page: int = 1,
        per_page: int = 25,
        sort: str = "finalized_at",
        order: str = "desc",
        query: str | None = None,
        collection: str | None = None,
        has_copies: bool | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "page": page,
            "per_page": per_page,
            "sort": sort,
            "order": order,
        }
        if query:
            params["q"] = query
        if collection:
            params["collection"] = collection
        if has_copies is not None:
            params["has_copies"] = has_copies
        return self._json("GET", "/v1/images", params=params)

    def get_glacier_report(
        self,
        *,
        image_id: str | None = None,
        collection: str | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {}
        if image_id:
            params["image_id"] = image_id
        if collection:
            params["collection"] = collection
        return self._json("GET", "/v1/glacier", params=params)

    def finalize_image(self, candidate_id: str) -> dict[str, Any]:
        return self._json("POST", f"/v1/plan/candidates/{candidate_id}/finalize")

    def get_image(self, image_id: str) -> dict[str, Any]:
        return self._json("GET", f"/v1/images/{image_id}")

    def get_recovery_session_for_image(self, image_id: str) -> dict[str, Any]:
        return self._json(
            "GET",
            f"/v1/images/{quote(image_id, safe='/')}/recovery-session",
        )

    def get_recovery_session(self, session_id: str) -> dict[str, Any]:
        return self._json("GET", f"/v1/recovery-sessions/{quote(session_id, safe='/')}")

    def approve_recovery_session(self, session_id: str) -> dict[str, Any]:
        return self._json(
            "POST",
            f"/v1/recovery-sessions/{quote(session_id, safe='/')}/approve",
        )

    def complete_recovery_session(self, session_id: str) -> dict[str, Any]:
        return self._json(
            "POST",
            f"/v1/recovery-sessions/{quote(session_id, safe='/')}/complete",
        )

    def download_iso(self, image_id: str, output: Path | None = None) -> bytes:
        with self._client() as client:
            response = client.get(f"/v1/images/{image_id}/iso")
        self._raise_for_error(response)
        content = response.content
        if output is not None:
            output.write_bytes(content)
        return content

    def register_copy(
        self,
        image_id: str,
        location: str,
        *,
        copy_id: str | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"location": location}
        if copy_id is not None:
            payload["copy_id"] = copy_id
        return self._json("POST", f"/v1/images/{image_id}/copies", json=payload)

    def list_copies(self, image_id: str) -> dict[str, Any]:
        return self._json("GET", f"/v1/images/{image_id}/copies")

    def update_copy(
        self,
        image_id: str,
        copy_id: str,
        *,
        location: str | None = None,
        state: str | None = None,
        verification_state: str | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {}
        if location is not None:
            payload["location"] = location
        if state is not None:
            payload["state"] = state
        if verification_state is not None:
            payload["verification_state"] = verification_state
        return self._json("PATCH", f"/v1/images/{image_id}/copies/{copy_id}", json=payload)

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

    def create_or_resume_fetch_entry_upload(self, fetch_id: str, entry_id: str) -> dict[str, Any]:
        return self._json("POST", f"/v1/fetches/{fetch_id}/entries/{entry_id}/upload")

    def append_upload_chunk(
        self,
        upload_url: str,
        *,
        offset: int,
        checksum_algorithm: str,
        content: bytes,
    ) -> dict[str, Any]:
        checksum = base64.b64encode(hashlib.new(checksum_algorithm, content).digest()).decode(
            "ascii"
        )
        response = self._request(
            "PATCH",
            upload_url,
            headers={
                "Content-Type": "application/offset+octet-stream",
                "Tus-Resumable": "1.0.0",
                "Upload-Offset": str(offset),
                "Upload-Checksum": f"{checksum_algorithm} {checksum}",
            },
            content=content,
        )
        next_offset = int(response.headers.get("Upload-Offset", offset + len(content)))
        return {
            "offset": next_offset,
            "expires_at": response.headers.get("Upload-Expires"),
        }

    def complete_fetch(self, fetch_id: str) -> dict[str, Any]:
        return self._json("POST", f"/v1/fetches/{fetch_id}/complete")

    def list_collection_files(
        self,
        collection_id: str,
        *,
        page: int = 1,
        per_page: int = 25,
    ) -> dict[str, Any]:
        return self._json(
            "GET",
            f"/v1/collection-files/{quote(collection_id, safe='/')}",
            params={"page": page, "per_page": per_page},
        )

    def query_files(
        self,
        target: str,
        *,
        page: int = 1,
        per_page: int = 25,
    ) -> dict[str, Any]:
        return self._json(
            "GET",
            "/v1/files",
            params={"target": target, "page": page, "per_page": per_page},
        )

    def get_file_content(self, target: str, output: Path | None = None) -> bytes:
        response = self._request("GET", f"/v1/files/{quote(target, safe='/')}/content")
        content = response.content
        if output is not None:
            output.write_bytes(content)
        return content
