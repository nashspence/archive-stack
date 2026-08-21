from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Iterable, Iterator, Sequence
from dataclasses import replace

from riverhog_age import CHUNK_SIZE, PORTABLE_MULTIPART_MIN_PART_BYTES
from riverhog_protocol.pack_ingress import (
    PackUnitDescriptor,
    PackUnitPayloadReader,
    PackUnitSource,
    canonical_json_bytes,
    pack_upload_plan_sha256,
)
from riverhog_protocol.paths import normalize_relpath

from riverhog_core.domain.archive import (
    ArchiveFile,
    PackMemberPlan,
    PackPaddingPlan,
    PackUploadUnitPlan,
    PackVolumePlan,
)

PACK_INDEX_SCHEMA = "riverhog-pack-index/v1"
PACK_VOLUME_PLAN_SCHEMA = "pack-volume-plan/v1"
PACK_INDEX_PATH = ".riverhog/pack-index.json"
PACK_PADDING_PREFIX = ".riverhog/padding/"
RESERVED_ARCHIVE_PREFIX = ".riverhog/"
DEFAULT_PACK_SOURCE_BYTES = 32 * 1024 * 1024
DEFAULT_PACK_FILES = 50_000
DEFAULT_PACK_MEMBER_BYTES = 16 * 1024 * 1024
DEFAULT_PART_PLAINTEXT_BYTES = 64 * 1024 * 1024
_TAR_BLOCK_SIZE = 512
_TAR_USTAR_SIZE_MAX = int("7" * 11, 8)
_SHA256_RE = re.compile(r"[0-9a-f]{64}")


def plan_pack_volumes(
    files: Sequence[ArchiveFile],
    *,
    source_bytes_per_volume: int = DEFAULT_PACK_SOURCE_BYTES,
    files_per_volume: int = DEFAULT_PACK_FILES,
    max_member_bytes: int = DEFAULT_PACK_MEMBER_BYTES,
    part_plaintext_bytes: int = DEFAULT_PART_PLAINTEXT_BYTES,
) -> tuple[PackVolumePlan, ...]:
    normalized = _normalized_files(files, max_member_bytes=max_member_bytes)
    if source_bytes_per_volume <= 0:
        raise ValueError("pack source byte target must be positive")
    if files_per_volume <= 0:
        raise ValueError("pack file limit must be positive")

    volumes: list[PackVolumePlan] = []
    pending: list[ArchiveFile] = []
    pending_bytes = 0
    for current in normalized:
        if pending and (
            len(pending) >= files_per_volume
            or pending_bytes + current.bytes > source_bytes_per_volume
        ):
            volumes.append(
                plan_pack_volume(
                    pending,
                    sequence=len(volumes),
                    max_member_bytes=max_member_bytes,
                    part_plaintext_bytes=part_plaintext_bytes,
                )
            )
            pending = []
            pending_bytes = 0
        pending.append(current)
        pending_bytes += current.bytes
    if pending:
        volumes.append(
            plan_pack_volume(
                pending,
                sequence=len(volumes),
                max_member_bytes=max_member_bytes,
                part_plaintext_bytes=part_plaintext_bytes,
            )
        )
    return tuple(volumes)


def plan_pack_volume(
    files: Sequence[ArchiveFile],
    *,
    sequence: int,
    max_member_bytes: int = DEFAULT_PACK_MEMBER_BYTES,
    part_plaintext_bytes: int = DEFAULT_PART_PLAINTEXT_BYTES,
) -> PackVolumePlan:
    """Plan the canonical sealed v1 pack layout for finalized members."""

    normalized = _normalized_files(files, max_member_bytes=max_member_bytes)
    if sequence < 0:
        raise ValueError("pack sequence must be non-negative")
    if part_plaintext_bytes < PORTABLE_MULTIPART_MIN_PART_BYTES:
        raise ValueError("pack multipart plaintext target is below the portable minimum")
    if part_plaintext_bytes % CHUNK_SIZE:
        raise ValueError("pack multipart plaintext target must align to the age chunk size")

    volume_id = f"pack-{sequence:012d}"
    members: list[PackMemberPlan] = []
    units: list[PackUploadUnitPlan] = []
    unit_sources: list[ArchiveFile] = []
    unit_start = 0
    cursor = 0
    unit_index = 0

    for file_index, current in enumerate(normalized):
        header = _tar_header(current.path, current.bytes)
        header_offset = cursor
        data_offset = header_offset + len(header)
        end_offset = data_offset + current.bytes + _tar_padding(current.bytes)
        members.append(
            PackMemberPlan(
                path=current.path,
                bytes=current.bytes,
                sha256=current.sha256,
                unit=unit_index,
                header_offset=header_offset,
                data_offset=data_offset,
                end_offset=end_offset,
            )
        )
        cursor = end_offset
        unit_sources.append(current)

        has_more_files = file_index < len(normalized) - 1
        if has_more_files and cursor - unit_start >= part_plaintext_bytes:
            padding, cursor = _alignment_padding(
                volume_id=volume_id,
                unit=unit_index,
                cursor=cursor,
            )
            units.append(
                PackUploadUnitPlan(
                    unit=unit_index,
                    plaintext_start=unit_start,
                    plaintext_end=cursor,
                    sources=tuple(unit_sources),
                    padding=padding,
                )
            )
            if units[-1].plaintext_bytes < PORTABLE_MULTIPART_MIN_PART_BYTES:
                raise RuntimeError("planned a non-final multipart unit below the portable minimum")
            unit_index += 1
            unit_start = cursor
            unit_sources = []

    provisional_members = tuple(members)
    index_bytes = _pack_index_bytes(
        volume_id=volume_id,
        sequence=sequence,
        members=provisional_members,
    )
    index_sha256 = hashlib.sha256(index_bytes).hexdigest()
    cursor += len(_tar_header(PACK_INDEX_PATH, len(index_bytes)))
    cursor += len(index_bytes) + _tar_padding(len(index_bytes))
    cursor += _TAR_BLOCK_SIZE * 2
    units.append(
        PackUploadUnitPlan(
            unit=unit_index,
            plaintext_start=unit_start,
            plaintext_end=cursor,
            sources=tuple(unit_sources),
            includes_index=True,
            includes_end_markers=True,
        )
    )

    plan_sha256 = pack_upload_plan_sha256(
        volume_id=volume_id,
        plaintext_bytes=cursor,
        units=[_unit_row(current) for current in units],
    )
    return PackVolumePlan(
        volume_id=volume_id,
        sequence=sequence,
        max_member_bytes=max_member_bytes,
        part_plaintext_bytes=part_plaintext_bytes,
        members=provisional_members,
        units=tuple(units),
        index_bytes=index_bytes,
        index_sha256=index_sha256,
        plaintext_bytes=cursor,
        plan_sha256=plan_sha256,
    )


def pack_volume_plan_payload(plan: PackVolumePlan) -> dict[str, object]:
    """Return the persisted canonical recipe for a completed v1 pack layout.

    The recipe rebuilds and verifies final identity; it does not make plan-first
    assembly or multipart transfer an archive-format requirement.
    """

    return {
        "schema": PACK_VOLUME_PLAN_SCHEMA,
        "volume_id": plan.volume_id,
        "sequence": plan.sequence,
        "max_member_bytes": plan.max_member_bytes,
        "part_plaintext_bytes": plan.part_plaintext_bytes,
        "plaintext_bytes": plan.plaintext_bytes,
        "index_sha256": plan.index_sha256,
        "plan_sha256": plan.plan_sha256,
        "files": [
            {"path": current.path, "bytes": current.bytes, "sha256": current.sha256}
            for current in plan.members
        ],
    }


def pack_volume_plan_bytes(plan: PackVolumePlan) -> bytes:
    return canonical_json_bytes(pack_volume_plan_payload(plan))


def parse_pack_volume_plan(content: bytes | str) -> PackVolumePlan:
    """Rebuild and verify a persisted v1 plan rather than trusting serialized offsets."""

    if isinstance(content, bytes):
        content = content.decode("utf-8")
    try:
        payload = json.loads(content)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("pack volume plan is not valid JSON") from exc
    if not isinstance(payload, dict) or payload.get("schema") != PACK_VOLUME_PLAN_SCHEMA:
        raise ValueError("pack volume plan schema mismatch")
    expected_keys = {
        "schema",
        "volume_id",
        "sequence",
        "max_member_bytes",
        "part_plaintext_bytes",
        "plaintext_bytes",
        "index_sha256",
        "plan_sha256",
        "files",
    }
    if set(payload) != expected_keys:
        raise ValueError("pack volume plan fields are invalid")
    sequence = _canonical_nonnegative_int(payload.get("sequence"), label="pack sequence")
    volume_id = str(payload.get("volume_id", ""))
    if volume_id != f"pack-{sequence:012d}":
        raise ValueError("pack volume plan identity is invalid")
    max_member_bytes = _canonical_positive_int(
        payload.get("max_member_bytes"), label="pack member byte limit"
    )
    part_plaintext_bytes = _canonical_positive_int(
        payload.get("part_plaintext_bytes"), label="pack part plaintext bytes"
    )
    raw_files = payload.get("files")
    if not isinstance(raw_files, list) or not raw_files:
        raise ValueError("pack volume plan files must be a non-empty list")
    files: list[ArchiveFile] = []
    for raw in raw_files:
        if not isinstance(raw, dict) or set(raw) != {"path", "bytes", "sha256"}:
            raise ValueError("pack volume plan file is invalid")
        files.append(
            ArchiveFile(
                path=str(raw.get("path", "")),
                bytes=_canonical_nonnegative_int(raw.get("bytes"), label="file bytes"),
                sha256=str(raw.get("sha256", "")),
            )
        )
    rebuilt = plan_pack_volume(
        files,
        sequence=sequence,
        max_member_bytes=max_member_bytes,
        part_plaintext_bytes=part_plaintext_bytes,
    )
    if (
        rebuilt.plaintext_bytes
        != _canonical_positive_int(payload.get("plaintext_bytes"), label="pack plaintext bytes")
        or rebuilt.index_sha256 != str(payload.get("index_sha256", ""))
        or rebuilt.plan_sha256 != str(payload.get("plan_sha256", ""))
    ):
        raise ValueError("persisted pack volume plan does not reproduce its identity")
    return rebuilt


def pack_unit_descriptors(plan: PackVolumePlan) -> tuple[PackUnitDescriptor, ...]:
    return tuple(
        PackUnitDescriptor(
            volume_id=plan.volume_id,
            unit=current.unit,
            payload_bytes=current.payload_bytes,
            plaintext_start=current.plaintext_start,
            plaintext_bytes=current.plaintext_bytes,
            final=current.final,
            sources=tuple(
                PackUnitSource(path=source.path, bytes=source.bytes, sha256=source.sha256)
                for source in current.sources
            ),
            plan_sha256=plan.plan_sha256,
        )
        for current in plan.units
    )


def iter_render_pack_upload_unit(
    plan: PackVolumePlan,
    unit_number: int,
    read_source_chunks: Callable[[str], Iterable[bytes]],
) -> Iterator[bytes]:
    """Yield one deterministic tar unit while validating every source stream."""

    unit = _require_unit(plan, unit_number)
    members = {current.path: current for current in plan.members}
    absolute_cursor = unit.plaintext_start
    emitted = 0

    def emit(data: bytes) -> Iterator[bytes]:
        nonlocal emitted
        if data:
            emitted += len(data)
            yield data

    for source in unit.sources:
        member = members[source.path]
        if member.unit != unit.unit or member.header_offset != absolute_cursor:
            raise RuntimeError("pack member offsets do not match their upload unit")
        header = _tar_header(source.path, source.bytes)
        yield from emit(header)
        absolute_cursor += len(header)
        if absolute_cursor != member.data_offset:
            raise RuntimeError("pack member data offset is inconsistent")
        byte_count = 0
        digest = hashlib.sha256()
        for chunk in read_source_chunks(source.path):
            data = bytes(chunk)
            if not data:
                continue
            byte_count += len(data)
            if byte_count > source.bytes:
                raise ValueError(f"pack source is longer than declared: {source.path}")
            digest.update(data)
            yield from emit(data)
        if byte_count != source.bytes:
            raise ValueError(f"pack source byte count mismatch: {source.path}")
        if digest.hexdigest() != source.sha256:
            raise ValueError(f"pack source sha256 mismatch: {source.path}")
        absolute_cursor += source.bytes
        padding = _tar_padding(source.bytes)
        if padding:
            yield from emit(b"\0" * padding)
            absolute_cursor += padding
        if absolute_cursor != member.end_offset:
            raise RuntimeError("pack member end offset is inconsistent")

    if unit.padding is not None:
        padding_plan = unit.padding
        if padding_plan.header_offset != absolute_cursor:
            raise RuntimeError("pack padding offset is inconsistent")
        header = _tar_header(padding_plan.path, padding_plan.payload_bytes)
        yield from emit(header)
        yield from emit(b"\0" * padding_plan.payload_bytes)
        payload_padding = _tar_padding(padding_plan.payload_bytes)
        if payload_padding:
            yield from emit(b"\0" * payload_padding)
        absolute_cursor += len(header) + padding_plan.payload_bytes + payload_padding
        if absolute_cursor != padding_plan.end_offset:
            raise RuntimeError("pack padding end offset is inconsistent")

    if unit.includes_index:
        header = _tar_header(PACK_INDEX_PATH, len(plan.index_bytes))
        yield from emit(header)
        yield from emit(plan.index_bytes)
        index_padding = _tar_padding(len(plan.index_bytes))
        if index_padding:
            yield from emit(b"\0" * index_padding)
        absolute_cursor += len(header) + len(plan.index_bytes) + index_padding
    if unit.includes_end_markers:
        yield from emit(b"\0" * (_TAR_BLOCK_SIZE * 2))
        absolute_cursor += _TAR_BLOCK_SIZE * 2

    if absolute_cursor != unit.plaintext_end or emitted != unit.plaintext_bytes:
        raise RuntimeError("rendered pack upload unit length does not match its plan")


def iter_render_pack_upload_unit_payload(
    plan: PackVolumePlan,
    unit_number: int,
    payload_chunks: Iterable[bytes],
) -> Iterator[bytes]:
    descriptor = pack_unit_descriptors(plan)[unit_number]
    reader = PackUnitPayloadReader(descriptor, payload_chunks)
    source_by_path = {source.path: source for source in descriptor.sources}

    def read(path: str) -> Iterator[bytes]:
        return reader.iter_source(source_by_path[path])

    yield from iter_render_pack_upload_unit(plan, unit_number, read)
    reader.finish()


def _normalized_files(
    files: Sequence[ArchiveFile],
    *,
    max_member_bytes: int,
) -> tuple[ArchiveFile, ...]:
    if max_member_bytes <= 0:
        raise ValueError("pack member byte limit must be positive")
    out: list[ArchiveFile] = []
    seen: set[str] = set()
    for current in files:
        path = normalize_relpath(current.path)
        if path.startswith(RESERVED_ARCHIVE_PREFIX):
            raise ValueError(f"collection path uses reserved archive namespace: {path}")
        if path in seen:
            raise ValueError(f"duplicate collection archive path: {path}")
        if current.bytes < 0 or current.bytes >= max_member_bytes:
            raise ValueError(f"file is outside the pack member size policy: {path}")
        if _SHA256_RE.fullmatch(current.sha256) is None:
            raise ValueError(f"collection archive file sha256 is invalid: {path}")
        seen.add(path)
        out.append(replace(current, path=path))
    if not out:
        raise ValueError("pack volume requires at least one file")
    return tuple(sorted(out, key=lambda current: current.path))


def _pack_index_bytes(
    *,
    volume_id: str,
    sequence: int,
    members: Sequence[PackMemberPlan],
) -> bytes:
    tree_digest = hashlib.sha256()
    total_bytes = 0
    rows: list[dict[str, object]] = []
    for current in members:
        total_bytes += current.bytes
        tree_digest.update(f"{current.path}\t{current.bytes}\t{current.sha256}\n".encode())
        rows.append(
            {
                "path": current.path,
                "bytes": current.bytes,
                "sha256": current.sha256,
                "unit": current.unit,
                "header_offset": current.header_offset,
                "data_offset": current.data_offset,
            }
        )
    return canonical_json_bytes(
        {
            "schema": PACK_INDEX_SCHEMA,
            "volume": {"id": volume_id, "sequence": sequence},
            "tree": {
                "files": len(members),
                "bytes": total_bytes,
                "sha256": tree_digest.hexdigest(),
            },
            "files": rows,
        }
    )


def _alignment_padding(
    *,
    volume_id: str,
    unit: int,
    cursor: int,
) -> tuple[PackPaddingPlan | None, int]:
    remainder = cursor % CHUNK_SIZE
    if remainder == 0:
        return None, cursor
    contribution = CHUNK_SIZE - remainder
    if contribution < _TAR_BLOCK_SIZE or contribution % _TAR_BLOCK_SIZE:
        raise RuntimeError("tar alignment padding is not representable")
    payload_bytes = contribution - _TAR_BLOCK_SIZE
    path = f"{PACK_PADDING_PREFIX}{volume_id}-{unit:06d}"
    header_offset = cursor
    end_offset = cursor + len(_tar_header(path, payload_bytes)) + payload_bytes
    end_offset += _tar_padding(payload_bytes)
    if end_offset % CHUNK_SIZE:
        raise RuntimeError("pack padding did not align to an age chunk boundary")
    return (
        PackPaddingPlan(
            path=path,
            header_offset=header_offset,
            payload_bytes=payload_bytes,
            end_offset=end_offset,
        ),
        end_offset,
    )


def _unit_row(unit: PackUploadUnitPlan) -> dict[str, object]:
    return {
        "unit": unit.unit,
        "payload_bytes": unit.payload_bytes,
        "plaintext_start": unit.plaintext_start,
        "plaintext_bytes": unit.plaintext_bytes,
        "final": unit.final,
        "sources": [
            {"path": source.path, "bytes": source.bytes, "sha256": source.sha256}
            for source in unit.sources
        ],
    }


def _require_unit(plan: PackVolumePlan, unit_number: int) -> PackUploadUnitPlan:
    if unit_number < 0 or unit_number >= len(plan.units):
        raise ValueError("pack upload unit is outside the plan")
    unit = plan.units[unit_number]
    if unit.unit != unit_number:
        raise RuntimeError("pack upload unit ordering is invalid")
    return unit


def _canonical_nonnegative_int(value: object, *, label: str) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{label} must be a non-negative integer")
    try:
        parsed = int(str(value))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be a non-negative integer") from exc
    if parsed < 0 or str(parsed) != str(value):
        raise ValueError(f"{label} must be a canonical non-negative integer")
    return parsed


def _canonical_positive_int(value: object, *, label: str) -> int:
    parsed = _canonical_nonnegative_int(value, label=label)
    if parsed < 1:
        raise ValueError(f"{label} must be positive")
    return parsed


def _tar_header(path: str, size: int) -> bytes:
    pax_attrs, file_header_path, file_header_size = _tar_file_header_values(path, size)
    file_header = _tar_ustar_header(file_header_path, file_header_size, typeflag=b"0")
    if not pax_attrs:
        return file_header
    pax_payload = _pax_payload(pax_attrs)
    return (
        _tar_ustar_header(_pax_header_path(path), len(pax_payload), typeflag=b"x")
        + pax_payload
        + (b"\0" * _tar_padding(len(pax_payload)))
        + file_header
    )


def _tar_file_header_values(path: str, size: int) -> tuple[dict[str, str], str, int]:
    pax_attrs: dict[str, str] = {}
    try:
        _ustar_name(path)
        file_header_path = path
    except ValueError:
        pax_attrs["path"] = path
        file_header_path = _pax_placeholder_path(path)
    if size > _TAR_USTAR_SIZE_MAX:
        pax_attrs["size"] = str(size)
        file_header_size = 0
    else:
        file_header_size = size
    return pax_attrs, file_header_path, file_header_size


def _tar_ustar_header(path: str, size: int, *, typeflag: bytes) -> bytes:
    name, prefix = _ustar_name(path)
    header = bytearray(_TAR_BLOCK_SIZE)
    _write_tar_field(header, 0, 100, name)
    _write_tar_octal(header, 100, 8, 0o644)
    _write_tar_octal(header, 108, 8, 0)
    _write_tar_octal(header, 116, 8, 0)
    _write_tar_octal(header, 124, 12, size)
    _write_tar_octal(header, 136, 12, 0)
    header[148:156] = b"        "
    if len(typeflag) != 1:
        raise ValueError("tar header typeflag must be one byte")
    header[156:157] = typeflag
    _write_tar_field(header, 257, 6, b"ustar\0")
    _write_tar_field(header, 263, 2, b"00")
    _write_tar_field(header, 345, 155, prefix)
    _write_tar_octal(header, 148, 8, sum(header))
    return bytes(header)


def _ustar_name(path: str) -> tuple[bytes, bytes]:
    encoded = path.encode("utf-8")
    if len(encoded) <= 100:
        return encoded, b""
    parts = path.split("/")
    for index in range(1, len(parts)):
        prefix = "/".join(parts[:index]).encode("utf-8")
        name = "/".join(parts[index:]).encode("utf-8")
        if len(prefix) <= 155 and len(name) <= 100:
            return name, prefix
    raise ValueError(f"collection archive path is too long for ustar: {path}")


def _write_tar_field(header: bytearray, offset: int, length: int, value: bytes) -> None:
    if len(value) > length:
        raise ValueError("tar header field is too long")
    header[offset : offset + len(value)] = value


def _write_tar_octal(header: bytearray, offset: int, length: int, value: int) -> None:
    encoded = f"{value:0{length - 1}o}\0".encode("ascii")
    if len(encoded) > length:
        raise ValueError("tar header numeric field is too large")
    header[offset : offset + length] = encoded.rjust(length, b"0")


def _tar_padding(size: int) -> int:
    return (-size) % _TAR_BLOCK_SIZE


def _pax_header_path(path: str) -> str:
    digest = hashlib.sha256(path.encode("utf-8")).hexdigest()[:32]
    return f"PaxHeaders/{digest}"


def _pax_placeholder_path(path: str) -> str:
    digest = hashlib.sha256(path.encode("utf-8")).hexdigest()[:32]
    return f"riverhog-pax/{digest}"


def _pax_payload(attrs: dict[str, str]) -> bytes:
    return b"".join(_pax_record(key, value) for key, value in attrs.items())


def _pax_record(key: str, value: str) -> bytes:
    suffix = f" {key}={value}\n".encode()
    size = len(suffix) + len(str(len(suffix)))
    while True:
        next_size = len(suffix) + len(str(size))
        if next_size == size:
            return f"{size}".encode("ascii") + suffix
        size = next_size
