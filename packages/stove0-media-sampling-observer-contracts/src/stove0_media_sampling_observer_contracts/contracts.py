"""Observer-owned media-sampling wire contract and fact models."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from importlib.resources import files
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from stove0_observer_protocol import (
    ArtifactSubject,
    JsonSchemaDocument,
    ObservationRequest,
    ObserverContract,
    ObserverContractPayload,
    SemanticFactsConformanceVectors,
    SemanticValidationProfile,
    SemanticValidationProfilePayload,
    SemanticValidatorBinding,
)

MEDIA_SAMPLING_OBSERVATION_ID = "stove0.review.media-sampling/v1"
MEDIA_SAMPLING_OPTIONS_SCHEMA_ID = "stove0.review.media-sampling-options/v1"
MEDIA_SAMPLING_FACTS_SCHEMA_ID = "stove0.review.media-sampling-facts/v1"


class MediaSamplingModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class SampleableRange(MediaSamplingModel):
    start_ms: int = Field(ge=0)
    duration_ms: int = Field(ge=1)


class MediaSamplingArtifactFacts(MediaSamplingModel):
    artifact_id: str = Field(min_length=1, max_length=160)
    duration_ms: int = Field(ge=1)
    sampleable_ranges: tuple[SampleableRange, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_ranges(self) -> Self:
        ordered = tuple(sorted(self.sampleable_ranges, key=lambda item: item.start_ms))
        if ordered != self.sampleable_ranges:
            raise ValueError("sampleable ranges must be ordered by start")
        previous_end = 0
        for current in self.sampleable_ranges:
            if current.start_ms < previous_end:
                raise ValueError("sampleable ranges must not overlap")
            if current.start_ms + current.duration_ms > self.duration_ms:
                raise ValueError("sampleable range exceeds artifact duration")
            previous_end = current.start_ms + current.duration_ms
        return self


class MediaSamplingFacts(MediaSamplingModel):
    artifacts: tuple[MediaSamplingArtifactFacts, ...] = Field(min_length=1)

    @field_validator("artifacts")
    @classmethod
    def canonical_artifacts(
        cls, value: tuple[MediaSamplingArtifactFacts, ...]
    ) -> tuple[MediaSamplingArtifactFacts, ...]:
        ids = [item.artifact_id for item in value]
        if ids != sorted(ids) or len(ids) != len(set(ids)):
            raise ValueError("sampling facts must be unique and ordered by artifact ID")
        return value


MEDIA_SAMPLING_OPTIONS_SCHEMA = JsonSchemaDocument.from_schema(
    MEDIA_SAMPLING_OPTIONS_SCHEMA_ID,
    {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "properties": {},
        "additionalProperties": False,
    },
)

MEDIA_SAMPLING_FACTS_SCHEMA = JsonSchemaDocument.from_schema(
    MEDIA_SAMPLING_FACTS_SCHEMA_ID,
    {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "required": ["artifacts"],
        "properties": {
            "artifacts": {
                "type": "array",
                "minItems": 1,
                "items": {
                    "type": "object",
                    "required": ["artifact_id", "duration_ms", "sampleable_ranges"],
                    "properties": {
                        "artifact_id": {"type": "string", "minLength": 1},
                        "duration_ms": {"type": "integer", "minimum": 1},
                        "sampleable_ranges": {
                            "type": "array",
                            "minItems": 1,
                            "items": {
                                "type": "object",
                                "required": ["start_ms", "duration_ms"],
                                "properties": {
                                    "start_ms": {"type": "integer", "minimum": 0},
                                    "duration_ms": {"type": "integer", "minimum": 1},
                                },
                                "additionalProperties": False,
                            },
                        },
                    },
                    "additionalProperties": False,
                },
            },
        },
        "additionalProperties": False,
    },
)

MEDIA_SAMPLING_FACTS_CONFORMANCE_VECTORS = SemanticFactsConformanceVectors.model_validate_json(
    files("stove0_media_sampling_observer_contracts")
    .joinpath("vectors/facts-semantics-v1.json")
    .read_text(encoding="utf-8")
)

MEDIA_SAMPLING_FACTS_SEMANTICS = SemanticValidationProfile.seal(
    SemanticValidationProfilePayload(
        id="stove0.review.media-sampling-facts-semantics/v1",
        rules=(
            "stove0.review.media-sampling-facts.complete-request-subjects/v1",
            "stove0.review.media-sampling-facts.nonoverlapping-bounded-ranges/v1",
        ),
        conformance_vectors_sha256=MEDIA_SAMPLING_FACTS_CONFORMANCE_VECTORS.sha256,
    )
)


def validate_media_sampling_facts(
    facts: Mapping[str, object],
    subjects: Sequence[ArtifactSubject],
) -> MediaSamplingFacts:
    """Apply the exact semantic profile advertised by the observer contract."""

    document = MediaSamplingFacts.model_validate(dict(facts))
    if tuple(item.artifact_id for item in document.artifacts) != tuple(
        item.id for item in subjects
    ):
        raise ValueError("sampling facts must cover the exact request subjects")
    return document


def validate_media_sampling_observation(
    request: ObservationRequest,
    facts: Mapping[str, object],
) -> None:
    """Execute the advertised profile through the generic consumer signature."""

    validate_media_sampling_facts(facts, request.subjects)


MEDIA_SAMPLING_SEMANTIC_VALIDATOR = SemanticValidatorBinding.from_profile(
    MEDIA_SAMPLING_FACTS_SEMANTICS,
    validate_media_sampling_observation,
)


MEDIA_SAMPLING_OBSERVER_CONTRACT = ObserverContract.seal(
    ObserverContractPayload(
        id=MEDIA_SAMPLING_OBSERVATION_ID,
        options_schema=MEDIA_SAMPLING_OPTIONS_SCHEMA,
        facts_schema=MEDIA_SAMPLING_FACTS_SCHEMA,
        facts_semantics=MEDIA_SAMPLING_FACTS_SEMANTICS,
        maximum_result_bytes=256 * 1024,
    )
)

__all__ = [
    "MEDIA_SAMPLING_FACTS_SCHEMA",
    "MEDIA_SAMPLING_FACTS_SCHEMA_ID",
    "MEDIA_SAMPLING_FACTS_SEMANTICS",
    "MEDIA_SAMPLING_FACTS_CONFORMANCE_VECTORS",
    "MEDIA_SAMPLING_OBSERVATION_ID",
    "MEDIA_SAMPLING_OBSERVER_CONTRACT",
    "MEDIA_SAMPLING_OPTIONS_SCHEMA",
    "MEDIA_SAMPLING_OPTIONS_SCHEMA_ID",
    "MEDIA_SAMPLING_SEMANTIC_VALIDATOR",
    "MediaSamplingArtifactFacts",
    "MediaSamplingFacts",
    "SampleableRange",
    "validate_media_sampling_facts",
    "validate_media_sampling_observation",
]
