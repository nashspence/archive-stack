from __future__ import annotations

import builtins
import hashlib
import json
import re
from collections.abc import Iterable
from dataclasses import dataclass

from riverhog_protocol.paths import normalize_relpath

RAW_SOURCE_DIGEST_MANIFEST_SCHEMA = "raw-source-digest-manifest/v1"
_SHA256_RE = re.compile(r"[0-9a-f]{64}")


@dataclass(frozen=True, slots=True)
class RawSourceDigestManifest:
    path: str
    bytes: int
    sha256: str
    part_plaintext_bytes: int
    part_sha256s: tuple[str, ...]

    def __post_init__(self) -> None:
        normalized = normalize_relpath(self.path)
        if normalized != self.path:
            raise ValueError("raw source digest path is not canonical")
        if self.bytes < 0 or self.part_plaintext_bytes < 65536:
            raise ValueError("raw source digest byte values are invalid")
        if self.part_plaintext_bytes % 65536:
            raise ValueError("raw source digest part size must align to age chunks")
        if _SHA256_RE.fullmatch(self.sha256) is None:
            raise ValueError("raw source digest sha256 is invalid")
        expected_parts = max(
            1,
            (self.bytes + self.part_plaintext_bytes - 1) // self.part_plaintext_bytes,
        )
        if len(self.part_sha256s) != expected_parts:
            raise ValueError("raw source digest part count is invalid")
        if any(_SHA256_RE.fullmatch(current) is None for current in self.part_sha256s):
            raise ValueError("raw source digest part sha256 is invalid")

    def to_json_bytes(self) -> builtins.bytes:
        return json.dumps(
            {
                "schema": RAW_SOURCE_DIGEST_MANIFEST_SCHEMA,
                "path": self.path,
                "bytes": self.bytes,
                "sha256": self.sha256,
                "part_plaintext_bytes": self.part_plaintext_bytes,
                "part_sha256s": list(self.part_sha256s),
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")

    @classmethod
    def from_json_bytes(cls, content: builtins.bytes | str) -> RawSourceDigestManifest:
        if isinstance(content, builtins.bytes):
            content = content.decode("utf-8")
        try:
            payload = json.loads(content)
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise ValueError("raw source digest manifest is not valid JSON") from exc
        if not isinstance(payload, dict) or set(payload) != {
            "schema",
            "path",
            "bytes",
            "sha256",
            "part_plaintext_bytes",
            "part_sha256s",
        }:
            raise ValueError("raw source digest manifest fields are invalid")
        if payload.get("schema") != RAW_SOURCE_DIGEST_MANIFEST_SCHEMA:
            raise ValueError("raw source digest manifest schema mismatch")
        part_sha256s = payload.get("part_sha256s")
        if not isinstance(part_sha256s, list):
            raise ValueError("raw source digest part sha256s must be a list")
        manifest = cls(
            path=normalize_relpath(str(payload.get("path", ""))),
            bytes=_uint(payload.get("bytes"), "raw source bytes"),
            sha256=str(payload.get("sha256", "")),
            part_plaintext_bytes=_positive_uint(
                payload.get("part_plaintext_bytes"),
                "raw source part plaintext bytes",
            ),
            part_sha256s=tuple(str(current) for current in part_sha256s),
        )
        if manifest.to_json_bytes().decode("utf-8") != content:
            raise ValueError("raw source digest manifest JSON is not canonical")
        return manifest


def hash_raw_source(
    *,
    path: str,
    chunks: Iterable[builtins.bytes],
    expected_bytes: int,
    part_plaintext_bytes: int,
) -> RawSourceDigestManifest:
    """Hash a raw source once, producing both whole-file and upload-part identities."""

    normalized = normalize_relpath(path)
    if expected_bytes < 0 or part_plaintext_bytes <= 0:
        raise ValueError("raw source hash byte values are invalid")
    whole = hashlib.sha256()
    part = hashlib.sha256()
    part_bytes = 0
    total = 0
    part_sha256s: list[str] = []
    for chunk in chunks:
        view = memoryview(builtins.bytes(chunk))
        offset = 0
        while offset < len(view):
            take = min(part_plaintext_bytes - part_bytes, len(view) - offset)
            current = view[offset : offset + take]
            whole.update(current)
            part.update(current)
            total += take
            part_bytes += take
            offset += take
            if total > expected_bytes:
                raise ValueError("raw source is longer than declared")
            if part_bytes == part_plaintext_bytes:
                part_sha256s.append(part.hexdigest())
                part = hashlib.sha256()
                part_bytes = 0
    if total != expected_bytes:
        raise ValueError("raw source byte count mismatch")
    if part_bytes or expected_bytes == 0:
        part_sha256s.append(part.hexdigest())
    return RawSourceDigestManifest(
        path=normalized,
        bytes=expected_bytes,
        sha256=whole.hexdigest(),
        part_plaintext_bytes=part_plaintext_bytes,
        part_sha256s=tuple(part_sha256s),
    )


def raw_volume_part_sha256s(
    manifest: RawSourceDigestManifest,
    *,
    file_offset: int,
    plaintext_bytes: int,
) -> tuple[str, ...]:
    """Select digest rows for an aligned raw volume from the whole-file manifest."""

    if file_offset < 0 or plaintext_bytes < 0:
        raise ValueError("raw volume digest range is invalid")
    if file_offset + plaintext_bytes > manifest.bytes:
        raise ValueError("raw volume digest range exceeds the source")
    part_bytes = manifest.part_plaintext_bytes
    if file_offset % part_bytes:
        raise ValueError("raw volume offset must align to the digest part size")
    final = file_offset + plaintext_bytes == manifest.bytes
    if not final and plaintext_bytes % part_bytes:
        raise ValueError("non-final raw volume must end on a digest part boundary")
    first = file_offset // part_bytes
    count = max(1, (plaintext_bytes + part_bytes - 1) // part_bytes)
    selected = manifest.part_sha256s[first : first + count]
    if len(selected) != count:
        raise ValueError("raw volume digest manifest does not cover the volume")
    return selected


def _positive_uint(value: object, label: str) -> int:
    parsed = _uint(value, label)
    if parsed < 1:
        raise ValueError(f"{label} must be positive")
    return parsed


def _uint(value: object, label: str) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{label} must be a non-negative integer")
    try:
        parsed = int(str(value))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be a non-negative integer") from exc
    if parsed < 0 or str(parsed) != str(value):
        raise ValueError(f"{label} must be a canonical non-negative integer")
    return parsed
