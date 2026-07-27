from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable


def collection_content_etag(files: Iterable[tuple[str, int, str]]) -> str:
    return collection_content_etag_ordered(sorted(files))


def collection_content_etag_ordered(files: Iterable[tuple[str, int, str]]) -> str:
    digest = hashlib.sha256()
    digest.update(b'{"files":[')
    separator = b""
    for path, byte_count, sha256 in files:
        digest.update(separator)
        digest.update(
            json.dumps(
                {"path": path, "bytes": byte_count, "sha256": sha256},
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        )
        separator = b","
    digest.update(b'],"format":"riverhog-collection-content/v1"}')
    return digest.hexdigest()


__all__ = ["collection_content_etag", "collection_content_etag_ordered"]
