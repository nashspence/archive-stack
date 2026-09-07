from __future__ import annotations

from collections.abc import Iterable
from typing import Literal, cast

from riverhog_protocol.manifest import collection_content_identity
from riverhog_protocol.portable_collection import (
    PortableCollectionFile,
    PortableCollectionHeader,
    PortableCollectionIdentityBuilder,
)


def collection_inventory_identity(
    *,
    collection_id: int,
    content_identity: str,
    encryption_format: str,
    passphrase_id: str,
    provenance_mode: str,
    provenance_identity: str | None,
    files: Iterable[tuple[str, int, str]],
) -> tuple[PortableCollectionHeader, str]:
    if provenance_mode not in {"captured", "mixed", "omitted"}:
        raise ValueError("collection provenance mode is invalid")
    header = PortableCollectionHeader(
        collection=collection_id,
        content_identity=content_identity,
        encryption_format=encryption_format,
        passphrase_id=passphrase_id,
        provenance_mode=cast(Literal["captured", "mixed", "omitted"], provenance_mode),
        provenance_identity=provenance_identity,
    )
    builder = PortableCollectionIdentityBuilder(header)
    for path, byte_count, sha256 in files:
        builder.add(
            PortableCollectionFile.from_mapping(
                {"path": path, "bytes": byte_count, "sha256": sha256}
            )
        )
    # passphrase_id is an opaque public identifier, not passphrase material.
    return header, builder.identity


__all__ = [
    "collection_content_identity",
    "collection_inventory_identity",
]
