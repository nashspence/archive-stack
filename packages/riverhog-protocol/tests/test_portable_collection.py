from __future__ import annotations

import pytest
from riverhog_protocol import (
    PortableCollectionError,
    PortableCollectionFile,
    PortableCollectionHeader,
    PortableCollectionIdentityBuilder,
    PortableCollectionInventoryAuthority,
    PortableCollectionInventoryPage,
)


def test_portable_collection_inventory_owns_incremental_identity() -> None:
    header = PortableCollectionHeader(
        collection=7,
        content_identity="a" * 64,
        encryption_format="age-v1-scrypt",
        passphrase_id="collection-test-key-v1",
        provenance_mode="omitted",
        provenance_identity=None,
    )
    files = (
        PortableCollectionFile(path="a.txt", bytes=1, sha256="c" * 64),
        PortableCollectionFile(path="z.txt", bytes=2, sha256="b" * 64),
    )
    builder = PortableCollectionIdentityBuilder(header)
    for file in files:
        builder.add(file)
    page = PortableCollectionInventoryPage(
        authority=PortableCollectionInventoryAuthority(
            header=header,
            inventory_identity=builder.identity,
            file_count=2,
            file_bytes=3,
        ),
        files=[file.to_mapping() for file in files],
        complete=True,
    )

    assert len(page.authority.inventory_identity) == 64
    assert [item.path for item in page.files] == ["a.txt", "z.txt"]


def test_portable_collection_inventory_rejects_invalid_or_noncanonical_files() -> None:
    with pytest.raises(PortableCollectionError, match="path is not canonical"):
        PortableCollectionFile(path="./not-canonical", bytes=1, sha256="b" * 64)
    with pytest.raises(PortableCollectionError, match="file bytes"):
        PortableCollectionFile(path="valid.txt", bytes=-1, sha256="b" * 64)
