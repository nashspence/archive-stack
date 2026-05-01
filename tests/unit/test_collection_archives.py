from __future__ import annotations

import hashlib
import tarfile
from collections.abc import Iterator
from io import BytesIO

import pytest

from arc_core.collection_archives import (
    COLLECTION_ARCHIVE_COMPRESSION,
    COLLECTION_ARCHIVE_FORMAT,
    CollectionArchiveExpectedFile,
    CollectionArchiveFile,
    build_collection_archive_package,
    build_collection_archive_package_from_chunk_reader,
    iter_collection_archive_files,
    verify_collection_archive_files,
    verify_collection_archive_manifest,
    verify_collection_archive_proof,
)
from tests.fixtures.crypto import FixtureProofStamper

_PROOF_STAMPER = FixtureProofStamper()


def test_collection_archive_package_uses_plain_tar_contract() -> None:
    content = b"invoice bytes"
    package = build_collection_archive_package(
        collection_id="docs",
        files=(
            CollectionArchiveFile(
                path="tax/2022/invoice.pdf",
                content=content,
                sha256=hashlib.sha256(content).hexdigest(),
            ),
        ),
        stamper=_PROOF_STAMPER,
    )

    assert package.archive_format == COLLECTION_ARCHIVE_FORMAT == "tar"
    assert package.compression == COLLECTION_ARCHIVE_COMPRESSION == "none"
    assert package.archive_bytes[257:263] in {b"ustar\0", b"ustar "}
    assert not package.archive_bytes.startswith(b"\x28\xb5\x2f\xfd")

    with tarfile.open(fileobj=BytesIO(package.archive_bytes), mode="r:") as archive:
        assert archive.getnames() == ["tax/2022/invoice.pdf"]


def test_manifest_and_proof_verification_use_catalog_and_manifest_digest() -> None:
    content = b"receipt bytes"
    digest = hashlib.sha256(content).hexdigest()
    package = build_collection_archive_package(
        collection_id="docs",
        files=(
            CollectionArchiveFile(
                path="tax/2022/receipt.pdf",
                content=content,
                sha256=digest,
            ),
        ),
        stamper=_PROOF_STAMPER,
    )
    expected_files = (
        CollectionArchiveExpectedFile(
            path="tax/2022/receipt.pdf",
            bytes=len(content),
            sha256=digest,
        ),
    )

    verify_collection_archive_manifest(
        manifest_bytes=package.manifest_bytes,
        expected_sha256=package.manifest_sha256,
        collection_id="docs",
        files=expected_files,
    )
    verify_collection_archive_proof(
        proof_bytes=package.proof_bytes,
        expected_sha256=package.proof_sha256,
        manifest_bytes=package.manifest_bytes,
    )

    bad_proof = b""
    with pytest.raises(ValueError, match="proof is empty"):
        verify_collection_archive_proof(
            proof_bytes=bad_proof,
            expected_sha256=hashlib.sha256(bad_proof).hexdigest(),
            manifest_bytes=package.manifest_bytes,
        )


def test_collection_archive_reader_streams_chunk_iterables() -> None:
    files = (
        CollectionArchiveFile(
            path="a.txt",
            content=b"alpha",
            sha256=hashlib.sha256(b"alpha").hexdigest(),
        ),
        CollectionArchiveFile(
            path="b.txt",
            content=b"beta",
            sha256=hashlib.sha256(b"beta").hexdigest(),
        ),
    )
    package = build_collection_archive_package(
        collection_id="docs",
        files=files,
        stamper=_PROOF_STAMPER,
    )
    chunks = (
        package.archive_bytes[offset : offset + 11]
        for offset in range(0, len(package.archive_bytes), 11)
    )

    assert list(iter_collection_archive_files(chunks)) == [
        ("a.txt", b"alpha"),
        ("b.txt", b"beta"),
    ]


def test_collection_archive_package_from_chunk_reader_does_not_require_whole_file_reads() -> None:
    content = b"0123456789" * 200
    digest = hashlib.sha256(content).hexdigest()
    chunk_sizes: list[int] = []

    def read_chunks(path: str) -> Iterator[bytes]:
        assert path == "large.bin"
        for offset in range(0, len(content), 13):
            chunk = content[offset : offset + 13]
            chunk_sizes.append(len(chunk))
            yield chunk

    package = build_collection_archive_package_from_chunk_reader(
        collection_id="docs",
        files=(
            CollectionArchiveExpectedFile(
                path="large.bin",
                bytes=len(content),
                sha256=digest,
            ),
        ),
        read_file_chunks=read_chunks,
        stamper=_PROOF_STAMPER,
    )

    assert max(chunk_sizes) == 13
    assert package.archive_size == len(package.archive_bytes)
    assert list(iter_collection_archive_files(package.iter_archive())) == [
        ("large.bin", content)
    ]


def test_collection_archive_file_verification_rejects_mismatched_members() -> None:
    package = build_collection_archive_package(
        collection_id="docs",
        files=(
            CollectionArchiveFile(
                path="a.txt",
                content=b"alpha",
                sha256=hashlib.sha256(b"alpha").hexdigest(),
            ),
        ),
        stamper=_PROOF_STAMPER,
    )
    corrupt_archive = package.archive_bytes.replace(b"alpha", b"omega", 1)

    with pytest.raises(ValueError, match="member sha256 mismatch"):
        verify_collection_archive_files(
            chunks=(corrupt_archive,),
            files=(
                CollectionArchiveExpectedFile(
                    path="a.txt",
                    bytes=len(b"alpha"),
                    sha256=hashlib.sha256(b"alpha").hexdigest(),
                ),
            ),
        )
