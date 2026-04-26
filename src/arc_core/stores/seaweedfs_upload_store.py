from __future__ import annotations

from urllib.parse import urljoin, urlsplit

import httpx

from arc_core.domain.errors import NotFound

_TIMEOUT = 30.0


def _ok_or_raise(r: httpx.Response) -> None:
    if r.status_code not in (200, 204, 404):
        r.raise_for_status()


class SeaweedFSTUSUploadStore:
    def __init__(self, filer_url: str) -> None:
        self._filer_url = filer_url.rstrip("/")
        parsed = urlsplit(self._filer_url)
        self._origin = f"{parsed.scheme}://{parsed.netloc}"
        self._base_path = parsed.path.rstrip("/")

    def _filer_url_for(self, target_path: str) -> str:
        return f"{self._filer_url}/{target_path.lstrip('/')}"

    def _tus_create_url_for(self, target_path: str) -> str:
        return f"{self._origin}/.tus{self._base_path}/{target_path.lstrip('/')}"

    def create_upload(self, target_path: str, length: int) -> str:
        url = self._tus_create_url_for(target_path)
        with httpx.Client(timeout=_TIMEOUT) as client:
            r = client.post(
                url,
                headers={
                    "Tus-Resumable": "1.0.0",
                    "Upload-Length": str(length),
                },
            )
            r.raise_for_status()
            return urljoin(f"{self._origin}/", r.headers["Location"])

    def get_offset(self, tus_url: str) -> int:
        with httpx.Client(timeout=_TIMEOUT) as client:
            r = client.head(tus_url, headers={"Tus-Resumable": "1.0.0"})
            if r.status_code == 404:
                return -1
            r.raise_for_status()
            return int(r.headers["Upload-Offset"])

    def read_target(self, target_path: str) -> bytes:
        url = self._filer_url_for(target_path)
        with httpx.Client(timeout=_TIMEOUT) as client:
            r = client.get(url)
            if r.status_code == 404:
                raise NotFound(f"upload target not found: {target_path}")
            r.raise_for_status()
            return r.content

    def delete_target(self, target_path: str) -> None:
        url = self._filer_url_for(target_path)
        with httpx.Client(timeout=_TIMEOUT) as client:
            _ok_or_raise(client.delete(url))

    def cancel_upload(self, tus_url: str) -> None:
        with httpx.Client(timeout=_TIMEOUT) as client:
            _ok_or_raise(client.delete(tus_url, headers={"Tus-Resumable": "1.0.0"}))
