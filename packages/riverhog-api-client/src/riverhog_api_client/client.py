from __future__ import annotations

import os
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any, Self
from urllib.parse import quote, urlsplit, urlunsplit
from xml.etree import ElementTree

import httpx
from lifecycle_events import EventPage
from riverhog_protocol.errors import (
    BadRequest,
    Conflict,
    HashMismatch,
    InvalidPath,
    InvalidState,
    NotFound,
    NotYetImplemented,
    RiverhogError,
    ServiceUnavailable,
)

from riverhog_api_client.tus import TusHttpClient

_HTTP_TIMEOUT_SECONDS = 300.0
_CANCEL_TIMEOUT_SECONDS = 1800.0
_UPLOAD_TIMEOUT_SECONDS = 300.0
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
    ) -> None:
        self.base_url = (
            base_url or os.getenv("RIVERHOG_BASE_URL") or "http://127.0.0.1:8000"
        ).rstrip("/")
        self.token = token or os.getenv(token_env)
        self.host_header = os.getenv("RIVERHOG_HOST_HEADER", "").strip() or None
        self.verify_tls = _bool_env("RIVERHOG_TLS_VERIFY", True)
        self.http2 = _bool_env("RIVERHOG_HTTP2", True)
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
            verify=self.verify_tls,
            http2=self.http2,
        )

    def _client(self) -> httpx.Client:
        return self._make_client(
            timeout_seconds=_timeout_seconds("RIVERHOG_HTTP_TIMEOUT_SECONDS", _HTTP_TIMEOUT_SECONDS)
        )

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
        error = data.get("error", {}) if isinstance(data, Mapping) else {}
        code = error.get("code", "bad_request")
        message = error.get("message", response.text)
        exc_map: dict[str, type[RiverhogError]] = {
            "bad_request": BadRequest,
            "invalid_path": InvalidPath,
            "not_found": NotFound,
            "conflict": Conflict,
            "invalid_state": InvalidState,
            "hash_mismatch": HashMismatch,
            "not_implemented": NotYetImplemented,
            "service_unavailable": ServiceUnavailable,
        }
        raise exc_map.get(code, RiverhogError)(str(message))

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
        output: Path | None = None,
        *,
        progress: DownloadProgress | None = None,
    ) -> bytes | int:
        client = self._persistent_download_client()
        if output is None:
            response = client.get(path)
            self._raise_for_error(response)
            return response.content

        with client.stream("GET", path) as response:
            if not response.is_success:
                response.read()
                self._raise_for_error(response)

            content_length = response.headers.get("Content-Length")
            try:
                total_bytes = int(content_length) if content_length is not None else None
            except ValueError:
                total_bytes = None
            tmp_output = output.with_name(f".{output.name}.part")
            output.parent.mkdir(parents=True, exist_ok=True)

            downloaded = 0
            try:
                with tmp_output.open("wb") as handle:
                    for chunk in response.iter_bytes(chunk_size=_DOWNLOAD_CHUNK_BYTES):
                        if not chunk:
                            continue
                        handle.write(chunk)
                        downloaded += len(chunk)
                        if progress is not None:
                            progress(downloaded, total_bytes)
            except httpx.TransportError as exc:
                tmp_output.unlink(missing_ok=True)
                self.close()
                raise ServiceUnavailable(
                    "download stream was interrupted before completion; "
                    "the partial file was discarded"
                ) from exc
            tmp_output.replace(output)
            return downloaded


class ApiClient(_HttpApiClient):
    def __init__(self, base_url: str | None = None, token: str | None = None) -> None:
        super().__init__(base_url, token, token_env="RIVERHOG_TOKEN")
        self.upload_base_url = os.getenv("RIVERHOG_UPLOAD_BASE_URL", "").rstrip("/") or None
        self.upload_http2 = _bool_env("RIVERHOG_UPLOAD_HTTP2", False)
        self._upload_client: TusHttpClient | None = None

    def close(self) -> None:
        super().close()
        if self._upload_client is not None:
            self._upload_client.close()
            self._upload_client = None

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

    def _upload_headers(self) -> dict[str, str]:
        headers: dict[str, str] = {}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        if self.host_header:
            headers["Host"] = self.host_header
        return headers

    def tus_client(self) -> TusHttpClient:
        if self._upload_client is None:
            self._upload_client = TusHttpClient(
                headers=self._upload_headers(),
                timeout_seconds=_timeout_seconds(
                    "RIVERHOG_UPLOAD_TIMEOUT_SECONDS",
                    _UPLOAD_TIMEOUT_SECONDS,
                ),
                verify_tls=self.verify_tls,
                http2=self.upload_http2,
                url_rewriter=self._upload_url,
            )
        return self._upload_client

    def _upload_url(self, upload_url: str) -> str:
        if self.upload_base_url is None:
            return upload_url
        parsed = urlsplit(upload_url)
        if not parsed.scheme or not parsed.netloc:
            return upload_url
        base = urlsplit(self.upload_base_url)
        return urlunsplit((base.scheme, base.netloc, parsed.path, parsed.query, parsed.fragment))

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
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"files": _file_selections_payload(files)}
        if lease_seconds is not None:
            payload["lease_seconds"] = lease_seconds
        return self._json("POST", "/v1/retrieval-plans", json=payload)

    def create_retrieval_job(
        self,
        files: Sequence[tuple[int, str]],
        *,
        plan_etag: str,
        lease_seconds: int | None = None,
        event_context: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"files": _file_selections_payload(files)}
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

    def download_retrieval_file(
        self,
        job_id: str,
        *,
        collection_id: int,
        path: str,
        output: Path,
        progress: DownloadProgress | None = None,
    ) -> int:
        result = self._download(
            f"/v1/retrieval-jobs/{quote(job_id, safe='')}/content?"
            f"collection_id={str(collection_id)}&path={quote(path, safe='')}",
            output,
            progress=progress,
        )
        return int(result)

    def download_retrieval_object(
        self,
        job_id: str,
        *,
        collection_id: int,
        object_id: str,
        output: Path,
        progress: DownloadProgress | None = None,
    ) -> int:
        result = self._download(
            f"/v1/retrieval-jobs/{quote(job_id, safe='')}/objects/"
            f"{quote(object_id, safe='')}/content?"
            f"collection_id={str(collection_id)}",
            output,
            progress=progress,
        )
        return int(result)

    def create_or_resume_collection_upload_session(
        self,
        idempotency_key: str,
        tags: Sequence[str],
        *,
        ingest_source: str | None = None,
        archive_store: str | None = None,
        event_context: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "idempotency_key": idempotency_key,
            "tags": list(tags),
        }
        if ingest_source is not None:
            payload["ingest_source"] = ingest_source
        if archive_store is not None:
            payload["archive_store"] = archive_store
        if event_context is not None:
            payload["event_context"] = dict(event_context)
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

    def complete_collection_upload_session(
        self,
        collection_id: int,
        *,
        files_total: int,
        content_etag: str,
    ) -> dict[str, Any]:
        return self._json(
            "POST",
            f"/v1/collection-upload-sessions/{str(collection_id)}/complete",
            json={"files_total": files_total, "content_etag": content_etag},
        )

    def cancel_collection_upload_session(self, collection_id: int) -> dict[str, Any]:
        return self._json(
            "POST",
            f"/v1/collection-upload-sessions/{str(collection_id)}/cancel",
            timeout=_CANCEL_TIMEOUT_SECONDS,
        )

    def get_collection_upload_session(self, collection_id: int) -> dict[str, Any]:
        return self._json("GET", f"/v1/collection-upload-sessions/{str(collection_id)}")

    def create_or_resume_collection_file_upload(
        self, collection_id: int, path: str
    ) -> dict[str, Any]:
        return self._json(
            "POST",
            f"/v1/collection-upload-sessions/{str(collection_id)}/files/"
            f"{quote(path, safe='/')}/upload",
        )

    def create_or_resume_registered_collection_file_upload(
        self,
        collection_id: int,
        file: Mapping[str, Any],
    ) -> dict[str, Any]:
        return self._json(
            "POST",
            f"/v1/collection-upload-sessions/{str(collection_id)}/files/upload",
            json=dict(file),
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

    def plan_collection_deletion(self, collection_id: int) -> dict[str, Any]:
        return self._json(
            "POST",
            f"/v1/collections/{str(collection_id)}/deletion-plan",
        )

    def delete_collection(
        self,
        collection_id: int,
        *,
        challenge: str,
        event_context: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"challenge": challenge}
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
        if all_items:
            params["all"] = True
        return self._json("GET", "/v1/collections", params=params)

    def get_archive_report(
        self,
        *,
        collection: int | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {}
        if collection:
            params["collection"] = collection
        return self._json("GET", "/v1/archive", params=params)

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
        app: str,
        key_id: str,
        *,
        page: int = 1,
        per_page: int = 25,
        q: str | None = None,
        sort: str = "permission",
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
        return self._json(
            "GET",
            f"/v1/apps/{quote(app, safe='')}/keys/{quote(key_id, safe='')}/access",
            params=params,
        )

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
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "collection_id": collection_id,
            "destination_store": destination_store,
        }
        if source_store is not None:
            payload["source_store"] = source_store
        return self._json("POST", "/v1/archive/copies", json=payload)

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

    def append_upload_chunk(
        self,
        upload_url: str,
        *,
        offset: int,
        checksum_algorithm: str,
        content: bytes,
    ) -> dict[str, Any]:
        next_offset = self.tus_client().patch_chunk(
            upload_url,
            offset=offset,
            checksum_algorithm=checksum_algorithm,
            content=content,
        )
        return {
            "offset": next_offset,
            "expires_at": None,
        }

    def get_file_content(self, path: str, output: Path | None = None) -> bytes:
        response = self._request("GET", f"/v1/files/{quote(path, safe='/')}/content")
        content = response.content
        if output is not None:
            output.write_bytes(content)
        return content
