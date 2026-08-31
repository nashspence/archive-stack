"""Bounded identities for direct raw-file archive ingress."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable
from dataclasses import dataclass

from riverhog_protocol.paths import normalize_relpath

RAW_SOURCE_DIGEST_SUMMARY_SCHEMA = "raw-source-digest-summary/v1"
RAW_SOURCE_DIGEST_BATCH_MAX = 1024
_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_COMMITMENT_SEED = hashlib.sha256(b"riverhog-ordered-raw-parts/v1\0").digest()


def _part_count(byte_count: int, part_plaintext_bytes: int) -> int:
    if byte_count < 0 or part_plaintext_bytes < 65536 or part_plaintext_bytes % 65536:
        raise ValueError("raw source digest byte values are invalid")
    return max(1, (byte_count + part_plaintext_bytes - 1) // part_plaintext_bytes)


def ordered_raw_part_commitment(part_sha256s: Iterable[str]) -> tuple[int, str]:
    """Commit an ordered logical digest sequence with constant working state."""

    state = _COMMITMENT_SEED
    count = 0
    for expected_number, value in enumerate(part_sha256s):
        state = _advance_part_commitment(state, expected_number, value)
        count += 1
    if count < 1:
        raise ValueError("raw source digest sequence must not be empty")
    return count, state.hex()


def advance_raw_part_commitment(
    prior_sha256: str | None,
    *,
    first_part: int,
    part_sha256s: Iterable[str],
) -> tuple[int, str]:
    """Advance a durable ordered commitment by one append-only bounded batch."""

    if first_part < 0:
        raise ValueError("raw source first part must be non-negative")
    if first_part == 0:
        if prior_sha256 is not None:
            raise ValueError("initial raw source commitment must not have prior state")
        state = _COMMITMENT_SEED
    else:
        if prior_sha256 is None or _SHA256_RE.fullmatch(prior_sha256) is None:
            raise ValueError("raw source commitment state is invalid")
        state = bytes.fromhex(prior_sha256)
    count = 0
    for offset, value in enumerate(part_sha256s):
        state = _advance_part_commitment(state, first_part + offset, value)
        count += 1
    if count < 1:
        raise ValueError("raw source digest batch must not be empty")
    return first_part + count, state.hex()


def _advance_part_commitment(state: bytes, number: int, value: str) -> bytes:
    if _SHA256_RE.fullmatch(value) is None:
        raise ValueError("raw source part SHA-256 is invalid")
    return hashlib.sha256(state + number.to_bytes(8, "big") + bytes.fromhex(value)).digest()


@dataclass(frozen=True, slots=True)
class RawSourceDigestSummary:
    """Small exact authority for an arbitrarily large raw source."""

    path: str
    bytes: int
    sha256: str
    part_plaintext_bytes: int
    part_count: int
    ordered_part_sha256: str
    schema: str = RAW_SOURCE_DIGEST_SUMMARY_SCHEMA

    def __post_init__(self) -> None:
        if normalize_relpath(self.path) != self.path:
            raise ValueError("raw source digest path is not canonical")
        expected_parts = _part_count(self.bytes, self.part_plaintext_bytes)
        if self.part_count != expected_parts:
            raise ValueError("raw source digest part count is invalid")
        if (
            _SHA256_RE.fullmatch(self.sha256) is None
            or _SHA256_RE.fullmatch(self.ordered_part_sha256) is None
        ):
            raise ValueError("raw source digest SHA-256 is invalid")
        if self.schema != RAW_SOURCE_DIGEST_SUMMARY_SCHEMA:
            raise ValueError("raw source digest summary schema mismatch")


def raw_volume_part_span(
    summary: RawSourceDigestSummary,
    *,
    file_offset: int,
    plaintext_bytes: int,
) -> tuple[int, int]:
    """Return the exact bounded digest-row span for one raw archive volume."""

    if file_offset < 0 or plaintext_bytes < 0:
        raise ValueError("raw volume digest range is invalid")
    if file_offset + plaintext_bytes > summary.bytes:
        raise ValueError("raw volume digest range exceeds the source")
    part_bytes = summary.part_plaintext_bytes
    if file_offset % part_bytes:
        raise ValueError("raw volume offset must align to the digest part size")
    final = file_offset + plaintext_bytes == summary.bytes
    if not final and plaintext_bytes % part_bytes:
        raise ValueError("non-final raw volume must end on a digest part boundary")
    first = file_offset // part_bytes
    count = max(1, (plaintext_bytes + part_bytes - 1) // part_bytes)
    if first + count > summary.part_count:
        raise ValueError("raw source digest sequence does not cover the volume")
    return first, count


__all__ = [
    "RAW_SOURCE_DIGEST_BATCH_MAX",
    "RAW_SOURCE_DIGEST_SUMMARY_SCHEMA",
    "RawSourceDigestSummary",
    "advance_raw_part_commitment",
    "ordered_raw_part_commitment",
    "raw_volume_part_span",
]
