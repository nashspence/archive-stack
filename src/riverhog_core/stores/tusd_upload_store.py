from __future__ import annotations

import base64
import threading
import time
from collections.abc import Iterator
from urllib.parse import quote, urljoin, urlsplit, urlunsplit

import httpx

from riverhog_core.domain.errors import Conflict, NotFound, ServiceUnavailable
from riverhog_core.runtime_config import RuntimeConfig
from riverhog_core.stores.s3_support import create_s3_client
from riverhog_core.tusd_ids import tusd_upload_id_for_target_path

_TIMEOUT = 300.0
_READ_TARGET_RETRY_SECONDS = 1.0
_READ_TARGET_RETRY_INTERVAL_SECONDS = 0.05
_HOOK_SECRET_HEADER = "X-Riverhog-Tusd-Hook-Secret"


def _ok_or_raise(response: httpx.Response) -> None:
    if response.status_code not in (200, 204, 404):
        response.raise_for_status()


class TusdUploadStore:
    def __init__(self, config: RuntimeConfig) -> None:
        self._bucket = config.s3_bucket
        self._client = create_s3_client(config)
        self._tusd_base_url = config.tusd_base_url.rstrip("/")
        self._hook_secret = config.tusd_hook_secret
        self._append_timeout_seconds = config.tusd_append_timeout_seconds
        self._thread_local = threading.local()

    def _http_client(self) -> httpx.Client:
        client = getattr(self._thread_local, "http_client", None)
        if client is None:
            client = httpx.Client(timeout=_TIMEOUT)
            self._thread_local.http_client = client
        return client

    @staticmethod
    def _object_key(target_path: str) -> str:
        return tusd_upload_id_for_target_path(target_path)

    def _metadata_header(self, target_path: str) -> str:
        target_path_b64 = base64.b64encode(target_path.encode("utf-8")).decode("ascii")
        encoded = base64.b64encode(target_path_b64.encode("ascii")).decode("ascii")
        return f"target_path_b64 {encoded}"

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

    def create_upload(self, target_path: str, length: int) -> str:
        response = self._http_client().post(
            self._tusd_base_url,
            headers=self._tus_headers(
                **{
                    "Upload-Length": str(length),
                    "Upload-Metadata": self._metadata_header(target_path),
                }
            ),
        )
        response.raise_for_status()
        location = response.headers["Location"]
        return self._normalize_tusd_location(location)

    def get_offset(self, tus_url: str) -> int:
        response = self._http_client().head(tus_url, headers=self._tus_headers())
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
            response = self._http_client().patch(
                tus_url,
                headers=self._tus_headers(
                    **{
                        "Content-Type": "application/offset+octet-stream",
                        "Upload-Offset": str(offset),
                        "Upload-Checksum": checksum,
                    }
                ),
                content=content,
                timeout=self._append_timeout_seconds,
            )
        except httpx.TransportError as exc:
            raise ServiceUnavailable(
                "upload backend did not accept the chunk before the server-side timeout; "
                "retry the chunk after resyncing the offset"
            ) from exc
        if response.status_code in {400, 409}:
            message = response.text.strip() or "upload chunk was rejected by tusd"
            raise Conflict(message)
        response.raise_for_status()
        return int(response.headers["Upload-Offset"]), response.headers.get("Upload-Expires")

    def read_target(self, target_path: str) -> bytes:
        return b"".join(self.iter_target(target_path))

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

        key = self._object_key(target_path)
        deadline = time.monotonic() + _READ_TARGET_RETRY_SECONDS
        range_header: str | None = None
        if offset > 0 or size is not None:
            end = "" if size is None else str(offset + size - 1)
            range_header = f"bytes={offset}-{end}"
        while True:
            missing_error: Exception | None = None
            try:
                kwargs: dict[str, object] = {"Bucket": self._bucket, "Key": key}
                if range_header is not None:
                    kwargs["Range"] = range_header
                response = self._client.get_object(**kwargs)
                body = response["Body"]
                try:
                    yield from body.iter_chunks(chunk_size=1024 * 1024)
                finally:
                    close = getattr(body, "close", None)
                    if callable(close):
                        close()
                return
            except self._client.exceptions.ClientError as exc:
                if exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode") != 404:
                    raise
                missing_error = exc
            if missing_error is not None:
                if time.monotonic() >= deadline:
                    raise NotFound(f"upload target not found: {target_path}") from missing_error
                time.sleep(_READ_TARGET_RETRY_INTERVAL_SECONDS)

    def delete_target(self, target_path: str) -> None:
        key = self._object_key(target_path)
        self._client.delete_objects(
            Bucket=self._bucket,
            Delete={
                "Objects": [
                    {"Key": key},
                    {"Key": f"{key}.info"},
                    {"Key": f"{key}.part"},
                ],
            },
        )

    def cancel_upload(self, tus_url: str) -> None:
        _ok_or_raise(self._http_client().delete(tus_url, headers=self._tus_headers()))
