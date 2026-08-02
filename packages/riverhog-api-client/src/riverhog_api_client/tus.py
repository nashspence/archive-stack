from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

import httpx
from riverhog_protocol.errors import Conflict, ServiceUnavailable
from tus_transport import TusHttpError, TusTransport

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
        http2: bool = True,
        url_rewriter: Callable[[str], str] | None = None,
    ) -> None:
        self._transport = TusTransport(
            headers=headers,
            timeout_seconds=timeout_seconds,
            http2=http2,
            url_rewriter=url_rewriter,
        )

    def close(self) -> None:
        self._transport.close()

    def __enter__(self) -> TusHttpClient:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def head_offset(self, upload_url: str) -> int:
        return self._transport.head_offset(upload_url)

    def patch_chunk(
        self,
        upload_url: str,
        *,
        offset: int,
        checksum_algorithm: str,
        content: bytes,
    ) -> int:
        try:
            return self._transport.patch_chunk(
                upload_url,
                offset=offset,
                content=content,
                checksum_algorithm=checksum_algorithm,
            )
        except httpx.TransportError as exc:
            self.close()
            raise ServiceUnavailable(
                "upload backend did not accept the chunk before the client timeout; "
                "retry after resyncing the offset"
            ) from exc
        except TusHttpError as exc:
            if exc.status in TRANSIENT_HTTP_STATUS_CODES:
                self.close()
                raise ServiceUnavailable(
                    "upload backend reported a transient failure; retry after resyncing the offset"
                ) from exc
            if exc.status in {404, 410}:
                raise Conflict(
                    "upload lease is no longer available; retry after requesting a current lease"
                ) from exc
            if exc.status in {400, 409}:
                message = exc.body.decode("utf-8", errors="replace").strip()
                raise Conflict(message or "upload chunk was rejected by tusd") from exc
            raise

    def delete_upload(self, upload_url: str) -> None:
        self._transport.delete_upload(upload_url)


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
