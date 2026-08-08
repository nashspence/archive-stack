from __future__ import annotations

import hashlib
import json

from riverhog_protocol.raw_ingress import (
    RawSourceDigestManifest,
    hash_raw_source,
    raw_volume_part_sha256s,
)


def test_raw_source_is_hashed_once_for_whole_file_and_upload_parts() -> None:
    content = b"abcdefgh" * 20000
    manifest = hash_raw_source(
        path="large.bin",
        chunks=(content[:1234], content[1234:]),
        expected_bytes=len(content),
        part_plaintext_bytes=65536,
    )

    assert manifest.sha256 == hashlib.sha256(content).hexdigest()
    assert manifest.part_sha256s[0] == hashlib.sha256(content[:65536]).hexdigest()
    assert (
        raw_volume_part_sha256s(
            manifest,
            file_offset=0,
            plaintext_bytes=len(content),
        )
        == manifest.part_sha256s
    )
    assert RawSourceDigestManifest.from_json_bytes(manifest.to_json_bytes()) == manifest


def test_raw_source_digest_manifest_has_canonical_identity() -> None:
    manifest = hash_raw_source(
        path="large.bin",
        chunks=(b"content",),
        expected_bytes=7,
        part_plaintext_bytes=65536,
    )
    assert json.loads(manifest.to_json_bytes())["schema"] == "raw-source-digest-manifest/v1"
