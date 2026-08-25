from __future__ import annotations

import hashlib

import pytest
from riverhog_core import raw_verification
from riverhog_core.domain.archive import (
    ArchiveFile,
    SealedRawVolume,
    StoredArchivePart,
)

from tests.fixtures.archive import age_state_json


def _sealed_segment(
    *,
    sequence: int,
    path: str,
    whole: bytes,
    offset: int,
    content: bytes,
) -> SealedRawVolume:
    digest = hashlib.sha256(content).hexdigest()
    volume_id = f"segment-{sequence:012d}"
    return SealedRawVolume(
        volume_id=volume_id,
        sequence=sequence,
        relative_path=f"volumes/{volume_id}.bin.age",
        source_path=path,
        file_offset=offset,
        plaintext_bytes=len(content),
        age_state_json=age_state_json(len(content)),
        file_bytes=len(whole),
        file_sha256=hashlib.sha256(whole).hexdigest(),
        parts=(
            StoredArchivePart(
                number=1,
                plaintext_start=0,
                plaintext_bytes=len(content),
                plaintext_sha256=digest,
                stored_bytes=len(content),
                stored_sha256=digest,
            ),
        ),
        revision=f"v-{sequence}",
        completed_at="2026-08-03T00:00:00Z",
    )


def test_raw_file_is_reassembled_and_verified_before_root_publication(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        raw_verification,
        "iter_decrypt_age_scrypt",
        lambda chunks, _passphrase: chunks,
    )
    whole = b"abcdefghij"
    first = _sealed_segment(sequence=0, path="large.bin", whole=whole, offset=0, content=whole[:6])
    second = _sealed_segment(sequence=1, path="large.bin", whole=whole, offset=6, content=whole[6:])
    stored = {
        first.relative_path: whole[:6],
        second.relative_path: whole[6:],
    }

    verified = raw_verification.verify_raw_file(
        file=ArchiveFile(
            path="large.bin",
            bytes=len(whole),
            sha256=hashlib.sha256(whole).hexdigest(),
        ),
        volumes=(second, first),
        passphrase="archive passphrase",
        read_ciphertext_chunks=lambda path: (stored[path],),
        verified_at="2026-08-03T00:00:01Z",
    )

    assert verified.path == "large.bin"
    assert verified.sha256 == hashlib.sha256(whole).hexdigest()
    assert verified.volume_set_sha256 == raw_verification.raw_file_volume_set_sha256(
        file=ArchiveFile(
            path="large.bin",
            bytes=len(whole),
            sha256=hashlib.sha256(whole).hexdigest(),
        ),
        volumes=(second, first),
    )
    payload = raw_verification.raw_file_verification_payload(verified)
    assert payload["schema"] == "raw-file-verification/v1"


def test_raw_verification_rejects_stored_part_corruption(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        raw_verification,
        "iter_decrypt_age_scrypt",
        lambda chunks, _passphrase: chunks,
    )
    whole = b"abcdefghij"
    volume = _sealed_segment(sequence=0, path="large.bin", whole=whole, offset=0, content=whole)

    with pytest.raises(ValueError, match="raw (stored|plaintext) part sha256 mismatch"):
        raw_verification.verify_raw_file(
            file=ArchiveFile(
                path="large.bin",
                bytes=len(whole),
                sha256=hashlib.sha256(whole).hexdigest(),
            ),
            volumes=(volume,),
            passphrase="archive passphrase",
            read_ciphertext_chunks=lambda _path: (b"abcdefghik",),
            verified_at="2026-08-03T00:00:01Z",
        )


def test_raw_volume_set_digest_changes_with_immutable_object_identity() -> None:
    whole = b"abcdefghij"
    file = ArchiveFile(
        path="large.bin",
        bytes=len(whole),
        sha256=hashlib.sha256(whole).hexdigest(),
    )
    first = _sealed_segment(sequence=0, path=file.path, whole=whole, offset=0, content=whole)
    changed_part = StoredArchivePart(
        number=1,
        plaintext_start=0,
        plaintext_bytes=first.parts[0].plaintext_bytes,
        plaintext_sha256=first.parts[0].plaintext_sha256,
        stored_bytes=first.parts[0].stored_bytes,
        stored_sha256=hashlib.sha256(b"different ciphertext").hexdigest(),
    )
    changed = SealedRawVolume(
        volume_id=first.volume_id,
        sequence=first.sequence,
        relative_path=first.relative_path,
        source_path=first.source_path,
        file_offset=first.file_offset,
        plaintext_bytes=first.plaintext_bytes,
        age_state_json=age_state_json(first.plaintext_bytes),
        file_bytes=first.file_bytes,
        file_sha256=first.file_sha256,
        parts=(changed_part,),
        revision="new-version",
        completed_at="2026-08-03T00:00:02Z",
    )

    assert raw_verification.raw_file_volume_set_sha256(
        file=file, volumes=(first,)
    ) != raw_verification.raw_file_volume_set_sha256(file=file, volumes=(changed,))


def test_part_manifest_verification_avoids_remote_read_after_write() -> None:
    from riverhog_protocol.raw_ingress import hash_raw_source

    whole = b"abcdefghij"
    file = ArchiveFile(
        path="large.bin",
        bytes=len(whole),
        sha256=hashlib.sha256(whole).hexdigest(),
    )
    volume = _sealed_segment(
        sequence=0,
        path=file.path,
        whole=whole,
        offset=0,
        content=whole,
    )
    manifest = hash_raw_source(
        path=file.path,
        chunks=(whole,),
        expected_bytes=len(whole),
        part_plaintext_bytes=65536,
    )

    verified = raw_verification.verify_raw_file_from_part_manifest(
        file=file,
        volumes=(volume,),
        manifest=manifest,
        verified_at="2026-08-03T00:00:01Z",
    )

    assert verified.sha256 == file.sha256
    assert verified.volume_set_sha256
