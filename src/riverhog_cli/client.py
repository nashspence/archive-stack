from __future__ import annotations

import os
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit, urlunsplit

import httpx

from riverhog_core.domain.errors import (
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
from riverhog_core.tus_upload import TusHttpClient

_HTTP_TIMEOUT_SECONDS = 300.0
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
    files: Sequence[tuple[str, str]],
) -> list[dict[str, str]]:
    return [{"collection_id": collection_id, "path": path} for collection_id, path in files]


class ApiClient:
    def __init__(self, base_url: str | None = None, token: str | None = None) -> None:
        self.base_url = (
            base_url or os.getenv("RIVERHOG_BASE_URL") or "http://127.0.0.1:8000"
        ).rstrip("/")
        self.token = token or os.getenv("RIVERHOG_TOKEN")
        self.upload_base_url = os.getenv("RIVERHOG_UPLOAD_BASE_URL", "").rstrip("/") or None
        self.host_header = os.getenv("RIVERHOG_HOST_HEADER", "").strip() or None
        self.verify_tls = _bool_env("RIVERHOG_TLS_VERIFY", True)
        self.http2 = _bool_env("RIVERHOG_HTTP2", True)
        self.upload_http2 = _bool_env("RIVERHOG_UPLOAD_HTTP2", self.http2)
        self._request_client: httpx.Client | None = None
        self._upload_client: TusHttpClient | None = None

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
        if self._upload_client is not None:
            self._upload_client.close()
            self._upload_client = None

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

    def _upload_url(self, upload_url: str) -> str:
        if self.upload_base_url is None:
            return upload_url
        parsed = urlsplit(upload_url)
        if not parsed.scheme or not parsed.netloc:
            return upload_url
        base = urlsplit(self.upload_base_url)
        return urlunsplit((base.scheme, base.netloc, parsed.path, parsed.query, parsed.fragment))

    def _json(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        payload = self._request(method, path, **kwargs).json()
        if not isinstance(payload, dict):
            raise BadRequest("API returned a non-object JSON payload")
        return payload

    def create_or_resume_collection_upload(
        self,
        slug: str,
        files: Sequence[Mapping[str, Any]],
        *,
        ingest_source: str | None = None,
        upload_timestamp: str | None = None,
        archive_store: str | None = None,
        retain_hot: bool = True,
        notify: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "slug": slug,
            "files": [dict(file) for file in files],
            "retain_hot": retain_hot,
        }
        if ingest_source is not None:
            payload["ingest_source"] = ingest_source
        if upload_timestamp is not None:
            payload["upload_timestamp"] = upload_timestamp
        if archive_store is not None:
            payload["archive_store"] = archive_store
        if notify is not None:
            payload["notify"] = dict(notify)
        return self._json("POST", "/v1/collection-uploads", json=payload)

    def create_or_resume_collection_upload_session(
        self,
        slug: str,
        *,
        ingest_source: str | None = None,
        upload_timestamp: str | None = None,
        archive_store: str | None = None,
        retain_hot: bool = True,
        notify: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"slug": slug, "retain_hot": retain_hot}
        if ingest_source is not None:
            payload["ingest_source"] = ingest_source
        if upload_timestamp is not None:
            payload["upload_timestamp"] = upload_timestamp
        if archive_store is not None:
            payload["archive_store"] = archive_store
        if notify is not None:
            payload["notify"] = dict(notify)
        return self._json("POST", "/v1/collection-upload-sessions", json=payload)

    def register_collection_upload_session_file(
        self,
        collection_id: str,
        file: Mapping[str, Any],
    ) -> dict[str, Any]:
        return self._json(
            "POST",
            f"/v1/collection-upload-sessions/{quote(collection_id, safe='/')}/files",
            json=dict(file),
        )

    def complete_collection_upload_session(self, collection_id: str) -> dict[str, Any]:
        return self._json(
            "POST",
            f"/v1/collection-upload-sessions/{quote(collection_id, safe='/')}/complete",
        )

    def cancel_collection_upload_session(self, collection_id: str) -> dict[str, Any]:
        return self._json(
            "POST",
            f"/v1/collection-upload-sessions/{quote(collection_id, safe='/')}/cancel",
        )

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

    def create_or_resume_registered_collection_file_upload(
        self,
        collection_id: str,
        file: Mapping[str, Any],
    ) -> dict[str, Any]:
        return self._json(
            "POST",
            f"/v1/collection-upload-sessions/{quote(collection_id, safe='/')}/files/upload",
            json=dict(file),
        )

    def search(
        self,
        query: str | None = None,
        *,
        page: int = 1,
        per_page: int = 25,
        sort: str = "logical_path",
        order: str = "asc",
        collection: str | None = None,
        hot: bool | None = None,
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
        if hot is not None:
            params["hot"] = hot
        return self._json("GET", "/v1/search", params=params)

    def get_collection(self, collection_id: str) -> dict[str, Any]:
        return self._json(
            "GET",
            f"/v1/collections/{quote(collection_id, safe='/')}",
        )

    def plan_collection_deletion(self, collection_id: str) -> dict[str, Any]:
        return self._json(
            "POST",
            f"/v1/collections/{quote(collection_id, safe='/')}/deletion-plan",
        )

    def delete_collection(self, collection_id: str, *, challenge: str) -> dict[str, Any]:
        return self._json(
            "POST",
            f"/v1/collections/{quote(collection_id, safe='/')}/delete",
            json={"challenge": challenge},
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
        collection: str | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {}
        if collection:
            params["collection"] = collection
        return self._json("GET", "/v1/archive", params=params)

    def create_or_resume_archive_copy(
        self,
        collection_id: str,
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
        collection_id: str,
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
        collection_id: str,
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

    def list_archive_restores(
        self,
        *,
        page: int = 1,
        per_page: int = 25,
        sort: str = "created_at",
        order: str = "desc",
        terminal: str = "all",
        state: str | None = None,
        collection: str | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "page": page,
            "per_page": per_page,
            "sort": sort,
            "order": order,
            "terminal": terminal,
        }
        if state is not None:
            params["state"] = state
        if collection is not None:
            params["collection"] = collection
        return self._json("GET", "/v1/archive-restores", params=params)

    def get_archive_restore(self, restore_id: str) -> dict[str, Any]:
        return self._json("GET", f"/v1/archive-restores/{quote(restore_id, safe='/')}")

    def cancel_archive_restore(self, restore_id: str) -> dict[str, Any]:
        return self._json(
            "POST",
            f"/v1/archive-restores/{quote(restore_id, safe='/')}/cancel",
        )

    def _download(
        self,
        path: str,
        output: Path | None = None,
        *,
        progress: DownloadProgress | None = None,
    ) -> bytes | int:
        timeout_seconds = _timeout_seconds(
            "RIVERHOG_DOWNLOAD_TIMEOUT_SECONDS",
            _DOWNLOAD_TIMEOUT_SECONDS,
        )
        with self._make_client(timeout_seconds=timeout_seconds) as client:
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

    def list_fetches(
        self,
        *,
        page: int = 1,
        per_page: int = 25,
        state: str | None = None,
        query: str | None = None,
        sort: str = "order",
        order: str = "asc",
        all_items: bool = False,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "page": page,
            "per_page": per_page,
            "sort": sort,
            "order": order,
        }
        if state is not None:
            params["state"] = state
        if query:
            params["q"] = query
        if all_items:
            params["all"] = True
        return self._json("GET", "/v1/fetches", params=params)

    def create_fetch(
        self,
        *,
        name: str,
        collections: Sequence[str],
        files: Sequence[tuple[str, str]] = (),
    ) -> dict[str, Any]:
        return self._json(
            "POST",
            "/v1/fetches",
            json={
                "name": name,
                "collections": list(collections),
                "files": _file_selections_payload(files),
            },
        )

    def add_fetch_collections(
        self,
        fetch_id: str,
        collections: Sequence[str],
    ) -> dict[str, Any]:
        return self._json(
            "POST",
            f"/v1/fetches/{quote(fetch_id, safe='/')}/collections",
            json={"collections": list(collections)},
        )

    def remove_fetch_collections(
        self,
        fetch_id: str,
        collections: Sequence[str],
    ) -> dict[str, Any]:
        return self._json(
            "DELETE",
            f"/v1/fetches/{quote(fetch_id, safe='/')}/collections",
            json={"collections": list(collections)},
        )

    def add_fetch_files(
        self,
        fetch_id: str,
        files: Sequence[tuple[str, str]],
    ) -> dict[str, Any]:
        return self._json(
            "POST",
            f"/v1/fetches/{quote(fetch_id, safe='/')}/files",
            json={"files": _file_selections_payload(files)},
        )

    def remove_fetch_files(
        self,
        fetch_id: str,
        files: Sequence[tuple[str, str]],
    ) -> dict[str, Any]:
        return self._json(
            "DELETE",
            f"/v1/fetches/{quote(fetch_id, safe='/')}/files",
            json={"files": _file_selections_payload(files)},
        )

    def start_fetch(self, fetch_id: str) -> dict[str, Any]:
        return self._json(
            "POST",
            f"/v1/fetches/{quote(fetch_id, safe='/')}/start",
        )

    def cancel_fetch(self, fetch_id: str) -> dict[str, Any]:
        return self._json("POST", f"/v1/fetches/{quote(fetch_id, safe='/')}/cancel")

    def evict_hot(
        self,
        collections: Sequence[str] = (),
        *,
        files: Sequence[tuple[str, str]] = (),
        dry_run: bool = False,
    ) -> dict[str, Any]:
        return self._json(
            "POST",
            "/v1/hot/evict",
            json={
                "collections": list(collections),
                "files": _file_selections_payload(files),
                "dry_run": dry_run,
            },
        )

    def get_fetch(self, fetch_id: str) -> dict[str, Any]:
        return self._json("GET", f"/v1/fetches/{fetch_id}")

    def get_fetch_status(self, fetch_id: str) -> dict[str, Any]:
        return self._json("GET", f"/v1/fetches/{fetch_id}/status")

    def list_fetch_files(
        self,
        fetch_id: str,
        *,
        page: int = 1,
        per_page: int = 25,
        sort: str = "logical_path",
        order: str = "asc",
        query: str | None = None,
        hot: bool | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "page": page,
            "per_page": per_page,
            "sort": sort,
            "order": order,
        }
        if query:
            params["q"] = query
        if hot is not None:
            params["hot"] = hot
        return self._json(
            "GET",
            f"/v1/fetches/{quote(fetch_id, safe='/')}/files",
            params=params,
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

    def query_files(
        self,
        path: str,
        *,
        page: int = 1,
        per_page: int = 25,
    ) -> dict[str, Any]:
        return self._json(
            "GET",
            "/v1/files",
            params={"path": path, "page": page, "per_page": per_page},
        )

    def get_jeb_status(self, *, include_backlog: bool = True) -> dict[str, Any]:
        return self._json(
            "GET",
            "/v1/jeb/status",
            params={"include_backlog": str(include_backlog).lower()},
        )

    def list_jeb_attempts(
        self,
        *,
        page: int = 1,
        per_page: int = 25,
        sort: str = "updated_at",
        order: str = "desc",
        terminal: str = "active",
        state: str | None = None,
        source: str | None = None,
        collection_slug: str | None = None,
        target: str | None = None,
        query: str | None = None,
        all_items: bool = False,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "page": page,
            "per_page": per_page,
            "sort": sort,
            "order": order,
            "terminal": terminal,
        }
        for key, value in {
            "state": state,
            "source": source,
            "collection_slug": collection_slug,
            "target": target,
            "q": query,
        }.items():
            if value is not None:
                params[key] = value
        if all_items:
            params["all"] = True
        return self._json("GET", "/v1/jeb/attempts", params=params)

    def check_jeb_config(self) -> dict[str, Any]:
        return self._json("GET", "/v1/jeb/config/check")

    def run_jeb_once(self) -> dict[str, Any]:
        return self._json("POST", "/v1/jeb/once")

    def archive_jeb_now(
        self,
        *,
        source: str,
        process: bool = True,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        return self._json(
            "POST",
            "/v1/jeb/archive-now",
            json={"source": source, "process": process, "dry_run": dry_run},
        )

    def list_jeb_sources(
        self,
        *,
        page: int = 1,
        per_page: int = 25,
        sort: str = "id",
        order: str = "asc",
        query: str | None = None,
        enabled: bool | None = None,
        adapter: str | None = None,
        target: str | None = None,
        all_items: bool = False,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "page": page,
            "per_page": per_page,
            "sort": sort,
            "order": order,
        }
        for key, value in {
            "q": query,
            "enabled": None if enabled is None else str(enabled).lower(),
            "adapter": adapter,
            "target": target,
        }.items():
            if value is not None:
                params[key] = value
        if all_items:
            params["all"] = True
        return self._json("GET", "/v1/jeb/sources", params=params)

    def get_jeb_source(self, source_id: str) -> dict[str, Any]:
        return self._json("GET", f"/v1/jeb/sources/{quote(source_id, safe='')}")

    def add_jeb_source(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        return self._json("POST", "/v1/jeb/sources", json=dict(payload))

    def update_jeb_source(
        self,
        source_id: str,
        changes: Mapping[str, Any],
    ) -> dict[str, Any]:
        return self._json(
            "PATCH",
            f"/v1/jeb/sources/{quote(source_id, safe='')}",
            json=dict(changes),
        )

    def set_jeb_source_enabled(self, source_id: str, *, enabled: bool) -> dict[str, Any]:
        action = "enable" if enabled else "disable"
        return self._json(
            "POST",
            f"/v1/jeb/sources/{quote(source_id, safe='')}/{action}",
        )

    def rotate_jeb_source_credential(
        self,
        source_id: str,
        *,
        credential: str | None = None,
    ) -> dict[str, Any]:
        payload = {} if credential is None else {"credential": credential}
        return self._json(
            "POST",
            f"/v1/jeb/sources/{quote(source_id, safe='')}/credential",
            json=payload,
        )

    def plan_jeb_source_removal(
        self,
        source_id: str,
        *,
        purge: bool,
    ) -> dict[str, Any]:
        return self._json(
            "POST",
            f"/v1/jeb/sources/{quote(source_id, safe='')}/removal-plan",
            json={"purge": purge},
        )

    def remove_jeb_source(self, source_id: str, *, challenge: str) -> dict[str, Any]:
        return self._json(
            "DELETE",
            f"/v1/jeb/sources/{quote(source_id, safe='')}",
            json={"challenge": challenge},
        )

    def get_file_content(self, path: str, output: Path | None = None) -> bytes:
        response = self._request("GET", f"/v1/files/{quote(path, safe='/')}/content")
        content = response.content
        if output is not None:
            output.write_bytes(content)
        return content
