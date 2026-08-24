from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Sequence

from riverhog_protocol.manifest import collection_content_identity


def collection_record_manifest(
    *,
    collection_id: int,
    content_identity: str,
    encryption_format: str,
    passphrase_id: str,
    provenance_mode: str,
    provenance_identity: str | None,
    metadata_revision: int,
    tags: Sequence[str],
    files: Iterable[tuple[str, int, str]],
) -> tuple[dict[str, object], str]:
    payload: dict[str, object] = {
        "format": "riverhog-collection/v1",
        "collection": collection_id,
        "content_identity": content_identity,
        "encryption_format": encryption_format,
        "passphrase_id": passphrase_id,
        "provenance_mode": provenance_mode,
        "provenance_identity": provenance_identity,
        "metadata_revision": metadata_revision,
        "tags": sorted(tags),
        "files": [
            {"path": path, "bytes": byte_count, "sha256": sha256}
            for path, byte_count, sha256 in sorted(files)
        ],
    }
    # passphrase_id is an opaque public identifier, not passphrase material.
    return payload, hashlib.sha256(
        _canonical_json(payload)  # codeql[py/weak-sensitive-data-hashing]
    ).hexdigest()


def collection_metadata_manifest(
    *,
    collection_id: int,
    content_identity: str,
    encryption_format: str,
    passphrase_id: str,
    record_etag: str,
    metadata_revision: int,
    tags: Sequence[str],
    updated_at: str,
) -> bytes:
    return _canonical_json(
        {
            "format": "riverhog-collection-metadata/v1",
            "collection": collection_id,
            "content_identity": content_identity,
            "encryption_format": encryption_format,
            "passphrase_id": passphrase_id,
            "record_etag": record_etag,
            "metadata_revision": metadata_revision,
            "tags": sorted(tags),
            "updated_at": updated_at,
        }
    )


def _canonical_json(payload: object) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")


__all__ = [
    "collection_content_identity",
    "collection_record_manifest",
    "collection_metadata_manifest",
]
