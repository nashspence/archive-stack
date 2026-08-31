from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Literal

from http_api_contracts import canonical_json_bytes
from pydantic import BaseModel, ConfigDict, Field, model_validator

from riverhog_protocol.file_identity import ImmutableFileIdentityDocument
from riverhog_protocol.paths import (
    CollectionId,
    normalize_relpath,
)

PORTABLE_COLLECTION_FORMAT: Literal["riverhog-collection/v1"] = "riverhog-collection/v1"
PORTABLE_COLLECTION_INVENTORY_PAGE_FORMAT: Literal["riverhog-collection-inventory-page/v1"] = (
    "riverhog-collection-inventory-page/v1"
)
_SHA256_RE = re.compile(r"[0-9a-f]{64}")


class PortableCollectionError(ValueError):
    """The document is not the canonical portable Riverhog collection contract."""


def _sha256(value: object, label: str) -> str:
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        raise PortableCollectionError(f"{label} is not a SHA-256 identity")
    return value


def _nonnegative_int(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise PortableCollectionError(f"{label} must be a non-negative integer")
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


class PortableCollectionInventoryAuthority(BaseModel):
    """The immutable authority shared by every bounded inventory page."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    header: PortableCollectionHeader
    inventory_identity: str = Field(pattern=r"^[0-9a-f]{64}$")
    file_count: int = Field(ge=1)
    file_bytes: int = Field(ge=0)


class PortableCollectionInventoryPage(BaseModel):
    """One bounded, canonically ordered slice of an immutable inventory."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    format: Literal["riverhog-collection-inventory-page/v1"] = (
        PORTABLE_COLLECTION_INVENTORY_PAGE_FORMAT
    )
    authority: PortableCollectionInventoryAuthority
    files: list[ImmutableFileIdentityDocument] = Field(
        max_length=1000,
        json_schema_extra={
            "x-riverhog-extent": {
                "policy": "segmented_no_total_max",
                "reason": "bounded-portable-inventory-page",
                "progression": "authority-bound-cursor",
            }
        },
    )
    next_cursor: str | None = Field(default=None, min_length=1, max_length=8192)
    complete: bool

    @model_validator(mode="after")
    def validate_page(self) -> PortableCollectionInventoryPage:
        paths = tuple(file.path for file in self.files)
        if paths != tuple(sorted(set(paths), key=lambda value: value.encode("utf-8"))):
            raise ValueError("portable inventory page files are not canonical")
        if self.complete != (self.next_cursor is None):
            raise ValueError("portable inventory page continuation is inconsistent")
        return self


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


__all__ = [
    "PORTABLE_COLLECTION_FORMAT",
    "PORTABLE_COLLECTION_INVENTORY_PAGE_FORMAT",
    "PortableCollectionError",
    "PortableCollectionFile",
    "PortableCollectionHeader",
    "PortableCollectionInventoryAuthority",
    "PortableCollectionIdentityBuilder",
    "PortableCollectionInventoryPage",
    "portable_collection_inventory_identity",
]
