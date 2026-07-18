from __future__ import annotations

import base64
import hashlib
from collections.abc import Callable, Mapping

import httpx

TUS_VERSION = "1.0.0"
TUS_CHUNK_CONTENT_TYPE = "application/offset+octet-stream"


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
        headers: Mapping[str, str] | None = None,
        timeout_seconds: float = 300.0,
        verify_tls: bool = True,
        http2: bool = True,
        url_rewriter: Callable[[str], str] | None = None,
    ) -> None:
        self._client = client
        self._owns_client = client is None
        self._headers = dict(headers or {})
        self._timeout_seconds = timeout_seconds
        self._verify_tls = verify_tls
        self._http2 = http2
        self._url_rewriter = url_rewriter or (lambda value: value)

    def close(self) -> None:
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
                verify=self._verify_tls,
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
        response = self._client_for_request().patch(
            self._url_rewriter(upload_url),
            headers=self._request_headers(headers),
            content=content,
        )
        self._raise_for_status(response, expected={204})
        expected_offset = offset + len(content)
        next_offset = int(response.headers.get("Upload-Offset", expected_offset))
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
    "TUS_CHUNK_CONTENT_TYPE",
    "TUS_VERSION",
    "TusHttpError",
    "TusTransport",
]
