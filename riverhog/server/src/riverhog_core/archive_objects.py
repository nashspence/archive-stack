from __future__ import annotations

import hashlib
import io
import re
import tarfile
import tempfile
from collections.abc import Buffer, Callable, Iterable, Iterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

import yaml
from riverhog_age import age_ciphertext_len_for_plaintext_len
from riverhog_protocol.paths import normalize_collection_id, normalize_relpath

from riverhog_core.proofs import CommandProofStamper, ProofStamper, ProofVerifier

COLLECTION_ARCHIVE_MANIFEST_SCHEMA = "collection-archive-manifest/v1"
SMALL_FILE_LIMIT = 16 * 1024 * 1024
PACK_PAYLOAD_LIMIT = 32 * 1024 * 1024
STORED_OBJECT_LIMIT = 32 * 1024 * 1024 * 1024

_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_TAR_BLOCK_SIZE = 512
_TAR_USTAR_SIZE_MAX = int("7" * 11, 8)


def max_age_plaintext_object_bytes(
    *,
    age_prefix_len: int,
    stored_object_limit: int = STORED_OBJECT_LIMIT,
) -> int:
    """Return the largest plaintext whose complete age object fits the stored cap."""

    if age_prefix_len <= 0:
        raise ValueError("age prefix length must be positive")
    if stored_object_limit <= age_prefix_len:
        raise ValueError("stored object limit cannot contain an age payload")
    lower = 0
    upper = stored_object_limit
    while lower < upper:
        candidate = (lower + upper + 1) // 2
        stored_bytes = age_ciphertext_len_for_plaintext_len(
            candidate,
            age_prefix_len=age_prefix_len,
        )
        if stored_bytes <= stored_object_limit:
            lower = candidate
        else:
            upper = candidate - 1
    return lower


@dataclass(frozen=True, slots=True)
class CollectionArchiveSourceFile:
    path: str
    content: bytes
    sha256: str

    @property
    def bytes(self) -> int:
        return len(self.content)


@dataclass(frozen=True, slots=True)
class CollectionArchiveFile:
    path: str
    bytes: int
    sha256: str


@dataclass(frozen=True, slots=True)
class ArchiveObjectPlacement:
    path: str
    file_offset: int
    bytes: int
    member: str | None = None


@dataclass(frozen=True, slots=True)
class CollectionArchiveDataObject:
    object_id: str
    kind: str
    plaintext_bytes: int
    sha256: str
    placements: tuple[ArchiveObjectPlacement, ...]
    _chunks: Callable[[], Iterator[bytes]] = field(repr=False, compare=False)
    _chunks_range: Callable[[int, int], Iterator[bytes]] | None = field(
        default=None,
        repr=False,
        compare=False,
    )

    def iter_plaintext(self) -> Iterator[bytes]:
        yield from self._chunks()

    def iter_plaintext_range(self, offset: int, size: int) -> Iterator[bytes]:
        if offset < 0:
            raise ValueError("archive object offset must be non-negative")
        if size < 0:
            raise ValueError("archive object range size must be non-negative")
        if offset + size > self.plaintext_bytes:
            raise ValueError("archive object range exceeds its plaintext size")
        if self._chunks_range is not None:
            yield from self._chunks_range(offset, size)
            return
        yield from _iter_range(self.iter_plaintext(), offset, size)

    @property
    def supports_ranges(self) -> bool:
        return self._chunks_range is not None


@dataclass(frozen=True, slots=True)
class CollectionArchive:
    collection_id: str
    files: tuple[CollectionArchiveFile, ...]
    data_objects: tuple[CollectionArchiveDataObject, ...]
    manifest_bytes: bytes
    manifest_sha256: str
    proof_bytes: bytes
    proof_sha256: str

    def require_object(self, object_id: str) -> CollectionArchiveDataObject:
        for current in self.data_objects:
            if current.object_id == object_id:
                return current
        raise KeyError(object_id)


def build_collection_archive(
    *,
    collection_id: str,
    files: Sequence[CollectionArchiveSourceFile],
    max_plaintext_object_bytes: int,
    stamper: ProofStamper | None = None,
) -> CollectionArchive:
    normalized_sources = _normalized_source_files(files)
    expected = tuple(
        CollectionArchiveFile(path=file.path, bytes=file.bytes, sha256=file.sha256)
        for file in normalized_sources
    )
    content = {file.path: file.content for file in normalized_sources}
    return build_collection_archive_from_chunk_reader(
        collection_id=collection_id,
        files=expected,
        read_file_chunks=lambda path: (content[path],),
        read_file_chunks_range=lambda path, offset, size: (
            content[path][offset:] if size is None else content[path][offset : offset + size],
        ),
        max_plaintext_object_bytes=max_plaintext_object_bytes,
        stamper=stamper,
    )


def build_collection_archive_from_chunk_reader(
    *,
    collection_id: str,
    files: Sequence[CollectionArchiveFile],
    read_file_chunks: Callable[[str], Iterable[bytes]],
    read_file_chunks_range: Callable[[str, int, int | None], Iterable[bytes]] | None,
    max_plaintext_object_bytes: int,
    stamper: ProofStamper | None = None,
) -> CollectionArchive:
    normalized_collection_id = normalize_collection_id(collection_id)
    normalized_files = _normalized_files(files)
    _validate_plaintext_object_limit(max_plaintext_object_bytes)
    planned = _plan_data_objects(
        normalized_files,
        read_file_chunks=read_file_chunks,
        read_file_chunks_range=read_file_chunks_range,
        max_plaintext_object_bytes=max_plaintext_object_bytes,
    )
    data_objects = tuple(_hash_planned_object(current) for current in planned)
    manifest_bytes = _manifest_bytes(
        collection_id=normalized_collection_id,
        files=normalized_files,
        data_objects=data_objects,
    )
    proof_bytes = _stamp_manifest_bytes(manifest_bytes, stamper=stamper)
    return CollectionArchive(
        collection_id=normalized_collection_id,
        files=normalized_files,
        data_objects=data_objects,
        manifest_bytes=manifest_bytes,
        manifest_sha256=_sha256(manifest_bytes),
        proof_bytes=proof_bytes,
        proof_sha256=_sha256(proof_bytes),
    )


def build_collection_archive_from_manifest(
    *,
    collection_id: str,
    files: Sequence[CollectionArchiveFile],
    read_file_chunks: Callable[[str], Iterable[bytes]],
    read_file_chunks_range: Callable[[str, int, int | None], Iterable[bytes]] | None,
    manifest_bytes: bytes,
    proof_bytes: bytes,
    verifier: ProofVerifier | None = None,
) -> CollectionArchive:
    normalized_collection_id = normalize_collection_id(collection_id)
    normalized_files = _normalized_files(files)
    descriptors = parse_collection_archive_manifest(
        manifest_bytes=manifest_bytes,
        collection_id=normalized_collection_id,
        files=normalized_files,
    )
    verify_collection_archive_manifest_proof(
        proof_bytes=proof_bytes,
        expected_sha256=_sha256(proof_bytes),
        manifest_bytes=manifest_bytes,
        verifier=verifier,
    )
    data_objects = tuple(
        _object_from_descriptor(
            descriptor,
            files=normalized_files,
            read_file_chunks=read_file_chunks,
            read_file_chunks_range=read_file_chunks_range,
        )
        for descriptor in descriptors
    )
    return CollectionArchive(
        collection_id=normalized_collection_id,
        files=normalized_files,
        data_objects=data_objects,
        manifest_bytes=manifest_bytes,
        manifest_sha256=_sha256(manifest_bytes),
        proof_bytes=proof_bytes,
        proof_sha256=_sha256(proof_bytes),
    )


def load_collection_archive(
    *,
    collection_id: str,
    files: Sequence[CollectionArchiveFile],
    manifest_bytes: bytes,
    proof_bytes: bytes,
    read_object_chunks: Callable[[str], Iterable[bytes]],
    read_object_chunks_range: Callable[[str, int, int], Iterable[bytes]] | None = None,
    verifier: ProofVerifier | None = None,
) -> CollectionArchive:
    """Load an archive from independently readable plaintext objects."""

    normalized_collection_id = normalize_collection_id(collection_id)
    normalized_files = _normalized_files(files)
    descriptors = parse_collection_archive_manifest(
        manifest_bytes=manifest_bytes,
        collection_id=normalized_collection_id,
        files=normalized_files,
    )
    verify_collection_archive_manifest_proof(
        proof_bytes=proof_bytes,
        expected_sha256=_sha256(proof_bytes),
        manifest_bytes=manifest_bytes,
        verifier=verifier,
    )
    data_objects: list[CollectionArchiveDataObject] = []
    for descriptor in descriptors:
        object_id = str(descriptor["id"])
        expected_bytes = int(descriptor["bytes"])
        expected_sha256 = str(descriptor["sha256"])

        def chunks(
            object_id: str = object_id,
            expected_bytes: int = expected_bytes,
            expected_sha256: str = expected_sha256,
        ) -> Iterator[bytes]:
            yield from _validated_source_chunks(
                read_object_chunks(object_id),
                expected_bytes=expected_bytes,
                expected_sha256=expected_sha256,
                label=object_id,
            )

        object_chunks_range: Callable[[int, int], Iterator[bytes]] | None = None
        if read_object_chunks_range is not None:

            def object_chunks_range(
                offset: int,
                size: int,
                object_id: str = object_id,
                expected_bytes: int = expected_bytes,
            ) -> Iterator[bytes]:
                if offset < 0 or size < 0 or offset + size > expected_bytes:
                    raise ValueError("archive object range exceeds its plaintext size")
                yield from _validated_source_chunks(
                    read_object_chunks_range(object_id, offset, size),
                    expected_bytes=size,
                    expected_sha256=None,
                    label=object_id,
                )

        data_objects.append(
            CollectionArchiveDataObject(
                object_id=object_id,
                kind=str(descriptor["kind"]),
                plaintext_bytes=expected_bytes,
                sha256=expected_sha256,
                placements=cast(tuple[ArchiveObjectPlacement, ...], descriptor["placements"]),
                _chunks=chunks,
                _chunks_range=object_chunks_range,
            )
        )
    return CollectionArchive(
        collection_id=normalized_collection_id,
        files=normalized_files,
        data_objects=tuple(data_objects),
        manifest_bytes=manifest_bytes,
        manifest_sha256=_sha256(manifest_bytes),
        proof_bytes=proof_bytes,
        proof_sha256=_sha256(proof_bytes),
    )


def parse_collection_archive_manifest(
    *,
    manifest_bytes: bytes,
    collection_id: str,
    files: Sequence[CollectionArchiveFile],
    expected_sha256: str | None = None,
) -> tuple[dict[str, Any], ...]:
    if expected_sha256 is not None and _sha256(manifest_bytes) != expected_sha256:
        raise ValueError("collection archive manifest sha256 mismatch")
    try:
        payload = yaml.safe_load(manifest_bytes)
    except yaml.YAMLError as exc:
        raise ValueError("collection archive manifest is not valid YAML") from exc
    if not isinstance(payload, dict):
        raise ValueError("collection archive manifest must be a mapping")
    if payload.get("schema") != COLLECTION_ARCHIVE_MANIFEST_SCHEMA:
        raise ValueError("collection archive manifest schema mismatch")
    if payload.get("collection") != normalize_collection_id(collection_id):
        raise ValueError("collection archive manifest collection mismatch")

    normalized_files = _normalized_files(files)
    expected_by_path = {file.path: file for file in normalized_files}
    object_rows = _object_rows(payload.get("objects"))
    object_by_id = {str(row["id"]): row for row in object_rows}
    file_rows = _file_rows(payload.get("files"))
    if set(file_rows) != set(expected_by_path):
        raise ValueError("collection archive manifest files do not match catalog")

    placements_by_object: dict[str, list[ArchiveObjectPlacement]] = {
        object_id: [] for object_id in object_by_id
    }
    tree_digest = hashlib.sha256()
    total_bytes = 0
    for path in sorted(file_rows):
        row = file_rows[path]
        expected = expected_by_path[path]
        if row["bytes"] != expected.bytes or row["sha256"] != expected.sha256:
            raise ValueError("collection archive manifest files do not match catalog")
        placements = row["objects"]
        _validate_file_placements(path, expected.bytes, placements, object_by_id)
        for placement in placements:
            placements_by_object[str(placement["object"])].append(
                ArchiveObjectPlacement(
                    path=path,
                    file_offset=int(placement["offset"]),
                    bytes=int(placement["bytes"]),
                    member=(str(placement["member"]) if placement.get("member") else None),
                )
            )
        total_bytes += expected.bytes
        tree_digest.update(f"{path}\t{expected.bytes}\t{expected.sha256}\n".encode())

    tree = payload.get("tree")
    if not isinstance(tree, dict):
        raise ValueError("collection archive manifest tree must be a mapping")
    if tree.get("sha256") != tree_digest.hexdigest() or tree.get("total_bytes") != total_bytes:
        raise ValueError("collection archive manifest tree mismatch")

    descriptors: list[dict[str, Any]] = []
    for row in object_rows:
        object_id = str(row["id"])
        placements = tuple(placements_by_object[object_id])
        if not placements:
            raise ValueError(f"collection archive object is not mapped: {object_id}")
        _validate_object_placements(row, placements)
        descriptors.append({**row, "placements": placements})
    return tuple(descriptors)


def verify_collection_archive_manifest_proof(
    *,
    proof_bytes: bytes,
    expected_sha256: str,
    manifest_bytes: bytes,
    verifier: ProofVerifier | None = None,
) -> None:
    if _sha256(proof_bytes) != expected_sha256:
        raise ValueError("collection archive manifest proof sha256 mismatch")
    if not proof_bytes:
        raise ValueError("collection archive manifest proof is empty")
    if verifier is not None:
        verifier.verify(manifest_bytes=manifest_bytes, proof_bytes=proof_bytes)


def iter_verified_file_chunks(
    archive: CollectionArchive,
    *,
    path: str,
    read_object: Callable[[str], Iterable[bytes]],
) -> tuple[Iterator[bytes], int]:
    normalized_path = normalize_relpath(path)
    expected = next((file for file in archive.files if file.path == normalized_path), None)
    if expected is None:
        raise ValueError(f"collection archive file is not present: {normalized_path}")
    placements = sorted(
        (
            (current, placement)
            for current in archive.data_objects
            for placement in current.placements
            if placement.path == normalized_path
        ),
        key=lambda item: item[1].file_offset,
    )
    if len(placements) == 1 and placements[0][0].kind == "pack":
        current, placement = placements[0]
        return (
            _iter_verified_pack_member(
                current,
                read_object(current.object_id),
                member=placement.member or normalized_path,
                expected=expected,
            ),
            expected.bytes,
        )
    return (
        _iter_verified_raw_file(
            placements,
            read_object=read_object,
            expected=expected,
        ),
        expected.bytes,
    )


@dataclass(frozen=True, slots=True)
class _PlannedObject:
    object_id: str
    kind: str
    plaintext_bytes: int
    placements: tuple[ArchiveObjectPlacement, ...]
    chunks: Callable[[], Iterator[bytes]]
    chunks_range: Callable[[int, int], Iterator[bytes]] | None


def _plan_data_objects(
    files: tuple[CollectionArchiveFile, ...],
    *,
    read_file_chunks: Callable[[str], Iterable[bytes]],
    read_file_chunks_range: Callable[[str, int, int | None], Iterable[bytes]] | None,
    max_plaintext_object_bytes: int,
) -> tuple[_PlannedObject, ...]:
    planned: list[_PlannedObject] = []
    pending_pack: list[CollectionArchiveFile] = []
    pending_payload = 0

    def next_id() -> str:
        return f"data-{len(planned):06d}"

    def flush_pack() -> None:
        nonlocal pending_pack, pending_payload
        if not pending_pack:
            return
        members = tuple(pending_pack)
        object_id = next_id()
        plaintext_bytes = _tar_stream_size(members)
        placements = tuple(
            ArchiveObjectPlacement(
                path=file.path,
                file_offset=0,
                bytes=file.bytes,
                member=file.path,
            )
            for file in members
        )

        def chunks(members: tuple[CollectionArchiveFile, ...] = members) -> Iterator[bytes]:
            yield from _iter_tar_chunks(members, read_file_chunks)

        pack_chunks_range: Callable[[int, int], Iterator[bytes]] | None = None
        if read_file_chunks_range is not None:

            def pack_chunks_range(
                offset: int,
                size: int,
                members: tuple[CollectionArchiveFile, ...] = members,
            ) -> Iterator[bytes]:
                yield from _iter_tar_chunks_range(
                    members,
                    read_file_chunks_range,
                    offset,
                    size,
                )

        planned.append(
            _PlannedObject(
                object_id=object_id,
                kind="pack",
                plaintext_bytes=plaintext_bytes,
                placements=placements,
                chunks=chunks,
                chunks_range=pack_chunks_range,
            )
        )
        pending_pack = []
        pending_payload = 0

    for file in files:
        if file.bytes < SMALL_FILE_LIMIT:
            if pending_pack and pending_payload + file.bytes > PACK_PAYLOAD_LIMIT:
                flush_pack()
            pending_pack.append(file)
            pending_payload += file.bytes
            continue
        flush_pack()
        if file.bytes <= max_plaintext_object_bytes:
            planned.append(
                _raw_planned_object(
                    object_id=next_id(),
                    kind="file",
                    file=file,
                    file_offset=0,
                    length=file.bytes,
                    read_file_chunks=read_file_chunks,
                    read_file_chunks_range=read_file_chunks_range,
                )
            )
            continue
        offset = 0
        while offset < file.bytes:
            length = min(max_plaintext_object_bytes, file.bytes - offset)
            planned.append(
                _raw_planned_object(
                    object_id=next_id(),
                    kind="segment",
                    file=file,
                    file_offset=offset,
                    length=length,
                    read_file_chunks=read_file_chunks,
                    read_file_chunks_range=read_file_chunks_range,
                )
            )
            offset += length
    flush_pack()
    return tuple(planned)


def _raw_planned_object(
    *,
    object_id: str,
    kind: str,
    file: CollectionArchiveFile,
    file_offset: int,
    length: int,
    read_file_chunks: Callable[[str], Iterable[bytes]],
    read_file_chunks_range: Callable[[str, int, int | None], Iterable[bytes]] | None,
) -> _PlannedObject:
    def chunks() -> Iterator[bytes]:
        source = (
            read_file_chunks_range(file.path, file_offset, length)
            if read_file_chunks_range is not None
            else _iter_range(read_file_chunks(file.path), file_offset, length)
        )
        yield from _validated_source_chunks(
            source,
            expected_bytes=length,
            expected_sha256=(file.sha256 if file_offset == 0 and length == file.bytes else None),
            label=file.path,
        )

    def chunks_range(object_offset: int, size: int) -> Iterator[bytes]:
        if object_offset < 0 or size < 0 or object_offset + size > length:
            raise ValueError("archive object range exceeds its plaintext size")
        source = (
            read_file_chunks_range(file.path, file_offset + object_offset, size)
            if read_file_chunks_range is not None
            else _iter_range(read_file_chunks(file.path), file_offset + object_offset, size)
        )
        yield from _validated_source_chunks(
            source,
            expected_bytes=size,
            expected_sha256=None,
            label=file.path,
        )

    return _PlannedObject(
        object_id=object_id,
        kind=kind,
        plaintext_bytes=length,
        placements=(
            ArchiveObjectPlacement(
                path=file.path,
                file_offset=file_offset,
                bytes=length,
            ),
        ),
        chunks=chunks,
        chunks_range=chunks_range if read_file_chunks_range is not None else None,
    )


def _hash_planned_object(planned: _PlannedObject) -> CollectionArchiveDataObject:
    byte_count, digest = _sized_sha256(planned.chunks())
    if byte_count != planned.plaintext_bytes:
        raise ValueError(f"archive object byte count mismatch: {planned.object_id}")
    return CollectionArchiveDataObject(
        object_id=planned.object_id,
        kind=planned.kind,
        plaintext_bytes=byte_count,
        sha256=digest,
        placements=planned.placements,
        _chunks=planned.chunks,
        _chunks_range=planned.chunks_range,
    )


def _manifest_bytes(
    *,
    collection_id: str,
    files: tuple[CollectionArchiveFile, ...],
    data_objects: tuple[CollectionArchiveDataObject, ...],
) -> bytes:
    placements_by_path: dict[str, list[tuple[CollectionArchiveDataObject, ArchiveObjectPlacement]]]
    placements_by_path = {file.path: [] for file in files}
    for current in data_objects:
        for placement in current.placements:
            placements_by_path[placement.path].append((current, placement))
    tree_digest = hashlib.sha256()
    file_rows: list[dict[str, object]] = []
    total_bytes = 0
    for file in files:
        total_bytes += file.bytes
        tree_digest.update(f"{file.path}\t{file.bytes}\t{file.sha256}\n".encode())
        object_rows: list[dict[str, object]] = []
        for current, placement in sorted(
            placements_by_path[file.path],
            key=lambda item: item[1].file_offset,
        ):
            row: dict[str, object] = {
                "object": current.object_id,
                "offset": placement.file_offset,
                "bytes": placement.bytes,
            }
            if placement.member is not None:
                row["member"] = placement.member
            object_rows.append(row)
        file_rows.append(
            {
                "path": file.path,
                "bytes": file.bytes,
                "sha256": file.sha256,
                "objects": object_rows,
            }
        )
    payload = {
        "schema": COLLECTION_ARCHIVE_MANIFEST_SCHEMA,
        "collection": collection_id,
        "tree": {"sha256": tree_digest.hexdigest(), "total_bytes": total_bytes},
        "objects": [
            {
                "id": current.object_id,
                "kind": current.kind,
                "bytes": current.plaintext_bytes,
                "sha256": current.sha256,
            }
            for current in data_objects
        ],
        "files": file_rows,
    }
    return yaml.safe_dump(payload, sort_keys=False, allow_unicode=True).encode("utf-8")


def _object_rows(value: object) -> tuple[dict[str, object], ...]:
    if not isinstance(value, list) or not value:
        raise ValueError("collection archive manifest objects must be a non-empty list")
    rows: list[dict[str, object]] = []
    seen: set[str] = set()
    for value_row in value:
        if not isinstance(value_row, dict):
            raise ValueError("collection archive manifest object must be a mapping")
        object_id = str(value_row.get("id", ""))
        kind = str(value_row.get("kind", ""))
        if not re.fullmatch(r"data-[0-9]{6}", object_id) or object_id in seen:
            raise ValueError("collection archive manifest object id is invalid")
        if kind not in {"pack", "file", "segment"}:
            raise ValueError("collection archive manifest object kind is invalid")
        try:
            byte_count = int(value_row.get("bytes", -1))
        except (TypeError, ValueError) as exc:
            raise ValueError("collection archive manifest object byte count is invalid") from exc
        sha256 = str(value_row.get("sha256", ""))
        if byte_count < 0 or not _SHA256_RE.fullmatch(sha256):
            raise ValueError("collection archive manifest object identity is invalid")
        seen.add(object_id)
        rows.append({"id": object_id, "kind": kind, "bytes": byte_count, "sha256": sha256})
    if [row["id"] for row in rows] != [f"data-{index:06d}" for index in range(len(rows))]:
        raise ValueError("collection archive manifest object order is invalid")
    return tuple(rows)


def _file_rows(value: object) -> dict[str, dict[str, Any]]:
    if not isinstance(value, list) or not value:
        raise ValueError("collection archive manifest files must be a non-empty list")
    rows: dict[str, dict[str, Any]] = {}
    for value_row in value:
        if not isinstance(value_row, dict):
            raise ValueError("collection archive manifest file must be a mapping")
        path = normalize_relpath(str(value_row.get("path", "")))
        if path in rows:
            raise ValueError(f"duplicate collection archive path: {path}")
        try:
            byte_count = int(value_row.get("bytes", -1))
        except (TypeError, ValueError) as exc:
            raise ValueError("collection archive manifest file byte count is invalid") from exc
        sha256 = str(value_row.get("sha256", ""))
        placements = value_row.get("objects")
        if byte_count < 0 or not _SHA256_RE.fullmatch(sha256) or not isinstance(placements, list):
            raise ValueError("collection archive manifest file identity is invalid")
        normalized_placements: list[dict[str, object]] = []
        for placement in placements:
            if not isinstance(placement, dict):
                raise ValueError("collection archive manifest file object must be a mapping")
            try:
                offset = int(placement.get("offset", -1))
                length = int(placement.get("bytes", -1))
            except (TypeError, ValueError) as exc:
                raise ValueError(
                    "collection archive manifest file object range is invalid"
                ) from exc
            normalized_placements.append(
                {
                    "object": str(placement.get("object", "")),
                    "offset": offset,
                    "bytes": length,
                    **(
                        {"member": normalize_relpath(str(placement["member"]))}
                        if placement.get("member") is not None
                        else {}
                    ),
                }
            )
        rows[path] = {
            "bytes": byte_count,
            "sha256": sha256,
            "objects": normalized_placements,
        }
    return rows


def _validate_file_placements(
    path: str,
    expected_bytes: int,
    placements: list[dict[str, object]],
    object_by_id: dict[str, dict[str, object]],
) -> None:
    if not placements:
        raise ValueError(f"collection archive file has no objects: {path}")
    offset = 0
    for placement in placements:
        object_id = str(placement["object"])
        current = object_by_id.get(object_id)
        if current is None:
            raise ValueError(f"collection archive file references an unknown object: {path}")
        length = int(cast(Any, placement["bytes"]))
        if int(cast(Any, placement["offset"])) != offset or length < 0:
            raise ValueError(f"collection archive file object ranges are not contiguous: {path}")
        kind = str(current["kind"])
        member = placement.get("member")
        if kind == "pack":
            if len(placements) != 1 or member != path or length != expected_bytes:
                raise ValueError(f"collection archive pack mapping is invalid: {path}")
        elif member is not None:
            raise ValueError(f"collection archive raw object has a member name: {path}")
        offset += length
    if offset != expected_bytes:
        raise ValueError(f"collection archive file object ranges do not cover the file: {path}")


def _validate_object_placements(
    row: dict[str, object],
    placements: tuple[ArchiveObjectPlacement, ...],
) -> None:
    kind = str(row["kind"])
    plaintext_bytes = int(cast(Any, row["bytes"]))
    if kind == "pack":
        members = tuple(
            CollectionArchiveFile(path=item.path, bytes=item.bytes, sha256="0" * 64)
            for item in placements
        )
        if any(item.file_offset != 0 or item.member != item.path for item in placements):
            raise ValueError("collection archive pack placements are invalid")
        if _tar_stream_size(members) != plaintext_bytes:
            raise ValueError("collection archive pack byte count is invalid")
        if sum(item.bytes for item in placements) > PACK_PAYLOAD_LIMIT:
            raise ValueError("collection archive pack payload exceeds policy")
        if any(item.bytes >= SMALL_FILE_LIMIT for item in placements):
            raise ValueError("collection archive pack contains a non-small file")
        return
    if len(placements) != 1 or placements[0].member is not None:
        raise ValueError("collection archive raw object placements are invalid")
    if placements[0].bytes != plaintext_bytes:
        raise ValueError("collection archive raw object byte count is invalid")
    if kind == "file" and placements[0].file_offset != 0:
        raise ValueError("collection archive whole-file object offset is invalid")
    if kind == "file" and placements[0].bytes < SMALL_FILE_LIMIT:
        raise ValueError("collection archive whole-file object contains a small file")


def _object_from_descriptor(
    descriptor: dict[str, Any],
    *,
    files: tuple[CollectionArchiveFile, ...],
    read_file_chunks: Callable[[str], Iterable[bytes]],
    read_file_chunks_range: Callable[[str, int, int | None], Iterable[bytes]] | None,
) -> CollectionArchiveDataObject:
    placements = cast(tuple[ArchiveObjectPlacement, ...], descriptor["placements"])
    object_chunks_range: Callable[[int, int], Iterator[bytes]] | None = None
    if descriptor["kind"] == "pack":
        file_by_path = {file.path: file for file in files}
        members = tuple(file_by_path[item.path] for item in placements)

        def chunks() -> Iterator[bytes]:
            yield from _iter_tar_chunks(members, read_file_chunks)

        if read_file_chunks_range is not None:

            def object_chunks_range(offset: int, size: int) -> Iterator[bytes]:
                yield from _iter_tar_chunks_range(
                    members,
                    read_file_chunks_range,
                    offset,
                    size,
                )

    else:
        placement = placements[0]
        raw = _raw_planned_object(
            object_id=str(descriptor["id"]),
            kind=str(descriptor["kind"]),
            file=next(file for file in files if file.path == placement.path),
            file_offset=placement.file_offset,
            length=placement.bytes,
            read_file_chunks=read_file_chunks,
            read_file_chunks_range=read_file_chunks_range,
        )
        chunks = raw.chunks
        object_chunks_range = raw.chunks_range
    expected_bytes = int(descriptor["bytes"])
    expected_sha256 = str(descriptor["sha256"])

    def verified_chunks() -> Iterator[bytes]:
        yield from _validated_source_chunks(
            chunks(),
            expected_bytes=expected_bytes,
            expected_sha256=expected_sha256,
            label=str(descriptor["id"]),
        )

    return CollectionArchiveDataObject(
        object_id=str(descriptor["id"]),
        kind=str(descriptor["kind"]),
        plaintext_bytes=int(descriptor["bytes"]),
        sha256=str(descriptor["sha256"]),
        placements=placements,
        _chunks=verified_chunks,
        _chunks_range=object_chunks_range,
    )


def _normalized_source_files(
    files: Sequence[CollectionArchiveSourceFile],
) -> tuple[CollectionArchiveSourceFile, ...]:
    out: list[CollectionArchiveSourceFile] = []
    seen: set[str] = set()
    for file in files:
        path = normalize_relpath(file.path)
        if path in seen:
            raise ValueError(f"duplicate collection archive path: {path}")
        digest = _sha256(file.content)
        if digest != file.sha256:
            raise ValueError(f"collection archive file sha256 mismatch: {path}")
        seen.add(path)
        out.append(CollectionArchiveSourceFile(path=path, content=file.content, sha256=digest))
    if not out:
        raise ValueError("collection archive requires at least one file")
    return tuple(sorted(out, key=lambda current: current.path))


def _normalized_files(
    files: Sequence[CollectionArchiveFile],
) -> tuple[CollectionArchiveFile, ...]:
    out: list[CollectionArchiveFile] = []
    seen: set[str] = set()
    for file in files:
        path = normalize_relpath(file.path)
        if path in seen:
            raise ValueError(f"duplicate collection archive path: {path}")
        if int(file.bytes) < 0 or not _SHA256_RE.fullmatch(file.sha256):
            raise ValueError(f"collection archive file identity is invalid: {path}")
        seen.add(path)
        out.append(CollectionArchiveFile(path=path, bytes=int(file.bytes), sha256=file.sha256))
    if not out:
        raise ValueError("collection archive requires at least one file")
    return tuple(sorted(out, key=lambda current: current.path))


def _validate_plaintext_object_limit(value: int) -> None:
    if value < SMALL_FILE_LIMIT or value >= STORED_OBJECT_LIMIT:
        raise ValueError("archive plaintext object limit is outside policy")


def _iter_tar_chunks(
    files: Sequence[CollectionArchiveFile],
    read_file_chunks: Callable[[str], Iterable[bytes]],
) -> Iterator[bytes]:
    for file in files:
        yield _tar_header(file.path, file.bytes)
        yield from _validated_source_chunks(
            read_file_chunks(file.path),
            expected_bytes=file.bytes,
            expected_sha256=file.sha256,
            label=file.path,
        )
        padding = (-file.bytes) % 512
        if padding:
            yield b"\0" * padding
    yield b"\0" * 1024


def _iter_tar_chunks_range(
    files: Sequence[CollectionArchiveFile],
    read_file_chunks_range: Callable[[str, int, int | None], Iterable[bytes]],
    archive_offset: int,
    size: int,
) -> Iterator[bytes]:
    yield from _iter_range(
        _iter_tar_chunks(
            files,
            lambda path: read_file_chunks_range(path, 0, None),
        ),
        archive_offset,
        size,
    )


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


def _tar_stream_size(files: Sequence[CollectionArchiveFile]) -> int:
    return sum(
        _tar_header_size(file.path, file.bytes) + file.bytes + _tar_padding(file.bytes)
        for file in files
    ) + (_TAR_BLOCK_SIZE * 2)


def _tar_header_size(path: str, size: int) -> int:
    pax_attrs, _file_header_path, _file_header_size = _tar_file_header_values(path, size)
    if not pax_attrs:
        return _TAR_BLOCK_SIZE
    pax_payload_length = len(_pax_payload(pax_attrs))
    return _TAR_BLOCK_SIZE + pax_payload_length + _tar_padding(pax_payload_length) + _TAR_BLOCK_SIZE


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
    header = bytearray(512)
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


def _iter_verified_pack_member(
    current: CollectionArchiveDataObject,
    chunks: Iterable[bytes],
    *,
    member: str,
    expected: CollectionArchiveFile,
) -> Iterator[bytes]:
    verified = iter_verified_object_chunks(current, chunks)
    stream = _ChunkIteratorReader(verified)
    found = False
    with tarfile.open(fileobj=cast(Any, stream), mode="r|*") as archive:
        for info in archive:
            if not info.isfile():
                continue
            path = normalize_relpath(info.name)
            handle = archive.extractfile(info)
            if handle is None:
                raise ValueError(f"collection archive pack member cannot be read: {path}")
            if path == member:
                if found:
                    raise ValueError(f"duplicate collection archive pack member: {path}")
                found = True
                yield from _validated_source_chunks(
                    _read_chunks(handle),
                    expected_bytes=expected.bytes,
                    expected_sha256=expected.sha256,
                    label=path,
                )
            else:
                for _chunk in _read_chunks(handle):
                    pass
    if not found:
        raise ValueError(f"collection archive pack member is missing: {member}")


def _iter_verified_raw_file(
    placements: Sequence[tuple[CollectionArchiveDataObject, ArchiveObjectPlacement]],
    *,
    read_object: Callable[[str], Iterable[bytes]],
    expected: CollectionArchiveFile,
) -> Iterator[bytes]:
    digest = hashlib.sha256()
    byte_count = 0
    for current, placement in placements:
        segment_bytes = 0
        for chunk in iter_verified_object_chunks(current, read_object(current.object_id)):
            segment_bytes += len(chunk)
            byte_count += len(chunk)
            digest.update(chunk)
            yield chunk
        if segment_bytes != placement.bytes:
            raise ValueError(f"collection archive segment byte count mismatch: {expected.path}")
    if byte_count != expected.bytes or digest.hexdigest() != expected.sha256:
        raise ValueError(f"collection archive file verification failed: {expected.path}")


def iter_verified_object_chunks(
    current: CollectionArchiveDataObject,
    chunks: Iterable[bytes],
) -> Iterator[bytes]:
    digest = hashlib.sha256()
    byte_count = 0
    for chunk in chunks:
        if not chunk:
            continue
        byte_count += len(chunk)
        digest.update(chunk)
        yield chunk
    if byte_count != current.plaintext_bytes or digest.hexdigest() != current.sha256:
        raise ValueError(f"collection archive object verification failed: {current.object_id}")


def _validated_source_chunks(
    chunks: Iterable[bytes],
    *,
    expected_bytes: int,
    expected_sha256: str | None,
    label: str,
) -> Iterator[bytes]:
    digest = hashlib.sha256() if expected_sha256 is not None else None
    byte_count = 0
    for chunk in chunks:
        if not chunk:
            continue
        byte_count += len(chunk)
        if digest is not None:
            digest.update(chunk)
        yield chunk
    if byte_count != expected_bytes:
        raise ValueError(f"collection archive source byte count mismatch: {label}")
    if digest is not None and digest.hexdigest() != expected_sha256:
        raise ValueError(f"collection archive source sha256 mismatch: {label}")


def _iter_range(chunks: Iterable[bytes], offset: int, length: int) -> Iterator[bytes]:
    remaining_offset = offset
    remaining_length = length
    for chunk in chunks:
        if not chunk:
            continue
        if remaining_offset >= len(chunk):
            remaining_offset -= len(chunk)
            continue
        current = chunk[remaining_offset:]
        remaining_offset = 0
        if len(current) > remaining_length:
            current = current[:remaining_length]
        if current:
            remaining_length -= len(current)
            yield current
        if remaining_length == 0:
            return
    if remaining_offset or remaining_length:
        raise ValueError("collection archive source ended before requested range")


def _iter_chunks_after_skipping(chunks: Iterable[bytes], offset: int) -> Iterator[bytes]:
    remaining = offset
    for chunk in chunks:
        if remaining >= len(chunk):
            remaining -= len(chunk)
            continue
        if remaining:
            yield chunk[remaining:]
            remaining = 0
        else:
            yield chunk
    if remaining:
        raise ValueError("archive object stream ended before requested offset")


def _sized_sha256(chunks: Iterable[bytes]) -> tuple[int, str]:
    digest = hashlib.sha256()
    byte_count = 0
    for chunk in chunks:
        digest.update(chunk)
        byte_count += len(chunk)
    return byte_count, digest.hexdigest()


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _stamp_manifest_bytes(
    manifest_bytes: bytes,
    *,
    stamper: ProofStamper | None,
) -> bytes:
    with tempfile.TemporaryDirectory(prefix="riverhog-collection-archive-proof-") as tmpdir:
        manifest_path = Path(tmpdir) / "manifest.yml"
        manifest_path.write_bytes(manifest_bytes)
        proof_path = (stamper or CommandProofStamper()).stamp(manifest_path)
        return proof_path.read_bytes()


class _ChunkIteratorReader(io.RawIOBase):
    def __init__(self, chunks: Iterable[bytes]) -> None:
        self._chunks = iter(chunks)
        self._buffer = bytearray()
        self._eof = False

    def readable(self) -> bool:
        return True

    def readinto(self, target: Buffer) -> int:
        view = memoryview(target).cast("B")
        while len(self._buffer) < len(view) and not self._eof:
            try:
                self._buffer.extend(next(self._chunks))
            except StopIteration:
                self._eof = True
        size = min(len(view), len(self._buffer))
        view[:size] = self._buffer[:size]
        del self._buffer[:size]
        return size


def _read_chunks(handle: Any, size: int = 1024 * 1024) -> Iterator[bytes]:
    while True:
        chunk = handle.read(size)
        if not chunk:
            return
        yield cast(bytes, chunk)
