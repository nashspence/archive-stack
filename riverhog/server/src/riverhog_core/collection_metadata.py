from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Sequence

import yaml


def collection_content_etag(files: Iterable[tuple[str, int, str]]) -> str:
    canonical = _canonical_json(
        {
            "format": "riverhog-collection-content/v1",
            "files": [
                {"path": path, "bytes": byte_count, "sha256": sha256}
                for path, byte_count, sha256 in sorted(files)
            ],
        }
    )
    return hashlib.sha256(canonical).hexdigest()


def collection_record_manifest(
    *,
    collection_id: int,
    content_etag: str,
    metadata_revision: int,
    tags: Sequence[str],
    files: Iterable[tuple[str, int, str]],
) -> tuple[dict[str, object], str]:
    payload: dict[str, object] = {
        "format": "riverhog-collection/v2",
        "collection": collection_id,
        "content_etag": content_etag,
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
    return yaml.safe_dump(
        {
            "format": "riverhog-collection-metadata/v1",
            "collection": collection_id,
            "content_etag": content_etag,
            "record_etag": record_etag,
            "metadata_revision": metadata_revision,
            "tags": sorted(tags),
            "updated_at": updated_at,
        },
        sort_keys=False,
        allow_unicode=True,
    ).encode("utf-8")


def _canonical_json(payload: object) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")


__all__ = [
    "collection_content_etag",
    "collection_record_manifest",
    "collection_metadata_manifest",
]
