from __future__ import annotations

import base64
import binascii
import builtins
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any, Literal

COLLECTION_ARCHIVE_MANIFEST_SCHEMA = "collection-archive-manifest/v1"
ARCHIVE_ENCRYPTION_FORMAT = "age-v1-scrypt"
AGE_UPLOAD_STATE_FORMAT = "age-v1-scrypt-resumable"
PACK_INDEX_SCHEMA = "riverhog-pack-index/v1"
PART_DIGEST_FORMAT = "sha256"
SELECTIVE_READ_FORMAT = "age-chunk-range/v1"

_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_VOLUME_ID_RE = re.compile(r"(?:pack|segment)-[0-9]{12}")


class ArchiveManifestError(ValueError):
    """The plaintext archive root is not the canonical v1 contract."""


def _canonical_json_bytes(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _mapping(value: object, fields: set[str], label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or set(value) != fields:
        raise ArchiveManifestError(f"{label} fields are invalid")
    return value


def _nonnegative_int(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ArchiveManifestError(f"{label} must be a non-negative integer")
    return value


def _positive_int(value: object, label: str) -> int:
    parsed = _nonnegative_int(value, label)
    if parsed < 1:
        raise ArchiveManifestError(f"{label} must be positive")
    return parsed


def _sha256(value: object, label: str) -> str:
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        raise ArchiveManifestError(f"{label} is not a SHA-256 identity")
    return value


def _relative_path(value: object, label: str) -> str:
    if not isinstance(value, str) or not value or "\\" in value:
        raise ArchiveManifestError(f"{label} is not a canonical relative path")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise ArchiveManifestError(f"{label} is not a canonical relative path")
    normalized = str(path)
    if normalized != value:
        raise ArchiveManifestError(f"{label} is not a canonical relative path")
    return normalized


def _base64(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise ArchiveManifestError(f"{label} is not canonical base64")
    try:
        decoded = base64.b64decode(value + "=" * (-len(value) % 4), validate=True)
    except (binascii.Error, TypeError, ValueError) as exc:
        raise ArchiveManifestError(f"{label} is not canonical base64") from exc
    if base64.b64encode(decoded).decode("ascii").rstrip("=") != value:
        raise ArchiveManifestError(f"{label} is not canonical base64")
    return value


@dataclass(frozen=True, slots=True)
class CollectionTreeIdentity:
    files: int
    bytes: int
    sha256: str

    def __post_init__(self) -> None:
        _positive_int(self.files, "archive tree files")
        _nonnegative_int(self.bytes, "archive tree bytes")
        _sha256(self.sha256, "archive tree sha256")

    @classmethod
    def from_mapping(cls, value: object) -> CollectionTreeIdentity:
        row = _mapping(value, {"files", "bytes", "sha256"}, "archive tree")
        files = _positive_int(row["files"], "archive tree files")
        return cls(
            files=files,
            bytes=_nonnegative_int(row["bytes"], "archive tree bytes"),
            sha256=_sha256(row["sha256"], "archive tree sha256"),
        )

    def to_mapping(self) -> dict[str, object]:
        return {"files": self.files, "bytes": self.bytes, "sha256": self.sha256}


@dataclass(frozen=True, slots=True)
class AgeUploadState:
    header_b64: str
    payload_nonce_b64: str
    plaintext_size: int
    format: str = AGE_UPLOAD_STATE_FORMAT

    def __post_init__(self) -> None:
        if self.format != AGE_UPLOAD_STATE_FORMAT:
            raise ArchiveManifestError("archive age state format is unsupported")
        _nonnegative_int(self.plaintext_size, "archive age state plaintext size")
        header_b64 = _base64(self.header_b64, "archive age header")
        nonce_b64 = _base64(self.payload_nonce_b64, "archive age payload nonce")
        if not base64.b64decode(header_b64 + "=" * (-len(header_b64) % 4)):
            raise ArchiveManifestError("archive age header must not be empty")
        if len(base64.b64decode(nonce_b64 + "=" * (-len(nonce_b64) % 4))) != 16:
            raise ArchiveManifestError("archive age payload nonce must be 16 bytes")

    @classmethod
    def from_mapping(cls, value: object, *, plaintext_bytes: int) -> AgeUploadState:
        row = _mapping(
            value,
            {"format", "header_b64", "payload_nonce_b64", "plaintext_size"},
            "archive age state",
        )
        if row["format"] != AGE_UPLOAD_STATE_FORMAT:
            raise ArchiveManifestError("archive age state format is unsupported")
        size = _nonnegative_int(row["plaintext_size"], "archive age state plaintext size")
        if size != plaintext_bytes:
            raise ArchiveManifestError("archive age state plaintext size does not match volume")
        header_b64 = _base64(row["header_b64"], "archive age header")
        nonce_b64 = _base64(row["payload_nonce_b64"], "archive age payload nonce")
        if not base64.b64decode(header_b64 + "=" * (-len(header_b64) % 4)):
            raise ArchiveManifestError("archive age header must not be empty")
        if len(base64.b64decode(nonce_b64 + "=" * (-len(nonce_b64) % 4))) != 16:
            raise ArchiveManifestError("archive age payload nonce must be 16 bytes")
        return cls(
            header_b64=header_b64,
            payload_nonce_b64=nonce_b64,
            plaintext_size=size,
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "format": self.format,
            "header_b64": self.header_b64,
            "payload_nonce_b64": self.payload_nonce_b64,
            "plaintext_size": self.plaintext_size,
        }


@dataclass(frozen=True, slots=True)
class StoredPartIdentity:
    number: int
    plaintext_start: int
    plaintext_bytes: int
    plaintext_sha256: str
    stored_bytes: int
    stored_sha256: str

    def __post_init__(self) -> None:
        _positive_int(self.number, "archive part number")
        _nonnegative_int(self.plaintext_start, "archive part plaintext start")
        _nonnegative_int(self.plaintext_bytes, "archive part plaintext bytes")
        _sha256(self.plaintext_sha256, "archive part plaintext sha256")
        _positive_int(self.stored_bytes, "archive part stored bytes")
        _sha256(self.stored_sha256, "archive part stored sha256")

    @classmethod
    def from_mapping(
        cls,
        value: object,
        *,
        expected_number: int,
        expected_start: int,
    ) -> StoredPartIdentity:
        row = _mapping(
            value,
            {
                "number",
                "plaintext_start",
                "plaintext_bytes",
                "plaintext_sha256",
                "stored_bytes",
                "stored_sha256",
            },
            "archive part",
        )
        number = _positive_int(row["number"], "archive part number")
        start = _nonnegative_int(row["plaintext_start"], "archive part plaintext start")
        if number != expected_number or start != expected_start:
            raise ArchiveManifestError("archive part order is not canonical")
        return cls(
            number=number,
            plaintext_start=start,
            plaintext_bytes=_nonnegative_int(
                row["plaintext_bytes"], "archive part plaintext bytes"
            ),
            plaintext_sha256=_sha256(row["plaintext_sha256"], "archive part plaintext sha256"),
            stored_bytes=_positive_int(row["stored_bytes"], "archive part stored bytes"),
            stored_sha256=_sha256(row["stored_sha256"], "archive part stored sha256"),
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "number": self.number,
            "plaintext_start": self.plaintext_start,
            "plaintext_bytes": self.plaintext_bytes,
            "plaintext_sha256": self.plaintext_sha256,
            "stored_bytes": self.stored_bytes,
            "stored_sha256": self.stored_sha256,
        }


def _parts(value: object, *, plaintext_bytes: int) -> tuple[StoredPartIdentity, ...]:
    if not isinstance(value, list) or not value:
        raise ArchiveManifestError("archive volume parts must be a non-empty list")
    result: list[StoredPartIdentity] = []
    expected_start = 0
    for number, raw in enumerate(value, start=1):
        part = StoredPartIdentity.from_mapping(
            raw, expected_number=number, expected_start=expected_start
        )
        result.append(part)
        expected_start += part.plaintext_bytes
    if expected_start != plaintext_bytes:
        raise ArchiveManifestError("archive volume parts do not cover its plaintext")
    return tuple(result)


def _validate_parts(
    value: object,
    *,
    plaintext_bytes: int,
) -> tuple[StoredPartIdentity, ...]:
    if (
        not isinstance(value, tuple)
        or not value
        or not all(isinstance(item, StoredPartIdentity) for item in value)
    ):
        raise ArchiveManifestError("archive volume parts must be a non-empty tuple")
    expected_start = 0
    for number, part in enumerate(value, start=1):
        if part.number != number or part.plaintext_start != expected_start:
            raise ArchiveManifestError("archive part order is not canonical")
        expected_start += part.plaintext_bytes
    if expected_start != plaintext_bytes:
        raise ArchiveManifestError("archive volume parts do not cover its plaintext")
    return value


@dataclass(frozen=True, slots=True)
class ArchiveFileIdentity:
    path: str
    bytes: int
    sha256: str

    def __post_init__(self) -> None:
        path = _relative_path(self.path, "archive file path")
        if path.startswith(".riverhog/"):
            raise ArchiveManifestError("archive file path is reserved")
        _nonnegative_int(self.bytes, "archive file bytes")
        _sha256(self.sha256, "archive file sha256")


@dataclass(frozen=True, slots=True)
class SegmentFilePlacement:
    path: str
    offset: int
    bytes: int
    file_bytes: int
    sha256: str

    def __post_init__(self) -> None:
        path = _relative_path(self.path, "archive segment source path")
        if path.startswith(".riverhog/"):
            raise ArchiveManifestError("archive segment source path is reserved")
        offset = _nonnegative_int(self.offset, "archive segment file offset")
        byte_count = _nonnegative_int(self.bytes, "archive segment bytes")
        file_bytes = _nonnegative_int(self.file_bytes, "archive segment file bytes")
        if offset + byte_count > file_bytes:
            raise ArchiveManifestError("archive segment placement is invalid")
        _sha256(self.sha256, "archive segment file sha256")

    @classmethod
    def from_mapping(cls, value: object, *, plaintext_bytes: int) -> SegmentFilePlacement:
        row = _mapping(
            value,
            {"path", "offset", "bytes", "file_bytes", "sha256"},
            "archive segment file",
        )
        path = _relative_path(row["path"], "archive segment source path")
        if path.startswith(".riverhog/"):
            raise ArchiveManifestError("archive segment source path is reserved")
        offset = _nonnegative_int(row["offset"], "archive segment file offset")
        byte_count = _nonnegative_int(row["bytes"], "archive segment bytes")
        file_bytes = _nonnegative_int(row["file_bytes"], "archive segment file bytes")
        if byte_count != plaintext_bytes or offset + byte_count > file_bytes:
            raise ArchiveManifestError("archive segment placement is invalid")
        return cls(
            path=path,
            offset=offset,
            bytes=byte_count,
            file_bytes=file_bytes,
            sha256=_sha256(row["sha256"], "archive segment file sha256"),
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "path": self.path,
            "offset": self.offset,
            "bytes": self.bytes,
            "file_bytes": self.file_bytes,
            "sha256": self.sha256,
        }


@dataclass(frozen=True, slots=True)
class PackArchiveVolume:
    id: str
    sequence: int
    path: str
    files: int
    source_bytes: int
    plaintext_bytes: int
    age_state: AgeUploadState
    index_sha256: str
    plan_sha256: str
    parts: tuple[StoredPartIdentity, ...]
    kind: Literal["pack"] = "pack"

    def __post_init__(self) -> None:
        sequence = _nonnegative_int(self.sequence, "archive volume sequence")
        if self.kind != "pack" or self.id != f"pack-{sequence:012d}":
            raise ArchiveManifestError("archive volume identity is not canonical")
        if self.path != f"volumes/{self.id}.tar.age":
            raise ArchiveManifestError("archive volume path is not canonical")
        _positive_int(self.files, "archive pack files")
        _nonnegative_int(self.source_bytes, "archive pack source bytes")
        plaintext_bytes = _nonnegative_int(self.plaintext_bytes, "archive volume bytes")
        if not isinstance(self.age_state, AgeUploadState):
            raise ArchiveManifestError("archive age state is invalid")
        if self.age_state.plaintext_size != plaintext_bytes:
            raise ArchiveManifestError("archive age state plaintext size does not match volume")
        _sha256(self.index_sha256, "archive pack index sha256")
        _sha256(self.plan_sha256, "archive pack plan sha256")
        _validate_parts(self.parts, plaintext_bytes=plaintext_bytes)

    def to_mapping(self) -> dict[str, object]:
        return {
            "id": self.id,
            "sequence": self.sequence,
            "kind": self.kind,
            "path": self.path,
            "files": self.files,
            "source_bytes": self.source_bytes,
            "plaintext_bytes": self.plaintext_bytes,
            "age_state": self.age_state.to_mapping(),
            "index_sha256": self.index_sha256,
            "plan_sha256": self.plan_sha256,
            "parts": [part.to_mapping() for part in self.parts],
        }


@dataclass(frozen=True, slots=True)
class SegmentArchiveVolume:
    id: str
    sequence: int
    path: str
    plaintext_bytes: int
    age_state: AgeUploadState
    file: SegmentFilePlacement
    parts: tuple[StoredPartIdentity, ...]
    kind: Literal["segment"] = "segment"

    def __post_init__(self) -> None:
        sequence = _nonnegative_int(self.sequence, "archive volume sequence")
        if self.kind != "segment" or self.id != f"segment-{sequence:012d}":
            raise ArchiveManifestError("archive volume identity is not canonical")
        if self.path != f"volumes/{self.id}.bin.age":
            raise ArchiveManifestError("archive volume path is not canonical")
        plaintext_bytes = _nonnegative_int(self.plaintext_bytes, "archive volume bytes")
        if not isinstance(self.age_state, AgeUploadState):
            raise ArchiveManifestError("archive age state is invalid")
        if self.age_state.plaintext_size != plaintext_bytes:
            raise ArchiveManifestError("archive age state plaintext size does not match volume")
        if not isinstance(self.file, SegmentFilePlacement) or self.file.bytes != plaintext_bytes:
            raise ArchiveManifestError("archive segment placement is invalid")
        _validate_parts(self.parts, plaintext_bytes=plaintext_bytes)

    @property
    def source_file(self) -> ArchiveFileIdentity:
        return ArchiveFileIdentity(self.file.path, self.file.file_bytes, self.file.sha256)

    @property
    def file_offset(self) -> int:
        return self.file.offset

    def to_mapping(self) -> dict[str, object]:
        return {
            "id": self.id,
            "sequence": self.sequence,
            "kind": self.kind,
            "path": self.path,
            "plaintext_bytes": self.plaintext_bytes,
            "age_state": self.age_state.to_mapping(),
            "file": self.file.to_mapping(),
            "parts": [part.to_mapping() for part in self.parts],
        }


ArchiveVolume = PackArchiveVolume | SegmentArchiveVolume


def _volume(value: object, *, expected_sequence: int) -> ArchiveVolume:
    if not isinstance(value, Mapping):
        raise ArchiveManifestError("archive volume is not a mapping")
    kind = value.get("kind")
    fields = (
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
    row = _mapping(value, fields, "archive volume")
    sequence = _nonnegative_int(row["sequence"], "archive volume sequence")
    volume_id = row["id"]
    if not isinstance(volume_id, str):
        raise ArchiveManifestError("archive volume identity is invalid")
    if sequence != expected_sequence or _VOLUME_ID_RE.fullmatch(volume_id) is None:
        raise ArchiveManifestError("archive volume identity is invalid")
    prefix = "pack" if kind == "pack" else "segment"
    if volume_id != f"{prefix}-{sequence:012d}":
        raise ArchiveManifestError("archive volume identity is not canonical")
    suffix = "tar.age" if kind == "pack" else "bin.age"
    path = _relative_path(row["path"], "archive volume path")
    if path != f"volumes/{volume_id}.{suffix}":
        raise ArchiveManifestError("archive volume path is not canonical")
    plaintext_bytes = _nonnegative_int(row["plaintext_bytes"], "archive volume bytes")
    age_state = AgeUploadState.from_mapping(row["age_state"], plaintext_bytes=plaintext_bytes)
    parts = _parts(row["parts"], plaintext_bytes=plaintext_bytes)
    if kind == "pack":
        return PackArchiveVolume(
            id=volume_id,
            sequence=sequence,
            path=path,
            files=_positive_int(row["files"], "archive pack files"),
            source_bytes=_nonnegative_int(row["source_bytes"], "archive pack source bytes"),
            plaintext_bytes=plaintext_bytes,
            age_state=age_state,
            index_sha256=_sha256(row["index_sha256"], "archive pack index sha256"),
            plan_sha256=_sha256(row["plan_sha256"], "archive pack plan sha256"),
            parts=parts,
        )
    return SegmentArchiveVolume(
        id=volume_id,
        sequence=sequence,
        path=path,
        plaintext_bytes=plaintext_bytes,
        age_state=age_state,
        file=SegmentFilePlacement.from_mapping(row["file"], plaintext_bytes=plaintext_bytes),
        parts=parts,
    )


@dataclass(frozen=True, slots=True)
class ProvenanceObjectIdentity:
    id: str
    kind: Literal["provenance-index", "provenance-bundle"]
    path: str
    plaintext_bytes: int
    sha256: str
    stored_bytes: int
    stored_sha256: str

    def __post_init__(self) -> None:
        if self.kind not in {"provenance-index", "provenance-bundle"}:
            raise ArchiveManifestError("archive provenance object kind is invalid")
        expected_id = "provenance-index" if self.kind == "provenance-index" else self.id
        if self.kind == "provenance-index" and self.id != expected_id:
            raise ArchiveManifestError("archive provenance index identity is invalid")
        if self.kind == "provenance-bundle" and re.fullmatch(r"bundle-[0-9]{12}", self.id) is None:
            raise ArchiveManifestError("archive provenance bundle identity is invalid")
        path = _relative_path(self.path, "archive provenance object path")
        expected_path = (
            "provenance/index.json.age"
            if self.kind == "provenance-index"
            else f"provenance/{self.id}.tar.age"
        )
        if path != expected_path:
            raise ArchiveManifestError("archive provenance object path is not canonical")
        _positive_int(self.plaintext_bytes, "archive provenance plaintext bytes")
        _sha256(self.sha256, "archive provenance sha256")
        _positive_int(self.stored_bytes, "archive provenance stored bytes")
        _sha256(self.stored_sha256, "archive provenance stored sha256")

    @classmethod
    def from_mapping(
        cls,
        value: object,
        *,
        kind: Literal["provenance-index", "provenance-bundle"],
    ) -> ProvenanceObjectIdentity:
        row = _mapping(
            value,
            {"id", "kind", "path", "plaintext_bytes", "sha256", "stored_bytes", "stored_sha256"},
            "archive provenance object",
        )
        if row["kind"] != kind:
            raise ArchiveManifestError("archive provenance object kind is invalid")
        object_id = row["id"]
        if not isinstance(object_id, str):
            raise ArchiveManifestError("archive provenance object identity is invalid")
        path = _relative_path(row["path"], "archive provenance object path")
        expected_path = (
            "provenance/index.json.age"
            if kind == "provenance-index"
            else f"provenance/{object_id}.tar.age"
        )
        if path != expected_path:
            raise ArchiveManifestError("archive provenance object path is not canonical")
        return cls(
            id=object_id,
            kind=kind,
            path=path,
            plaintext_bytes=_positive_int(
                row["plaintext_bytes"], "archive provenance plaintext bytes"
            ),
            sha256=_sha256(row["sha256"], "archive provenance sha256"),
            stored_bytes=_positive_int(row["stored_bytes"], "archive provenance stored bytes"),
            stored_sha256=_sha256(row["stored_sha256"], "archive provenance stored sha256"),
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "id": self.id,
            "kind": self.kind,
            "path": self.path,
            "plaintext_bytes": self.plaintext_bytes,
            "sha256": self.sha256,
            "stored_bytes": self.stored_bytes,
            "stored_sha256": self.stored_sha256,
        }


@dataclass(frozen=True, slots=True)
class ArchiveProvenanceIdentity:
    identity: str
    index: ProvenanceObjectIdentity
    bundles: tuple[ProvenanceObjectIdentity, ...]

    def __post_init__(self) -> None:
        _sha256(self.identity, "archive provenance identity")
        if (
            not isinstance(self.index, ProvenanceObjectIdentity)
            or self.index.kind != "provenance-index"
            or self.index.sha256 != self.identity
        ):
            raise ArchiveManifestError("archive provenance index identity does not match")
        if (
            not isinstance(self.bundles, tuple)
            or not self.bundles
            or not all(
                isinstance(item, ProvenanceObjectIdentity) and item.kind == "provenance-bundle"
                for item in self.bundles
            )
        ):
            raise ArchiveManifestError("archive provenance bundles must be a non-empty tuple")
        if [item.id for item in self.bundles] != [
            f"bundle-{sequence:012d}" for sequence in range(len(self.bundles))
        ]:
            raise ArchiveManifestError("archive provenance bundle order is not canonical")

    @classmethod
    def from_mapping(cls, value: object) -> ArchiveProvenanceIdentity:
        row = _mapping(value, {"identity", "index", "bundles"}, "archive provenance")
        identity = _sha256(row["identity"], "archive provenance identity")
        index = ProvenanceObjectIdentity.from_mapping(row["index"], kind="provenance-index")
        raw_bundles = row["bundles"]
        if not isinstance(raw_bundles, list) or not raw_bundles:
            raise ArchiveManifestError("archive provenance bundles must be a non-empty list")
        bundles = tuple(
            ProvenanceObjectIdentity.from_mapping(item, kind="provenance-bundle")
            for item in raw_bundles
        )
        if index.sha256 != identity:
            raise ArchiveManifestError("archive provenance index identity does not match")
        if [item.id for item in bundles] != [
            f"bundle-{sequence:012d}" for sequence in range(len(bundles))
        ]:
            raise ArchiveManifestError("archive provenance bundle order is not canonical")
        return cls(identity=identity, index=index, bundles=bundles)

    def to_mapping(self) -> dict[str, object]:
        return {
            "identity": self.identity,
            "index": self.index.to_mapping(),
            "bundles": [item.to_mapping() for item in self.bundles],
        }


@dataclass(frozen=True, slots=True)
class CollectionArchiveManifest:
    tree: CollectionTreeIdentity
    volumes: tuple[ArchiveVolume, ...]
    provenance: ArchiveProvenanceIdentity | None = None
    schema: str = COLLECTION_ARCHIVE_MANIFEST_SCHEMA

    def __post_init__(self) -> None:
        if self.schema != COLLECTION_ARCHIVE_MANIFEST_SCHEMA:
            raise ArchiveManifestError("collection archive manifest schema is unsupported")
        if not isinstance(self.tree, CollectionTreeIdentity):
            raise ArchiveManifestError("collection archive tree is invalid")
        if (
            not isinstance(self.volumes, tuple)
            or not self.volumes
            or not all(
                isinstance(item, (PackArchiveVolume, SegmentArchiveVolume)) for item in self.volumes
            )
        ):
            raise ArchiveManifestError("collection archive volumes must be a non-empty tuple")
        if [item.sequence for item in self.volumes] != list(range(len(self.volumes))):
            raise ArchiveManifestError("collection archive volume sequence is not canonical")
        if len({item.id for item in self.volumes}) != len(self.volumes) or len(
            {item.path for item in self.volumes}
        ) != len(self.volumes):
            raise ArchiveManifestError("collection archive repeats a volume identity")
        if self.provenance is not None and not isinstance(
            self.provenance, ArchiveProvenanceIdentity
        ):
            raise ArchiveManifestError("collection archive provenance is invalid")

    @classmethod
    def from_mapping(cls, value: object) -> CollectionArchiveManifest:
        if not isinstance(value, Mapping):
            raise ArchiveManifestError("collection archive manifest is not a mapping")
        expected = {"schema", "format", "tree", "volumes"}
        if set(value) not in (expected, expected | {"provenance"}):
            raise ArchiveManifestError("collection archive manifest fields are invalid")
        if value.get("schema") != COLLECTION_ARCHIVE_MANIFEST_SCHEMA:
            raise ArchiveManifestError("collection archive manifest schema is unsupported")
        if value.get("format") != {
            "encryption": ARCHIVE_ENCRYPTION_FORMAT,
            "pack_index": PACK_INDEX_SCHEMA,
            "part_digest": PART_DIGEST_FORMAT,
            "selective_read": SELECTIVE_READ_FORMAT,
        }:
            raise ArchiveManifestError("collection archive format is unsupported")
        raw_volumes = value.get("volumes")
        if not isinstance(raw_volumes, list) or not raw_volumes:
            raise ArchiveManifestError("collection archive volumes must be a non-empty list")
        volumes = tuple(
            _volume(item, expected_sequence=sequence) for sequence, item in enumerate(raw_volumes)
        )
        if len({item.id for item in volumes}) != len(volumes) or len(
            {item.path for item in volumes}
        ) != len(volumes):
            raise ArchiveManifestError("collection archive repeats a volume identity")
        provenance = (
            ArchiveProvenanceIdentity.from_mapping(value["provenance"])
            if "provenance" in value
            else None
        )
        return cls(
            tree=CollectionTreeIdentity.from_mapping(value.get("tree")),
            volumes=volumes,
            provenance=provenance,
        )

    @classmethod
    def from_json_bytes(cls, content: bytes | str) -> CollectionArchiveManifest:
        try:
            text = content.decode("utf-8") if isinstance(content, bytes) else content
            value: Any = json.loads(text)
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise ArchiveManifestError("collection archive manifest is not valid JSON") from exc
        manifest = cls.from_mapping(value)
        if manifest.to_json_bytes() != text.encode("utf-8"):
            raise ArchiveManifestError("collection archive manifest JSON is not canonical")
        return manifest

    @property
    def files(self) -> int:
        return self.tree.files

    @property
    def bytes(self) -> int:
        return self.tree.bytes

    @property
    def tree_sha256(self) -> str:
        return self.tree.sha256

    def to_mapping(self) -> dict[str, object]:
        value: dict[str, object] = {
            "schema": self.schema,
            "format": {
                "encryption": ARCHIVE_ENCRYPTION_FORMAT,
                "pack_index": PACK_INDEX_SCHEMA,
                "part_digest": PART_DIGEST_FORMAT,
                "selective_read": SELECTIVE_READ_FORMAT,
            },
            "tree": self.tree.to_mapping(),
            "volumes": [volume.to_mapping() for volume in self.volumes],
        }
        if self.provenance is not None:
            value["provenance"] = self.provenance.to_mapping()
        return value

    def to_json_bytes(self) -> builtins.bytes:
        return _canonical_json_bytes(self.to_mapping())
