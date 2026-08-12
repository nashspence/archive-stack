from __future__ import annotations

import re
from collections.abc import Iterator
from typing import Any, cast

from riverhog_core.runtime_config import ArchiveStoreConfig, RuntimeConfig
from riverhog_core.stores.s3_client import create_archive_s3_client
from riverhog_core.throughput import S3TransportTuning

_CONTENT_RANGE_RE = re.compile(r"bytes ([0-9]+)-([0-9]+)/([0-9]+|\*)")


class S3ArchiveObjectRangeStore:
    """Exact ranged reads with strict response-length and Content-Range validation."""

    def __init__(
        self,
        config: RuntimeConfig,
        store: ArchiveStoreConfig,
        *,
        read_chunk_bytes: int = 8 * 1024 * 1024,
        transport_tuning: S3TransportTuning | None = None,
    ) -> None:
        if read_chunk_bytes < 64 * 1024:
            raise ValueError("S3 range read chunk must be at least 64 KiB")
        self._bucket = store.bucket
        self._client = create_archive_s3_client(
            config,
            store,
            tuning=transport_tuning,
        )
        self._read_chunk_bytes = read_chunk_bytes

    def iter_object_range(
        self,
        *,
        object_path: str,
        version_id: str | None,
        offset: int,
        size: int,
    ) -> Iterator[bytes]:
        if not object_path:
            raise ValueError("archive range object path is required")
        if offset < 0 or size < 0:
            raise ValueError("archive object range must be non-negative")
        if size == 0:
            return
        end = offset + size - 1
        request: dict[str, Any] = {
            "Bucket": self._bucket,
            "Key": object_path,
            "Range": f"bytes={offset}-{end}",
        }
        if version_id is not None:
            request["VersionId"] = version_id
        response = cast(dict[str, Any], self._client.get_object(**request))
        body = response.get("Body")
        read = getattr(body, "read", None)
        close = getattr(body, "close", None)
        try:
            if int(str(response.get("ContentLength", -1))) != size:
                raise RuntimeError("S3 range response length does not match the request")
            content_range = str(response.get("ContentRange", ""))
            match = _CONTENT_RANGE_RE.fullmatch(content_range)
            if match is None or int(match.group(1)) != offset or int(match.group(2)) != end:
                raise RuntimeError("S3 range response does not match the requested byte interval")
            if not callable(read):
                raise RuntimeError("S3 range response body is not readable")
            emitted = 0
            while emitted < size:
                read_chunk_bytes = getattr(self, "_read_chunk_bytes", 8 * 1024 * 1024)
                chunk = bytes(read(min(read_chunk_bytes, size - emitted)))
                if not chunk:
                    raise RuntimeError("S3 range response ended before its declared length")
                emitted += len(chunk)
                yield chunk
            if read(1):
                raise RuntimeError("S3 range response contains trailing bytes")
        finally:
            if callable(close):
                close()
