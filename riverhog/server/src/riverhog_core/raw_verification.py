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
from riverhog_protocol.raw_ingress import (
    RawSourceDigestManifest,
    raw_volume_part_sha256s,
)

from riverhog_core.domain.archive import (
    ArchiveFile,
    SealedRawVolume,
    StoredPartReceipt,
    VerifiedRawFile,
)

RAW_FILE_VOLUME_SET_SCHEMA = "raw-file-volume-set/v1"
RAW_FILE_VERIFICATION_SCHEMA = "raw-file-verification/v1"
_SHA256_RE = re.compile(r"[0-9a-f]{64}")


class _Digest(Protocol):
    def update(self, data: bytes) -> object: ...


def raw_file_volume_set_payload(
    *,
    file: ArchiveFile,
    volumes: Sequence[SealedRawVolume],
) -> dict[str, object]:
    """Return the canonical logical identity of the exact sealed segments for one file.

    Store-specific ETags, version IDs, and timestamps are deliberately excluded. The
    identity binds the file to each immutable object path, range, and plaintext/stored
    part digest, which prevents a stale verification receipt from authorizing a different
    set of segment objects.
    """

    normalized_file, ordered = _validated_raw_volume_set(file=file, volumes=volumes)
    return {
        "schema": RAW_FILE_VOLUME_SET_SCHEMA,
        "file": {
            "path": normalized_file.path,
            "bytes": normalized_file.bytes,
            "sha256": normalized_file.sha256,
        },
        "volumes": [
            {
                "id": current.volume_id,
                "sequence": current.sequence,
                "path": current.relative_path,
                "file_offset": current.file_offset,
                "plaintext_bytes": current.plaintext_bytes,
                "age_state": json.loads(current.age_state_json),
                "parts": [_part_identity(current_part) for current_part in current.parts],
            }
            for current in ordered
        ],
    }


def raw_file_volume_set_sha256(
    *,
    file: ArchiveFile,
    volumes: Sequence[SealedRawVolume],
) -> str:
    return hashlib.sha256(
        canonical_json_bytes(raw_file_volume_set_payload(file=file, volumes=volumes))
    ).hexdigest()


def raw_file_verification_payload(receipt: VerifiedRawFile) -> dict[str, object]:
    path = normalize_relpath(receipt.path)
    if path.startswith(RESERVED_ARCHIVE_PREFIX):
        raise ValueError("raw verification path uses the reserved archive namespace")
    if receipt.bytes < 0 or _SHA256_RE.fullmatch(receipt.sha256) is None:
        raise ValueError("raw verification file identity is invalid")
    if _SHA256_RE.fullmatch(receipt.volume_set_sha256) is None or not receipt.verified_at:
        raise ValueError("raw verification receipt identity is invalid")
    return {
        "schema": RAW_FILE_VERIFICATION_SCHEMA,
        "path": path,
        "bytes": receipt.bytes,
        "sha256": receipt.sha256,
        "volume_set_sha256": receipt.volume_set_sha256,
        "verified_at": receipt.verified_at,
    }


def verify_raw_file_from_part_manifest(
    *,
    file: ArchiveFile,
    volumes: Sequence[SealedRawVolume],
    manifest: RawSourceDigestManifest,
    verified_at: str,
) -> VerifiedRawFile:
    """Verify a raw file without downloading the newly written archive objects.

    The official client computes the flat file digest and fixed-size part digests in one
    source pass. RawVolumeUploader verifies each received part against those registered
    digests before committing it. This function then binds the complete digest manifest to
    the exact sealed volume set. ``verify_raw_file`` remains available as an optional
    remote read-after-write audit for operators who prefer the extra transfer cost.
    """

    normalized_file, ordered = _validated_raw_volume_set(file=file, volumes=volumes)
    if not verified_at:
        raise ValueError("raw verification timestamp is required")
    if (
        manifest.path != normalized_file.path
        or manifest.bytes != normalized_file.bytes
        or manifest.sha256 != normalized_file.sha256
    ):
        raise ValueError("raw source digest manifest does not match the file")
    for volume in ordered:
        expected = raw_volume_part_sha256s(
            manifest,
            file_offset=volume.file_offset,
            plaintext_bytes=volume.plaintext_bytes,
        )
        actual = tuple(current.plaintext_sha256 for current in volume.parts)
        if actual != expected:
            raise ValueError(
                f"sealed raw volume does not match its source digest manifest: {volume.volume_id}"
            )
    return VerifiedRawFile(
        path=normalized_file.path,
        bytes=normalized_file.bytes,
        sha256=normalized_file.sha256,
        volume_set_sha256=raw_file_volume_set_sha256(
            file=normalized_file,
            volumes=ordered,
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

    Pack members are verified before their containing multipart part is committed. A large
    file spans resumable upload parts, so its registered flat SHA-256 is instead verified by
    this bounded-memory read before the immutable root may be published.
    """

    normalized_file, ordered = _validated_raw_volume_set(file=file, volumes=volumes)
    if not passphrase or not verified_at:
        raise ValueError("raw verification requires a passphrase and verification timestamp")
    volume_set_sha256 = raw_file_volume_set_sha256(file=normalized_file, volumes=ordered)

    file_digest = hashlib.sha256()
    file_bytes = 0
    for volume in ordered:
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
    if file_bytes != normalized_file.bytes or file_digest.hexdigest() != normalized_file.sha256:
        raise ValueError(f"sealed raw file verification failed: {normalized_file.path}")
    return VerifiedRawFile(
        path=normalized_file.path,
        bytes=normalized_file.bytes,
        sha256=normalized_file.sha256,
        volume_set_sha256=volume_set_sha256,
        verified_at=verified_at,
    )


def _validated_raw_volume_set(
    *,
    file: ArchiveFile,
    volumes: Sequence[SealedRawVolume],
) -> tuple[ArchiveFile, tuple[SealedRawVolume, ...]]:
    path = normalize_relpath(file.path)
    if path.startswith(RESERVED_ARCHIVE_PREFIX):
        raise ValueError("raw file uses the reserved archive namespace")
    if file.bytes < 0 or _SHA256_RE.fullmatch(file.sha256) is None:
        raise ValueError("raw file identity is invalid")
    normalized_file = ArchiveFile(path=path, bytes=file.bytes, sha256=file.sha256)
    ordered = tuple(sorted(volumes, key=lambda current: current.file_offset))
    if not ordered:
        raise ValueError("raw file verification requires at least one sealed volume")

    expected_offset = 0
    seen_ids: set[str] = set()
    seen_paths: set[str] = set()
    for volume in ordered:
        source_path = normalize_relpath(volume.source_path)
        expected_relative_path = f"volumes/{volume.volume_id}.bin.age"
        relative_path = normalize_relpath(volume.relative_path)
        if (
            source_path != path
            or volume.file_bytes != file.bytes
            or volume.file_sha256 != file.sha256
            or volume.file_offset != expected_offset
            or volume.plaintext_bytes < 0
            or volume.volume_id != f"segment-{volume.sequence:012d}"
            or relative_path != expected_relative_path
            or volume.volume_id in seen_ids
            or relative_path in seen_paths
        ):
            raise ValueError("sealed raw volumes do not form the requested file")
        state = UploadState.from_json_bytes(volume.age_state_json)
        if state.plaintext_size != volume.plaintext_bytes:
            raise ValueError("raw volume age state plaintext size mismatch")
        _validate_part_receipts(volume.parts, plaintext_bytes=volume.plaintext_bytes)
        expected_offset += volume.plaintext_bytes
        seen_ids.add(volume.volume_id)
        seen_paths.add(relative_path)
    if expected_offset != file.bytes:
        raise ValueError("sealed raw volumes do not cover the requested file")
    return normalized_file, ordered


def _part_identity(part: StoredPartReceipt) -> dict[str, object]:
    return {
        "number": part.number,
        "plaintext_start": part.plaintext_start,
        "plaintext_bytes": part.plaintext_bytes,
        "plaintext_sha256": part.plaintext_sha256,
        "stored_bytes": part.stored_bytes,
        "stored_sha256": part.stored_sha256,
    }


def _validate_part_receipts(
    parts: Sequence[StoredPartReceipt],
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
            or not part.etag
        ):
            raise ValueError("raw volume part receipt is invalid")
        expected_start += part.plaintext_bytes
    if expected_start != plaintext_bytes:
        raise ValueError("raw volume parts do not cover its plaintext")


def _iter_verified_stored_parts(
    chunks: Iterable[bytes],
    parts: Sequence[StoredPartReceipt],
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
    parts: Sequence[StoredPartReceipt],
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
