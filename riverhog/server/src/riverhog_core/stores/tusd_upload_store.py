from __future__ import annotations

import base64
import threading
from collections.abc import Iterator
from typing import Any
from urllib.parse import quote, unquote, urljoin, urlsplit, urlunsplit

import httpx
from riverhog_protocol.errors import Conflict, NotFound, ServiceUnavailable

from riverhog_core.runtime_config import RuntimeConfig
from riverhog_core.stores.s3_support import create_ingress_s3_client, delete_exact_object
from riverhog_core.tusd_ids import tusd_upload_id_for_target_path

_TIMEOUT = 300.0
_HOOK_SECRET_HEADER = "X-Riverhog-Tusd-Hook-Secret"


def _ok_or_raise(response: httpx.Response) -> None:
    if response.status_code not in (200, 204, 404):
        response.raise_for_status()


class _TusdHttpUploadStore:
    def __init__(self, config: RuntimeConfig) -> None:
        self._tusd_base_url = config.tusd_base_url.rstrip("/")
        self._hook_secret = config.tusd_hook_secret
        self._append_timeout_seconds = config.tusd_append_timeout.total_seconds()
        self._clients = threading.local()

    def _metadata_header(self, target_path: str) -> str:
        upload_id = tusd_upload_id_for_target_path(target_path)
        encoded = base64.b64encode(upload_id.encode("utf-8")).decode("ascii")
        return f"upload_id {encoded}"

    def _tus_headers(self, **headers: str) -> dict[str, str]:
        return {
            "Tus-Resumable": "1.0.0",
            _HOOK_SECRET_HEADER: self._hook_secret,
            **headers,
        }

    def _normalize_tusd_location(self, location: str) -> str:
        joined = urljoin(f"{self._tusd_base_url}/", location)
        parsed = urlsplit(joined)
        base_path = urlsplit(self._tusd_base_url).path.rstrip("/")
        prefix = f"{base_path}/"
        if not parsed.path.startswith(prefix):
            return joined
        upload_id = parsed.path.removeprefix(prefix)
        normalized_path = f"{prefix}{quote(upload_id, safe='+')}"
        return urlunsplit(
            (parsed.scheme, parsed.netloc, normalized_path, parsed.query, parsed.fragment)
        )

    def _http_client(self, name: str, *, timeout: float) -> httpx.Client:
        clients = getattr(self._clients, "items", None)
        if clients is None:
            clients = {}
            self._clients.items = clients
        client = clients.get(name)
        if client is None:
            client = httpx.Client(timeout=timeout)
            clients[name] = client
        return client

    def _drop_client(self, name: str) -> None:
        clients = getattr(self._clients, "items", None)
        if not isinstance(clients, dict):
            return
        client = clients.pop(name, None)
        if client is not None:
            client.close()

    def create_upload(self, target_path: str, length: int) -> str:
        try:
            response = self._http_client("control", timeout=_TIMEOUT).post(
                self._tusd_base_url,
                headers=self._tus_headers(
                    **{
                        "Upload-Length": str(length),
                        "Upload-Metadata": self._metadata_header(target_path),
                    }
                ),
            )
        except httpx.TransportError:
            self._drop_client("control")
            raise
        response.raise_for_status()
        location = response.headers["Location"]
        return self._normalize_tusd_location(location)

    def get_offset(self, tus_url: str) -> int:
        try:
            response = self._http_client("control", timeout=_TIMEOUT).head(
                tus_url,
                headers=self._tus_headers(),
            )
        except httpx.TransportError:
            self._drop_client("control")
            raise
        if response.status_code == 404:
            return -1
        response.raise_for_status()
        return int(response.headers["Upload-Offset"])

    def append_upload_chunk(
        self,
        tus_url: str,
        *,
        offset: int,
        checksum: str,
        content: bytes,
    ) -> tuple[int, str | None]:
        try:
            response = self._http_client("append", timeout=self._append_timeout_seconds).patch(
                tus_url,
                headers=self._tus_headers(
                    **{
                        "Content-Type": "application/offset+octet-stream",
                        "Upload-Offset": str(offset),
                        "Upload-Checksum": checksum,
                    }
                ),
                content=content,
            )
        except httpx.TransportError as exc:
            self._drop_client("append")
            raise ServiceUnavailable(
                "upload backend did not accept the chunk before the server-side timeout; "
                "retry the chunk after resyncing the offset"
            ) from exc
        if response.status_code in {400, 409}:
            message = response.text.strip() or "upload chunk was rejected by tusd"
            raise Conflict(message)
        response.raise_for_status()
        return int(response.headers["Upload-Offset"]), response.headers.get("Upload-Expires")

    def cancel_upload(self, tus_url: str) -> None:
        try:
            response = self._http_client("control", timeout=_TIMEOUT).delete(
                tus_url,
                headers=self._tus_headers(),
            )
        except httpx.TransportError:
            self._drop_client("control")
            raise
        _ok_or_raise(response)

    def _upload_id_from_tus_url(self, tus_url: str) -> str | None:
        parsed = urlsplit(tus_url)
        base_path = urlsplit(self._tusd_base_url).path.rstrip("/")
        prefix = f"{base_path}/"
        if not parsed.path.startswith(prefix):
            return None
        return unquote(parsed.path.removeprefix(prefix))


class TusdUploadStore(_TusdHttpUploadStore):
    def __init__(self, config: RuntimeConfig) -> None:
        super().__init__(config)
        self._store = config.ingress_store
        self._s3 = create_ingress_s3_client(config, self._store)

    @staticmethod
    def _upload_id(target_path: str) -> str:
        return tusd_upload_id_for_target_path(target_path)

    def _object_key(self, upload_id: str) -> str:
        parts = [part for part in (self._store.prefix, upload_id.lstrip("/")) if part]
        return "/".join(parts)

    def read_target(self, target_path: str) -> bytes:
        try:
            response = self._s3.get_object(
                Bucket=self._store.bucket,
                Key=self._object_key(self._upload_id(target_path)),
            )
            body = response["Body"]
            try:
                return bytes(body.read())
            finally:
                close = getattr(body, "close", None)
                if callable(close):
                    close()
        except Exception as exc:
            if not _is_missing_object(exc):
                raise
            raise NotFound(f"upload target not found: {target_path}") from exc

    def iter_target(
        self,
        target_path: str,
        *,
        offset: int = 0,
        size: int | None = None,
    ) -> Iterator[bytes]:
        if offset < 0:
            raise ValueError("upload target offset must be non-negative")
        if size is not None and size < 0:
            raise ValueError("upload target size must be non-negative")
        if size == 0:
            return

        request: dict[str, object] = {
            "Bucket": self._store.bucket,
            "Key": self._object_key(self._upload_id(target_path)),
        }
        if offset or size is not None:
            end = "" if size is None else str(offset + size - 1)
            request["Range"] = f"bytes={offset}-{end}"
        try:
            response = self._s3.get_object(**request)
            body = response["Body"]
            remaining = size
            try:
                for chunk in body.iter_chunks(chunk_size=1024 * 1024):
                    if remaining is not None:
                        chunk = chunk[:remaining]
                        remaining -= len(chunk)
                    if chunk:
                        yield chunk
                    if remaining == 0:
                        return
            finally:
                close = getattr(body, "close", None)
                if callable(close):
                    close()
        except Exception as exc:
            if not _is_missing_object(exc):
                raise
            raise NotFound(f"upload target not found: {target_path}") from exc

    def delete_target(self, target_path: str) -> None:
        self._delete_upload_id(self._upload_id(target_path))

    def cancel_upload(self, tus_url: str) -> None:
        super().cancel_upload(tus_url)
        upload_id = self._upload_id_from_tus_url(tus_url)
        if upload_id is not None:
            self._delete_upload_id(upload_id)

    def _delete_upload_id(self, upload_id: str) -> None:
        for suffix in ("", ".info", ".part"):
            self._delete_all_versions(self._object_key(f"{upload_id}{suffix}"))

    def _delete_all_versions(self, object_key: str) -> None:
        delete_exact_object(
            self._s3,
            bucket=self._store.bucket,
            key=object_key,
        )


def _is_missing_object(exc: Exception) -> bool:
    response: Any = getattr(exc, "response", {})
    if not isinstance(response, dict):
        return False
    error = response.get("Error", {})
    if isinstance(error, dict) and str(error.get("Code", "")) in {
        "404",
        "NoSuchKey",
        "NotFound",
    }:
        return True
    metadata = response.get("ResponseMetadata", {})
    return isinstance(metadata, dict) and metadata.get("HTTPStatusCode") == 404
