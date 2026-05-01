from __future__ import annotations

import hashlib
import tarfile
import tempfile
from collections.abc import Callable, Iterable, Iterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

import yaml

from arc_core.fs_paths import normalize_collection_id, normalize_relpath
from arc_core.proofs import CommandProofStamper, ProofStamper

COLLECTION_ARCHIVE_MANIFEST_SCHEMA = "collection-archive-manifest/v1"
COLLECTION_ARCHIVE_FORMAT = "tar"
COLLECTION_ARCHIVE_COMPRESSION = "none"


@dataclass(frozen=True, slots=True)
class CollectionArchiveFile:
    path: str
    content: bytes
    sha256: str

    @property
    def bytes(self) -> int:
        return len(self.content)


@dataclass(frozen=True, slots=True)
class CollectionArchiveExpectedFile:
    path: str
    bytes: int
    sha256: str


@dataclass(frozen=True, slots=True)
class CollectionArchivePackage:
    collection_id: str
    archive_size: int
    archive_sha256: str
    manifest_bytes: bytes
    manifest_sha256: str
    proof_bytes: bytes
    proof_sha256: str
    archive_format: str
    compression: str
    _archive_chunks: Callable[[], Iterator[bytes]] = field(repr=False)

    def iter_archive(self) -> Iterator[bytes]:
        yield from self._archive_chunks()

    @property
    def archive_bytes(self) -> bytes:
        return b"".join(self.iter_archive())


def build_collection_archive_package(
    *,
    collection_id: str,
    files: Sequence[CollectionArchiveFile],
    stamper: ProofStamper | None = None,
) -> CollectionArchivePackage:
    normalized_collection_id = normalize_collection_id(collection_id)
    normalized_files = _normalized_files(files)
    expected_files = _expected_files_from_archive_files(normalized_files)
    return _build_collection_archive_package(
        collection_id=normalized_collection_id,
        files=expected_files,
        archive_chunks=lambda: _archive_chunks_from_reader(
            expected_files,
            lambda path: (next(file.content for file in normalized_files if file.path == path),),
        ),
        stamper=stamper,
    )


def build_collection_archive_package_from_reader(
    *,
    collection_id: str,
    files: Sequence[CollectionArchiveExpectedFile],
    read_file: Callable[[str], bytes],
    stamper: ProofStamper | None = None,
) -> CollectionArchivePackage:
    normalized_collection_id = normalize_collection_id(collection_id)
    normalized_files = _normalized_expected_files(files)
    return build_collection_archive_package_from_chunk_reader(
        collection_id=normalized_collection_id,
        files=normalized_files,
        read_file_chunks=lambda path: (read_file(path),),
        stamper=stamper,
    )


def build_collection_archive_package_from_chunk_reader(
    *,
    collection_id: str,
    files: Sequence[CollectionArchiveExpectedFile],
    read_file_chunks: Callable[[str], Iterable[bytes]],
    stamper: ProofStamper | None = None,
) -> CollectionArchivePackage:
    normalized_collection_id = normalize_collection_id(collection_id)
    normalized_files = _normalized_expected_files(files)
    return _build_collection_archive_package(
        collection_id=normalized_collection_id,
        files=normalized_files,
        archive_chunks=lambda: _archive_chunks_from_reader(
            normalized_files,
            read_file_chunks,
        ),
        stamper=stamper,
    )


def _build_collection_archive_package(
    *,
    collection_id: str,
    files: Sequence[CollectionArchiveExpectedFile],
    archive_chunks: Callable[[], Iterator[bytes]],
    stamper: ProofStamper | None,
) -> CollectionArchivePackage:
    manifest = _manifest_payload(
        collection_id=collection_id,
        files=files,
    )
    manifest_bytes = yaml.safe_dump(
        manifest,
        sort_keys=False,
        allow_unicode=True,
    ).encode("utf-8")
    proof_bytes = _stamp_manifest_bytes(manifest_bytes, stamper=stamper)
    archive_size, archive_sha256 = _sized_sha256(archive_chunks())
    return CollectionArchivePackage(
        collection_id=collection_id,
        archive_size=archive_size,
        archive_sha256=archive_sha256,
        manifest_bytes=manifest_bytes,
        manifest_sha256=_sha256(manifest_bytes),
        proof_bytes=proof_bytes,
        proof_sha256=_sha256(proof_bytes),
        archive_format=COLLECTION_ARCHIVE_FORMAT,
        compression=COLLECTION_ARCHIVE_COMPRESSION,
        _archive_chunks=archive_chunks,
    )


def verify_collection_archive_member(
    *,
    path: str,
    content: bytes,
    expected_sha256: str,
) -> None:
    digest = _sha256(content)
    if digest != expected_sha256:
        raise ValueError(f"collection archive member sha256 mismatch: {path}")


def verify_collection_archive_manifest(
    *,
    manifest_bytes: bytes,
    expected_sha256: str,
    collection_id: str,
    files: Sequence[CollectionArchiveExpectedFile],
) -> None:
    digest = _sha256(manifest_bytes)
    if digest != expected_sha256:
        raise ValueError("collection archive manifest sha256 mismatch")
    try:
        manifest = yaml.safe_load(manifest_bytes)
    except yaml.YAMLError as exc:
        raise ValueError("collection archive manifest is not valid YAML") from exc
    if not isinstance(manifest, dict):
        raise ValueError("collection archive manifest must be a mapping")
    normalized_collection_id = normalize_collection_id(collection_id)
    if manifest.get("schema") != COLLECTION_ARCHIVE_MANIFEST_SCHEMA:
        raise ValueError("collection archive manifest schema mismatch")
    if manifest.get("collection") != normalized_collection_id:
        raise ValueError("collection archive manifest collection mismatch")

    archive = _mapping(manifest.get("archive"), "archive")
    if archive.get("format") != COLLECTION_ARCHIVE_FORMAT:
        raise ValueError("collection archive manifest format mismatch")
    if archive.get("compression") != COLLECTION_ARCHIVE_COMPRESSION:
        raise ValueError("collection archive manifest compression mismatch")

    expected_rows, expected_tree = _expected_manifest_rows(files)
    rows = _manifest_rows(manifest.get("files"))
    if rows != expected_rows:
        raise ValueError("collection archive manifest files do not match catalog")

    tree = _mapping(manifest.get("tree"), "tree")
    if tree.get("sha256") != expected_tree["sha256"]:
        raise ValueError("collection archive manifest tree sha256 mismatch")
    if tree.get("total_bytes") != expected_tree["total_bytes"]:
        raise ValueError("collection archive manifest total bytes mismatch")


def verify_collection_archive_proof(
    *,
    proof_bytes: bytes,
    expected_sha256: str,
    manifest_bytes: bytes,
) -> None:
    digest = _sha256(proof_bytes)
    if digest != expected_sha256:
        raise ValueError("collection archive proof sha256 mismatch")
    if not proof_bytes:
        raise ValueError("collection archive proof is empty")


def iter_collection_archive_files(
    chunks: Iterable[bytes],
) -> Iterator[tuple[str, bytes]]:
    stream = _ChunkIteratorReader(chunks)
    with tarfile.open(fileobj=cast(Any, stream), mode="r|*") as archive:
        for member in archive:
            if not member.isfile():
                continue
            handle = archive.extractfile(member)
            if handle is None:
                continue
            yield normalize_relpath(member.name), handle.read()


def verify_collection_archive_files(
    *,
    chunks: Iterable[bytes],
    files: Sequence[CollectionArchiveExpectedFile],
) -> None:
    expected = {file.path: file for file in _normalized_expected_files(files)}
    seen: set[str] = set()
    stream = _ChunkIteratorReader(chunks)
    with tarfile.open(fileobj=cast(Any, stream), mode="r|*") as archive:
        for member in archive:
            if not member.isfile():
                continue
            path = normalize_relpath(member.name)
            if path in seen:
                raise ValueError(f"duplicate collection archive member: {path}")
            seen.add(path)
            expected_file = expected.get(path)
            if expected_file is None:
                raise ValueError(f"unexpected collection archive member: {path}")
            handle = archive.extractfile(member)
            if handle is None:
                raise ValueError(f"collection archive member cannot be read: {path}")
            _verify_collection_archive_member_stream(
                path=path,
                chunks=_read_chunks(handle),
                expected_bytes=expected_file.bytes,
                expected_sha256=expected_file.sha256,
            )
    missing = sorted(set(expected) - seen)
    if missing:
        raise ValueError(f"collection archive missing member: {missing[0]}")


def iter_verified_collection_archive_files(
    chunks: Iterable[bytes],
    *,
    files: Sequence[CollectionArchiveExpectedFile],
    selected_paths: set[str] | None = None,
) -> Iterator[tuple[str, bytes]]:
    expected = {file.path: file for file in _normalized_expected_files(files)}
    normalized_selected = (
        {normalize_relpath(path) for path in selected_paths}
        if selected_paths is not None
        else None
    )
    seen: set[str] = set()
    yielded: set[str] = set()
    stream = _ChunkIteratorReader(chunks)
    with tarfile.open(fileobj=cast(Any, stream), mode="r|*") as archive:
        for member in archive:
            if not member.isfile():
                continue
            path = normalize_relpath(member.name)
            if path in seen:
                raise ValueError(f"duplicate collection archive member: {path}")
            seen.add(path)
            expected_file = expected.get(path)
            if expected_file is None:
                raise ValueError(f"unexpected collection archive member: {path}")
            handle = archive.extractfile(member)
            if handle is None:
                raise ValueError(f"collection archive member cannot be read: {path}")
            selected = normalized_selected is None or path in normalized_selected
            if selected:
                content = handle.read()
                verify_collection_archive_member(
                    path=path,
                    content=content,
                    expected_sha256=expected_file.sha256,
                )
                if len(content) != expected_file.bytes:
                    raise ValueError(f"collection archive member byte count mismatch: {path}")
                yielded.add(path)
                yield path, content
            else:
                _verify_collection_archive_member_stream(
                    path=path,
                    chunks=_read_chunks(handle),
                    expected_bytes=expected_file.bytes,
                    expected_sha256=expected_file.sha256,
                )
    missing = sorted(set(expected) - seen)
    if missing:
        raise ValueError(f"collection archive missing member: {missing[0]}")
    if normalized_selected is not None:
        missing_selected = sorted(normalized_selected - yielded)
        if missing_selected:
            raise ValueError(f"collection archive missing selected member: {missing_selected[0]}")


def _normalized_files(
    files: Sequence[CollectionArchiveFile],
) -> tuple[CollectionArchiveFile, ...]:
    out: list[CollectionArchiveFile] = []
    seen: set[str] = set()
    for file in files:
        path = normalize_relpath(file.path)
        if path in seen:
            raise ValueError(f"duplicate collection archive path: {path}")
        seen.add(path)
        digest = _sha256(file.content)
        if digest != file.sha256:
            raise ValueError(f"collection archive file sha256 mismatch: {path}")
        out.append(CollectionArchiveFile(path=path, content=file.content, sha256=digest))
    if not out:
        raise ValueError("collection archive package requires at least one file")
    return tuple(sorted(out, key=lambda current: current.path))


def _normalized_expected_files(
    files: Sequence[CollectionArchiveExpectedFile],
) -> tuple[CollectionArchiveExpectedFile, ...]:
    out: list[CollectionArchiveExpectedFile] = []
    seen: set[str] = set()
    for file in files:
        path = normalize_relpath(file.path)
        if path in seen:
            raise ValueError(f"duplicate collection archive path: {path}")
        seen.add(path)
        out.append(
            CollectionArchiveExpectedFile(
                path=path,
                bytes=int(file.bytes),
                sha256=file.sha256,
            )
        )
    if not out:
        raise ValueError("collection archive package requires at least one file")
    return tuple(sorted(out, key=lambda current: current.path))


def _expected_files_from_archive_files(
    files: Sequence[CollectionArchiveFile],
) -> tuple[CollectionArchiveExpectedFile, ...]:
    return tuple(
        CollectionArchiveExpectedFile(path=file.path, bytes=file.bytes, sha256=file.sha256)
        for file in files
    )


def _manifest_payload(
    *,
    collection_id: str,
    files: Sequence[CollectionArchiveExpectedFile],
) -> dict[str, object]:
    tree_digest = hashlib.sha256()
    rows: list[dict[str, object]] = []
    total_bytes = 0
    for file in files:
        total_bytes += file.bytes
        rows.append(
            {
                "path": file.path,
                "bytes": file.bytes,
                "sha256": file.sha256,
            }
        )
        tree_digest.update(f"{file.path}\t{file.bytes}\t{file.sha256}\n".encode())
    return {
        "schema": COLLECTION_ARCHIVE_MANIFEST_SCHEMA,
        "collection": collection_id,
        "archive": {
            "format": COLLECTION_ARCHIVE_FORMAT,
            "compression": COLLECTION_ARCHIVE_COMPRESSION,
        },
        "tree": {
            "sha256": tree_digest.hexdigest(),
            "total_bytes": total_bytes,
        },
        "files": rows,
    }


def _expected_manifest_rows(
    files: Sequence[CollectionArchiveExpectedFile],
) -> tuple[tuple[dict[str, object], ...], dict[str, object]]:
    tree_digest = hashlib.sha256()
    rows: list[dict[str, object]] = []
    seen: set[str] = set()
    total_bytes = 0
    for file in sorted(files, key=lambda current: normalize_relpath(current.path)):
        path = normalize_relpath(file.path)
        if path in seen:
            raise ValueError(f"duplicate collection archive path: {path}")
        seen.add(path)
        total_bytes += file.bytes
        rows.append({"path": path, "bytes": file.bytes, "sha256": file.sha256})
        tree_digest.update(f"{path}\t{file.bytes}\t{file.sha256}\n".encode())
    if not rows:
        raise ValueError("collection archive manifest requires at least one file")
    return tuple(rows), {"sha256": tree_digest.hexdigest(), "total_bytes": total_bytes}


def _manifest_rows(value: object) -> tuple[dict[str, object], ...]:
    if not isinstance(value, list):
        raise ValueError("collection archive manifest files must be a list")
    rows: list[dict[str, object]] = []
    for row in value:
        mapping = _mapping(row, "file")
        path = normalize_relpath(str(mapping.get("path", "")))
        try:
            byte_count = int(mapping.get("bytes", -1))
        except (TypeError, ValueError) as exc:
            raise ValueError("collection archive manifest file has invalid byte count") from exc
        sha256 = str(mapping.get("sha256", ""))
        rows.append({"path": path, "bytes": byte_count, "sha256": sha256})
    return tuple(sorted(rows, key=lambda current: str(current["path"])))


def _mapping(value: object, name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"collection archive manifest {name} must be a mapping")
    return value


def _stamp_manifest_bytes(
    manifest_bytes: bytes,
    *,
    stamper: ProofStamper | None,
) -> bytes:
    with tempfile.TemporaryDirectory(prefix="arc-collection-archive-proof-") as tmpdir:
        manifest_path = Path(tmpdir) / "manifest.yml"
        manifest_path.write_bytes(manifest_bytes)
        proof_path = (stamper or CommandProofStamper()).stamp(manifest_path)
        return proof_path.read_bytes()


def _archive_chunks_from_reader(
    files: Sequence[CollectionArchiveExpectedFile],
    read_file_chunks: Callable[[str], Iterable[bytes]],
) -> Iterator[bytes]:
    for file in files:
        yield _tar_header(file.path, file.bytes)
        digest = hashlib.sha256()
        byte_count = 0
        for chunk in read_file_chunks(file.path):
            if not chunk:
                continue
            digest.update(chunk)
            byte_count += len(chunk)
            yield chunk
        if byte_count != file.bytes:
            raise ValueError(f"collection archive file byte count mismatch: {file.path}")
        if digest.hexdigest() != file.sha256:
            raise ValueError(f"collection archive member sha256 mismatch: {file.path}")
        padding = (-file.bytes) % 512
        if padding:
            yield b"\0" * padding
    yield b"\0" * 1024


def _tar_header(path: str, size: int) -> bytes:
    name, prefix = _ustar_name(path)
    header = bytearray(512)
    _write_tar_field(header, 0, 100, name)
    _write_tar_octal(header, 100, 8, 0o644)
    _write_tar_octal(header, 108, 8, 0)
    _write_tar_octal(header, 116, 8, 0)
    _write_tar_octal(header, 124, 12, size)
    _write_tar_octal(header, 136, 12, 0)
    header[148:156] = b"        "
    header[156:157] = b"0"
    _write_tar_field(header, 257, 6, b"ustar\0")
    _write_tar_field(header, 263, 2, b"00")
    _write_tar_field(header, 345, 155, prefix)
    checksum = sum(header)
    _write_tar_octal(header, 148, 8, checksum)
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


def _read_chunks(handle: Any, chunk_size: int = 1024 * 1024) -> Iterator[bytes]:
    while True:
        chunk = handle.read(chunk_size)
        if not chunk:
            return
        yield chunk


def _verify_collection_archive_member_stream(
    *,
    path: str,
    chunks: Iterable[bytes],
    expected_bytes: int,
    expected_sha256: str,
) -> None:
    digest = hashlib.sha256()
    byte_count = 0
    for chunk in chunks:
        digest.update(chunk)
        byte_count += len(chunk)
    if byte_count != expected_bytes:
        raise ValueError(f"collection archive member byte count mismatch: {path}")
    if digest.hexdigest() != expected_sha256:
        raise ValueError(f"collection archive member sha256 mismatch: {path}")


class _ChunkIteratorReader:
    def __init__(self, chunks: Iterable[bytes]) -> None:
        self._chunks = iter(chunks)
        self._buffer = bytearray()
        self._finished = False

    def read(self, size: int = -1) -> bytes:
        if size == 0:
            return b""
        if size < 0:
            while self._fill():
                pass
            out = bytes(self._buffer)
            self._buffer.clear()
            return out
        while len(self._buffer) < size and self._fill():
            pass
        out = bytes(self._buffer[:size])
        del self._buffer[:size]
        return out

    def _fill(self) -> bool:
        if self._finished:
            return False
        for chunk in self._chunks:
            if chunk:
                self._buffer.extend(chunk)
                return True
        self._finished = True
        return False


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _sized_sha256(chunks: Iterable[bytes]) -> tuple[int, str]:
    digest = hashlib.sha256()
    size = 0
    for chunk in chunks:
        digest.update(chunk)
        size += len(chunk)
    return size, digest.hexdigest()
