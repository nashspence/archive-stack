from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from typing import TypedDict

from riverhog_age import UploadState
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

COLLECTION_ARCHIVE_MANIFEST_SCHEMA = "collection-archive-manifest/v1"
PACK_INDEX_SCHEMA = "riverhog-pack-index/v1"
AGE_ENCRYPTION_FORMAT = "age-v1-scrypt"
SELECTIVE_READ_FORMAT = "age-chunk-range/v1"
_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_VOLUME_ID_RE = re.compile(r"(?:pack|segment)-[0-9]{12}")


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
            "encryption": AGE_ENCRYPTION_FORMAT,
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
    return canonical_json_bytes(payload)


def parse_collection_archive_manifest(content: bytes | str) -> dict[str, object]:
    if isinstance(content, bytes):
        content = content.decode("utf-8")
    try:
        payload = json.loads(content)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("collection archive manifest is not valid JSON") from exc
    expected_fields = {"schema", "format", "tree", "volumes"}
    if (
        not isinstance(payload, dict)
        or payload.get("schema") != COLLECTION_ARCHIVE_MANIFEST_SCHEMA
        or frozenset(payload)
        not in {frozenset(expected_fields), frozenset(expected_fields | {"provenance"})}
    ):
        raise ValueError("collection archive manifest schema mismatch")
    format_row = payload.get("format")
    tree = payload.get("tree")
    raw_volumes = payload.get("volumes")
    if not isinstance(format_row, dict):
        raise ValueError("collection archive format is invalid")
    if format_row != {
        "encryption": AGE_ENCRYPTION_FORMAT,
        "pack_index": PACK_INDEX_SCHEMA,
        "part_digest": "sha256",
        "selective_read": SELECTIVE_READ_FORMAT,
    }:
        raise ValueError("collection archive format is unsupported")
    normalized_tree = _normalized_tree(tree)
    if not isinstance(raw_volumes, list) or not raw_volumes:
        raise ValueError("collection archive volumes must be a non-empty list")

    volumes: list[dict[str, object]] = []
    seen_ids: set[str] = set()
    seen_paths: set[str] = set()
    for sequence, raw in enumerate(raw_volumes):
        row = _normalized_volume(raw, expected_sequence=sequence)
        volume_id = str(row["id"])
        relative_path = str(row["path"])
        if volume_id in seen_ids or relative_path in seen_paths:
            raise ValueError("collection archive repeats a volume identity")
        seen_ids.add(volume_id)
        seen_paths.add(relative_path)
        volumes.append(row)
    result: dict[str, object] = {
        "schema": COLLECTION_ARCHIVE_MANIFEST_SCHEMA,
        "format": dict(format_row),
        "tree": normalized_tree,
        "volumes": volumes,
    }
    if "provenance" in payload:
        result["provenance"] = _normalized_provenance(payload["provenance"])
    return result


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


def _normalized_provenance(value: object) -> dict[str, object]:
    if not isinstance(value, Mapping) or set(value) != {"identity", "index", "bundles"}:
        raise ValueError("collection archive provenance descriptor is invalid")
    identity = str(value.get("identity", ""))
    if _SHA256_RE.fullmatch(identity) is None:
        raise ValueError("collection archive provenance identity is invalid")
    raw_index = value.get("index")
    raw_bundles = value.get("bundles")
    if not isinstance(raw_index, Mapping) or not isinstance(raw_bundles, list) or not raw_bundles:
        raise ValueError("collection archive provenance object set is invalid")
    index = _normalized_provenance_object(raw_index, kind="provenance-index")
    bundles = [
        _normalized_provenance_object(item, kind="provenance-bundle") for item in raw_bundles
    ]
    if index["sha256"] != identity:
        raise ValueError("collection archive provenance index identity mismatch")
    expected_bundle_ids = [f"bundle-{sequence:012d}" for sequence in range(len(bundles))]
    if [item["id"] for item in bundles] != expected_bundle_ids:
        raise ValueError("collection archive provenance bundle order is not canonical")
    return {"identity": identity, "index": index, "bundles": bundles}


def _normalized_provenance_object(value: object, *, kind: str) -> dict[str, object]:
    fields = {"id", "kind", "path", "plaintext_bytes", "sha256", "stored_bytes", "stored_sha256"}
    if not isinstance(value, Mapping) or set(value) != fields or value.get("kind") != kind:
        raise ValueError("collection archive provenance object descriptor is invalid")
    object_id = str(value.get("id", ""))
    path = normalize_relpath(str(value.get("path", "")))
    expected_path = (
        "provenance/index.json.age"
        if kind == "provenance-index"
        else f"provenance/{object_id}.tar.age"
    )
    if path != expected_path:
        raise ValueError("collection archive provenance object path is not canonical")
    plaintext_bytes = _stored_int(value.get("plaintext_bytes"), "provenance plaintext bytes")
    stored_bytes = _stored_int(value.get("stored_bytes"), "provenance stored bytes")
    sha256 = str(value.get("sha256", ""))
    stored_sha256 = str(value.get("stored_sha256", ""))
    if (
        plaintext_bytes < 1
        or stored_bytes < 1
        or _SHA256_RE.fullmatch(sha256) is None
        or _SHA256_RE.fullmatch(stored_sha256) is None
    ):
        raise ValueError("collection archive provenance object identity is invalid")
    return {
        "id": object_id,
        "kind": kind,
        "path": path,
        "plaintext_bytes": plaintext_bytes,
        "sha256": sha256,
        "stored_bytes": stored_bytes,
        "stored_sha256": stored_sha256,
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


def _normalized_tree(value: object) -> dict[str, object]:
    if not isinstance(value, Mapping) or set(value) != {"files", "bytes", "sha256"}:
        raise ValueError("collection archive tree must be a canonical mapping")
    files = _canonical_nonnegative_int(value.get("files"), label="tree files")
    byte_count = _canonical_nonnegative_int(value.get("bytes"), label="tree bytes")
    sha256 = str(value.get("sha256", ""))
    if files < 1 or _SHA256_RE.fullmatch(sha256) is None:
        raise ValueError("collection archive tree identity is invalid")
    return {"files": files, "bytes": byte_count, "sha256": sha256}


def _normalized_volume(value: object, *, expected_sequence: int) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise ValueError("collection archive volume must be a mapping")
    volume_id = str(value.get("id", ""))
    sequence = _canonical_nonnegative_int(value.get("sequence"), label="volume sequence")
    kind = str(value.get("kind", ""))
    expected_fields = (
        {
            "id",
            "sequence",
            "kind",
            "path",
            "files",
            "source_bytes",
            "plaintext_bytes",
            "age_state",
            "index_sha256",
            "plan_sha256",
            "parts",
        }
        if kind == "pack"
        else {
            "id",
            "sequence",
            "kind",
            "path",
            "plaintext_bytes",
            "age_state",
            "file",
            "parts",
        }
        if kind == "segment"
        else set()
    )
    if set(value) != expected_fields:
        raise ValueError("collection archive volume fields are invalid")
    relative_path = normalize_relpath(str(value.get("path", "")))
    plaintext_bytes = _canonical_nonnegative_int(
        value.get("plaintext_bytes"), label="volume plaintext bytes"
    )
    if sequence != expected_sequence or _VOLUME_ID_RE.fullmatch(volume_id) is None:
        raise ValueError("collection archive volume identity is invalid")
    expected_prefix = "pack" if kind == "pack" else "segment" if kind == "segment" else None
    if expected_prefix is None or volume_id != f"{expected_prefix}-{sequence:012d}":
        raise ValueError("collection archive volume kind is invalid")
    expected_path = (
        f"volumes/{volume_id}.tar.age" if kind == "pack" else f"volumes/{volume_id}.bin.age"
    )
    if relative_path != expected_path:
        raise ValueError("collection archive volume path is not canonical")
    age_state = _normalized_age_state(value.get("age_state"), plaintext_bytes=plaintext_bytes)
    parts = _normalized_parts(value.get("parts"), plaintext_bytes=plaintext_bytes)
    base: dict[str, object] = {
        "id": volume_id,
        "sequence": sequence,
        "kind": kind,
        "path": relative_path,
        "plaintext_bytes": plaintext_bytes,
        "age_state": age_state,
        "parts": parts,
    }
    if kind == "pack":
        files = _canonical_nonnegative_int(value.get("files"), label="pack files")
        source_bytes = _canonical_nonnegative_int(
            value.get("source_bytes"), label="pack source bytes"
        )
        index_sha256 = str(value.get("index_sha256", ""))
        plan_sha256 = str(value.get("plan_sha256", ""))
        if files < 1 or _SHA256_RE.fullmatch(index_sha256) is None:
            raise ValueError("collection archive pack identity is invalid")
        if _SHA256_RE.fullmatch(plan_sha256) is None:
            raise ValueError("collection archive pack plan identity is invalid")
        base.update(
            {
                "files": files,
                "source_bytes": source_bytes,
                "index_sha256": index_sha256,
                "plan_sha256": plan_sha256,
            }
        )
        return base

    file_row = value.get("file")
    if not isinstance(file_row, Mapping) or set(file_row) != {
        "path",
        "offset",
        "bytes",
        "file_bytes",
        "sha256",
    }:
        raise ValueError("collection archive segment identity is invalid")
    source_path = normalize_relpath(str(file_row.get("path", "")))
    if source_path.startswith(RESERVED_ARCHIVE_PREFIX):
        raise ValueError("collection archive segment source path is reserved")
    offset = _canonical_nonnegative_int(file_row.get("offset"), label="segment file offset")
    byte_count = _canonical_nonnegative_int(file_row.get("bytes"), label="segment file bytes")
    file_bytes = _canonical_nonnegative_int(
        file_row.get("file_bytes"), label="segment file total bytes"
    )
    file_sha256 = str(file_row.get("sha256", ""))
    if byte_count != plaintext_bytes or offset + byte_count > file_bytes:
        raise ValueError("collection archive segment placement byte count mismatch")
    if _SHA256_RE.fullmatch(file_sha256) is None:
        raise ValueError("collection archive segment file sha256 is invalid")
    base.update(
        {
            "file": {
                "path": source_path,
                "offset": offset,
                "bytes": byte_count,
                "file_bytes": file_bytes,
                "sha256": file_sha256,
            },
        }
    )
    return base


def _normalized_parts(value: object, *, plaintext_bytes: int) -> list[dict[str, object]]:
    if not isinstance(value, list) or not value:
        raise ValueError("collection archive volume parts must be a non-empty list")
    parts: list[dict[str, object]] = []
    expected_plaintext_start = 0
    for index, raw in enumerate(value, start=1):
        expected_fields = {
            "number",
            "plaintext_start",
            "plaintext_bytes",
            "plaintext_sha256",
            "stored_bytes",
            "stored_sha256",
        }
        if not isinstance(raw, Mapping) or set(raw) != expected_fields:
            raise ValueError("collection archive volume part must be a canonical mapping")
        number = _canonical_nonnegative_int(raw.get("number"), label="part number")
        plaintext_start = _canonical_nonnegative_int(
            raw.get("plaintext_start"), label="part plaintext start"
        )
        part_plaintext_bytes = _canonical_nonnegative_int(
            raw.get("plaintext_bytes"), label="part plaintext bytes"
        )
        plaintext_sha256 = str(raw.get("plaintext_sha256", ""))
        stored_bytes = _canonical_nonnegative_int(
            raw.get("stored_bytes"), label="part stored bytes"
        )
        stored_sha256 = str(raw.get("stored_sha256", ""))
        if number != index or plaintext_start != expected_plaintext_start:
            raise ValueError("collection archive volume part order is invalid")
        if stored_bytes < 1:
            raise ValueError("collection archive volume part stored bytes must be positive")
        if _SHA256_RE.fullmatch(plaintext_sha256) is None:
            raise ValueError("collection archive volume part plaintext sha256 is invalid")
        if _SHA256_RE.fullmatch(stored_sha256) is None:
            raise ValueError("collection archive volume part stored sha256 is invalid")
        parts.append(
            {
                "number": number,
                "plaintext_start": plaintext_start,
                "plaintext_bytes": part_plaintext_bytes,
                "plaintext_sha256": plaintext_sha256,
                "stored_bytes": stored_bytes,
                "stored_sha256": stored_sha256,
            }
        )
        expected_plaintext_start += part_plaintext_bytes
    if expected_plaintext_start != plaintext_bytes:
        raise ValueError("collection archive volume parts do not cover its plaintext")
    return parts


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
