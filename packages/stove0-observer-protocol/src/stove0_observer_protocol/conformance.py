"""Portable, language-neutral semantic conformance-vector documents."""

from __future__ import annotations

from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, JsonValue, field_validator, model_validator
from stove0_protocol.models import ArtifactSubject, SemanticId, canonical_json_sha256


class SemanticConformanceModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class SemanticFactsConformanceVector(SemanticConformanceModel):
    id: SemanticId
    accepted: bool
    subjects: tuple[ArtifactSubject, ...] = Field(min_length=1)
    options: dict[str, JsonValue] = Field(default_factory=dict)
    facts: dict[str, JsonValue]

    @field_validator("subjects")
    @classmethod
    def canonical_subjects(
        cls,
        value: tuple[ArtifactSubject, ...],
    ) -> tuple[ArtifactSubject, ...]:
        ids = [subject.id for subject in value]
        if ids != sorted(ids) or len(ids) != len(set(ids)):
            raise ValueError("semantic vector subjects must be unique and ordered")
        return value


class SemanticFactsConformanceVectors(SemanticConformanceModel):
    format: Literal["stove0-semantic-facts-conformance/v1"] = "stove0-semantic-facts-conformance/v1"
    profile_id: SemanticId
    vectors: tuple[SemanticFactsConformanceVector, ...] = Field(min_length=2)

    @field_validator("vectors")
    @classmethod
    def canonical_vectors(
        cls,
        value: tuple[SemanticFactsConformanceVector, ...],
    ) -> tuple[SemanticFactsConformanceVector, ...]:
        ids = [vector.id for vector in value]
        if ids != sorted(ids) or len(ids) != len(set(ids)):
            raise ValueError("semantic conformance vectors must be unique and ordered")
        return value

    @model_validator(mode="after")
    def covers_acceptance_and_rejection(self) -> Self:
        outcomes = {vector.accepted for vector in self.vectors}
        if outcomes != {False, True}:
            raise ValueError("semantic conformance vectors require accepted and rejected cases")
        return self

    @property
    def sha256(self) -> str:
        return canonical_json_sha256(self.model_dump(mode="json"))


__all__ = ["SemanticFactsConformanceVector", "SemanticFactsConformanceVectors"]
