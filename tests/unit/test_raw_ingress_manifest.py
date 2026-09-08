from __future__ import annotations

import hashlib

from riverhog_api_client.source_hashing import hash_raw_source_chunks
from riverhog_protocol.raw_ingress import (
    RawSourceDigestSummary,
    ordered_raw_part_commitment,
    raw_volume_part_span,
)


def test_raw_source_is_hashed_once_into_small_authority_and_bounded_batches() -> None:
    content = b"abcdefgh" * 20000
    result = hash_raw_source_chunks(
        path="large.bin",
        chunks=(content[:1234], content[1234:]),
        expected_bytes=len(content),
        part_plaintext_bytes=65536,
    )
    try:
        batches = tuple(result.iter_batches(limit=2))
        parts = tuple(value for _first, batch in batches for value in batch)
        assert [first for first, _batch in batches] == [0, 2]
        assert result.summary.sha256 == hashlib.sha256(content).hexdigest()
        assert parts[0] == hashlib.sha256(content[:65536]).hexdigest()
        assert raw_volume_part_span(
            result.summary,
            file_offset=0,
            plaintext_bytes=len(content),
        ) == (0, len(parts))
        assert ordered_raw_part_commitment(parts) == (
            result.summary.part_count,
            result.summary.ordered_part_sha256,
        )
    finally:
        result.close()


def test_raw_source_digest_summary_has_no_resource_growing_member_list() -> None:
    digest = hashlib.sha256(b"content").hexdigest()
    count, commitment = ordered_raw_part_commitment((digest,))
    summary = RawSourceDigestSummary(
        path="large.bin",
        bytes=7,
        sha256=digest,
        part_plaintext_bytes=65536,
        part_count=count,
        ordered_part_sha256=commitment,
    )

    assert summary.schema == "raw-source-digest-summary/v1"
    assert not hasattr(summary, "part_sha256s")
