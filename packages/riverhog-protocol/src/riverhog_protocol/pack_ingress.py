from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass

from riverhog_protocol.paths import normalize_relpath

PACK_UPLOAD_PLAN_SCHEMA = "pack-upload-plan/v1"
RESERVED_ARCHIVE_PREFIX = ".riverhog/"
_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_PACK_VOLUME_ID_RE = re.compile(r"pack-[0-9a-f]{64}")


@dataclass(frozen=True, slots=True)
class PackUnitSource:
    path: str
    bytes: int
    sha256: str

    def __post_init__(self) -> None:
        normalized = normalize_relpath(self.path)
        if normalized.startswith(RESERVED_ARCHIVE_PREFIX):
            raise ValueError(f"collection path uses reserved archive namespace: {normalized}")
        if self.bytes < 0:
            raise ValueError("pack unit source bytes must be non-negative")
        if _SHA256_RE.fullmatch(self.sha256) is None:
            raise ValueError("pack unit source sha256 is invalid")
        object.__setattr__(self, "path", normalized)


@dataclass(frozen=True, slots=True)
class PackUnitDescriptor:
    volume_id: str
    unit: int
    payload_bytes: int
    plaintext_start: int
    plaintext_bytes: int
    final: bool
    sources: tuple[PackUnitSource, ...]
    plan_sha256: str

    def __post_init__(self) -> None:
        if _PACK_VOLUME_ID_RE.fullmatch(self.volume_id) is None:
            raise ValueError("pack volume id is invalid")
        if self.unit < 0:
            raise ValueError("pack unit number must be non-negative")
        if self.payload_bytes < 0:
            raise ValueError("pack unit payload bytes must be non-negative")
        if self.plaintext_start < 0 or self.plaintext_bytes < 0:
            raise ValueError("pack unit plaintext range is invalid")
        if sum(source.bytes for source in self.sources) != self.payload_bytes:
            raise ValueError("pack unit source bytes do not match payload bytes")
        if _SHA256_RE.fullmatch(self.plan_sha256) is None:
            raise ValueError("pack unit plan sha256 is invalid")
        paths = [source.path for source in self.sources]
        if len(paths) != len(set(paths)):
            raise ValueError("pack unit repeats a source path")

    @property
    def plaintext_end(self) -> int:
        return self.plaintext_start + self.plaintext_bytes


def canonical_json_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def pack_upload_plan_sha256(
    *,
    volume_id: str,
    plaintext_bytes: int,
    units: Sequence[PackUnitDescriptor] | Sequence[Mapping[str, object]],
) -> str:
    if _PACK_VOLUME_ID_RE.fullmatch(volume_id) is None:
        raise ValueError("pack volume id is invalid")
    if plaintext_bytes <= 0:
        raise ValueError("pack upload plaintext bytes must be positive")
    if not units:
        raise ValueError("pack upload plan unit count is invalid")
    normalized_rows: list[dict[str, object]] = []
    for index, current in enumerate(units):
        if isinstance(current, PackUnitDescriptor):
            row: Mapping[str, object] = {
                "unit": current.unit,
                "payload_bytes": current.payload_bytes,
                "plaintext_start": current.plaintext_start,
                "plaintext_bytes": current.plaintext_bytes,
                "final": current.final,
                "sources": [
                    {
                        "path": source.path,
                        "bytes": source.bytes,
                        "sha256": source.sha256,
                    }
                    for source in current.sources
                ],
            }
        else:
            if not isinstance(current, Mapping):
                raise ValueError("pack upload unit must be a canonical mapping")
            row = current
        normalized_rows.append(_normalized_unit_row(row, expected_unit=index))
    payload = {
        "schema": PACK_UPLOAD_PLAN_SCHEMA,
        "volume_id": volume_id,
        "plaintext_bytes": plaintext_bytes,
        "units": normalized_rows,
    }
    return hashlib.sha256(canonical_json_bytes(payload)).hexdigest()


class PackUnitPayloadReader:
    """Split one complete unit payload into its declared source byte streams.

    The transport handler should pass one complete unit body. A unit is an archive-layout
    boundary; partial unit offsets are never committed as custody. Operational write
    segmentation is selected later by the storage adapter boundary.
    """

    def __init__(self, descriptor: PackUnitDescriptor, chunks: Iterable[bytes]) -> None:
        self._descriptor = descriptor
        self._source = iter(chunks)
        self._buffer = bytearray()
        self._buffer_offset = 0
        self._consumed = 0
        self._next_source = 0

    def iter_source(self, source: PackUnitSource) -> Iterator[bytes]:
        expected = self._descriptor.sources
        if self._next_source >= len(expected) or expected[self._next_source] != source:
            raise RuntimeError("pack unit sources must be read exactly once in descriptor order")
        remaining = source.bytes
        digest = hashlib.sha256()
        while remaining:
            available = len(self._buffer) - self._buffer_offset
            if available == 0:
                self._buffer.clear()
                self._buffer_offset = 0
                try:
                    self._buffer.extend(bytes(next(self._source)))
                except StopIteration as exc:
                    raise ValueError("pack unit payload ended before its declared length") from exc
                available = len(self._buffer)
                if available == 0:
                    continue
            take = min(remaining, available)
            end = self._buffer_offset + take
            chunk = bytes(self._buffer[self._buffer_offset : end])
            self._buffer_offset = end
            remaining -= take
            self._consumed += take
            digest.update(chunk)
            yield chunk
        if digest.hexdigest() != source.sha256:
            raise ValueError(f"pack unit source sha256 mismatch: {source.path}")
        self._next_source += 1

    def finish(self) -> None:
        if self._next_source != len(self._descriptor.sources):
            raise ValueError("pack unit payload sources were not completely consumed")
        if self._consumed != self._descriptor.payload_bytes:
            raise ValueError("pack unit payload was not completely consumed")
        if len(self._buffer) - self._buffer_offset:
            raise ValueError("pack unit payload is longer than declared")
        for chunk in self._source:
            if bytes(chunk):
                raise ValueError("pack unit payload is longer than declared")


def _normalized_unit_row(value: object, *, expected_unit: int) -> dict[str, object]:
    expected_fields = {
        "unit",
        "payload_bytes",
        "plaintext_start",
        "plaintext_bytes",
        "final",
        "sources",
    }
    if not isinstance(value, Mapping) or set(value) != expected_fields:
        raise ValueError("pack upload unit must be a canonical mapping")
    unit = _required_nonnegative_int(value, "unit")
    if unit != expected_unit:
        raise ValueError("pack upload unit order is invalid")
    payload_bytes = _required_nonnegative_int(value, "payload_bytes")
    plaintext_start = _required_nonnegative_int(value, "plaintext_start")
    plaintext_bytes = _required_nonnegative_int(value, "plaintext_bytes")
    final = value.get("final")
    if not isinstance(final, bool):
        raise ValueError("pack upload unit final must be boolean")
    sources = _source_rows(value.get("sources"))
    if sum(_required_nonnegative_int(source, "bytes") for source in sources) != payload_bytes:
        raise ValueError("pack upload unit payload bytes mismatch")
    return {
        "unit": unit,
        "payload_bytes": payload_bytes,
        "plaintext_start": plaintext_start,
        "plaintext_bytes": plaintext_bytes,
        "final": final,
        "sources": sources,
    }


def _source_rows(value: object) -> list[dict[str, object]]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        raise ValueError("pack upload unit sources must be a list")
    out: list[dict[str, object]] = []
    seen: set[str] = set()
    for current in value:
        if not isinstance(current, Mapping) or set(current) != {
            "path",
            "bytes",
            "sha256",
        }:
            raise ValueError("pack upload unit source must be a canonical mapping")
        path = normalize_relpath(str(current.get("path", "")))
        if path.startswith(RESERVED_ARCHIVE_PREFIX):
            raise ValueError(f"collection path uses reserved archive namespace: {path}")
        if path in seen:
            raise ValueError(f"pack upload unit repeats source path: {path}")
        byte_count = _required_nonnegative_int(current, "bytes")
        sha256 = str(current.get("sha256", ""))
        if _SHA256_RE.fullmatch(sha256) is None:
            raise ValueError("pack upload unit source sha256 is invalid")
        seen.add(path)
        out.append({"path": path, "bytes": byte_count, "sha256": sha256})
    return out


def _required_nonnegative_int(value: Mapping[str, object], key: str) -> int:
    raw = value.get(key)
    if isinstance(raw, bool):
        raise ValueError(f"{key} must be a non-negative integer")
    try:
        parsed = int(str(raw))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{key} must be a non-negative integer") from exc
    if parsed < 0 or str(parsed) != str(raw):
        raise ValueError(f"{key} must be a canonical non-negative integer")
    return parsed
