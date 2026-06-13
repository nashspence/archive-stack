from __future__ import annotations

import base64
import hashlib
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

import httpx

from riverhog_core.domain.errors import Conflict, ServiceUnavailable

TUS_RESUMABLE = "1.0.0"
TUS_CHUNK_CONTENT_TYPE = "application/offset+octet-stream"
TRANSIENT_HTTP_STATUS_CODES = {408, 425, 429, 500, 502, 503, 504}


@dataclass(frozen=True, slots=True)
class TusUploadLease:
    upload_url: str
    offset: int
    length: int
    checksum_algorithm: str = "sha256"


@dataclass(frozen=True, slots=True)
class TusUploadResult:
    offset: int
    bytes_sent: int
    chunks_sent: int


class TusHttpClient:
    def __init__(
        self,
        *,
        headers: Mapping[str, str] | None = None,
        timeout_seconds: float = 300.0,
        verify_tls: bool = True,
        http2: bool = True,
        url_rewriter: Callable[[str], str] | None = None,
    ) -> None:
        self._headers = dict(headers or {})
        self._timeout_seconds = timeout_seconds
        self._verify_tls = verify_tls
        self._http2 = http2
        self._url_rewriter = url_rewriter or (lambda value: value)
        self._client: httpx.Client | None = None

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None

    def __enter__(self) -> TusHttpClient:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def _client_for_request(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(
                headers=self._headers,
                timeout=self._timeout_seconds,
                verify=self._verify_tls,
                http2=self._http2,
            )
        return self._client

    def head_offset(self, upload_url: str) -> int:
        response = self._client_for_request().head(
            self._url_rewriter(upload_url),
            headers={"Tus-Resumable": TUS_RESUMABLE},
        )
        if response.status_code == 404:
            return -1
        response.raise_for_status()
        return int(response.headers["Upload-Offset"])

    def patch_chunk(
        self,
        upload_url: str,
        *,
        offset: int,
        checksum_algorithm: str,
        content: bytes,
    ) -> int:
        checksum = base64.b64encode(hashlib.new(checksum_algorithm, content).digest()).decode(
            "ascii"
        )
        try:
            response = self._client_for_request().patch(
                self._url_rewriter(upload_url),
                headers={
                    "Content-Length": str(len(content)),
                    "Content-Type": TUS_CHUNK_CONTENT_TYPE,
                    "Tus-Resumable": TUS_RESUMABLE,
                    "Upload-Offset": str(offset),
                    "Upload-Checksum": f"{checksum_algorithm} {checksum}",
                },
                content=content,
            )
        except httpx.TransportError as exc:
            self.close()
            raise ServiceUnavailable(
                "upload backend did not accept the chunk before the client timeout; "
                "retry after resyncing the offset"
            ) from exc
        if response.status_code in TRANSIENT_HTTP_STATUS_CODES:
            self.close()
            raise ServiceUnavailable(
                "upload backend reported a transient failure; retry after resyncing the offset"
            )
        if response.status_code in {400, 409}:
            message = response.text.strip() or "upload chunk was rejected by tusd"
            raise Conflict(message)
        response.raise_for_status()
        return int(response.headers.get("Upload-Offset", offset + len(content)))

    def delete_upload(self, upload_url: str) -> None:
        response = self._client_for_request().delete(
            self._url_rewriter(upload_url),
            headers={"Tus-Resumable": TUS_RESUMABLE},
        )
        if response.status_code not in {200, 204, 404}:
            response.raise_for_status()


def upload_path_to_tus(
    *,
    client: TusHttpClient,
    source_path: Path,
    lease: TusUploadLease,
    chunk_bytes: int,
    cancel_check: Callable[[], None] | None = None,
    progress: Callable[[int], None] | None = None,
) -> TusUploadResult:
    if lease.length != source_path.stat().st_size:
        raise ValueError(
            f"tus upload length {lease.length} does not match {source_path} size "
            f"{source_path.stat().st_size}"
        )
    if lease.offset < 0 or lease.offset > lease.length:
        raise ValueError(f"tus upload offset is outside file bounds: {lease.offset}")
    offset = lease.offset
    bytes_sent = 0
    chunks_sent = 0
    if offset >= lease.length:
        return TusUploadResult(offset=offset, bytes_sent=0, chunks_sent=0)

    with source_path.open("rb") as handle:
        handle.seek(offset)
        while offset < lease.length:
            if cancel_check is not None:
                cancel_check()
            content = handle.read(min(max(1, chunk_bytes), lease.length - offset))
            if not content:
                break
            next_offset = client.patch_chunk(
                lease.upload_url,
                offset=offset,
                checksum_algorithm=lease.checksum_algorithm,
                content=content,
            )
            if next_offset != offset + len(content):
                raise RuntimeError("tus upload offset advanced unexpectedly")
            offset = next_offset
            bytes_sent += len(content)
            chunks_sent += 1
            if progress is not None:
                progress(len(content))

    if offset != lease.length:
        raise RuntimeError(f"tus upload stopped at {offset} of {lease.length} bytes")
    return TusUploadResult(offset=offset, bytes_sent=bytes_sent, chunks_sent=chunks_sent)
