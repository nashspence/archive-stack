from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass

from riverhog_protocol.paths import normalize_relpath

PACK_UPLOAD_PLAN_SCHEMA = "pack-upload-plan/v1"
PACK_UNIT_PAYLOAD_MEDIA_TYPE = "application/vnd.riverhog.pack-unit"
RESERVED_ARCHIVE_PREFIX = ".riverhog/"
_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_PACK_VOLUME_ID_RE = re.compile(r"pack-[0-9]{12}")


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


def pack_upload_plan_payload(
    *,
    volume_id: str,
    plaintext_bytes: int,
    units: Sequence[PackUnitDescriptor],
) -> dict[str, object]:
    if _PACK_VOLUME_ID_RE.fullmatch(volume_id) is None:
        raise ValueError("pack volume id is invalid")
    if plaintext_bytes <= 0:
        raise ValueError("pack upload plaintext bytes must be positive")
    if not units:
        raise ValueError("pack upload plan requires at least one unit")
    _validate_unit_ranges(units, plaintext_bytes=plaintext_bytes)
    expected_digest = pack_upload_plan_sha256(
        volume_id=volume_id, plaintext_bytes=plaintext_bytes, units=units
    )
    if any(
        current.volume_id != volume_id or current.plan_sha256 != expected_digest
        for current in units
    ):
        raise ValueError("pack upload unit identity does not match the plan")
    unit_rows = [
        {
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
        for current in units
    ]
    return {
        "schema": PACK_UPLOAD_PLAN_SCHEMA,
        "volume_id": volume_id,
        "plaintext_bytes": plaintext_bytes,
        "units": unit_rows,
    }


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
    if not units or len(units) > 10_000:
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


def parse_pack_upload_plan(content: bytes | str) -> tuple[str, int, tuple[PackUnitDescriptor, ...]]:
    if isinstance(content, bytes):
        content = content.decode("utf-8")
    try:
        payload = json.loads(content)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("pack upload plan is not valid JSON") from exc
    expected_fields = {"schema", "volume_id", "plaintext_bytes", "units"}
    if (
        not isinstance(payload, dict)
        or payload.get("schema") != PACK_UPLOAD_PLAN_SCHEMA
        or set(payload) != expected_fields
    ):
        raise ValueError("pack upload plan schema mismatch")
    volume_id = str(payload.get("volume_id", ""))
    if _PACK_VOLUME_ID_RE.fullmatch(volume_id) is None:
        raise ValueError("pack upload plan volume id is invalid")
    plaintext_bytes = _required_nonnegative_int(payload, "plaintext_bytes")
    if plaintext_bytes <= 0:
        raise ValueError("pack upload plan plaintext bytes must be positive")
    raw_units = payload.get("units")
    if not isinstance(raw_units, list) or not raw_units:
        raise ValueError("pack upload plan units must be a non-empty list")
    normalized_rows = [
        _normalized_unit_row(current, expected_unit=index)
        for index, current in enumerate(raw_units)
    ]
    plan_sha256 = hashlib.sha256(
        canonical_json_bytes(
            {
                "schema": PACK_UPLOAD_PLAN_SCHEMA,
                "volume_id": volume_id,
                "plaintext_bytes": plaintext_bytes,
                "units": normalized_rows,
            }
        )
    ).hexdigest()
    units = tuple(
        PackUnitDescriptor(
            volume_id=volume_id,
            unit=_required_nonnegative_int(row, "unit"),
            payload_bytes=_required_nonnegative_int(row, "payload_bytes"),
            plaintext_start=_required_nonnegative_int(row, "plaintext_start"),
            plaintext_bytes=_required_nonnegative_int(row, "plaintext_bytes"),
            final=bool(row["final"]),
            sources=tuple(
                PackUnitSource(
                    path=str(source["path"]),
                    bytes=_required_nonnegative_int(source, "bytes"),
                    sha256=str(source["sha256"]),
                )
                for source in _source_rows(row["sources"])
            ),
            plan_sha256=plan_sha256,
        )
        for row in normalized_rows
    )
    _validate_unit_ranges(units, plaintext_bytes=plaintext_bytes)
    return volume_id, plaintext_bytes, units


def iter_pack_unit_payload(
    descriptor: PackUnitDescriptor,
    read_source_chunks: Callable[[str], Iterable[bytes]],
) -> Iterator[bytes]:
    """Yield the wire payload: source file bytes concatenated in descriptor order."""

    for source in descriptor.sources:
        byte_count = 0
        digest = hashlib.sha256()
        chunks = read_source_chunks(source.path)
        if not isinstance(chunks, Iterable):
            raise TypeError("pack source reader must return an iterable of bytes")
        for chunk in chunks:
            data = bytes(chunk)
            if not data:
                continue
            byte_count += len(data)
            digest.update(data)
            if byte_count > source.bytes:
                raise ValueError(f"pack unit source is longer than declared: {source.path}")
            yield data
        if byte_count != source.bytes:
            raise ValueError(f"pack unit source byte count mismatch: {source.path}")
        if digest.hexdigest() != source.sha256:
            raise ValueError(f"pack unit source sha256 mismatch: {source.path}")


class PackUnitPayloadReader:
    """Split one complete unit payload into its declared source byte streams.

    The transport handler should pass one complete unit body. A unit is the durability
    boundary and maps to exactly one multipart part, so partial unit offsets are never
    committed as custody.
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


def _validate_unit_ranges(
    units: Sequence[PackUnitDescriptor],
    *,
    plaintext_bytes: int,
) -> None:
    if not units or len(units) > 10_000:
        raise ValueError("pack upload plan unit count is invalid")
    expected_start = 0
    source_paths: set[str] = set()
    for index, current in enumerate(units):
        if current.plaintext_bytes <= 0:
            raise ValueError("pack upload unit plaintext bytes must be positive")
        if current.unit != index or current.plaintext_start != expected_start:
            raise ValueError("pack upload unit plaintext ranges are not contiguous")
        if current.final != (index == len(units) - 1):
            raise ValueError("only the final pack upload unit may be marked final")
        for source in current.sources:
            if source.path in source_paths:
                raise ValueError(f"pack upload plan repeats source path: {source.path}")
            source_paths.add(source.path)
        expected_start = current.plaintext_end
    if expected_start != plaintext_bytes:
        raise ValueError("pack upload units do not cover the pack plaintext")
