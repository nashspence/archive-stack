from __future__ import annotations

import hashlib
import os
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Self
from urllib.parse import quote
from xml.etree import ElementTree

import httpx
from file_download import verified_download
from http_api_contracts import parse_error_payload, safe_http_base_url
from lifecycle_events import EventPage
from riverhog_protocol.errors import (
    BadRequest,
    Conflict,
    DownloadAllowanceExceeded,
    Forbidden,
    HashMismatch,
    InvalidPath,
    InvalidState,
    NotFound,
    RiverhogError,
    ServiceUnavailable,
    Unauthorized,
)

from riverhog_api_client.workflows import CollectionWorkflowMethods

_HTTP_TIMEOUT_SECONDS = 300.0
_UPLOAD_TIMEOUT_SECONDS = 1800.0
_CANCEL_TIMEOUT_SECONDS = 1800.0
_DOWNLOAD_TIMEOUT_SECONDS = 3600.0
_DOWNLOAD_CHUNK_BYTES = 8 * 1024 * 1024
_TRANSIENT_HTTP_STATUS_CODES = {408, 425, 429, 500, 502, 503, 504}
DownloadProgress = Callable[[int, int | None], None]


def _bool_env(env_name: str, default: bool) -> bool:
    raw_value = os.getenv(env_name)
    if raw_value is None or raw_value.strip() == "":
        return default
    normalized = raw_value.strip().casefold()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise BadRequest(f"{env_name} must be true or false")


def _timeout_seconds(env_name: str, default: float) -> float:
    raw_value = os.getenv(env_name)
    if raw_value is None or raw_value.strip() == "":
        return default
    try:
        value = float(raw_value)
    except ValueError as exc:
        raise BadRequest(f"{env_name} must be a positive number of seconds") from exc
    if value <= 0:
        raise BadRequest(f"{env_name} must be a positive number of seconds")
    return value


def _file_selections_payload(
    files: Sequence[tuple[int, str]],
) -> list[dict[str, object]]:
    return [{"collection_id": collection_id, "path": path} for collection_id, path in files]


class _HttpApiClient:
    def __init__(
        self,
        base_url: str | None = None,
        token: str | None = None,
        *,
        token_env: str,
        allow_insecure_http: bool | None = None,
    ) -> None:
        self.allow_insecure_http = (
            _bool_env("RIVERHOG_ALLOW_INSECURE_HTTP", False)
            if allow_insecure_http is None
            else allow_insecure_http
        )
        try:
            self.base_url = safe_http_base_url(
                base_url or os.getenv("RIVERHOG_BASE_URL") or "http://127.0.0.1:8000",
                setting="RIVERHOG_BASE_URL",
                allow_insecure_http=self.allow_insecure_http,
            )
        except ValueError as exc:
            raise BadRequest(str(exc)) from exc
        self.token = token or os.getenv(token_env)
        self.host_header = os.getenv("RIVERHOG_HOST_HEADER", "").strip() or None
        self.http2 = _bool_env("RIVERHOG_HTTP2", True)
        self.timeout_seconds = _timeout_seconds(
            "RIVERHOG_HTTP_TIMEOUT_SECONDS",
            _HTTP_TIMEOUT_SECONDS,
        )
        self._request_client: httpx.Client | None = None
        self._download_client: httpx.Client | None = None

    def _make_client(self, *, timeout_seconds: float) -> httpx.Client:
        headers = {"Accept": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        if self.host_header:
            headers["Host"] = self.host_header
        return httpx.Client(
            base_url=self.base_url,
            headers=headers,
            timeout=timeout_seconds,
            http2=self.http2,
        )

    def _client(self) -> httpx.Client:
        return self._make_client(timeout_seconds=self.timeout_seconds)

    def _persistent_client(self) -> httpx.Client:
        if self._request_client is None:
            self._request_client = self._client()
        return self._request_client

    def close(self) -> None:
        if self._request_client is not None:
            self._request_client.close()
            self._request_client = None
        if self._download_client is not None:
            self._download_client.close()
            self._download_client = None

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def _persistent_download_client(self) -> httpx.Client:
        if self._download_client is None:
            self._download_client = self._make_client(
                timeout_seconds=_timeout_seconds(
                    "RIVERHOG_DOWNLOAD_TIMEOUT_SECONDS",
                    _DOWNLOAD_TIMEOUT_SECONDS,
                )
            )
        return self._download_client

    def _raise_for_error(self, response: httpx.Response) -> None:
        if response.is_success:
            return
        try:
            data = response.json()
        except Exception:  # pragma: no cover
            response.raise_for_status()
        code, message, details = parse_error_payload(
            data,
            fallback_message=response.text or f"HTTP {response.status_code}",
        )
        exc_map: dict[str, type[RiverhogError]] = {
            "bad_request": BadRequest,
            "unauthorized": Unauthorized,
            "forbidden": Forbidden,
            "invalid_path": InvalidPath,
            "not_found": NotFound,
            "conflict": Conflict,
            "invalid_state": InvalidState,
            "hash_mismatch": HashMismatch,
            "service_unavailable": ServiceUnavailable,
            "download_allowance_exceeded": DownloadAllowanceExceeded,
        }
        error_type = exc_map.get(code)
        if error_type is None:
            error_type = (
                ServiceUnavailable
                if response.status_code in _TRANSIENT_HTTP_STATUS_CODES
                else RiverhogError
            )
        raise error_type(
            str(message),
            code=str(code),
            status=response.status_code,
            details=details,
        )

    def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        try:
            response = self._persistent_client().request(method, path, **kwargs)
        except httpx.TransportError:
            self.close()
            raise
        if response.status_code in _TRANSIENT_HTTP_STATUS_CODES:
            self.close()
        self._raise_for_error(response)
        return response

    def _json(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        payload = self._request(method, path, **kwargs).json()
        if not isinstance(payload, dict):
            raise BadRequest("API returned a non-object JSON payload")
        return payload

    def _download(
        self,
        path: str,
        output: Path,
        *,
        expected_bytes: int,
        expected_sha256: str,
        progress: DownloadProgress | None = None,
    ) -> int:
        client = self._persistent_download_client()
        with client.stream("GET", path) as response:
            if not response.is_success:
                response.read()
                self._raise_for_error(response)

            content_length = response.headers.get("Content-Length")
            try:
                returned_bytes = int(content_length) if content_length is not None else -1
            except ValueError as exc:
                raise InvalidState("download returned an invalid Content-Length") from exc
            if returned_bytes != expected_bytes:
                raise InvalidState(
                    "download Content-Length does not match planned metadata: "
                    f"{returned_bytes} != {expected_bytes}"
                )
            returned_etag = response.headers.get("ETag", "").strip().strip('"').casefold()
            if returned_etag != expected_sha256.casefold():
                raise InvalidState("download ETag does not match planned SHA-256")
            try:
                receipt = verified_download(
                    response.iter_bytes(chunk_size=_DOWNLOAD_CHUNK_BYTES),
                    output=output,
                    expected_bytes=expected_bytes,
                    expected_sha256=expected_sha256,
                    progress=(
                        (lambda current, total: progress(current, total))
                        if progress is not None
                        else None
                    ),
                )
            except httpx.TransportError as exc:
                self.close()
                raise ServiceUnavailable(
                    "download stream was interrupted before completion; "
                    "the partial file was discarded"
                ) from exc
            return receipt.bytes


class ApiClient(CollectionWorkflowMethods, _HttpApiClient):
    def __init__(
        self,
        base_url: str | None = None,
        token: str | None = None,
        *,
        allow_insecure_http: bool | None = None,
    ) -> None:
        super().__init__(
            base_url,
            token,
            token_env="RIVERHOG_TOKEN",
            allow_insecure_http=allow_insecure_http,
        )
        self.upload_timeout_seconds = _timeout_seconds(
            "RIVERHOG_UPLOAD_TIMEOUT_SECONDS",
            _UPLOAD_TIMEOUT_SECONDS,
        )

    def spawn(self) -> ApiClient:
        worker = ApiClient(
            base_url=self.base_url,
            token=self.token,
            allow_insecure_http=self.allow_insecure_http,
        )
        worker.host_header = self.host_header
        worker.http2 = self.http2
        worker.timeout_seconds = self.timeout_seconds
        worker.upload_timeout_seconds = self.upload_timeout_seconds
        return worker

    def list_lifecycle_events(
        self,
        *,
        after: str | None = None,
        limit: int = 100,
    ) -> EventPage:
        payload = self._json(
            "GET",
            "/v1/events",
            params={"after": after or "0", "limit": limit},
        )
        return EventPage.model_validate(payload)

    def resourcesync_discovery(self) -> dict[str, object]:
        response = self._request("GET", "/.well-known/resourcesync")
        root = ElementTree.fromstring(response.content)
        capabilities: list[dict[str, str]] = []
        for url in root:
            location = next((child.text for child in url if child.tag.endswith("loc")), None)
            metadata = next((child for child in url if child.tag.endswith("md")), None)
            if location and metadata is not None:
                capabilities.append(
                    {
                        "capability": str(metadata.attrib.get("capability", "")),
                        "location": location,
                    }
                )
        return {"capabilities": capabilities}

    def resourcesync_capabilities(self) -> dict[str, object]:
        response = self._request("GET", "/resourcesync/capabilitylist.xml")
        root = ElementTree.fromstring(response.content)
        capabilities: list[dict[str, str]] = []
        for url in root:
            location = next((child.text for child in url if child.tag.endswith("loc")), None)
            metadata = next((child for child in url if child.tag.endswith("md")), None)
            if location and metadata is not None:
                capabilities.append(
                    {
                        "capability": str(metadata.attrib.get("capability", "")),
                        "location": location,
                    }
                )
        return {"capabilities": capabilities}

    def resourcesync_resource_pages(self) -> dict[str, object]:
        response = self._request("GET", "/resourcesync/resourcelist.xml")
        root = ElementTree.fromstring(response.content)
        pages = [
            location
            for sitemap in root
            if sitemap.tag.endswith("sitemap")
            if (
                location := next(
                    (child.text for child in sitemap if child.tag.endswith("loc")),
                    None,
                )
            )
        ]
        return {"pages": pages}

    def resourcesync_resources(self, *, page: int = 1) -> dict[str, object]:
        if page < 1:
            raise ValueError("ResourceSync resource-list page must be positive")
        response = self._request("GET", f"/resourcesync/resourcelist/{page}.xml")
        root = ElementTree.fromstring(response.content)
        resources: list[dict[str, object]] = []
        for url in root:
            if not url.tag.endswith("url"):
                continue
            location = next((child.text for child in url if child.tag.endswith("loc")), None)
            metadata = next((child for child in url if child.tag.endswith("md")), None)
            if location is None or metadata is None:
                continue
            collection_id = int(
                location.split("/v1/catalog/collections/", 1)[-1].rsplit("/manifest", 1)[0]
            )
            resources.append(
                {
                    "collection_id": collection_id,
                    "etag": metadata.attrib.get("hash", "").removeprefix("sha-256:"),
                    "location": location,
                }
            )
        return {"page": page, "resources": resources}

    def catalog_changes(self, *, after: int = 0) -> dict[str, Any]:
        response = self._request(
            "GET",
            "/resourcesync/changelist.xml",
            params={"after": after},
        )
        root = ElementTree.fromstring(response.content)
        changes: list[dict[str, Any]] = []
        for url in root:
            loc = next((child.text for child in url if child.tag.endswith("loc")), None)
            metadata = next((child for child in url if child.tag.endswith("md")), None)
            if loc is None or metadata is None:
                continue
            collection_id = int(
                loc.split("/v1/catalog/collections/", 1)[-1].rsplit("/manifest", 1)[0]
            )
            changes.append(
                {
                    "collection_id": collection_id,
                    "change": metadata.attrib.get("change"),
                    "datetime": metadata.attrib.get("datetime"),
                    "etag": metadata.attrib.get("hash", "").removeprefix("sha-256:"),
                }
            )
        return {
            "cursor": int(root.attrib.get("data-cursor", after)),
            "has_more": root.attrib.get("data-has-more", "false") == "true",
            "changes": changes,
        }

    def get_portable_collection_manifest(self, collection_id: int) -> dict[str, Any]:
        return self._json(
            "GET",
            f"/v1/catalog/collections/{str(collection_id)}/manifest",
        )

    def plan_retrieval(
        self,
        files: Sequence[tuple[int, str]],
        *,
        lease_seconds: int | None = None,
        restore_policy: str = "allow",
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "files": _file_selections_payload(files),
            "restore_policy": restore_policy,
        }
        if lease_seconds is not None:
            payload["lease_seconds"] = lease_seconds
        return self._json("POST", "/v1/retrieval-plans", json=payload)

    def create_retrieval_job(
        self,
        files: Sequence[tuple[int, str]],
        *,
        plan_etag: str,
        lease_seconds: int | None = None,
        restore_policy: str = "allow",
        event_context: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "files": _file_selections_payload(files),
            "restore_policy": restore_policy,
        }
        if lease_seconds is not None:
            payload["lease_seconds"] = lease_seconds
        if event_context is not None:
            payload["event_context"] = dict(event_context)
        return self._json(
            "POST",
            "/v1/retrieval-jobs",
            json=payload,
            headers={"If-Match": f'"{plan_etag}"'},
        )

    def get_retrieval_job(self, job_id: str) -> dict[str, Any]:
        return self._json("GET", f"/v1/retrieval-jobs/{quote(job_id, safe='')}")

    def cancel_retrieval_job(self, job_id: str) -> dict[str, Any]:
        return self._json("DELETE", f"/v1/retrieval-jobs/{quote(job_id, safe='')}")

    def acknowledge_retrieval_job(self, job_id: str) -> dict[str, Any]:
        return self._json("POST", f"/v1/retrieval-jobs/{quote(job_id, safe='')}/ack")

    def renew_retrieval_job(self, job_id: str, *, lease_seconds: int) -> dict[str, Any]:
        return self._json(
            "POST",
            f"/v1/retrieval-jobs/{quote(job_id, safe='')}/renew",
            json={"lease_seconds": lease_seconds},
        )

    def retrieval_cache_status(self) -> dict[str, Any]:
        return self._json("GET", "/v1/retrieval-cache")

    def list_retrieval_cache_objects(
        self,
        *,
        page: int = 1,
        per_page: int = 25,
        q: str | None = None,
        tag: str | None = None,
        collection_id: int | None = None,
        source_store: str | None = None,
        state: str | None = None,
        protection: str | None = None,
        expires_before: str | None = None,
        expires_after: str | None = None,
        sort: str = "cached_at",
        order: str = "desc",
        all_items: bool = False,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "page": page,
            "per_page": per_page,
            "sort": sort,
            "order": order,
        }
        if q:
            params["q"] = q
        if tag:
            params["tag"] = tag
        if collection_id is not None:
            params["collection_id"] = collection_id
        if source_store:
            params["source_store"] = source_store
        if state:
            params["state"] = state
        if protection:
            params["protection"] = protection
        if expires_before:
            params["expires_before"] = expires_before
        if expires_after:
            params["expires_after"] = expires_after
        if all_items:
            params["all"] = True
        return self._json("GET", "/v1/retrieval-cache/objects", params=params)

    def get_retrieval_cache_object(
        self,
        collection_id: int,
        source_store: str,
        object_id: str,
    ) -> dict[str, Any]:
        return self._json(
            "GET",
            "/v1/retrieval-cache/objects/"
            f"{str(collection_id)}/{quote(source_store, safe='')}/{quote(object_id, safe='')}",
        )

    def download_retrieval_file(
        self,
        job_id: str,
        *,
        collection_id: int,
        path: str,
        output: Path,
        expected_bytes: int,
        expected_sha256: str,
        progress: DownloadProgress | None = None,
    ) -> int:
        result = self._download(
            f"/v1/retrieval-jobs/{quote(job_id, safe='')}/content?"
            f"collection_id={str(collection_id)}&path={quote(path, safe='')}",
            output,
            expected_bytes=expected_bytes,
            expected_sha256=expected_sha256,
            progress=progress,
        )
        return int(result)

    @contextmanager
    def stream_retrieval_file(
        self,
        job_id: str,
        *,
        collection_id: int,
        path: str,
        expected_bytes: int,
        expected_sha256: str,
        start: int = 0,
        end: int | None = None,
        chunk_size: int = _DOWNLOAD_CHUNK_BYTES,
    ) -> Iterator[Iterator[bytes]]:
        """Stream one verified retrieval file or byte range.

        The returned iterator must be consumed completely before leaving the
        context. Full-file reads are SHA-256 verified; range reads are bound to
        the whole-file ETag and exact Content-Range returned by Riverhog.
        """

        if isinstance(expected_bytes, bool) or expected_bytes < 0:
            raise ValueError("expected retrieval bytes must be non-negative")
        digest = expected_sha256.casefold()
        if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
            raise ValueError("expected retrieval SHA-256 is invalid")
        resolved_end = expected_bytes if end is None else end
        if (
            isinstance(start, bool)
            or isinstance(resolved_end, bool)
            or start < 0
            or resolved_end < start
            or resolved_end > expected_bytes
        ):
            raise ValueError("retrieval byte range is invalid")
        if chunk_size < 1:
            raise ValueError("retrieval stream chunk size must be positive")
        partial = start != 0 or resolved_end != expected_bytes
        if partial and start == resolved_end:
            raise ValueError("retrieval byte range must be nonempty")
        headers: dict[str, str] = {"Accept-Encoding": "identity"}
        if partial:
            headers["Range"] = f"bytes={start}-{resolved_end - 1}"
        client = self._persistent_download_client()
        request_path = f"/v1/retrieval-jobs/{quote(job_id, safe='')}/content"
        params: dict[str, str | int] = {"collection_id": collection_id, "path": path}
        try:
            with client.stream("GET", request_path, params=params, headers=headers) as response:
                if not response.is_success:
                    response.read()
                    self._raise_for_error(response)
                expected_status = 206 if partial else 200
                if response.status_code != expected_status:
                    raise InvalidState(
                        "retrieval stream returned an unexpected HTTP status: "
                        f"{response.status_code}"
                    )
                returned_etag = response.headers.get("ETag", "").strip().strip('"').casefold()
                if returned_etag != digest:
                    raise InvalidState("retrieval stream ETag does not match the planned SHA-256")
                expected_length = resolved_end - start
                raw_length = response.headers.get("Content-Length")
                try:
                    returned_length = int(raw_length) if raw_length is not None else -1
                except ValueError as exc:
                    raise InvalidState(
                        "retrieval stream returned an invalid Content-Length"
                    ) from exc
                if returned_length != expected_length:
                    raise InvalidState(
                        "retrieval stream Content-Length does not match the requested range"
                    )
                if partial:
                    expected_range = f"bytes {start}-{resolved_end - 1}/{expected_bytes}"
                    if response.headers.get("Content-Range") != expected_range:
                        raise InvalidState("retrieval stream Content-Range is inconsistent")

                returned = 0
                hasher = hashlib.sha256() if not partial else None

                def chunks() -> Iterator[bytes]:
                    nonlocal returned
                    for chunk in response.iter_bytes(chunk_size=chunk_size):
                        if not chunk:
                            continue
                        returned += len(chunk)
                        if returned > expected_length:
                            raise InvalidState("retrieval stream exceeded the requested byte range")
                        if hasher is not None:
                            hasher.update(chunk)
                        yield chunk

                yield chunks()
                if returned != expected_length:
                    raise InvalidState("retrieval stream ended before the requested byte range")
                if hasher is not None and hasher.hexdigest() != digest:
                    raise HashMismatch("retrieval stream SHA-256 verification failed")
        except httpx.TransportError as exc:
            self.close()
            raise ServiceUnavailable("retrieval stream was interrupted") from exc

    def create_or_resume_collection_upload_session(
        self,
        idempotency_key: str,
        tags: Sequence[str],
        *,
        ingest_source: str | None = None,
        archive_store: str | None = None,
        event_context: Mapping[str, Any] | None = None,
        provenance_mode: str = "captured",
        provenance_omission_reason: str | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "idempotency_key": idempotency_key,
            "tags": list(tags),
            "provenance_mode": provenance_mode,
        }
        if ingest_source is not None:
            payload["ingest_source"] = ingest_source
        if archive_store is not None:
            payload["archive_store"] = archive_store
        if event_context is not None:
            payload["event_context"] = dict(event_context)
        if provenance_omission_reason is not None:
            payload["provenance_omission_reason"] = provenance_omission_reason
        return self._json("POST", "/v1/collection-upload-sessions", json=payload)

    def register_collection_upload_session_files(
        self,
        collection_id: int,
        files: Sequence[Mapping[str, Any]],
    ) -> dict[str, Any]:
        return self._json(
            "POST",
            f"/v1/collection-upload-sessions/{str(collection_id)}/files",
            json={"files": [dict(file) for file in files]},
        )

    def list_collection_upload_session_files(
        self,
        collection_id: int,
        *,
        page: int = 1,
        per_page: int = 25,
        all_items: bool = False,
    ) -> dict[str, Any]:
        return self._json(
            "GET",
            f"/v1/collection-upload-sessions/{str(collection_id)}/files",
            params={
                "page": page,
                "per_page": per_page,
                "all": str(all_items).lower(),
            },
        )

    def put_collection_upload_session_provenance_journal(
        self,
        collection_id: int,
        journal_id: str,
        *,
        content: bytes,
        sha256: str,
    ) -> dict[str, Any]:
        return self._json(
            "PUT",
            f"/v1/collection-upload-sessions/{str(collection_id)}/provenance/journals/"
            f"{quote(journal_id, safe='')}",
            headers={
                "Content-Type": "application/json-seq",
                "X-Riverhog-Provenance-SHA256": sha256,
            },
            content=content,
            timeout=self.upload_timeout_seconds,
        )

    def export_collection_upload_session_provenance_journal(
        self,
        collection_id: int,
        journal_id: str,
    ) -> bytes:
        response = self._request(
            "GET",
            f"/v1/collection-upload-sessions/{str(collection_id)}/provenance/journals/"
            f"{quote(journal_id, safe='')}",
        )
        content = response.content
        expected = response.headers.get("ETag", "").strip().strip('"')
        if not expected or hashlib.sha256(content).hexdigest() != expected:
            raise InvalidState("staged provenance journal does not match its ETag")
        return content

    def list_collection_upload_sessions(
        self,
        *,
        page: int = 1,
        per_page: int = 25,
        q: str | None = None,
        state: str | None = None,
        tag: str | None = None,
        sort: str = "created_at",
        order: str = "desc",
        all_items: bool = False,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "page": page,
            "per_page": per_page,
            "sort": sort,
            "order": order,
        }
        if q:
            params["q"] = q
        if state:
            params["state"] = state
        if tag:
            params["tag"] = tag
        if all_items:
            params["all"] = True
        return self._json("GET", "/v1/collection-upload-sessions", params=params)

    def complete_collection_upload_session(
        self,
        collection_id: int,
        *,
        files_total: int,
        content_identity: str,
        provenance_identity: str | None,
    ) -> dict[str, Any]:
        return self._json(
            "POST",
            f"/v1/collection-upload-sessions/{str(collection_id)}/complete",
            json={
                "files_total": files_total,
                "content_identity": content_identity,
                "provenance_identity": provenance_identity,
            },
        )

    def cancel_collection_upload_session(self, collection_id: int) -> dict[str, Any]:
        return self._json(
            "POST",
            f"/v1/collection-upload-sessions/{str(collection_id)}/cancel",
            timeout=_CANCEL_TIMEOUT_SECONDS,
        )

    def get_collection_upload_session(self, collection_id: int) -> dict[str, Any]:
        return self._json("GET", f"/v1/collection-upload-sessions/{str(collection_id)}")

    def list_collection_upload_session_volumes(self, collection_id: int) -> dict[str, Any]:
        return self._json(
            "GET",
            f"/v1/collection-upload-sessions/{str(collection_id)}/volumes",
        )

    def get_collection_upload_session_volume(
        self,
        collection_id: int,
        volume_id: str,
    ) -> dict[str, Any]:
        return self._json(
            "GET",
            f"/v1/collection-upload-sessions/{str(collection_id)}/volumes/"
            f"{quote(volume_id, safe='')}",
        )

    def get_collection_upload_session_unit(
        self,
        collection_id: int,
        volume_id: str,
        unit: int,
    ) -> dict[str, Any]:
        return self._json(
            "GET",
            f"/v1/collection-upload-sessions/{str(collection_id)}/volumes/"
            f"{quote(volume_id, safe='')}/units/{str(unit)}",
        )

    def put_collection_upload_session_unit(
        self,
        collection_id: int,
        volume_id: str,
        unit: int,
        *,
        plan_sha256: str,
        content: bytes,
    ) -> dict[str, Any]:
        return self._json(
            "PUT",
            f"/v1/collection-upload-sessions/{str(collection_id)}/volumes/"
            f"{quote(volume_id, safe='')}/units/{str(unit)}",
            headers={
                "Content-Type": "application/octet-stream",
                "If-Match": f'"{plan_sha256}"',
            },
            content=content,
            timeout=self.upload_timeout_seconds,
        )

    def search(
        self,
        query: str | None = None,
        *,
        page: int = 1,
        per_page: int = 25,
        sort: str = "file_ref",
        order: str = "asc",
        collection: int | None = None,
        all_items: bool = False,
    ) -> dict[str, Any]:
        params: dict[str, object] = {
            "page": page,
            "per_page": per_page,
            "sort": sort,
            "order": order,
        }
        if query:
            params["q"] = query
        if collection:
            params["collection"] = collection
        if all_items:
            params["all"] = True
        return self._json("GET", "/v1/search", params=params)

    def get_collection(self, collection_id: int) -> dict[str, Any]:
        return self._json(
            "GET",
            f"/v1/collections/{str(collection_id)}",
        )

    def list_collection_provenance(
        self,
        collection_id: int,
        *,
        page: int = 1,
        per_page: int = 25,
        q: str | None = None,
        status: str | None = None,
        sort: str = "path",
        order: str = "asc",
        all_items: bool = False,
    ) -> dict[str, Any]:
        params: dict[str, object] = {
            "page": page,
            "per_page": per_page,
            "sort": sort,
            "order": order,
        }
        if q:
            params["q"] = q
        if status:
            params["status"] = status
        if all_items:
            params["all"] = True
        return self._json(
            "GET",
            f"/v1/collections/{collection_id}/provenance/files",
            params=params,
        )

    def get_collection_file_provenance(
        self,
        collection_id: int,
        path: str,
    ) -> dict[str, Any]:
        return self._json(
            "GET",
            f"/v1/collections/{collection_id}/provenance/files/{quote(path, safe='/')}",
        )

    def trace_collection_file_provenance(
        self,
        collection_id: int,
        path: str,
    ) -> dict[str, Any]:
        return self._json(
            "GET",
            f"/v1/collections/{collection_id}/provenance/trace/{quote(path, safe='/')}",
        )

    def export_collection_provenance_journal(
        self,
        collection_id: int,
        journal_id: str,
    ) -> bytes:
        response = self._request(
            "GET",
            f"/v1/collections/{collection_id}/provenance/journals/{quote(journal_id, safe='')}",
        )
        content = response.content
        expected = response.headers.get("ETag", "").strip().strip('"')
        if not expected or hashlib.sha256(content).hexdigest() != expected:
            raise InvalidState("provenance export does not match its ETag")
        return content

    def verify_collection_provenance(self, collection_id: int) -> dict[str, Any]:
        return self._json(
            "POST",
            f"/v1/collections/{collection_id}/provenance/verify",
        )

    def plan_collection_deletion(
        self,
        collection_id: int,
        *,
        retirement_claim_id: str | None = None,
    ) -> dict[str, Any]:
        params = (
            {"retirement_claim_id": retirement_claim_id}
            if retirement_claim_id is not None
            else None
        )
        return self._json(
            "POST",
            f"/v1/collections/{str(collection_id)}/deletion-plan",
            params=params,
        )

    def delete_collection(
        self,
        collection_id: int,
        *,
        challenge: str,
        retirement_claim_id: str | None = None,
        event_context: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"challenge": challenge}
        if retirement_claim_id is not None:
            payload["retirement_claim_id"] = retirement_claim_id
        if event_context is not None:
            payload["event_context"] = dict(event_context)
        return self._json(
            "POST",
            f"/v1/collections/{str(collection_id)}/delete",
            json=payload,
        )

    def list_collections(
        self,
        *,
        page: int = 1,
        per_page: int = 25,
        q: str | None = None,
        tag: str | None = None,
        sort: str = "id",
        order: str = "asc",
        all_items: bool = False,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "page": page,
            "per_page": per_page,
        }
        if sort != "id":
            params["sort"] = sort
        if order != "asc":
            params["order"] = order
        if q:
            params["q"] = q
        if tag:
            params["tag"] = tag
        if all_items:
            params["all"] = True
        return self._json("GET", "/v1/collections", params=params)

    def list_archive_stores(
        self,
        *,
        page: int = 1,
        per_page: int = 25,
        q: str | None = None,
        sort: str = "store",
        order: str = "asc",
        all_items: bool = False,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "page": page,
            "per_page": per_page,
            "sort": sort,
            "order": order,
        }
        if q:
            params["q"] = q
        if all_items:
            params["all"] = True
        return self._json("GET", "/v1/archive/stores", params=params)

    def get_archive_store(self, store: str) -> dict[str, Any]:
        return self._json("GET", f"/v1/archive/stores/{quote(store, safe='')}")

    def list_apps(
        self,
        *,
        page: int = 1,
        per_page: int = 25,
        q: str | None = None,
        sort: str = "name",
        order: str = "asc",
        active: bool | None = None,
        all_items: bool = False,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "page": page,
            "per_page": per_page,
            "sort": sort,
            "order": order,
        }
        if q:
            params["q"] = q
        if active is not None:
            params["active"] = str(active).lower()
        if all_items:
            params["all"] = True
        return self._json("GET", "/v1/apps", params=params)

    def create_app_key(
        self,
        app: str,
        *,
        access: Sequence[Mapping[str, str]],
        expires_in_seconds: int | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "access": [dict(current) for current in access],
        }
        if expires_in_seconds is not None:
            payload["expires_in_seconds"] = expires_in_seconds
        return self._json(
            "POST",
            f"/v1/apps/{quote(app, safe='')}/keys",
            json=payload,
        )

    def list_app_keys(
        self,
        app: str,
        *,
        page: int = 1,
        per_page: int = 25,
        q: str | None = None,
        sort: str = "created_at",
        order: str = "desc",
        active: bool | None = None,
        all_items: bool = False,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "page": page,
            "per_page": per_page,
            "sort": sort,
            "order": order,
        }
        if q:
            params["q"] = q
        if active is not None:
            params["active"] = str(active).lower()
        if all_items:
            params["all"] = True
        return self._json(
            "GET",
            f"/v1/apps/{quote(app, safe='')}/keys",
            params=params,
        )

    def revoke_app_key(self, app: str, key_id: str) -> dict[str, Any]:
        return self._json(
            "POST",
            f"/v1/apps/{quote(app, safe='')}/keys/{quote(key_id, safe='')}/revoke",
        )

    def rotate_app_key(self, app: str, key_id: str) -> dict[str, Any]:
        return self._json(
            "POST",
            f"/v1/apps/{quote(app, safe='')}/keys/{quote(key_id, safe='')}/rotate",
        )

    def list_app_key_access(
        self,
        *,
        page: int = 1,
        per_page: int = 25,
        q: str | None = None,
        sort: str = "permission",
        order: str = "asc",
        app: str | None = None,
        key_id: str | None = None,
        permission: str | None = None,
        resource: str | None = None,
        active: bool | None = None,
        all_items: bool = False,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "page": page,
            "per_page": per_page,
            "sort": sort,
            "order": order,
        }
        if q:
            params["q"] = q
        if app:
            params["app"] = app
        if key_id:
            params["key"] = key_id
        if permission:
            params["permission"] = permission
        if resource:
            params["resource"] = resource
        if active is not None:
            params["active"] = str(active).lower()
        if all_items:
            params["all"] = True
        return self._json("GET", "/v1/app-key-access", params=params)

    def replace_app_key_access(
        self,
        app: str,
        key_id: str,
        *,
        access: Sequence[Mapping[str, str]],
    ) -> dict[str, Any]:
        return self._json(
            "PUT",
            f"/v1/apps/{quote(app, safe='')}/keys/{quote(key_id, safe='')}/access",
            json={"access": [dict(current) for current in access]},
        )

    def add_app_key_access(
        self,
        app: str,
        key_id: str,
        *,
        permission: str,
        resource: str,
    ) -> dict[str, Any]:
        return self._json(
            "POST",
            f"/v1/apps/{quote(app, safe='')}/keys/{quote(key_id, safe='')}/access",
            json={"permission": permission, "resource": resource},
        )

    def remove_app_key_access(
        self,
        app: str,
        key_id: str,
        *,
        permission: str,
        resource: str,
    ) -> dict[str, Any]:
        return self._json(
            "DELETE",
            f"/v1/apps/{quote(app, safe='')}/keys/{quote(key_id, safe='')}/access",
            json={"permission": permission, "resource": resource},
        )

    def create_tag(self, tag: str) -> dict[str, Any]:
        return self._json("POST", "/v1/tags", json={"id": tag})

    def get_tag(self, tag: str) -> dict[str, Any]:
        return self._json("GET", f"/v1/tags/{quote(tag, safe='')}")

    def list_tags(
        self,
        *,
        page: int = 1,
        per_page: int = 25,
        q: str | None = None,
        sort: str = "id",
        order: str = "asc",
        all_items: bool = False,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "page": page,
            "per_page": per_page,
            "sort": sort,
            "order": order,
        }
        if q:
            params["q"] = q
        if all_items:
            params["all"] = True
        return self._json("GET", "/v1/tags", params=params)

    def plan_tag_deletion(self, tag: str) -> dict[str, Any]:
        return self._json(
            "POST",
            f"/v1/tags/{quote(tag, safe='')}/deletion-plan",
        )

    def delete_tag(self, tag: str, *, challenge: str) -> dict[str, Any]:
        return self._json(
            "POST",
            f"/v1/tags/{quote(tag, safe='')}/delete",
            json={"challenge": challenge},
        )

    def get_collection_tags(self, collection_id: int) -> dict[str, Any]:
        return self._json("GET", f"/v1/collections/{collection_id}/tags")

    def replace_collection_tags(
        self,
        collection_id: int,
        tags: Sequence[str],
        *,
        event_context: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"tags": list(tags)}
        if event_context is not None:
            payload["event_context"] = dict(event_context)
        return self._json("PUT", f"/v1/collections/{collection_id}/tags", json=payload)

    def add_collection_tag(
        self,
        collection_id: int,
        tag: str,
        *,
        event_context: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        payload = {} if event_context is None else {"event_context": dict(event_context)}
        return self._json(
            "POST",
            f"/v1/collections/{collection_id}/tags/{quote(tag, safe='')}",
            json=payload,
        )

    def remove_collection_tag(
        self,
        collection_id: int,
        tag: str,
        *,
        event_context: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        payload = {} if event_context is None else {"event_context": dict(event_context)}
        return self._json(
            "DELETE",
            f"/v1/collections/{collection_id}/tags/{quote(tag, safe='')}",
            json=payload,
        )

    def get_download_quota(self) -> dict[str, Any]:
        return self._json("GET", "/v1/download-quota")

    def set_app_key_download_quota(
        self,
        app: str,
        key_id: str,
        *,
        monthly_bytes: int | None,
    ) -> dict[str, Any]:
        return self._json(
            "PUT",
            f"/v1/apps/{quote(app, safe='')}/keys/{quote(key_id, safe='')}/download-quota",
            json={"monthly_bytes": monthly_bytes},
        )

    def list_download_quotas(
        self,
        *,
        page: int = 1,
        per_page: int = 25,
        q: str | None = None,
        sort: str = "app",
        order: str = "asc",
        app: str | None = None,
        active: bool | None = None,
        all_items: bool = False,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "page": page,
            "per_page": per_page,
            "sort": sort,
            "order": order,
        }
        if q:
            params["q"] = q
        if app:
            params["app"] = app
        if active is not None:
            params["active"] = str(active).lower()
        if all_items:
            params["all"] = True
        return self._json("GET", "/v1/download-quotas", params=params)

    def create_or_resume_archive_copy(
        self,
        collection_id: int,
        *,
        destination_store: str,
        source_store: str | None = None,
        event_context: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "collection_id": collection_id,
            "destination_store": destination_store,
        }
        if source_store is not None:
            payload["source_store"] = source_store
        if event_context is not None:
            payload["event_context"] = dict(event_context)
        return self._json("POST", "/v1/archive/copies", json=payload)

    def list_archive_copy_jobs(
        self,
        *,
        page: int = 1,
        per_page: int = 25,
        q: str | None = None,
        state: str | None = None,
        sort: str = "requested_at",
        order: str = "desc",
        all_items: bool = False,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "page": page,
            "per_page": per_page,
            "sort": sort,
            "order": order,
        }
        if q:
            params["q"] = q
        if state:
            params["state"] = state
        if all_items:
            params["all"] = True
        return self._json("GET", "/v1/archive/copies", params=params)

    def get_archive_copy_job(
        self,
        collection_id: int,
        *,
        destination_store: str,
    ) -> dict[str, Any]:
        return self._json(
            "GET",
            f"/v1/archive/copies/{collection_id}/{quote(destination_store, safe='')}",
        )

    def cancel_archive_copy_job(
        self,
        collection_id: int,
        *,
        destination_store: str,
    ) -> dict[str, Any]:
        return self._json(
            "DELETE",
            f"/v1/archive/copies/{collection_id}/{quote(destination_store, safe='')}",
        )

    def plan_archive_copy_retirement(
        self,
        collection_id: int,
        *,
        store: str,
    ) -> dict[str, Any]:
        return self._json(
            "POST",
            "/v1/archive/copies/retirement-plan",
            json={"collection_id": collection_id, "store": store},
        )

    def retire_archive_copy(
        self,
        collection_id: int,
        *,
        store: str,
        challenge: str,
    ) -> dict[str, Any]:
        return self._json(
            "POST",
            "/v1/archive/copies/retire",
            json={
                "collection_id": collection_id,
                "store": store,
                "challenge": challenge,
            },
        )
