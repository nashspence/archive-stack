from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal

from riverhog_protocol.paths import normalize_collection_id, normalize_relpath, normalize_tag

PORTABLE_COLLECTION_FORMAT = "riverhog-collection/v1"
_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_PASSPHRASE_ID_RE = re.compile(r"[A-Za-z0-9_-]{16,128}")


class PortableCollectionError(ValueError):
    """The document is not the canonical portable Riverhog collection contract."""


def _sha256(value: object, label: str) -> str:
    parsed = str(value)
    if _SHA256_RE.fullmatch(parsed) is None:
        raise PortableCollectionError(f"{label} is not a SHA-256 identity")
    return parsed


def _nonnegative_int(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise PortableCollectionError(f"{label} must be a non-negative integer")
    return value


@dataclass(frozen=True, slots=True)
class PortableCollectionFile:
    path: str
    bytes: int
    sha256: str

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


@dataclass(frozen=True, slots=True)
class PortableCollectionRecord:
    collection: int
    content_identity: str
    encryption_format: str
    passphrase_id: str
    provenance_mode: Literal["captured", "mixed", "omitted"]
    provenance_identity: str | None
    metadata_revision: int
    tags: tuple[str, ...]
    files: tuple[PortableCollectionFile, ...]
    format: str = PORTABLE_COLLECTION_FORMAT

    def __post_init__(self) -> None:
        try:
            collection = normalize_collection_id(self.collection)
        except ValueError as exc:
            raise PortableCollectionError("portable collection id is invalid") from exc
        if collection != self.collection:
            raise PortableCollectionError("portable collection id is not canonical")
        _sha256(self.content_identity, "portable collection content identity")
        if not self.encryption_format or self.encryption_format.strip() != self.encryption_format:
            raise PortableCollectionError("portable collection encryption format is invalid")
        if _PASSPHRASE_ID_RE.fullmatch(self.passphrase_id) is None:
            raise PortableCollectionError("portable collection passphrase id is invalid")
        if self.provenance_mode not in {"captured", "mixed", "omitted"}:
            raise PortableCollectionError("portable collection provenance mode is invalid")
        if self.provenance_identity is not None:
            _sha256(self.provenance_identity, "portable collection provenance identity")
        if self.provenance_mode == "omitted" and self.provenance_identity is not None:
            raise PortableCollectionError("omitted provenance cannot have an identity")
        if self.provenance_mode != "omitted" and self.provenance_identity is None:
            raise PortableCollectionError("captured provenance requires an identity")
        _nonnegative_int(self.metadata_revision, "portable collection metadata revision")
        if not self.files:
            raise PortableCollectionError("portable collection files must not be empty")
        if tuple(sorted(self.files, key=lambda item: item.path)) != self.files or len(
            {item.path for item in self.files}
        ) != len(self.files):
            raise PortableCollectionError("portable collection files are not canonical")
        normalized_tags: list[str] = []
        for raw in self.tags:
            try:
                normalized = normalize_tag(raw)
            except ValueError as exc:
                raise PortableCollectionError("portable collection tag is invalid") from exc
            if normalized != raw:
                raise PortableCollectionError("portable collection tag is not canonical")
            normalized_tags.append(normalized)
        if tuple(sorted(set(normalized_tags))) != self.tags:
            raise PortableCollectionError("portable collection tags are not canonical")
        if self.format != PORTABLE_COLLECTION_FORMAT:
            raise PortableCollectionError("portable collection format is unsupported")

    @classmethod
    def create(
        cls,
        *,
        collection: int,
        content_identity: str,
        encryption_format: str,
        passphrase_id: str,
        provenance_mode: Literal["captured", "mixed", "omitted"],
        provenance_identity: str | None,
        metadata_revision: int,
        tags: Sequence[str],
        files: Iterable[tuple[str, int, str]],
    ) -> PortableCollectionRecord:
        return cls(
            collection=collection,
            content_identity=content_identity,
            encryption_format=encryption_format,
            passphrase_id=passphrase_id,
            provenance_mode=provenance_mode,
            provenance_identity=provenance_identity,
            metadata_revision=metadata_revision,
            tags=tuple(sorted(tags)),
            files=tuple(
                sorted(
                    (
                        PortableCollectionFile(path, byte_count, sha256)
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
            "metadata_revision",
            "tags",
            "files",
        }
        if not isinstance(value, Mapping) or set(value) != fields:
            raise PortableCollectionError("portable collection fields are invalid")
        raw_tags = value["tags"]
        raw_files = value["files"]
        if not isinstance(raw_tags, list) or not all(isinstance(item, str) for item in raw_tags):
            raise PortableCollectionError("portable collection tags are invalid")
        if not isinstance(raw_files, list):
            raise PortableCollectionError("portable collection files are invalid")
        mode = value["provenance_mode"]
        if mode not in {"captured", "mixed", "omitted"}:
            raise PortableCollectionError("portable collection provenance mode is invalid")
        return cls(
            format=str(value["format"]),
            collection=_nonnegative_int(value["collection"], "portable collection id"),
            content_identity=str(value["content_identity"]),
            encryption_format=str(value["encryption_format"]),
            passphrase_id=str(value["passphrase_id"]),
            provenance_mode=mode,
            provenance_identity=(
                None if value["provenance_identity"] is None else str(value["provenance_identity"])
            ),
            metadata_revision=_nonnegative_int(
                value["metadata_revision"], "portable collection metadata revision"
            ),
            tags=tuple(raw_tags),
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
            "metadata_revision": self.metadata_revision,
            "tags": list(self.tags),
            "files": [item.to_mapping() for item in self.files],
        }

    def to_json_bytes(self) -> bytes:
        return json.dumps(self.to_mapping(), sort_keys=True, separators=(",", ":")).encode()

    @property
    def identity(self) -> str:
        return hashlib.sha256(self.to_json_bytes()).hexdigest()


__all__ = [
    "PORTABLE_COLLECTION_FORMAT",
    "PortableCollectionError",
    "PortableCollectionFile",
    "PortableCollectionRecord",
]
