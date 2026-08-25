"""Canonical Riverhog retrieval request documents."""

from __future__ import annotations

from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from riverhog_protocol.paths import CanonicalRelPath
from riverhog_protocol.transport import RETRIEVAL_FILE_BATCH_MAX


class RetrievalTransportDocument(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class RetrievalFileReferenceDocument(RetrievalTransportDocument):
    collection_id: int = Field(ge=1)
    path: CanonicalRelPath


class RetrievalFileReferenceSetDocument(RetrievalTransportDocument):
    files: list[RetrievalFileReferenceDocument] = Field(
        min_length=1,
        max_length=RETRIEVAL_FILE_BATCH_MAX,
    )

    @model_validator(mode="after")
    def validate_exact_reference_set(self) -> Self:
        identities = [(item.collection_id, item.path) for item in self.files]
        if len(identities) != len(set(identities)):
            raise ValueError("retrieval file references must be unique")
        canonical = sorted(identities, key=lambda item: (item[0], item[1].encode("utf-8")))
        if identities != canonical:
            raise ValueError("retrieval file references must be in canonical order")
        return self


__all__ = [
    "RetrievalFileReferenceDocument",
    "RetrievalFileReferenceSetDocument",
]
