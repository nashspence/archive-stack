from __future__ import annotations

import hashlib
import tarfile
from io import BytesIO

import yaml
from riverhog_age import age_ciphertext_len_for_plaintext_len
from riverhog_core import archive_objects as archive_object_module
from riverhog_core.archive_objects import (
    COLLECTION_ARCHIVE_MANIFEST_SCHEMA,
    PACK_PAYLOAD_LIMIT,
    SMALL_FILE_LIMIT,
    STORED_OBJECT_LIMIT,
    CollectionArchiveFile,
    CollectionArchiveSourceFile,
    build_collection_archive,
    build_collection_archive_from_manifest,
    iter_verified_file_chunks,
    max_age_plaintext_object_bytes,
    parse_collection_archive_manifest,
)

from tests.fixtures.crypto import FixtureProofStamper, FixtureProofVerifier

COLLECTION_ID = 1


def _source(path: str, content: bytes) -> CollectionArchiveSourceFile:
    return CollectionArchiveSourceFile(
        path=path,
        content=content,
        sha256=hashlib.sha256(content).hexdigest(),
    )


def test_small_files_form_deterministic_bounded_tar_packs() -> None:
    first = b"a" * (20 * 1024 * 1024)
    second = b"b" * (12 * 1024 * 1024)
    third = b"c" * (8 * 1024 * 1024)
    archive = build_collection_archive(
        collection_id=COLLECTION_ID,
        files=(
            _source("a.bin", first[: SMALL_FILE_LIMIT - 1]),
            _source("b.bin", second),
            _source("c.txt", third),
        ),
        max_plaintext_object_bytes=SMALL_FILE_LIMIT,
        stamper=FixtureProofStamper(),
    )

    assert [current.kind for current in archive.data_objects] == ["pack", "pack"]
    assert [current.object_id for current in archive.data_objects] == [
        "data-000000",
        "data-000001",
    ]
    assert all(
        sum(placement.bytes for placement in current.placements) <= PACK_PAYLOAD_LIMIT
        for current in archive.data_objects
    )
    with tarfile.open(fileobj=BytesIO(b"".join(archive.data_objects[0].iter_plaintext()))) as pack:
        assert pack.getnames() == ["a.bin", "b.bin"]


def test_whole_file_and_segments_are_independent_raw_objects() -> None:
    whole = b"w" * SMALL_FILE_LIMIT
    divided = b"s" * (SMALL_FILE_LIMIT + 1)
    archive = build_collection_archive(
        collection_id=COLLECTION_ID,
        files=(_source("divided.bin", divided), _source("whole.bin", whole)),
        max_plaintext_object_bytes=SMALL_FILE_LIMIT,
        stamper=FixtureProofStamper(),
    )

    assert [(current.kind, current.plaintext_bytes) for current in archive.data_objects] == [
        ("segment", SMALL_FILE_LIMIT),
        ("segment", 1),
        ("file", SMALL_FILE_LIMIT),
    ]
    assert b"".join(archive.data_objects[0].iter_plaintext()) == divided[:SMALL_FILE_LIMIT]
    assert b"".join(archive.data_objects[1].iter_plaintext()) == divided[SMALL_FILE_LIMIT:]
    assert b"".join(archive.data_objects[2].iter_plaintext()) == whole
    assert b"".join(archive.data_objects[2].iter_plaintext_range(7, 11)) == whole[7:18]


def test_manifest_identity_is_portable_across_catalogs() -> None:
    files = (_source("note.txt", b"portable content"),)
    first = build_collection_archive(
        collection_id=1,
        files=files,
        max_plaintext_object_bytes=SMALL_FILE_LIMIT,
        stamper=FixtureProofStamper(),
    )
    second = build_collection_archive(
        collection_id=2,
        files=files,
        max_plaintext_object_bytes=SMALL_FILE_LIMIT,
        stamper=FixtureProofStamper(),
    )

    assert first.manifest_bytes == second.manifest_bytes
    assert first.manifest_sha256 == second.manifest_sha256


def test_manifest_binds_files_to_pack_file_and_segment_objects() -> None:
    contents = {
        "note.txt": b"note",
        "video.bin": b"v" * SMALL_FILE_LIMIT,
        "large.bin": b"l" * (SMALL_FILE_LIMIT + 1),
    }
    archive = build_collection_archive(
        collection_id=COLLECTION_ID,
        files=tuple(_source(path, content) for path, content in contents.items()),
        max_plaintext_object_bytes=SMALL_FILE_LIMIT,
        stamper=FixtureProofStamper(),
    )
    payload = yaml.safe_load(archive.manifest_bytes)

    assert payload["schema"] == COLLECTION_ARCHIVE_MANIFEST_SCHEMA
    assert [row["kind"] for row in payload["objects"]] == [
        "segment",
        "segment",
        "pack",
        "file",
    ]
    assert [row["path"] for row in payload["files"]] == sorted(contents)
    assert len(next(row for row in payload["files"] if row["path"] == "large.bin")["objects"]) == 2
    assert (
        next(row for row in payload["files"] if row["path"] == "note.txt")["objects"][0]["member"]
        == "note.txt"
    )

    descriptors = parse_collection_archive_manifest(
        manifest_bytes=archive.manifest_bytes,
        expected_sha256=archive.manifest_sha256,
        files=archive.files,
    )
    assert len(descriptors) == 4


def test_manifest_rebuild_and_file_reads_verify_each_required_object() -> None:
    contents = {
        "a.txt": b"alpha",
        "b.bin": b"b" * SMALL_FILE_LIMIT,
        "c.bin": b"c" * (SMALL_FILE_LIMIT + 1),
    }
    initial = build_collection_archive(
        collection_id=COLLECTION_ID,
        files=tuple(_source(path, content) for path, content in contents.items()),
        max_plaintext_object_bytes=SMALL_FILE_LIMIT,
        stamper=FixtureProofStamper(),
    )
    expected_files = tuple(
        CollectionArchiveFile(
            path=path,
            bytes=len(content),
            sha256=hashlib.sha256(content).hexdigest(),
        )
        for path, content in contents.items()
    )
    rebuilt = build_collection_archive_from_manifest(
        collection_id=COLLECTION_ID,
        files=expected_files,
        read_file_chunks=lambda path: (contents[path],),
        read_file_chunks_range=lambda path, offset, size: (
            contents[path][offset:] if size is None else contents[path][offset : offset + size],
        ),
        manifest_bytes=initial.manifest_bytes,
        proof_bytes=initial.proof_bytes,
        verifier=FixtureProofVerifier(),
    )
    stored = {
        current.object_id: b"".join(current.iter_plaintext()) for current in rebuilt.data_objects
    }

    for path, expected in contents.items():
        chunks, byte_count = iter_verified_file_chunks(
            rebuilt,
            path=path,
            read_object=lambda object_id: (stored[object_id],),
        )
        assert byte_count == len(expected)
        assert b"".join(chunks) == expected


def test_plaintext_limit_accounts_for_complete_age_ciphertext() -> None:
    stored_limit = 32 * 1024 * 1024
    plaintext_limit = max_age_plaintext_object_bytes(
        age_prefix_len=200,
        stored_object_limit=stored_limit,
    )

    assert (
        age_ciphertext_len_for_plaintext_len(
            plaintext_limit,
            age_prefix_len=200,
        )
        <= stored_limit
    )
    assert (
        age_ciphertext_len_for_plaintext_len(
            plaintext_limit + 1,
            age_prefix_len=200,
        )
        > stored_limit
    )


def test_real_stored_object_boundary_plans_ordered_segments_without_reading_content() -> None:
    plaintext_limit = max_age_plaintext_object_bytes(age_prefix_len=200)

    def unread(*_args: object) -> tuple[bytes, ...]:
        raise AssertionError("archive boundary planning must not read file content")

    at_limit = archive_object_module._plan_data_objects(
        (
            CollectionArchiveFile(
                path="at-limit.bin",
                bytes=plaintext_limit,
                sha256="0" * 64,
            ),
        ),
        read_file_chunks=unread,
        read_file_chunks_range=unread,
        max_plaintext_object_bytes=plaintext_limit,
    )
    over_limit = archive_object_module._plan_data_objects(
        (
            CollectionArchiveFile(
                path="over-limit.bin",
                bytes=plaintext_limit + 1,
                sha256="0" * 64,
            ),
        ),
        read_file_chunks=unread,
        read_file_chunks_range=unread,
        max_plaintext_object_bytes=plaintext_limit,
    )

    assert [(obj.kind, obj.plaintext_bytes) for obj in at_limit] == [("file", plaintext_limit)]
    assert [(obj.kind, obj.plaintext_bytes) for obj in over_limit] == [
        ("segment", plaintext_limit),
        ("segment", 1),
    ]
    assert [obj.placements[0].file_offset for obj in over_limit] == [0, plaintext_limit]
    assert all(
        age_ciphertext_len_for_plaintext_len(obj.plaintext_bytes, age_prefix_len=200)
        <= STORED_OBJECT_LIMIT
        for obj in (*at_limit, *over_limit)
    )
