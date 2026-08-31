"""Bounded-memory source hashing support for official Riverhog producers."""

from __future__ import annotations

import hashlib
import tempfile
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from typing import BinaryIO

from riverhog_protocol.raw_ingress import (
    RAW_SOURCE_DIGEST_BATCH_MAX,
    RawSourceDigestSummary,
    advance_raw_part_commitment,
)


@dataclass(slots=True)
class RawSourceHash:
    """One small authority plus a private bounded-step digest construction spool."""

    summary: RawSourceDigestSummary
    _parts: BinaryIO

    def iter_batches(
        self,
        *,
        limit: int = RAW_SOURCE_DIGEST_BATCH_MAX,
    ) -> Iterator[tuple[int, tuple[str, ...]]]:
        if limit < 1 or limit > RAW_SOURCE_DIGEST_BATCH_MAX:
            raise ValueError("raw digest batch limit is invalid")
        self._parts.seek(0)
        first = 0
        while first < self.summary.part_count:
            count = min(limit, self.summary.part_count - first)
            content = self._parts.read(count * 32)
            if len(content) != count * 32:
                raise RuntimeError("raw digest construction spool is incomplete")
            yield (
                first,
                tuple(content[offset : offset + 32].hex() for offset in range(0, len(content), 32)),
            )
            first += count

    def close(self) -> None:
        self._parts.close()

    def __del__(self) -> None:
        self._parts.close()


def hash_raw_source_chunks(
    *,
    path: str,
    chunks: Iterable[bytes],
    expected_bytes: int,
    part_plaintext_bytes: int,
) -> RawSourceHash:
    """Hash one logical source while retaining only a bounded part and private spool."""

    if expected_bytes < 0 or part_plaintext_bytes < 65536 or part_plaintext_bytes % 65536:
        raise ValueError("raw source hash byte values are invalid")
    parts = tempfile.TemporaryFile(mode="w+b")
    whole = hashlib.sha256()
    part = hashlib.sha256()
    part_bytes = 0
    total = 0
    part_count = 0
    commitment: str | None = None
    try:
        for source in chunks:
            chunk = bytes(source)
            whole.update(chunk)
            total += len(chunk)
            if total > expected_bytes:
                raise ValueError("raw source is longer than declared")
            view = memoryview(chunk)
            offset = 0
            while offset < len(view):
                take = min(part_plaintext_bytes - part_bytes, len(view) - offset)
                part.update(view[offset : offset + take])
                part_bytes += take
                offset += take
                if part_bytes == part_plaintext_bytes:
                    digest = part.hexdigest()
                    parts.write(bytes.fromhex(digest))
                    part_count, commitment = advance_raw_part_commitment(
                        commitment,
                        first_part=part_count,
                        part_sha256s=(digest,),
                    )
                    part = hashlib.sha256()
                    part_bytes = 0
        if total != expected_bytes:
            raise ValueError("raw source byte count mismatch")
        if part_bytes or expected_bytes == 0:
            digest = part.hexdigest()
            parts.write(bytes.fromhex(digest))
            part_count, commitment = advance_raw_part_commitment(
                commitment,
                first_part=part_count,
                part_sha256s=(digest,),
            )
        if commitment is None:
            raise RuntimeError("raw source digest commitment was not constructed")
        parts.flush()
        return RawSourceHash(
            summary=RawSourceDigestSummary(
                path=path,
                bytes=expected_bytes,
                sha256=whole.hexdigest(),
                part_plaintext_bytes=part_plaintext_bytes,
                part_count=part_count,
                ordered_part_sha256=commitment,
            ),
            _parts=parts,
        )
    except Exception:
        parts.close()
        raise


__all__ = ["RawSourceHash", "hash_raw_source_chunks"]
