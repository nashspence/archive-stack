"""Portable, language-neutral target-intent conformance vectors."""

from __future__ import annotations

from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, JsonValue, field_validator, model_validator
from stove0_protocol.models import SemanticId, canonical_json_sha256


class SemanticConformanceModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class SemanticIntentConformanceVector(SemanticConformanceModel):
    id: SemanticId
    accepted: bool
    intent: dict[str, JsonValue]


class SemanticIntentConformanceVectors(SemanticConformanceModel):
    format: Literal["stove0-semantic-intent-conformance/v1"] = (
        "stove0-semantic-intent-conformance/v1"
    )
    profile_id: SemanticId
    vectors: tuple[SemanticIntentConformanceVector, ...] = Field(min_length=2)

    @field_validator("vectors")
    @classmethod
    def canonical_vectors(
        cls,
        value: tuple[SemanticIntentConformanceVector, ...],
    ) -> tuple[SemanticIntentConformanceVector, ...]:
        ids = [vector.id for vector in value]
        if ids != sorted(ids) or len(ids) != len(set(ids)):
            raise ValueError("semantic conformance vectors must be unique and ordered")
        return value

    @model_validator(mode="after")
    def covers_acceptance_and_rejection(self) -> Self:
        if {vector.accepted for vector in self.vectors} != {False, True}:
            raise ValueError("semantic conformance vectors require accepted and rejected cases")
        return self

    @property
    def sha256(self) -> str:
        return canonical_json_sha256(self.model_dump(mode="json"))


__all__ = ["SemanticIntentConformanceVector", "SemanticIntentConformanceVectors"]
