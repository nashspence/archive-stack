from __future__ import annotations

import hashlib
import json
import re
import sysconfig
import tempfile
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from http_api_contracts import canonical_json_bytes, iter_json_sequence_records
from pydantic import BaseModel, ConfigDict, Field, model_validator

from riverhog_protocol.paths import (
    CollectionId,
    normalize_relpath,
    validate_collection_id,
)

PORTABLE_COLLECTION_FORMAT: Literal["riverhog-collection/v1"] = "riverhog-collection/v1"
PORTABLE_COLLECTION_STREAM_FORMAT: Literal["riverhog-collection-inventory-stream/v1"] = (
    "riverhog-collection-inventory-stream/v1"
)
_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_PASSPHRASE_ID_RE = re.compile(r"[A-Za-z0-9_-]{16,128}")


class PortableCollectionError(ValueError):
    """The document is not the canonical portable Riverhog collection contract."""


def portable_collection_json_schema() -> dict[str, Any]:
    """Load the shipped structural projection of the canonical portable record."""

    candidates = (
        Path(__file__).parents[2] / "schemas" / "riverhog-collection-v1.schema.json",
        Path(sysconfig.get_path("data"))
        / "share"
        / "riverhog-protocol"
        / "schemas"
        / "riverhog-collection-v1.schema.json",
    )
    for path in candidates:
        if path.is_file():
            document = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(document, dict):
                return document
    raise RuntimeError("the riverhog portable-collection schema is not installed")


def _sha256(value: object, label: str) -> str:
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        raise PortableCollectionError(f"{label} is not a SHA-256 identity")
    return value


def _nonnegative_int(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise PortableCollectionError(f"{label} must be a non-negative integer")
    return value


def _string(value: object, label: str) -> str:
    if not isinstance(value, str):
        raise PortableCollectionError(f"{label} must be a string")
    return value


@dataclass(frozen=True, slots=True)
class PortableCollectionFile:
    path: str
    bytes: int
    sha256: str

    def __post_init__(self) -> None:
        if not isinstance(self.path, str):
            raise PortableCollectionError("portable collection file path is invalid")
        try:
            path = normalize_relpath(self.path)
        except ValueError as exc:
            raise PortableCollectionError("portable collection file path is invalid") from exc
        if path != self.path:
            raise PortableCollectionError("portable collection file path is not canonical")
        _nonnegative_int(self.bytes, "portable collection file bytes")
        _sha256(self.sha256, "portable collection file sha256")

    @classmethod
    def from_mapping(cls, value: object) -> PortableCollectionFile:
        if not isinstance(value, Mapping) or set(value) != {"path", "bytes", "sha256"}:
            raise PortableCollectionError("portable collection file fields are invalid")
        try:
            path = normalize_relpath(str(value["path"]))
        except ValueError as exc:
            raise PortableCollectionError("portable collection file path is invalid") from exc
        if path != value["path"]:
            raise PortableCollectionError("portable collection file path is not canonical")
        return cls(
            path=path,
            bytes=_nonnegative_int(value["bytes"], "portable collection file bytes"),
            sha256=_sha256(value["sha256"], "portable collection file sha256"),
        )

    def to_mapping(self) -> dict[str, object]:
        return {"path": self.path, "bytes": self.bytes, "sha256": self.sha256}


class PortableCollectionHeader(BaseModel):
    """Bounded immutable metadata that owns one portable file inventory."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    format: Literal["riverhog-collection/v1"] = PORTABLE_COLLECTION_FORMAT
    collection: CollectionId
    content_identity: str = Field(pattern=r"^[0-9a-f]{64}$")
    encryption_format: str = Field(min_length=1)
    passphrase_id: str = Field(pattern=r"^[A-Za-z0-9_-]{16,128}$")
    provenance_mode: Literal["captured", "mixed", "omitted"]
    provenance_identity: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_provenance_binding(self) -> PortableCollectionHeader:
        if (self.provenance_mode == "omitted") != (self.provenance_identity is None):
            raise ValueError("portable collection provenance binding is inconsistent")
        if self.encryption_format.strip() != self.encryption_format:
            raise ValueError("portable collection encryption format is not canonical")
        return self


class PortableCollectionInventoryBegin(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    type: Literal["begin"] = "begin"
    format: Literal["riverhog-collection-inventory-stream/v1"] = PORTABLE_COLLECTION_STREAM_FORMAT
    header: PortableCollectionHeader
    inventory_identity: str = Field(pattern=r"^[0-9a-f]{64}$")
    files: int = Field(ge=1)
    bytes: int = Field(ge=0)


class PortableCollectionInventoryFile(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    type: Literal["file"] = "file"
    ordinal: int = Field(ge=0)
    file: dict[str, object]


class PortableCollectionInventoryEnd(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    type: Literal["end"] = "end"
    files: int = Field(ge=1)
    bytes: int = Field(ge=0)
    inventory_identity: str = Field(pattern=r"^[0-9a-f]{64}$")


class PortableCollectionIdentityBuilder:
    """Incrementally seal one canonically ordered immutable file inventory."""

    def __init__(self, header: PortableCollectionHeader) -> None:
        self.header = header
        self._digest = hashlib.sha256()
        self._digest.update(canonical_json_bytes(header.model_dump(mode="json")))
        self._previous_path: str | None = None
        self.files = 0
        self.bytes = 0

    def add(self, file: PortableCollectionFile) -> None:
        if self._previous_path is not None and file.path <= self._previous_path:
            raise PortableCollectionError("portable collection files are not canonical")
        encoded = canonical_json_bytes(file.to_mapping())
        self._digest.update(len(encoded).to_bytes(8, "big"))
        self._digest.update(encoded)
        self._previous_path = file.path
        self.files += 1
        self.bytes += file.bytes

    @property
    def identity(self) -> str:
        if self.files < 1:
            raise PortableCollectionError("portable collection files must not be empty")
        return self._digest.hexdigest()


def portable_collection_inventory_identity(
    header: PortableCollectionHeader,
    files: Iterable[PortableCollectionFile],
) -> str:
    builder = PortableCollectionIdentityBuilder(header)
    for file in files:
        builder.add(file)
    return builder.identity


@dataclass(frozen=True, slots=True)
class PortableCollectionRecord:
    collection: CollectionId
    content_identity: str
    encryption_format: str
    passphrase_id: str
    provenance_mode: Literal["captured", "mixed", "omitted"]
    provenance_identity: str | None
    files: tuple[PortableCollectionFile, ...]
    format: str = PORTABLE_COLLECTION_FORMAT

    def __post_init__(self) -> None:
        try:
            collection = validate_collection_id(self.collection)
        except ValueError as exc:
            raise PortableCollectionError("portable collection id is invalid") from exc
        if collection != self.collection:
            raise PortableCollectionError("portable collection id is not canonical")
        _sha256(self.content_identity, "portable collection content identity")
        if (
            not isinstance(self.encryption_format, str)
            or not self.encryption_format
            or self.encryption_format.strip() != self.encryption_format
        ):
            raise PortableCollectionError("portable collection encryption format is invalid")
        if (
            not isinstance(self.passphrase_id, str)
            or _PASSPHRASE_ID_RE.fullmatch(self.passphrase_id) is None
        ):
            raise PortableCollectionError("portable collection passphrase id is invalid")
        if self.provenance_mode not in {"captured", "mixed", "omitted"}:
            raise PortableCollectionError("portable collection provenance mode is invalid")
        if self.provenance_identity is not None:
            _sha256(self.provenance_identity, "portable collection provenance identity")
        if self.provenance_mode == "omitted" and self.provenance_identity is not None:
            raise PortableCollectionError("omitted provenance cannot have an identity")
        if self.provenance_mode != "omitted" and self.provenance_identity is None:
            raise PortableCollectionError("captured provenance requires an identity")
        if (
            not isinstance(self.files, tuple)
            or not self.files
            or not all(isinstance(item, PortableCollectionFile) for item in self.files)
        ):
            raise PortableCollectionError("portable collection files must not be empty")
        if tuple(sorted(self.files, key=lambda item: item.path)) != self.files or len(
            {item.path for item in self.files}
        ) != len(self.files):
            raise PortableCollectionError("portable collection files are not canonical")
        if self.format != PORTABLE_COLLECTION_FORMAT:
            raise PortableCollectionError("portable collection format is unsupported")

    @classmethod
    def create(
        cls,
        *,
        collection: CollectionId,
        content_identity: str,
        encryption_format: str,
        passphrase_id: str,
        provenance_mode: Literal["captured", "mixed", "omitted"],
        provenance_identity: str | None,
        files: Iterable[tuple[str, int, str]],
    ) -> PortableCollectionRecord:
        return cls(
            collection=collection,
            content_identity=content_identity,
            encryption_format=encryption_format,
            passphrase_id=passphrase_id,
            provenance_mode=provenance_mode,
            provenance_identity=provenance_identity,
            files=tuple(
                sorted(
                    (
                        PortableCollectionFile.from_mapping(
                            {"path": path, "bytes": byte_count, "sha256": sha256}
                        )
                        for path, byte_count, sha256 in files
                    ),
                    key=lambda item: item.path,
                )
            ),
        )

    @classmethod
    def from_mapping(cls, value: object) -> PortableCollectionRecord:
        fields = {
            "format",
            "collection",
            "content_identity",
            "encryption_format",
            "passphrase_id",
            "provenance_mode",
            "provenance_identity",
            "files",
        }
        if not isinstance(value, Mapping) or set(value) != fields:
            raise PortableCollectionError("portable collection fields are invalid")
        raw_files = value["files"]
        if not isinstance(raw_files, list):
            raise PortableCollectionError("portable collection files are invalid")
        mode = value["provenance_mode"]
        if mode not in {"captured", "mixed", "omitted"}:
            raise PortableCollectionError("portable collection provenance mode is invalid")
        return cls(
            format=_string(value["format"], "portable collection format"),
            collection=_nonnegative_int(value["collection"], "portable collection id"),
            content_identity=_string(
                value["content_identity"], "portable collection content identity"
            ),
            encryption_format=_string(
                value["encryption_format"], "portable collection encryption format"
            ),
            passphrase_id=_string(value["passphrase_id"], "portable collection passphrase id"),
            provenance_mode=mode,
            provenance_identity=(
                None
                if value["provenance_identity"] is None
                else _string(
                    value["provenance_identity"],
                    "portable collection provenance identity",
                )
            ),
            files=tuple(PortableCollectionFile.from_mapping(item) for item in raw_files),
        )

    @classmethod
    def from_json_bytes(cls, content: bytes | str) -> PortableCollectionRecord:
        try:
            text = content.decode("utf-8") if isinstance(content, bytes) else content
            value: Any = json.loads(text)
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise PortableCollectionError("portable collection is not valid JSON") from exc
        record = cls.from_mapping(value)
        if record.to_json_bytes() != text.encode("utf-8"):
            raise PortableCollectionError("portable collection JSON is not canonical")
        return record

    def to_mapping(self) -> dict[str, object]:
        return {
            "format": self.format,
            "collection": self.collection,
            "content_identity": self.content_identity,
            "encryption_format": self.encryption_format,
            "passphrase_id": self.passphrase_id,
            "provenance_mode": self.provenance_mode,
            "provenance_identity": self.provenance_identity,
            "files": [item.to_mapping() for item in self.files],
        }

    def to_json_bytes(self) -> bytes:
        return json.dumps(self.to_mapping(), sort_keys=True, separators=(",", ":")).encode()

    @property
    def identity(self) -> str:
        return portable_collection_inventory_identity(self.header, self.files)

    @property
    def header(self) -> PortableCollectionHeader:
        return PortableCollectionHeader(
            collection=self.collection,
            content_identity=self.content_identity,
            encryption_format=self.encryption_format,
            passphrase_id=self.passphrase_id,
            provenance_mode=self.provenance_mode,
            provenance_identity=self.provenance_identity,
        )


def _json_sequence_record(value: object) -> bytes:
    return b"\x1e" + canonical_json_bytes(value) + b"\n"


def iter_portable_collection_inventory(
    header: PortableCollectionHeader,
    files: Iterable[PortableCollectionFile],
    *,
    inventory_identity: str,
    file_count: int,
    file_bytes: int,
) -> Iterator[bytes]:
    """Validate one immutable inventory before emitting its bounded JSON sequence."""

    builder = PortableCollectionIdentityBuilder(header)
    with tempfile.TemporaryFile(mode="w+b") as snapshot:
        for ordinal, file in enumerate(files):
            builder.add(file)
            frame = PortableCollectionInventoryFile(
                ordinal=ordinal,
                file=file.to_mapping(),
            )
            snapshot.write(_json_sequence_record(frame.model_dump(mode="json")))
        if (
            builder.identity != inventory_identity
            or builder.files != file_count
            or builder.bytes != file_bytes
        ):
            raise PortableCollectionError("portable collection inventory identity differs")
        begin = PortableCollectionInventoryBegin(
            header=header,
            inventory_identity=inventory_identity,
            files=file_count,
            bytes=file_bytes,
        )
        snapshot.seek(0)
        yield _json_sequence_record(begin.model_dump(mode="json"))
        while chunk := snapshot.read(64 * 1024):
            yield chunk
        end = PortableCollectionInventoryEnd(
            files=file_count,
            bytes=file_bytes,
            inventory_identity=inventory_identity,
        )
        yield _json_sequence_record(end.model_dump(mode="json"))


class PortableCollectionInventoryReader:
    """Incrementally validate one exact portable inventory JSON sequence."""

    def __init__(self, chunks: Iterable[bytes]) -> None:
        self._records = iter_json_sequence_records(chunks)
        try:
            self.begin = PortableCollectionInventoryBegin.model_validate(next(self._records))
        except StopIteration as exc:
            raise PortableCollectionError(
                "portable collection inventory has no begin frame"
            ) from exc
        self.complete = False
        self._started = False

    def __iter__(self) -> Iterator[PortableCollectionFile]:
        if self._started:
            raise RuntimeError("portable collection inventory reader is single-use")
        self._started = True
        builder = PortableCollectionIdentityBuilder(self.begin.header)
        ordinal = 0
        for record in self._records:
            if record.get("type") == "end":
                end = PortableCollectionInventoryEnd.model_validate(record)
                if (
                    ordinal != self.begin.files
                    or builder.files != end.files
                    or builder.bytes != self.begin.bytes
                    or builder.bytes != end.bytes
                    or builder.identity != self.begin.inventory_identity
                    or builder.identity != end.inventory_identity
                ):
                    raise PortableCollectionError(
                        "portable collection inventory terminal proof differs"
                    )
                self.complete = True
                return
            frame = PortableCollectionInventoryFile.model_validate(record)
            if frame.ordinal != ordinal:
                raise PortableCollectionError("portable collection inventory ordinal differs")
            file = PortableCollectionFile.from_mapping(frame.file)
            builder.add(file)
            ordinal += 1
            yield file
        raise PortableCollectionError("portable collection inventory has no terminal frame")

    def require_complete(self) -> None:
        if not self.complete:
            raise PortableCollectionError("portable collection inventory terminal proof is absent")


__all__ = [
    "PORTABLE_COLLECTION_FORMAT",
    "PORTABLE_COLLECTION_STREAM_FORMAT",
    "PortableCollectionError",
    "PortableCollectionFile",
    "PortableCollectionHeader",
    "PortableCollectionIdentityBuilder",
    "PortableCollectionInventoryBegin",
    "PortableCollectionInventoryEnd",
    "PortableCollectionInventoryFile",
    "PortableCollectionInventoryReader",
    "PortableCollectionRecord",
    "iter_portable_collection_inventory",
    "portable_collection_inventory_identity",
    "portable_collection_json_schema",
]
