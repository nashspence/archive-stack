from __future__ import annotations

import base64
from urllib.parse import urljoin

import httpx

from arc_core.domain.errors import NotFound
from arc_core.runtime_config import RuntimeConfig
from arc_core.stores.s3_support import create_s3_client

_TIMEOUT = 30.0
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

    def create_upload(self, target_path: str, length: int) -> str:
        with httpx.Client(timeout=_TIMEOUT) as client:
            response = client.post(
                self._tusd_base_url,
                headers={
                    "Tus-Resumable": "1.0.0",
                    "Upload-Length": str(length),
                    "Upload-Metadata": self._metadata_header(target_path),
                    _HOOK_SECRET_HEADER: self._hook_secret,
                },
            )
            response.raise_for_status()
            location = response.headers["Location"]
            return urljoin(f"{self._tusd_base_url}/", location)

    def get_offset(self, tus_url: str) -> int:
        with httpx.Client(timeout=_TIMEOUT) as client:
            response = client.head(tus_url, headers={"Tus-Resumable": "1.0.0"})
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
                headers={
                    "Content-Type": "application/offset+octet-stream",
                    "Tus-Resumable": "1.0.0",
                    "Upload-Offset": str(offset),
                    "Upload-Checksum": checksum,
                },
                content=content,
            )
            response.raise_for_status()
            return int(response.headers["Upload-Offset"]), response.headers.get("Upload-Expires")

    def read_target(self, target_path: str) -> bytes:
        key = self._object_key(target_path)
        try:
            response = self._client.get_object(Bucket=self._bucket, Key=key)
        except self._client.exceptions.ClientError as exc:  # type: ignore[attr-defined]
            if exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode") != 404:
                raise
            raise NotFound(f"upload target not found: {target_path}") from exc
        return response["Body"].read()

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
            _ok_or_raise(client.delete(tus_url, headers={"Tus-Resumable": "1.0.0"}))
