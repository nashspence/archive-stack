from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal, cast

from .common import canonical_json
from .journal import ProvenanceValidationError

PROVENANCE_ROOT_SCHEMA = "riverhog-provenance-root/v1"
PROVENANCE_VOLUME_SCHEMA = "riverhog-provenance-volume/v1"
PROVENANCE_TERMINAL_SCHEMA = "riverhog-provenance-terminal/v1"
PROVENANCE_BINDING_SEGMENT_SCHEMA = "riverhog-provenance-bindings/v1"
PROVENANCE_BINDING_SEGMENT_FILES_MAX = 512
PROVENANCE_BINDING_SEGMENT_BYTES_MAX = 4 * 1024 * 1024
PROVENANCE_JOURNAL_SEGMENT_BYTES_MAX = 8 * 1024 * 1024
PROVENANCE_ROOT_DOCUMENT_BYTES_MAX = 64 * 1024
PROVENANCE_VOLUME_DOCUMENT_BYTES_MAX = 64 * 1024
PROVENANCE_SEQUENCE_BITS = 256
PROVENANCE_SEQUENCE_HEX_WIDTH = PROVENANCE_SEQUENCE_BITS // 4

_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_SEQUENCE_RE = re.compile(r"[0-9a-f]{64}")
_JOURNAL_ID_RE = re.compile(
    r"urn:uuid:[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
)


@dataclass(frozen=True, slots=True)
class ProvenancePayloadIdentity:
    kind: Literal["bindings", "journal"]
    path: str
    bytes: int
    sha256: str

    def __post_init__(self) -> None:
        if self.kind not in {"bindings", "journal"}:
            raise ProvenanceValidationError("provenance payload kind is invalid")
        if not self.path.startswith("provenance/payloads/volume-") or not self.path.endswith(
            ".bin.age"
        ):
            raise ProvenanceValidationError("provenance payload path is not canonical")
        _positive_int(self.bytes, "provenance payload bytes")
        _sha256(self.sha256, "provenance payload SHA-256")

    def to_mapping(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "path": self.path,
            "bytes": self.bytes,
            "sha256": self.sha256,
        }


@dataclass(frozen=True, slots=True)
class ProvenanceVolumeDocument:
    archive_generation: str
    archive_tree_sha256: str
    sequence: int
    payload: ProvenancePayloadIdentity
    first_file_order: int | None = None
    file_count: int | None = None
    journal_id: str | None = None
    journal_offset: int | None = None
    journal_bytes: int | None = None
    journal_sha256: str | None = None
    schema: str = PROVENANCE_VOLUME_SCHEMA

    def __post_init__(self) -> None:
        if self.schema != PROVENANCE_VOLUME_SCHEMA:
            raise ProvenanceValidationError("provenance volume schema is unsupported")
        _sha256(self.archive_generation, "archive generation")
        _sha256(self.archive_tree_sha256, "archive tree SHA-256")
        _nonnegative_int(self.sequence, "provenance volume sequence")
        sequence = format_provenance_sequence(self.sequence)
        expected_path = f"provenance/payloads/volume-{sequence}.bin.age"
        if self.payload.path != expected_path:
            raise ProvenanceValidationError("provenance volume payload path is not canonical")
        if self.payload.kind == "bindings":
            if any(
                value is not None
                for value in (
                    self.journal_id,
                    self.journal_offset,
                    self.journal_bytes,
                    self.journal_sha256,
                )
            ):
                raise ProvenanceValidationError("binding volume carries journal fields")
            _nonnegative_int(self.first_file_order, "binding first file order")
            count = _positive_int(self.file_count, "binding file count")
            if count > PROVENANCE_BINDING_SEGMENT_FILES_MAX:
                raise ProvenanceValidationError("binding volume exceeds its segmentation rule")
            return
        if self.first_file_order is not None or self.file_count is not None:
            raise ProvenanceValidationError("journal volume carries binding fields")
        _journal_id(self.journal_id)
        offset = _nonnegative_int(self.journal_offset, "journal segment offset")
        total = _positive_int(self.journal_bytes, "journal bytes")
        if offset >= total or offset + self.payload.bytes > total:
            raise ProvenanceValidationError("journal segment exceeds its journal authority")
        if self.payload.bytes > PROVENANCE_JOURNAL_SEGMENT_BYTES_MAX:
            raise ProvenanceValidationError("journal segment exceeds its segmentation rule")
        _sha256(self.journal_sha256, "journal SHA-256")

    @property
    def metadata_path(self) -> str:
        return f"provenance/metadata/volume-{format_provenance_sequence(self.sequence)}.json.age"

    def to_mapping(self) -> dict[str, object]:
        value: dict[str, object] = {
            "schema": self.schema,
            "archive_generation": self.archive_generation,
            "archive_tree_sha256": self.archive_tree_sha256,
            "sequence": format_provenance_sequence(self.sequence),
            "payload": self.payload.to_mapping(),
        }
        if self.payload.kind == "bindings":
            value["binding_range"] = {
                "first_file_order": self.first_file_order,
                "file_count": self.file_count,
            }
        else:
            value["journal_range"] = {
                "journal_id": self.journal_id,
                "offset": self.journal_offset,
                "bytes": self.journal_bytes,
                "sha256": self.journal_sha256,
            }
        return value

    def to_json_bytes(self) -> bytes:
        content = canonical_json(self.to_mapping()) + b"\n"
        if len(content) > PROVENANCE_VOLUME_DOCUMENT_BYTES_MAX:
            raise ProvenanceValidationError("provenance volume exceeds its byte limit")
        return content

    @classmethod
    def from_json_bytes(cls, content: bytes) -> ProvenanceVolumeDocument:
        if len(content) > PROVENANCE_VOLUME_DOCUMENT_BYTES_MAX:
            raise ProvenanceValidationError("provenance volume exceeds its byte limit")
        value = _canonical_object(content, "provenance volume")
        expected = {
            "schema",
            "archive_generation",
            "archive_tree_sha256",
            "sequence",
            "payload",
        }
        ranges = {"binding_range", "journal_range"} & set(value)
        if set(value) != expected | ranges or len(ranges) != 1:
            raise ProvenanceValidationError("provenance volume fields are invalid")
        payload_row = _mapping(value["payload"], {"kind", "path", "bytes", "sha256"})
        payload = ProvenancePayloadIdentity(
            kind=cast(Any, payload_row["kind"]),
            path=str(payload_row["path"]),
            bytes=_positive_int(payload_row["bytes"], "provenance payload bytes"),
            sha256=_sha256(payload_row["sha256"], "provenance payload SHA-256"),
        )
        schema = str(value["schema"])
        archive_generation = str(value["archive_generation"])
        archive_tree_sha256 = str(value["archive_tree_sha256"])
        sequence = parse_provenance_sequence(value["sequence"], "provenance volume sequence")
        if "binding_range" in value:
            row = _mapping(value["binding_range"], {"first_file_order", "file_count"})
            return cls(
                schema=schema,
                archive_generation=archive_generation,
                archive_tree_sha256=archive_tree_sha256,
                sequence=sequence,
                payload=payload,
                first_file_order=_nonnegative_int(
                    row["first_file_order"], "binding first file order"
                ),
                file_count=_positive_int(row["file_count"], "binding file count"),
            )
        row = _mapping(value["journal_range"], {"journal_id", "offset", "bytes", "sha256"})
        return cls(
            schema=schema,
            archive_generation=archive_generation,
            archive_tree_sha256=archive_tree_sha256,
            sequence=sequence,
            payload=payload,
            journal_id=_journal_id(row["journal_id"]),
            journal_offset=_nonnegative_int(row["offset"], "journal segment offset"),
            journal_bytes=_positive_int(row["bytes"], "journal bytes"),
            journal_sha256=_sha256(row["sha256"], "journal SHA-256"),
        )


@dataclass(frozen=True, slots=True)
class ProvenanceTerminalDocument:
    """Authenticated end marker at the next deterministic provenance sequence."""

    archive_generation: str
    archive_tree_sha256: str
    sequence: int
    kind: Literal["terminal"] = "terminal"
    schema: str = PROVENANCE_TERMINAL_SCHEMA

    def __post_init__(self) -> None:
        if self.schema != PROVENANCE_TERMINAL_SCHEMA or self.kind != "terminal":
            raise ProvenanceValidationError("provenance terminal schema is unsupported")
        _sha256(self.archive_generation, "archive generation")
        _sha256(self.archive_tree_sha256, "archive tree SHA-256")
        _positive_int(self.sequence, "provenance terminal sequence")
        format_provenance_sequence(self.sequence)

    @property
    def metadata_path(self) -> str:
        return f"provenance/metadata/volume-{format_provenance_sequence(self.sequence)}.json.age"

    def to_mapping(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "archive_generation": self.archive_generation,
            "archive_tree_sha256": self.archive_tree_sha256,
            "sequence": format_provenance_sequence(self.sequence),
            "kind": self.kind,
        }

    def to_json_bytes(self) -> bytes:
        content = canonical_json(self.to_mapping()) + b"\n"
        if len(content) > PROVENANCE_VOLUME_DOCUMENT_BYTES_MAX:
            raise ProvenanceValidationError("provenance terminal exceeds its byte limit")
        return content

    @classmethod
    def from_json_bytes(cls, content: bytes) -> ProvenanceTerminalDocument:
        if len(content) > PROVENANCE_VOLUME_DOCUMENT_BYTES_MAX:
            raise ProvenanceValidationError("provenance terminal exceeds its byte limit")
        value = _canonical_object(content, "provenance terminal")
        row = _mapping(
            value,
            {"schema", "archive_generation", "archive_tree_sha256", "sequence", "kind"},
        )
        return cls(
            schema=str(row["schema"]),
            archive_generation=str(row["archive_generation"]),
            archive_tree_sha256=str(row["archive_tree_sha256"]),
            sequence=parse_provenance_sequence(row["sequence"], "provenance terminal sequence"),
            kind=cast(Any, row["kind"]),
        )


@dataclass(frozen=True, slots=True)
class ProvenanceRootDocument:
    archive_generation: str
    archive_tree_sha256: str
    ordered_volume_sha256: str
    schema: str = PROVENANCE_ROOT_SCHEMA

    def __post_init__(self) -> None:
        if self.schema != PROVENANCE_ROOT_SCHEMA:
            raise ProvenanceValidationError("provenance root schema is unsupported")
        _sha256(self.archive_generation, "archive generation")
        _sha256(self.archive_tree_sha256, "archive tree SHA-256")
        _sha256(self.ordered_volume_sha256, "ordered provenance volume SHA-256")

    @property
    def identity(self) -> str:
        return hashlib.sha256(self.to_json_bytes()).hexdigest()

    def to_mapping(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "archive_generation": self.archive_generation,
            "archive_tree_sha256": self.archive_tree_sha256,
            "volume_sequence": {
                "sha256": self.ordered_volume_sha256,
            },
        }

    def to_json_bytes(self) -> bytes:
        content = canonical_json(self.to_mapping()) + b"\n"
        if len(content) > PROVENANCE_ROOT_DOCUMENT_BYTES_MAX:
            raise ProvenanceValidationError("provenance root exceeds its byte limit")
        return content

    @classmethod
    def from_json_bytes(cls, content: bytes) -> ProvenanceRootDocument:
        if len(content) > PROVENANCE_ROOT_DOCUMENT_BYTES_MAX:
            raise ProvenanceValidationError("provenance root exceeds its byte limit")
        value = _canonical_object(content, "provenance root")
        if set(value) != {
            "schema",
            "archive_generation",
            "archive_tree_sha256",
            "volume_sequence",
        }:
            raise ProvenanceValidationError("provenance root fields are invalid")
        sequence = _mapping(value["volume_sequence"], {"sha256"})
        return cls(
            schema=str(value["schema"]),
            archive_generation=str(value["archive_generation"]),
            archive_tree_sha256=str(value["archive_tree_sha256"]),
            ordered_volume_sha256=_sha256(sequence["sha256"], "ordered provenance volume SHA-256"),
        )


def binding_segment_bytes(
    *,
    first_file_order: int,
    files: list[Mapping[str, object]],
) -> bytes:
    _nonnegative_int(first_file_order, "binding first file order")
    if not files or len(files) > PROVENANCE_BINDING_SEGMENT_FILES_MAX:
        raise ProvenanceValidationError("binding segment exceeds its segmentation rule")
    content = (
        canonical_json(
            {
                "schema": PROVENANCE_BINDING_SEGMENT_SCHEMA,
                "first_file_order": first_file_order,
                "files": [dict(item) for item in files],
            }
        )
        + b"\n"
    )
    if len(content) > PROVENANCE_BINDING_SEGMENT_BYTES_MAX:
        raise ProvenanceValidationError("binding segment exceeds its byte limit")
    return content


def bounded_binding_segment_bytes(
    *,
    first_file_order: int,
    files: list[Mapping[str, object]],
) -> tuple[bytes, int]:
    """Return the largest canonical bounded prefix and its exact member count."""

    if not files:
        raise ProvenanceValidationError("binding segment must not be empty")
    upper = min(len(files), PROVENANCE_BINDING_SEGMENT_FILES_MAX)
    low = 1
    best: tuple[bytes, int] | None = None
    while low <= upper:
        middle = (low + upper) // 2
        try:
            content = binding_segment_bytes(
                first_file_order=first_file_order,
                files=files[:middle],
            )
        except ProvenanceValidationError:
            upper = middle - 1
        else:
            best = (content, middle)
            low = middle + 1
    if best is None:
        raise ProvenanceValidationError("one provenance binding exceeds its segment limit")
    return best


def parse_binding_segment(content: bytes) -> tuple[int, list[dict[str, object]]]:
    if len(content) > PROVENANCE_BINDING_SEGMENT_BYTES_MAX:
        raise ProvenanceValidationError("binding segment exceeds its byte limit")
    value = _canonical_object(content, "provenance binding segment")
    if (
        set(value) != {"schema", "first_file_order", "files"}
        or value.get("schema") != PROVENANCE_BINDING_SEGMENT_SCHEMA
    ):
        raise ProvenanceValidationError("provenance binding segment fields are invalid")
    files = value["files"]
    if (
        not isinstance(files, list)
        or not files
        or any(not isinstance(item, dict) for item in files)
    ):
        raise ProvenanceValidationError("provenance binding segment files are invalid")
    if len(files) > PROVENANCE_BINDING_SEGMENT_FILES_MAX:
        raise ProvenanceValidationError("binding segment exceeds its segmentation rule")
    return (
        _nonnegative_int(value["first_file_order"], "binding first file order"),
        cast(list[dict[str, object]], files),
    )


ProvenanceSequenceDocument = ProvenanceVolumeDocument | ProvenanceTerminalDocument


def update_ordered_volume_commitment(
    digest: Any,
    document: ProvenanceSequenceDocument,
) -> None:
    """Commit one ordinal and canonical plaintext-document identity unambiguously."""

    kind = "volume" if isinstance(document, ProvenanceVolumeDocument) else "terminal"
    digest.update(b"riverhog-provenance-volume-sequence/v1\x00")
    digest.update(document.sequence.to_bytes(PROVENANCE_SEQUENCE_BITS // 8, "big"))
    digest.update(b"\x00")
    digest.update(kind.encode("ascii"))
    digest.update(b"\x00")
    digest.update(hashlib.sha256(document.to_json_bytes()).digest())


def _canonical_object(content: bytes, label: str) -> dict[str, object]:
    try:
        value = json.loads(content)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProvenanceValidationError(f"{label} is not valid JSON") from exc
    if not isinstance(value, dict) or canonical_json(value) + b"\n" != content:
        raise ProvenanceValidationError(f"{label} is not canonical JSON")
    return cast(dict[str, object], value)


def _mapping(value: object, fields: set[str]) -> dict[str, object]:
    if not isinstance(value, Mapping) or set(value) != fields:
        raise ProvenanceValidationError("provenance document fields are invalid")
    return {str(key): item for key, item in value.items()}


def _positive_int(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ProvenanceValidationError(f"{label} is invalid")
    return value


def _nonnegative_int(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ProvenanceValidationError(f"{label} is invalid")
    return value


def format_provenance_sequence(value: int) -> str:
    """Return the fixed-width v1 representation of one provenance ordinal."""

    parsed = _nonnegative_int(value, "provenance sequence")
    if parsed >= 1 << PROVENANCE_SEQUENCE_BITS:
        raise ProvenanceValidationError("provenance sequence exceeds the v1 representation")
    return f"{parsed:0{PROVENANCE_SEQUENCE_HEX_WIDTH}x}"


def parse_provenance_sequence(value: object, label: str = "provenance sequence") -> int:
    if not isinstance(value, str) or _SEQUENCE_RE.fullmatch(value) is None:
        raise ProvenanceValidationError(f"{label} is not a canonical 256-bit ordinal")
    return int(value, 16)


def _sha256(value: object, label: str) -> str:
    text = str(value or "")
    if _SHA256_RE.fullmatch(text) is None:
        raise ProvenanceValidationError(f"{label} is invalid")
    return text


def _journal_id(value: object) -> str:
    text = str(value or "")
    if _JOURNAL_ID_RE.fullmatch(text) is None:
        raise ProvenanceValidationError("provenance journal ID is invalid")
    return text


__all__ = [
    "PROVENANCE_BINDING_SEGMENT_FILES_MAX",
    "PROVENANCE_BINDING_SEGMENT_BYTES_MAX",
    "PROVENANCE_BINDING_SEGMENT_SCHEMA",
    "PROVENANCE_JOURNAL_SEGMENT_BYTES_MAX",
    "PROVENANCE_ROOT_DOCUMENT_BYTES_MAX",
    "PROVENANCE_VOLUME_DOCUMENT_BYTES_MAX",
    "PROVENANCE_ROOT_SCHEMA",
    "PROVENANCE_SEQUENCE_BITS",
    "PROVENANCE_SEQUENCE_HEX_WIDTH",
    "PROVENANCE_TERMINAL_SCHEMA",
    "PROVENANCE_VOLUME_SCHEMA",
    "ProvenancePayloadIdentity",
    "ProvenanceRootDocument",
    "ProvenanceTerminalDocument",
    "ProvenanceVolumeDocument",
    "binding_segment_bytes",
    "bounded_binding_segment_bytes",
    "format_provenance_sequence",
    "parse_binding_segment",
    "parse_provenance_sequence",
    "update_ordered_volume_commitment",
]
