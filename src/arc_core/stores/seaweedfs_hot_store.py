from __future__ import annotations

from urllib.parse import urlsplit

import httpx

from arc_core.domain.errors import NotFound

_LIST_LIMIT = 100000
_TIMEOUT = 30.0


def _ok_or_raise(r: httpx.Response) -> None:
    if r.status_code not in (200, 204, 404):
        r.raise_for_status()


class SeaweedFSHotStore:
    def __init__(self, filer_url: str) -> None:
        self._filer_url = filer_url.rstrip("/")
        self._base_path = urlsplit(self._filer_url).path.rstrip("/")

    def _url(self, collection_id: str, path: str) -> str:
        return f"{self._filer_url}/collections/{collection_id}/{path}"

    def put_collection_file(self, collection_id: str, path: str, content: bytes) -> None:
        with httpx.Client(timeout=_TIMEOUT) as client:
            client.put(self._url(collection_id, path), content=content).raise_for_status()

    def get_collection_file(self, collection_id: str, path: str) -> bytes:
        with httpx.Client(timeout=_TIMEOUT) as client:
            r = client.get(self._url(collection_id, path))
            if r.status_code == 404:
                raise NotFound(f"file not found in hot store: {collection_id}/{path}")
            r.raise_for_status()
            return r.content

    def has_collection_file(self, collection_id: str, path: str) -> bool:
        with httpx.Client(timeout=_TIMEOUT) as client:
            r = client.head(self._url(collection_id, path))
            return r.status_code == 200

    def delete_collection_file(self, collection_id: str, path: str) -> None:
        with httpx.Client(timeout=_TIMEOUT) as client:
            _ok_or_raise(client.delete(self._url(collection_id, path)))

    def list_collection_files(self, collection_id: str) -> list[tuple[str, int]]:
        prefix = f"{self._base_path}/collections/{collection_id}/"
        results: list[tuple[str, int]] = []
        seen_dirs: set[str] = set()

        def walk(dir_relpath: str) -> None:
            dir_url = (
                f"{self._filer_url}/collections/{collection_id}/{dir_relpath}".rstrip("/")
                + "/"
            )
            if dir_url in seen_dirs:
                return
            seen_dirs.add(dir_url)

            with httpx.Client(timeout=_TIMEOUT) as client:
                r = client.get(
                    dir_url,
                    params={"recursive": "true", "limit": str(_LIST_LIMIT)},
                    headers={"Accept": "application/json"},
                )
                if r.status_code == 404:
                    return
                r.raise_for_status()
                data = r.json()

                for entry in data.get("Entries") or []:
                    full_path: str = entry.get("FullPath", "")
                    rel = full_path.removeprefix(prefix)
                    child_url = (
                        f"{self._filer_url}/collections/{collection_id}/{rel}".rstrip("/") + "/"
                    )
                    child = client.get(child_url, headers={"Accept": "application/json"})
                    if child.status_code == 200:
                        try:
                            child_data = child.json()
                        except ValueError:
                            child_data = None
                        if isinstance(child_data, dict) and "Entries" in child_data:
                            walk(rel)
                            continue
                    size = int(entry.get("FileSize", 0))
                    results.append((rel, size))

        walk("")
        return results
