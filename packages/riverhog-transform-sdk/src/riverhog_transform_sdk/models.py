"""Immutable public values for the collection transform data plane."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from riverhog_protocol.collection_workflows import (
    CollectionDerivation,
    CollectionRootIdentity,
    OperationIdentity,
    RecipeIdentity,
)
from riverhog_protocol.paths import normalize_relpath, normalize_tag


def _sha256(value: str, label: str) -> str:
    digest = value.casefold()
    if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
        raise ValueError(f"{label} must be a lowercase SHA-256")
    return digest


@dataclass(frozen=True, slots=True)
class DerivedCollectionSpec:
    """Controller-sealed authorities needed to create one derived collection.

    The data-plane SDK deliberately does not depend on stove0 core or on a
    workflow-specific intent model. It receives only exact immutable input roots,
    opaque recipe/operation identities, and the authorized output tags.
    """

    inputs: tuple[CollectionRootIdentity, ...]
    recipe: RecipeIdentity
    operation: OperationIdentity
    output_tags: tuple[str, ...]

    def __post_init__(self) -> None:
        normalized_inputs = tuple(sorted(self.inputs))
        if not normalized_inputs or normalized_inputs != self.inputs:
            raise ValueError("derived collection inputs must be nonempty and canonical")
        if len({item.collection_id for item in normalized_inputs}) != len(normalized_inputs):
            raise ValueError("derived collection inputs must be unique")
        tags = tuple(sorted(normalize_tag(item) for item in self.output_tags))
        if not tags or tags != self.output_tags or len(tags) != len(set(tags)):
            raise ValueError("derived collection output tags must be unique and canonical")


@dataclass(frozen=True, order=True, slots=True)
class ClaimedArtifact:
    """One immutable logical file authorized by a transform claim."""

    root: CollectionRootIdentity
    path: str
    bytes: int
    sha256: str
    control: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "path", normalize_relpath(self.path))
        if isinstance(self.bytes, bool) or self.bytes < 0:
            raise ValueError("claimed artifact byte count must be non-negative")
        object.__setattr__(self, "sha256", _sha256(self.sha256, "claimed artifact identity"))

    @property
    def key(self) -> tuple[int, str]:
        return self.root.collection_id, self.path

    def as_dict(self) -> dict[str, object]:
        return {
            "collection": self.root.as_dict(),
            "path": self.path,
            "bytes": self.bytes,
            "sha256": self.sha256,
            "control": self.control,
        }


@dataclass(frozen=True, slots=True)
class DerivedCollectionReceipt:
    """Finalized output identity returned by a transform target."""

    collection_id: int
    manifest_sha256: str
    content_etag: str
    derivation: CollectionDerivation

    def __post_init__(self) -> None:
        if isinstance(self.collection_id, bool) or self.collection_id < 1:
            raise ValueError("derived collection id must be positive")
        object.__setattr__(
            self,
            "manifest_sha256",
            _sha256(self.manifest_sha256, "derived collection manifest identity"),
        )
        object.__setattr__(
            self,
            "content_etag",
            _sha256(self.content_etag, "derived collection content identity"),
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "collection_id": self.collection_id,
            "manifest_sha256": self.manifest_sha256,
            "content_etag": self.content_etag,
            "derivation": self.derivation.as_dict(),
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> DerivedCollectionReceipt:
        if set(value) != {
            "collection_id",
            "manifest_sha256",
            "content_etag",
            "derivation",
        }:
            raise ValueError("derived collection receipt fields are invalid")
        derivation = value.get("derivation")
        if not isinstance(derivation, Mapping):
            raise ValueError("derived collection receipt has no derivation")
        collection_id = value.get("collection_id")
        if isinstance(collection_id, bool) or not isinstance(collection_id, int):
            raise ValueError("derived collection receipt id is invalid")
        return cls(
            collection_id=collection_id,
            manifest_sha256=str(value.get("manifest_sha256") or ""),
            content_etag=str(value.get("content_etag") or ""),
            derivation=CollectionDerivation.from_mapping(derivation),
        )


__all__ = ["ClaimedArtifact", "DerivedCollectionReceipt", "DerivedCollectionSpec"]
