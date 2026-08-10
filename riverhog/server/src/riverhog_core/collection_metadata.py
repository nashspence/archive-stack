from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Sequence

from riverhog_protocol.manifest import collection_content_etag


def collection_record_manifest(
    *,
    collection_id: int,
    content_etag: str,
    provenance_mode: str,
    provenance_etag: str | None,
    metadata_revision: int,
    tags: Sequence[str],
    files: Iterable[tuple[str, int, str]],
) -> tuple[dict[str, object], str]:
    payload: dict[str, object] = {
        "format": "riverhog-collection/v1",
        "collection": collection_id,
        "content_etag": content_etag,
        "provenance_mode": provenance_mode,
        "provenance_etag": provenance_etag,
        "metadata_revision": metadata_revision,
        "tags": sorted(tags),
        "files": [
            {"path": path, "bytes": byte_count, "sha256": sha256}
            for path, byte_count, sha256 in sorted(files)
        ],
    }
    return payload, hashlib.sha256(_canonical_json(payload)).hexdigest()


def collection_metadata_manifest(
    *,
    collection_id: int,
    content_etag: str,
    record_etag: str,
    metadata_revision: int,
    tags: Sequence[str],
    updated_at: str,
) -> bytes:
    return _canonical_json(
        {
            "format": "riverhog-collection-metadata/v1",
            "collection": collection_id,
            "content_etag": content_etag,
            "record_etag": record_etag,
            "metadata_revision": metadata_revision,
            "tags": sorted(tags),
            "updated_at": updated_at,
        }
    )


def _canonical_json(payload: object) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")


__all__ = [
    "collection_content_etag",
    "collection_record_manifest",
    "collection_metadata_manifest",
]
