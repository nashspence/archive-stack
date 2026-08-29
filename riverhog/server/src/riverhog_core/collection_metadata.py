from __future__ import annotations

import json
from collections.abc import Iterable, Sequence
from typing import Literal, cast

from riverhog_protocol.manifest import collection_content_identity
from riverhog_protocol.portable_collection import PortableCollectionRecord


def collection_record_manifest(
    *,
    collection_id: int,
    content_identity: str,
    encryption_format: str,
    passphrase_id: str,
    provenance_mode: str,
    provenance_identity: str | None,
    files: Iterable[tuple[str, int, str]],
) -> tuple[PortableCollectionRecord, str]:
    if provenance_mode not in {"captured", "mixed", "omitted"}:
        raise ValueError("collection provenance mode is invalid")
    record = PortableCollectionRecord.create(
        collection=collection_id,
        content_identity=content_identity,
        encryption_format=encryption_format,
        passphrase_id=passphrase_id,
        provenance_mode=cast(Literal["captured", "mixed", "omitted"], provenance_mode),
        provenance_identity=provenance_identity,
        files=files,
    )
    # passphrase_id is an opaque public identifier, not passphrase material.
    return record, record.identity


def collection_metadata_manifest(
    *,
    collection_id: int,
    content_identity: str,
    encryption_format: str,
    passphrase_id: str,
    inventory_identity: str,
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
            "inventory_identity": inventory_identity,
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
