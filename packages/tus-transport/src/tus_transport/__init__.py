from __future__ import annotations

import base64
import hashlib
import http.client
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from threading import Lock, local
from urllib.parse import SplitResult, urljoin, urlsplit

import httpx

TUS_VERSION = "1.0.0"
TUS_CHUNK_CONTENT_TYPE = "application/offset+octet-stream"
DEFAULT_TUS_UPLOAD_CHUNK_MIB = 64


@dataclass(frozen=True)
class _Http11Response:
    status: int
    headers: Mapping[str, str]
    body: bytes


class _PersistentHttp11Client:
    """Keep one direct upload connection per caller thread and origin."""

    def __init__(self, *, timeout_seconds: float) -> None:
        self._timeout_seconds = timeout_seconds
        self._local = local()
        self._lock = Lock()
        self._connections: set[http.client.HTTPConnection] = set()
        self._closed = False

    @staticmethod
    def _target(parsed: SplitResult) -> str:
        target = parsed.path or "/"
        if parsed.query:
            target = f"{target}?{parsed.query}"
        return target

    def _thread_connections(
        self,
    ) -> dict[tuple[str, str, int | None], http.client.HTTPConnection]:
        connections = getattr(self._local, "connections", None)
        if connections is None:
            connections = {}
            self._local.connections = connections
        return connections

    def _connection(
        self,
        parsed: SplitResult,
    ) -> tuple[tuple[str, str, int | None], http.client.HTTPConnection]:
        host = parsed.hostname
        if parsed.scheme not in {"http", "https"} or host is None:
            raise ValueError("tus upload URL must use HTTP or HTTPS and include a host")
        if parsed.username is not None or parsed.password is not None:
            raise ValueError("tus upload URL credentials must be supplied as headers")
        key = (parsed.scheme, host, parsed.port)
        thread_connections = self._thread_connections()
        connection = thread_connections.get(key)
        if connection is not None:
            return key, connection
        if parsed.scheme == "https":
            connection = http.client.HTTPSConnection(
                host,
                port=parsed.port,
                timeout=self._timeout_seconds,
            )
        else:
            connection = http.client.HTTPConnection(
                host,
                port=parsed.port,
                timeout=self._timeout_seconds,
            )
        with self._lock:
            if self._closed:
                connection.close()
                raise RuntimeError("tus transport is closed")
            self._connections.add(connection)
        thread_connections[key] = connection
        return key, connection

    def _discard(
        self,
        key: tuple[str, str, int | None],
        connection: http.client.HTTPConnection,
    ) -> None:
        thread_connections = self._thread_connections()
        if thread_connections.get(key) is connection:
            thread_connections.pop(key)
        with self._lock:
            self._connections.discard(connection)
        connection.close()

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str],
        content: bytes,
    ) -> _Http11Response:
        parsed = urlsplit(url)
        key, connection = self._connection(parsed)
        try:
            connection.request(
                method,
                self._target(parsed),
                body=content,
                headers=dict(headers),
            )
            response = connection.getresponse()
            body = response.read()
            result = _Http11Response(
                status=response.status,
                headers={key.casefold(): value for key, value in response.getheaders()},
                body=body,
            )
            if response.will_close:
                self._discard(key, connection)
            return result
        except (OSError, http.client.HTTPException):
            self._discard(key, connection)
            raise

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            connections = tuple(self._connections)
            self._connections.clear()
        for connection in connections:
            connection.close()


class TusHttpError(RuntimeError):
    def __init__(self, method: str, url: str, status: int, body: bytes) -> None:
        detail = body.decode("utf-8", errors="replace").strip()
        message = f"{method} {url} returned HTTP {status}"
        if detail:
            message = f"{message}: {detail}"
        super().__init__(message)
        self.method = method
        self.url = url
        self.status = status
        self.body = body


class TusTransport:
    def __init__(
        self,
        *,
        client: httpx.Client | None = None,
        patch_client: httpx.Client | None = None,
        headers: Mapping[str, str] | None = None,
        timeout_seconds: float = 300.0,
        http2: bool = True,
        url_rewriter: Callable[[str], str] | None = None,
    ) -> None:
        self._client = client
        self._patch_client = patch_client
        self._owns_client = client is None
        self._headers = dict(headers or {})
        self._timeout_seconds = timeout_seconds
        self._http2 = http2
        self._url_rewriter = url_rewriter or (lambda value: value)
        self._http11 = _PersistentHttp11Client(timeout_seconds=timeout_seconds)

    def close(self) -> None:
        self._http11.close()
        if self._owns_client and self._client is not None:
            self._client.close()
            self._client = None

    def __enter__(self) -> TusTransport:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def _client_for_request(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(
                timeout=self._timeout_seconds,
                http2=self._http2,
            )
        return self._client

    def _request_headers(self, headers: Mapping[str, str]) -> dict[str, str]:
        return {**self._headers, **headers}

    @staticmethod
    def _raise_for_status(response: httpx.Response, *, expected: set[int]) -> None:
        if response.status_code in expected:
            return
        raise TusHttpError(
            response.request.method,
            str(response.request.url),
            response.status_code,
            response.content,
        )

    def head_offset(self, upload_url: str) -> int:
        response = self._client_for_request().head(
            self._url_rewriter(upload_url),
            headers=self._request_headers({"Tus-Resumable": TUS_VERSION}),
        )
        if response.status_code == 404:
            return -1
        self._raise_for_status(response, expected={200})
        return int(response.headers["Upload-Offset"])

    def create_upload(
        self,
        collection_url: str,
        *,
        length: int,
        metadata: Mapping[str, str],
    ) -> str:
        if length < 0:
            raise ValueError("tus upload length must be non-negative")
        encoded_metadata = ",".join(
            f"{key} {base64.b64encode(value.encode('utf-8')).decode('ascii')}"
            for key, value in sorted(metadata.items())
        )
        response = self._client_for_request().post(
            self._url_rewriter(collection_url),
            headers=self._request_headers(
                {
                    "Tus-Resumable": TUS_VERSION,
                    "Upload-Length": str(length),
                    "Upload-Metadata": encoded_metadata,
                }
            ),
        )
        self._raise_for_status(response, expected={201})
        location = response.headers.get("Location")
        if not location:
            raise RuntimeError("tus upload creation returned no Location")
        return str(urljoin(str(response.request.url), str(location)))

    def patch_chunk(
        self,
        upload_url: str,
        *,
        offset: int,
        content: bytes,
        checksum_algorithm: str | None = None,
    ) -> int:
        headers = {
            "Content-Length": str(len(content)),
            "Content-Type": TUS_CHUNK_CONTENT_TYPE,
            "Tus-Resumable": TUS_VERSION,
            "Upload-Offset": str(offset),
        }
        if checksum_algorithm is not None:
            checksum = base64.b64encode(hashlib.new(checksum_algorithm, content).digest()).decode(
                "ascii"
            )
            headers["Upload-Checksum"] = f"{checksum_algorithm} {checksum}"
        url = self._url_rewriter(upload_url)
        request_headers = self._request_headers(headers)
        response_headers: Mapping[str, str]
        if self._patch_client is not None:
            httpx_response = self._patch_client.patch(
                url,
                headers=request_headers,
                content=content,
            )
            self._raise_for_status(httpx_response, expected={204})
            status = httpx_response.status_code
            response_headers = httpx_response.headers
            response_body = httpx_response.content
        else:
            http11_response = self._http11.request(
                "PATCH",
                url,
                headers=request_headers,
                content=content,
            )
            status = http11_response.status
            response_headers = http11_response.headers
            response_body = http11_response.body
        if status != 204:
            raise TusHttpError("PATCH", url, status, response_body)
        expected_offset = offset + len(content)
        next_offset = int(response_headers.get("upload-offset", expected_offset))
        if next_offset != expected_offset:
            raise RuntimeError(
                f"tus upload offset advanced to {next_offset}; expected {expected_offset}"
            )
        return next_offset

    def delete_upload(self, upload_url: str) -> None:
        response = self._client_for_request().delete(
            self._url_rewriter(upload_url),
            headers=self._request_headers({"Tus-Resumable": TUS_VERSION}),
        )
        self._raise_for_status(response, expected={200, 204, 404})


__all__ = [
    "DEFAULT_TUS_UPLOAD_CHUNK_MIB",
    "TUS_CHUNK_CONTENT_TYPE",
    "TUS_VERSION",
    "TusHttpError",
    "TusTransport",
]
