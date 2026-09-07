"""One bounded mutable human description for a Riverhog collection."""

from __future__ import annotations

import hashlib
import json
import unicodedata
from typing import Annotated, Any, Literal, Self

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    model_validator,
)

COLLECTION_DESCRIPTION_DOCUMENT_FORMAT: Literal["riverhog-collection-description/v1"] = (
    "riverhog-collection-description/v1"
)
COLLECTION_DESCRIPTION_RELATIVE_PATH = "description.json.age"
COLLECTION_DESCRIPTION_UTF8_BYTES_MAX = 32 * 1024
# A JSON integer which every maintained client runtime can represent exactly. The bound is
# representational rather than a product policy limiting collection size or membership.
MAX_COLLECTION_DESCRIPTION_REVISION = 9_007_199_254_740_991
_DESCRIPTION_IDENTITY_DOMAIN = b"riverhog-collection-description-state/v1\x00"
_SHA256_PATTERN = r"^[0-9a-f]{64}$"
_UNICODE_WHITESPACE = frozenset(
    "\u0009\u000a\u000b\u000c\u000d\u0020\u0085\u00a0\u1680"
    "\u2000\u2001\u2002\u2003\u2004\u2005\u2006\u2007\u2008"
    "\u2009\u200a\u2028\u2029\u202f\u205f\u3000"
)


def validate_collection_description(value: str) -> str:
    """Return one exact NFC prose value or reject it without repair."""

    if type(value) is not str:
        raise ValueError("collection description must be a string")
    if not value or all(character in _UNICODE_WHITESPACE for character in value):
        raise ValueError("collection description must contain visible text")
    if unicodedata.normalize("NFC", value) != value:
        raise ValueError("collection description must use NFC normalization")
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ValueError("collection description must contain Unicode scalar values") from exc
    if len(encoded) > COLLECTION_DESCRIPTION_UTF8_BYTES_MAX:
        raise ValueError("collection description exceeds its UTF-8 byte limit")
    for character in value:
        codepoint = ord(character)
        if codepoint <= 0x08 or 0x0B <= codepoint <= 0x1F or 0x7F <= codepoint <= 0x9F:
            raise ValueError("collection description contains a control character")
    return value


type CollectionDescription = Annotated[
    str,
    StringConstraints(strict=True, min_length=1, max_length=COLLECTION_DESCRIPTION_UTF8_BYTES_MAX),
    AfterValidator(validate_collection_description),
    Field(
        json_schema_extra={
            "x-riverhog-encoded-bytes-max": COLLECTION_DESCRIPTION_UTF8_BYTES_MAX,
            "x-riverhog-extent": {
                "policy": "contract_max",
                "reason": "bounded-human-authored-catalog-description",
            },
            "x-unicode-normalization": "NFC",
        }
    ),
]


def _description_authority_bytes(
    *,
    archive_root_sha256: str,
    revision: int,
    description: str | None,
) -> bytes:
    return json.dumps(
        {
            "archive_root_sha256": archive_root_sha256,
            "description": description,
            "format": COLLECTION_DESCRIPTION_DOCUMENT_FORMAT,
            "revision": revision,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def collection_description_identity(
    *,
    archive_root_sha256: str,
    revision: int,
    description: str | None,
) -> str:
    """Return the exact, revision-sensitive identity of one description state."""

    if len(archive_root_sha256) != 64 or any(
        character not in "0123456789abcdef" for character in archive_root_sha256
    ):
        raise ValueError("archive root sha256 is invalid")
    if (
        not isinstance(revision, int)
        or isinstance(revision, bool)
        or not 0 <= revision <= MAX_COLLECTION_DESCRIPTION_REVISION
    ):
        raise ValueError("collection description revision is invalid")
    if description is not None:
        validate_collection_description(description)
    return hashlib.sha256(
        _DESCRIPTION_IDENTITY_DOMAIN
        + _description_authority_bytes(
            archive_root_sha256=archive_root_sha256,
            revision=revision,
            description=description,
        )
    ).hexdigest()


class CollectionDescriptionDocument(BaseModel):
    """Canonical independently recoverable description state beside an archive copy."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    format: Literal["riverhog-collection-description/v1"] = COLLECTION_DESCRIPTION_DOCUMENT_FORMAT
    archive_root_sha256: str = Field(pattern=_SHA256_PATTERN)
    revision: int = Field(
        ge=1,
        le=MAX_COLLECTION_DESCRIPTION_REVISION,
        strict=True,
        json_schema_extra={
            "x-riverhog-extent": {
                "policy": "fixed",
                "reason": "exact-json-safe-monotonic-description-revision",
            }
        },
    )
    description: CollectionDescription | None
    description_identity: str = Field(pattern=_SHA256_PATTERN)

    @model_validator(mode="after")
    def validate_identity(self) -> Self:
        expected = collection_description_identity(
            archive_root_sha256=self.archive_root_sha256,
            revision=self.revision,
            description=self.description,
        )
        if self.description_identity != expected:
            raise ValueError("collection description identity does not match its state")
        return self

    @classmethod
    def seal(
        cls,
        *,
        archive_root_sha256: str,
        revision: int,
        description: str | None,
    ) -> CollectionDescriptionDocument:
        return cls(
            archive_root_sha256=archive_root_sha256,
            revision=revision,
            description=description,
            description_identity=collection_description_identity(
                archive_root_sha256=archive_root_sha256,
                revision=revision,
                description=description,
            ),
        )

    def to_json_bytes(self) -> bytes:
        return json.dumps(
            self.model_dump(mode="json"),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")

    @classmethod
    def from_json_bytes(cls, content: bytes | str) -> CollectionDescriptionDocument:
        try:
            encoded = content if isinstance(content, bytes) else content.encode("utf-8")
            value: Any = json.loads(encoded)
            document = cls.model_validate(value)
        except (UnicodeError, json.JSONDecodeError, ValueError) as exc:
            raise ValueError("collection description document is invalid") from exc
        if document.to_json_bytes() != encoded:
            raise ValueError("collection description document is not canonical")
        return document


__all__ = [
    "COLLECTION_DESCRIPTION_DOCUMENT_FORMAT",
    "COLLECTION_DESCRIPTION_RELATIVE_PATH",
    "COLLECTION_DESCRIPTION_UTF8_BYTES_MAX",
    "MAX_COLLECTION_DESCRIPTION_REVISION",
    "CollectionDescription",
    "CollectionDescriptionDocument",
    "collection_description_identity",
    "validate_collection_description",
]
