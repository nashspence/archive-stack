from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from typing import TypedDict

from riverhog_age import UploadState
from riverhog_archive_contracts import (
    ARCHIVE_ENCRYPTION_FORMAT,
    COLLECTION_ARCHIVE_MANIFEST_SCHEMA,
    PACK_INDEX_SCHEMA,
    SELECTIVE_READ_FORMAT,
    CollectionArchiveManifest,
)
from riverhog_protocol.pack_ingress import RESERVED_ARCHIVE_PREFIX, canonical_json_bytes
from riverhog_protocol.paths import normalize_relpath

from riverhog_core.domain.archive import (
    ArchiveFile,
    PackVolumePlan,
    RawVolumePlan,
    SealedPackVolume,
    SealedProvenanceObject,
    SealedRawVolume,
    StoredPartReceipt,
    VerifiedRawFile,
)
from riverhog_core.raw_verification import raw_file_volume_set_sha256

_SHA256_RE = re.compile(r"[0-9a-f]{64}")


class CollectionTreeIdentity(TypedDict):
    files: int
    bytes: int
    sha256: str


def validate_collection_archive_plan(
    *,
    files: Sequence[ArchiveFile],
    packs: Sequence[PackVolumePlan],
    raw_volumes: Sequence[RawVolumePlan | SealedRawVolume] = (),
) -> tuple[ArchiveFile, ...]:
    """Require volume plans to cover the exact immutable collection tree once."""

    normalized_files = _normalized_files(files)
    expected_by_path = {current.path: current for current in normalized_files}
    coverage: dict[str, list[tuple[int, int, str]]] = {
        current.path: [] for current in normalized_files
    }
    plans = sorted((*packs, *raw_volumes), key=lambda current: current.sequence)
    if [current.sequence for current in plans] != list(range(len(plans))):
        raise ValueError("archive volume plan sequence must be canonical and contiguous")
    if len({current.volume_id for current in plans}) != len(plans):
        raise ValueError("archive volume plan ids must be unique")

    for pack_plan in packs:
        for member in pack_plan.members:
            expected = expected_by_path.get(member.path)
            if (
                expected is None
                or expected.bytes != member.bytes
                or expected.sha256 != member.sha256
            ):
                raise ValueError(
                    f"pack plan does not match collection file identity: {member.path}"
                )
            coverage[member.path].append((0, member.bytes, pack_plan.volume_id))

    for raw_plan in raw_volumes:
        source_path = normalize_relpath(raw_plan.source_path)
        expected = expected_by_path.get(source_path)
        if expected is None:
            raise ValueError(f"raw volume references an unknown collection path: {source_path}")
        if raw_plan.file_offset < 0 or raw_plan.plaintext_bytes < 0:
            raise ValueError("raw volume placement is invalid")
        if raw_plan.file_bytes != expected.bytes or raw_plan.file_sha256 != expected.sha256:
            raise ValueError(f"raw volume file identity mismatch: {source_path}")
        if raw_plan.file_offset + raw_plan.plaintext_bytes > expected.bytes:
            raise ValueError(f"raw volume exceeds its collection file: {source_path}")
        coverage[source_path].append(
            (raw_plan.file_offset, raw_plan.plaintext_bytes, raw_plan.volume_id)
        )

    _validate_file_coverage(normalized_files, coverage)
    return normalized_files


def build_collection_archive_manifest(
    *,
    files: Sequence[ArchiveFile],
    packs: Sequence[tuple[PackVolumePlan, SealedPackVolume]],
    raw_volumes: Sequence[SealedRawVolume] = (),
    verified_raw_files: Sequence[VerifiedRawFile] = (),
    provenance_identity: str | None = None,
    provenance_objects: Sequence[SealedProvenanceObject] = (),
) -> bytes:
    normalized_files = validate_collection_archive_plan(
        files=files,
        packs=tuple(plan for plan, _receipt in packs),
        raw_volumes=raw_volumes,
    )
    expected_by_path = {current.path: current for current in normalized_files}
    volume_rows: list[dict[str, object]] = []

    for plan, pack_receipt in packs:
        _validate_pack_receipt(plan, pack_receipt)
        volume_rows.append(_pack_volume_row(plan, pack_receipt))

    verified_by_path = _verified_raw_files(verified_raw_files)
    raw_by_path: dict[str, list[SealedRawVolume]] = {}
    for current in raw_volumes:
        raw_by_path.setdefault(normalize_relpath(current.source_path), []).append(current)
    if set(raw_by_path) != set(verified_by_path):
        raise ValueError("every raw file must be verified exactly once before root publication")
    for path, verified in verified_by_path.items():
        expected = expected_by_path.get(path)
        if (
            expected is None
            or verified.bytes != expected.bytes
            or verified.sha256 != expected.sha256
            or verified.volume_set_sha256
            != raw_file_volume_set_sha256(file=expected, volumes=raw_by_path[path])
        ):
            raise ValueError(f"raw file verification does not match sealed volumes: {path}")

    for raw_receipt in raw_volumes:
        source_path = normalize_relpath(raw_receipt.source_path)
        expected = expected_by_path.get(source_path)
        if expected is None:
            raise ValueError(f"raw volume references an unknown collection path: {source_path}")
        if raw_receipt.file_offset < 0 or raw_receipt.plaintext_bytes < 0:
            raise ValueError("raw volume placement is invalid")
        if raw_receipt.file_bytes != expected.bytes or raw_receipt.file_sha256 != expected.sha256:
            raise ValueError(f"raw volume file identity mismatch: {source_path}")
        if raw_receipt.file_offset + raw_receipt.plaintext_bytes > expected.bytes:
            raise ValueError(f"raw volume exceeds its collection file: {source_path}")
        _validate_part_receipts(
            raw_receipt.parts,
            plaintext_bytes=raw_receipt.plaintext_bytes,
        )
        volume_rows.append(_raw_volume_row(raw_receipt))

    volume_rows.sort(key=lambda row: _stored_int(row["sequence"], "volume sequence"))
    if [_stored_int(row["sequence"], "volume sequence") for row in volume_rows] != list(
        range(len(volume_rows))
    ):
        raise ValueError("archive volume sequence must be canonical and contiguous")
    if len({str(row["id"]) for row in volume_rows}) != len(volume_rows):
        raise ValueError("archive volume ids must be unique")
    if len({str(row["path"]) for row in volume_rows}) != len(volume_rows):
        raise ValueError("archive volume paths must be unique")

    tree = collection_tree_identity(normalized_files)
    payload = {
        "schema": COLLECTION_ARCHIVE_MANIFEST_SCHEMA,
        "format": {
            "encryption": ARCHIVE_ENCRYPTION_FORMAT,
            "pack_index": PACK_INDEX_SCHEMA,
            "part_digest": "sha256",
            "selective_read": SELECTIVE_READ_FORMAT,
        },
        "tree": tree,
        "volumes": volume_rows,
    }
    if provenance_identity is not None:
        if _SHA256_RE.fullmatch(provenance_identity) is None:
            raise ValueError("archive provenance identity is invalid")
        if not provenance_objects:
            raise ValueError("archive provenance objects are required")
        index = [item for item in provenance_objects if item.kind == "provenance-index"]
        bundles = [item for item in provenance_objects if item.kind == "provenance-bundle"]
        if len(index) != 1 or not bundles:
            raise ValueError("archive provenance requires one index and at least one bundle")
        payload["provenance"] = {
            "identity": provenance_identity,
            "index": _provenance_object_row(index[0]),
            "bundles": [_provenance_object_row(item) for item in bundles],
        }
    return CollectionArchiveManifest.from_mapping(payload).to_json_bytes()


def _provenance_object_row(item: SealedProvenanceObject) -> dict[str, object]:
    return {
        "id": item.object_id,
        "kind": item.kind,
        "path": normalize_relpath(item.relative_path),
        "plaintext_bytes": item.plaintext_bytes,
        "sha256": item.plaintext_sha256,
        "stored_bytes": item.stored_bytes,
        "stored_sha256": item.stored_sha256,
    }


def collection_tree_identity(files: Sequence[ArchiveFile]) -> CollectionTreeIdentity:
    normalized = _normalized_files(files)
    digest = hashlib.sha256()
    byte_count = 0
    for current in normalized:
        digest.update(f"{current.path}\t{current.bytes}\t{current.sha256}\n".encode())
        byte_count += current.bytes
    return {"files": len(normalized), "bytes": byte_count, "sha256": digest.hexdigest()}


def _pack_volume_row(plan: PackVolumePlan, receipt: SealedPackVolume) -> dict[str, object]:
    expected_path = f"volumes/{receipt.volume_id}.tar.age"
    if normalize_relpath(receipt.relative_path) != expected_path:
        raise ValueError("sealed pack receipt path is not canonical")
    return {
        "id": receipt.volume_id,
        "sequence": receipt.sequence,
        "kind": "pack",
        "path": expected_path,
        "files": receipt.files,
        "source_bytes": receipt.source_bytes,
        "plaintext_bytes": receipt.plaintext_bytes,
        "age_state": _age_state_row(
            receipt.age_state_json, plaintext_bytes=receipt.plaintext_bytes
        ),
        "index_sha256": receipt.index_sha256,
        "plan_sha256": receipt.plan_sha256,
        "parts": [_part_row(current) for current in receipt.parts],
    }


def _raw_volume_row(receipt: SealedRawVolume) -> dict[str, object]:
    source_path = normalize_relpath(receipt.source_path)
    if source_path.startswith(RESERVED_ARCHIVE_PREFIX):
        raise ValueError("raw volume source uses the reserved archive namespace")
    expected_path = f"volumes/{receipt.volume_id}.bin.age"
    if normalize_relpath(receipt.relative_path) != expected_path:
        raise ValueError("sealed raw receipt path is not canonical")
    if receipt.volume_id != f"segment-{receipt.sequence:012d}":
        raise ValueError("sealed raw receipt identity is not canonical")
    return {
        "id": receipt.volume_id,
        "sequence": receipt.sequence,
        "kind": "segment",
        "path": expected_path,
        "plaintext_bytes": receipt.plaintext_bytes,
        "age_state": _age_state_row(
            receipt.age_state_json, plaintext_bytes=receipt.plaintext_bytes
        ),
        "file": {
            "path": source_path,
            "offset": receipt.file_offset,
            "bytes": receipt.plaintext_bytes,
            "file_bytes": receipt.file_bytes,
            "sha256": receipt.file_sha256,
        },
        "parts": [_part_row(current) for current in receipt.parts],
    }


def _age_state_row(age_state_json: str, *, plaintext_bytes: int) -> dict[str, object]:
    try:
        value = json.loads(age_state_json)
    except json.JSONDecodeError as exc:
        raise ValueError("sealed archive volume age state is not valid JSON") from exc
    return _normalized_age_state(value, plaintext_bytes=plaintext_bytes)


def _normalized_age_state(
    value: object,
    *,
    plaintext_bytes: int,
) -> dict[str, object]:
    expected = {"format", "header_b64", "payload_nonce_b64", "plaintext_size"}
    if not isinstance(value, Mapping) or set(value) != expected:
        raise ValueError("collection archive volume age state is invalid")
    canonical = canonical_json_bytes(dict(value)).decode("utf-8")
    try:
        state = UploadState.from_json_bytes(canonical)
    except (TypeError, ValueError) as exc:
        raise ValueError("collection archive volume age state is invalid") from exc
    if state.plaintext_size != plaintext_bytes:
        raise ValueError("collection archive volume age state size mismatch")
    normalized = json.loads(state.to_json_bytes())
    if not isinstance(normalized, dict):
        raise ValueError("collection archive volume age state is invalid")
    if canonical_json_bytes(normalized) != canonical_json_bytes(dict(value)):
        raise ValueError("collection archive volume age state is not canonical")
    return dict(normalized)


def _stored_int(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{label} must be a non-negative integer")
    return value


def _part_row(current: StoredPartReceipt) -> dict[str, object]:
    return {
        "number": current.number,
        "plaintext_start": current.plaintext_start,
        "plaintext_bytes": current.plaintext_bytes,
        "plaintext_sha256": current.plaintext_sha256,
        "stored_bytes": current.stored_bytes,
        "stored_sha256": current.stored_sha256,
    }


def _validate_pack_receipt(plan: PackVolumePlan, receipt: SealedPackVolume) -> None:
    if (
        receipt.volume_id != plan.volume_id
        or receipt.sequence != plan.sequence
        or receipt.volume_id != f"pack-{receipt.sequence:012d}"
    ):
        raise ValueError("sealed pack receipt does not match its plan")
    if receipt.files != len(plan.members):
        raise ValueError("sealed pack receipt file count mismatch")
    if receipt.source_bytes != sum(current.bytes for current in plan.members):
        raise ValueError("sealed pack receipt source byte count mismatch")
    if receipt.plaintext_bytes != plan.plaintext_bytes:
        raise ValueError("sealed pack receipt plaintext byte count mismatch")
    if receipt.index_sha256 != plan.index_sha256 or receipt.plan_sha256 != plan.plan_sha256:
        raise ValueError("sealed pack receipt identity mismatch")
    _validate_part_receipts(receipt.parts, plaintext_bytes=plan.plaintext_bytes)


def _validate_part_receipts(
    parts: Sequence[StoredPartReceipt],
    *,
    plaintext_bytes: int,
) -> None:
    if not parts:
        raise ValueError("sealed archive volume requires at least one part")
    expected_plaintext_start = 0
    for index, current in enumerate(parts, start=1):
        if current.number != index:
            raise ValueError("sealed archive volume part order is invalid")
        if current.plaintext_start != expected_plaintext_start or current.plaintext_bytes < 0:
            raise ValueError("sealed archive volume plaintext ranges are not contiguous")
        if current.stored_bytes <= 0 or not current.etag:
            raise ValueError("sealed archive volume stored part must not be empty")
        if _SHA256_RE.fullmatch(current.plaintext_sha256) is None:
            raise ValueError("sealed archive volume plaintext part sha256 is invalid")
        if _SHA256_RE.fullmatch(current.stored_sha256) is None:
            raise ValueError("sealed archive volume stored part sha256 is invalid")
        expected_plaintext_start += current.plaintext_bytes
    if expected_plaintext_start != plaintext_bytes:
        raise ValueError("sealed archive volume parts do not cover its plaintext")


def _validate_file_coverage(
    files: Sequence[ArchiveFile],
    coverage: Mapping[str, list[tuple[int, int, str]]],
) -> None:
    for current in files:
        ranges = sorted(coverage[current.path])
        if not ranges:
            raise ValueError(f"collection archive file has no volume placement: {current.path}")
        expected_offset = 0
        for offset, byte_count, _volume_id in ranges:
            if offset != expected_offset or byte_count < 0:
                raise ValueError(
                    f"collection archive file placements are not contiguous: {current.path}"
                )
            expected_offset += byte_count
        if expected_offset != current.bytes:
            raise ValueError(f"collection archive file placements do not cover it: {current.path}")


def _verified_raw_files(
    files: Sequence[VerifiedRawFile],
) -> dict[str, VerifiedRawFile]:
    out: dict[str, VerifiedRawFile] = {}
    for current in files:
        path = normalize_relpath(current.path)
        if path.startswith(RESERVED_ARCHIVE_PREFIX) or path in out:
            raise ValueError("raw file verification path is invalid")
        if (
            current.bytes < 0
            or _SHA256_RE.fullmatch(current.sha256) is None
            or _SHA256_RE.fullmatch(current.volume_set_sha256) is None
            or not current.verified_at
        ):
            raise ValueError("raw file verification identity is invalid")
        out[path] = VerifiedRawFile(
            path=path,
            bytes=current.bytes,
            sha256=current.sha256,
            volume_set_sha256=current.volume_set_sha256,
            verified_at=current.verified_at,
        )
    return out


def _normalized_files(files: Sequence[ArchiveFile]) -> tuple[ArchiveFile, ...]:
    out: list[ArchiveFile] = []
    seen: set[str] = set()
    for current in files:
        path = normalize_relpath(current.path)
        if path.startswith(RESERVED_ARCHIVE_PREFIX):
            raise ValueError(f"collection path uses reserved archive namespace: {path}")
        if path in seen:
            raise ValueError(f"duplicate collection archive path: {path}")
        if current.bytes < 0 or _SHA256_RE.fullmatch(current.sha256) is None:
            raise ValueError(f"collection archive file identity is invalid: {path}")
        seen.add(path)
        out.append(ArchiveFile(path=path, bytes=current.bytes, sha256=current.sha256))
    if not out:
        raise ValueError("collection archive requires at least one file")
    return tuple(sorted(out, key=lambda current: current.path))
