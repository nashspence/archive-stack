from __future__ import annotations

import base64
import time
from collections.abc import Iterator
from urllib.parse import quote, urljoin, urlsplit, urlunsplit

import httpx

from arc_core.domain.errors import NotFound
from arc_core.runtime_config import RuntimeConfig
from arc_core.stores.s3_support import create_s3_client

_TIMEOUT = 30.0
_READ_TARGET_RETRY_SECONDS = 1.0
_READ_TARGET_RETRY_INTERVAL_SECONDS = 0.05
_HOOK_SECRET_HEADER = "X-Arc-Tusd-Hook-Secret"


def _ok_or_raise(response: httpx.Response) -> None:
    if response.status_code not in (200, 204, 404):
        response.raise_for_status()


class TusdUploadStore:
    def __init__(self, config: RuntimeConfig) -> None:
        self._bucket = config.s3_bucket
        self._client = create_s3_client(config)
        self._tusd_base_url = config.tusd_base_url.rstrip("/")
        self._hook_secret = config.tusd_hook_secret

    @staticmethod
    def _object_key(target_path: str) -> str:
        return target_path.lstrip("/")

    def _metadata_header(self, target_path: str) -> str:
        encoded = base64.b64encode(target_path.encode("utf-8")).decode("ascii")
        return f"target_path {encoded}"

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
        with httpx.Client(timeout=_TIMEOUT) as client:
            response = client.post(
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
        with httpx.Client(timeout=_TIMEOUT) as client:
            response = client.head(tus_url, headers=self._tus_headers())
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
        with httpx.Client(timeout=_TIMEOUT) as client:
            response = client.patch(
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
            response.raise_for_status()
            return int(response.headers["Upload-Offset"]), response.headers.get("Upload-Expires")

    def read_target(self, target_path: str) -> bytes:
        return b"".join(self.iter_target(target_path))

    def iter_target(self, target_path: str) -> Iterator[bytes]:
        key = self._object_key(target_path)
        deadline = time.monotonic() + _READ_TARGET_RETRY_SECONDS
        while True:
            try:
                response = self._client.get_object(Bucket=self._bucket, Key=key)
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
                if time.monotonic() >= deadline:
                    raise NotFound(f"upload target not found: {target_path}") from exc
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
                ]
            },
        )

    def cancel_upload(self, tus_url: str) -> None:
        with httpx.Client(timeout=_TIMEOUT) as client:
            _ok_or_raise(client.delete(tus_url, headers=self._tus_headers()))
