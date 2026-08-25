from __future__ import annotations

import json
import re
from collections.abc import Sequence

from riverhog_age import CHUNK_SIZE, AgeAlignedUnitPlan, ResumableAgeScryptSession
from riverhog_protocol.pack_ingress import RESERVED_ARCHIVE_PREFIX, canonical_json_bytes
from riverhog_protocol.paths import normalize_relpath

from riverhog_core.domain.archive import ArchiveFile, RawVolumePlan

RAW_VOLUME_PLAN_SCHEMA = "raw-volume-plan/v1"
DEFAULT_RAW_VOLUME_PLAINTEXT_BYTES = 16 * 1024 * 1024 * 1024
DEFAULT_RAW_PART_PLAINTEXT_BYTES = 64 * 1024 * 1024
_SHA256_RE = re.compile(r"[0-9a-f]{64}")


def plan_raw_volumes(
    files: Sequence[ArchiveFile],
    *,
    starting_sequence: int,
    max_plaintext_bytes: int = DEFAULT_RAW_VOLUME_PLAINTEXT_BYTES,
) -> tuple[RawVolumePlan, ...]:
    if starting_sequence < 0:
        raise ValueError("raw volume starting sequence must be non-negative")
    if max_plaintext_bytes <= 0:
        raise ValueError("raw volume plaintext limit must be positive")
    plans: list[RawVolumePlan] = []
    seen: set[str] = set()
    for current in sorted(files, key=lambda value: normalize_relpath(value.path)):
        path = normalize_relpath(current.path)
        if path.startswith(RESERVED_ARCHIVE_PREFIX):
            raise ValueError(f"collection path uses reserved archive namespace: {path}")
        if path in seen:
            raise ValueError(f"duplicate collection archive path: {path}")
        if current.bytes < 0 or _SHA256_RE.fullmatch(current.sha256) is None:
            raise ValueError(f"collection archive file identity is invalid: {path}")
        seen.add(path)
        offset = 0
        if current.bytes == 0:
            plans.append(
                RawVolumePlan(
                    volume_id=f"segment-{starting_sequence + len(plans):012d}",
                    sequence=starting_sequence + len(plans),
                    source_path=path,
                    file_offset=0,
                    plaintext_bytes=0,
                    file_bytes=0,
                    file_sha256=current.sha256,
                )
            )
            continue
        while offset < current.bytes:
            length = min(max_plaintext_bytes, current.bytes - offset)
            sequence = starting_sequence + len(plans)
            plans.append(
                RawVolumePlan(
                    volume_id=f"segment-{sequence:012d}",
                    sequence=sequence,
                    source_path=path,
                    file_offset=offset,
                    plaintext_bytes=length,
                    file_bytes=current.bytes,
                    file_sha256=current.sha256,
                )
            )
            offset += length
    return tuple(plans)


def raw_volume_plan_payload(plan: RawVolumePlan) -> dict[str, object]:
    return {
        "schema": RAW_VOLUME_PLAN_SCHEMA,
        "volume_id": plan.volume_id,
        "sequence": plan.sequence,
        "source_path": plan.source_path,
        "file_offset": plan.file_offset,
        "plaintext_bytes": plan.plaintext_bytes,
        "file_bytes": plan.file_bytes,
        "file_sha256": plan.file_sha256,
    }


def raw_volume_plan_bytes(plan: RawVolumePlan) -> bytes:
    return canonical_json_bytes(raw_volume_plan_payload(plan))


def parse_raw_volume_plan(content: bytes | str) -> RawVolumePlan:
    if isinstance(content, bytes):
        content = content.decode("utf-8")
    try:
        payload = json.loads(content)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("raw volume plan is not valid JSON") from exc
    if not isinstance(payload, dict) or payload.get("schema") != RAW_VOLUME_PLAN_SCHEMA:
        raise ValueError("raw volume plan schema mismatch")
    expected = {
        "schema",
        "volume_id",
        "sequence",
        "source_path",
        "file_offset",
        "plaintext_bytes",
        "file_bytes",
        "file_sha256",
    }
    if set(payload) != expected:
        raise ValueError("raw volume plan fields are invalid")
    sequence = _canonical_nonnegative_int(payload.get("sequence"), label="raw sequence")
    volume_id = str(payload.get("volume_id", ""))
    if volume_id != f"segment-{sequence:012d}":
        raise ValueError("raw volume plan identity is invalid")
    source_path = normalize_relpath(str(payload.get("source_path", "")))
    if source_path.startswith(RESERVED_ARCHIVE_PREFIX):
        raise ValueError("raw volume source uses the reserved archive namespace")
    file_offset = _canonical_nonnegative_int(payload.get("file_offset"), label="file offset")
    plaintext_bytes = _canonical_nonnegative_int(
        payload.get("plaintext_bytes"), label="plaintext bytes"
    )
    file_bytes = _canonical_nonnegative_int(payload.get("file_bytes"), label="file bytes")
    file_sha256 = str(payload.get("file_sha256", ""))
    if file_offset + plaintext_bytes > file_bytes or _SHA256_RE.fullmatch(file_sha256) is None:
        raise ValueError("raw volume file identity is invalid")
    return RawVolumePlan(
        volume_id=volume_id,
        sequence=sequence,
        source_path=source_path,
        file_offset=file_offset,
        plaintext_bytes=plaintext_bytes,
        file_bytes=file_bytes,
        file_sha256=file_sha256,
    )


def raw_age_aligned_unit_plans(
    plan: RawVolumePlan,
    session: ResumableAgeScryptSession,
    *,
    target_plaintext_bytes: int = DEFAULT_RAW_PART_PLAINTEXT_BYTES,
) -> tuple[AgeAlignedUnitPlan, ...]:
    if target_plaintext_bytes <= 0 or target_plaintext_bytes % CHUNK_SIZE:
        raise ValueError("raw part target must be a positive age-chunk multiple")
    chunks_per_unit = max(1, target_plaintext_bytes // CHUNK_SIZE)
    return tuple(
        session.age_aligned_unit_plans(
            plan.plaintext_bytes,
            chunks_per_unit=chunks_per_unit,
        )
    )


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
