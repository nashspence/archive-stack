from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Iterable, Iterator, Sequence
from typing import Protocol

from riverhog_age import UploadState, iter_decrypt_age_scrypt
from riverhog_protocol.pack_ingress import (
    RESERVED_ARCHIVE_PREFIX,
    canonical_json_bytes,
)
from riverhog_protocol.paths import normalize_relpath
from riverhog_protocol.raw_ingress import RawSourceDigestSummary

from riverhog_core.domain.archive import (
    ArchiveFile,
    SealedRawVolume,
    StoredArchivePart,
    VerifiedRawFile,
)

RAW_FILE_VOLUME_SEQUENCE_SCHEMA = "raw-file-volume-sequence/v1"
RAW_FILE_VERIFICATION_SCHEMA = "raw-file-verification/v1"
_SHA256_RE = re.compile(r"[0-9a-f]{64}")


class _Digest(Protocol):
    def update(self, data: bytes) -> object: ...


def raw_file_ordered_volume_commitment(
    *,
    file: ArchiveFile,
    volumes: Iterable[SealedRawVolume],
) -> str:
    """Commit one file's exact ordered volume sequence with bounded working memory."""

    normalized = _normalized_raw_file(file)
    digest = hashlib.sha256()
    header = canonical_json_bytes(
        {
            "schema": RAW_FILE_VOLUME_SEQUENCE_SCHEMA,
            "file": {
                "path": normalized.path,
                "bytes": normalized.bytes,
                "sha256": normalized.sha256,
            },
        }
    )
    digest.update(len(header).to_bytes(8, "big"))
    digest.update(header)
    expected_offset = 0
    previous_sequence = -1
    volume_count = 0
    for volume in volumes:
        _validate_raw_volume(
            file=normalized,
            volume=volume,
            expected_offset=expected_offset,
            previous_sequence=previous_sequence,
        )
        encoded = canonical_json_bytes(_volume_identity(volume))
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)
        expected_offset += volume.plaintext_bytes
        previous_sequence = volume.sequence
        volume_count += 1
    if volume_count < 1 or expected_offset != normalized.bytes:
        raise ValueError("sealed raw volume sequence does not cover the requested file")
    return digest.hexdigest()


def raw_file_verification_payload(receipt: VerifiedRawFile) -> dict[str, object]:
    path = normalize_relpath(receipt.path)
    if path.startswith(RESERVED_ARCHIVE_PREFIX):
        raise ValueError("raw verification path uses the reserved archive namespace")
    if receipt.bytes < 0 or _SHA256_RE.fullmatch(receipt.sha256) is None:
        raise ValueError("raw verification file identity is invalid")
    if _SHA256_RE.fullmatch(receipt.ordered_volume_sha256) is None or not receipt.verified_at:
        raise ValueError("raw verification receipt identity is invalid")
    return {
        "schema": RAW_FILE_VERIFICATION_SCHEMA,
        "path": path,
        "bytes": receipt.bytes,
        "sha256": receipt.sha256,
        "ordered_volume_sha256": receipt.ordered_volume_sha256,
        "verified_at": receipt.verified_at,
    }


def verify_raw_file_from_digest_summary(
    *,
    file: ArchiveFile,
    volumes: Iterable[SealedRawVolume],
    summary: RawSourceDigestSummary,
    verified_at: str,
) -> VerifiedRawFile:
    """Verify a raw file without downloading the newly written archive objects.

    The official client computes the flat file digest and an ordered commitment over
    fixed-size part digests in one source pass. RawVolumeUploader verifies each received
    part against the bounded registered rows before committing it. This function binds the
    sealed logical file to its exact volume sequence without reading storage again.
    """

    normalized_file = _normalized_raw_file(file)
    if not verified_at:
        raise ValueError("raw verification timestamp is required")
    if (
        summary.path != normalized_file.path
        or summary.bytes != normalized_file.bytes
        or summary.sha256 != normalized_file.sha256
    ):
        raise ValueError("raw source digest summary does not match the file")
    return VerifiedRawFile(
        path=normalized_file.path,
        bytes=normalized_file.bytes,
        sha256=normalized_file.sha256,
        ordered_volume_sha256=raw_file_ordered_volume_commitment(
            file=normalized_file,
            volumes=volumes,
        ),
        verified_at=verified_at,
    )


def verify_raw_file(
    *,
    file: ArchiveFile,
    volumes: Sequence[SealedRawVolume],
    passphrase: str,
    read_ciphertext_chunks: Callable[[str], Iterable[bytes]],
    verified_at: str,
) -> VerifiedRawFile:
    """Re-read sealed raw volumes, stream-decrypt, and verify the flat file SHA-256.

    Pack members are verified before their containing archive part is committed. A large
    file spans resumable upload parts, so its registered flat SHA-256 is instead verified by
    this bounded-memory read before the immutable root may be published.
    """

    normalized_file = _normalized_raw_file(file)
    if not passphrase or not verified_at:
        raise ValueError("raw verification requires a passphrase and verification timestamp")
    sequence_digest = hashlib.sha256()
    header = canonical_json_bytes(
        {
            "schema": RAW_FILE_VOLUME_SEQUENCE_SCHEMA,
            "file": {
                "path": normalized_file.path,
                "bytes": normalized_file.bytes,
                "sha256": normalized_file.sha256,
            },
        }
    )
    sequence_digest.update(len(header).to_bytes(8, "big"))
    sequence_digest.update(header)
    file_digest = hashlib.sha256()
    file_bytes = 0
    previous_sequence = -1
    volume_count = 0
    for volume in volumes:
        _validate_raw_volume(
            file=normalized_file,
            volume=volume,
            expected_offset=file_bytes,
            previous_sequence=previous_sequence,
        )
        encoded = canonical_json_bytes(_volume_identity(volume))
        sequence_digest.update(len(encoded).to_bytes(8, "big"))
        sequence_digest.update(encoded)
        ciphertext = _iter_verified_stored_parts(
            read_ciphertext_chunks(volume.relative_path),
            volume.parts,
        )
        plaintext = iter_decrypt_age_scrypt(ciphertext, passphrase)
        segment_bytes = _consume_verified_plaintext_parts(
            plaintext,
            volume.parts,
            file_digest=file_digest,
        )
        if segment_bytes != volume.plaintext_bytes:
            raise ValueError("decrypted raw volume byte count mismatch")
        file_bytes += segment_bytes
        previous_sequence = volume.sequence
        volume_count += 1
    if (
        volume_count < 1
        or file_bytes != normalized_file.bytes
        or file_digest.hexdigest() != normalized_file.sha256
    ):
        raise ValueError(f"sealed raw file verification failed: {normalized_file.path}")
    return VerifiedRawFile(
        path=normalized_file.path,
        bytes=normalized_file.bytes,
        sha256=normalized_file.sha256,
        ordered_volume_sha256=sequence_digest.hexdigest(),
        verified_at=verified_at,
    )


def _normalized_raw_file(file: ArchiveFile) -> ArchiveFile:
    path = normalize_relpath(file.path)
    if path.startswith(RESERVED_ARCHIVE_PREFIX):
        raise ValueError("raw file uses the reserved archive namespace")
    if file.bytes < 0 or _SHA256_RE.fullmatch(file.sha256) is None:
        raise ValueError("raw file identity is invalid")
    return ArchiveFile(path=path, bytes=file.bytes, sha256=file.sha256)


def _validate_raw_volume(
    *,
    file: ArchiveFile,
    volume: SealedRawVolume,
    expected_offset: int,
    previous_sequence: int,
) -> None:
    source_path = normalize_relpath(volume.source_path)
    expected_relative_path = f"volumes/{volume.volume_id}.bin.age"
    relative_path = normalize_relpath(volume.relative_path)
    if (
        source_path != file.path
        or volume.file_bytes != file.bytes
        or volume.file_sha256 != file.sha256
        or volume.file_offset != expected_offset
        or volume.plaintext_bytes < 0
        or volume.sequence <= previous_sequence
        or volume.sequence >= 1 << 256
        or volume.volume_id != f"segment-{volume.sequence:064x}"
        or relative_path != expected_relative_path
    ):
        raise ValueError("sealed raw volumes do not form the requested file")
    state = UploadState.from_json_bytes(volume.age_state_json)
    if state.plaintext_size != volume.plaintext_bytes:
        raise ValueError("raw volume age state plaintext size mismatch")
    _validate_part_receipts(volume.parts, plaintext_bytes=volume.plaintext_bytes)


def _volume_identity(volume: SealedRawVolume) -> dict[str, object]:
    return {
        "id": volume.volume_id,
        "sequence": volume.sequence,
        "path": normalize_relpath(volume.relative_path),
        "file_offset": volume.file_offset,
        "plaintext_bytes": volume.plaintext_bytes,
        "age_state": json.loads(UploadState.from_json_bytes(volume.age_state_json).to_json_bytes()),
        "parts": [_part_identity(current_part) for current_part in volume.parts],
    }


def _part_identity(part: StoredArchivePart) -> dict[str, object]:
    return {
        "number": part.number,
        "plaintext_start": part.plaintext_start,
        "plaintext_bytes": part.plaintext_bytes,
        "plaintext_sha256": part.plaintext_sha256,
        "stored_bytes": part.stored_bytes,
        "stored_sha256": part.stored_sha256,
    }


def _validate_part_receipts(
    parts: Sequence[StoredArchivePart],
    *,
    plaintext_bytes: int,
) -> None:
    if not parts:
        raise ValueError("raw volume requires at least one stored part")
    expected_start = 0
    for expected_number, part in enumerate(parts, start=1):
        if (
            part.number != expected_number
            or part.plaintext_start != expected_start
            or part.plaintext_bytes < 0
            or part.stored_bytes < 1
            or _SHA256_RE.fullmatch(part.plaintext_sha256) is None
            or _SHA256_RE.fullmatch(part.stored_sha256) is None
        ):
            raise ValueError("raw volume part receipt is invalid")
        expected_start += part.plaintext_bytes
    if expected_start != plaintext_bytes:
        raise ValueError("raw volume parts do not cover its plaintext")


def _iter_verified_stored_parts(
    chunks: Iterable[bytes],
    parts: Sequence[StoredArchivePart],
) -> Iterator[bytes]:
    source = iter(chunks)
    buffer = bytearray()
    for expected_number, part in enumerate(parts, start=1):
        if part.number != expected_number or part.stored_bytes <= 0:
            raise ValueError("raw stored part order is invalid")
        digest = hashlib.sha256()
        remaining = part.stored_bytes
        while remaining:
            if not buffer:
                try:
                    buffer.extend(bytes(next(source)))
                except StopIteration as exc:
                    raise ValueError("raw ciphertext ended before its recorded parts") from exc
                if not buffer:
                    continue
            take = min(remaining, len(buffer))
            current = bytes(buffer[:take])
            del buffer[:take]
            remaining -= take
            digest.update(current)
            yield current
        if digest.hexdigest() != part.stored_sha256:
            raise ValueError("raw stored part sha256 mismatch")
    if buffer:
        raise ValueError("raw ciphertext is longer than its recorded parts")
    for current in source:
        if bytes(current):
            raise ValueError("raw ciphertext is longer than its recorded parts")


def _consume_verified_plaintext_parts(
    chunks: Iterable[bytes],
    parts: Sequence[StoredArchivePart],
    *,
    file_digest: _Digest,
) -> int:
    source = iter(chunks)
    buffer = bytearray()
    total = 0
    for expected_number, part in enumerate(parts, start=1):
        if part.number != expected_number or part.plaintext_bytes < 0:
            raise ValueError("raw plaintext part order is invalid")
        digest = hashlib.sha256()
        remaining = part.plaintext_bytes
        while remaining:
            if not buffer:
                try:
                    buffer.extend(bytes(next(source)))
                except StopIteration as exc:
                    raise ValueError("raw plaintext ended before its recorded parts") from exc
                if not buffer:
                    continue
            take = min(remaining, len(buffer))
            current = bytes(buffer[:take])
            del buffer[:take]
            remaining -= take
            total += take
            digest.update(current)
            file_digest.update(current)
        if digest.hexdigest() != part.plaintext_sha256:
            raise ValueError("raw plaintext part sha256 mismatch")
    if buffer:
        raise ValueError("raw plaintext is longer than its recorded parts")
    for current in source:
        if bytes(current):
            raise ValueError("raw plaintext is longer than its recorded parts")
    return total
