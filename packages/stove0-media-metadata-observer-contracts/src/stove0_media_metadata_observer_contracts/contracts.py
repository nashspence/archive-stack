"""Portable media-metadata observation contract and authoring models."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from importlib.resources import files
from typing import Final, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, JsonValue, field_validator, model_validator
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
    canonical_json_bytes,
)

MEDIA_METADATA_OBSERVATION_ID: Final = "stove0.media.metadata/v1"
MEDIA_METADATA_OPTIONS_SCHEMA_ID: Final = "stove0.media.metadata-options/v1"
MEDIA_METADATA_FACTS_SCHEMA_ID: Final = "stove0.media.metadata-facts/v1"

MediaFactName = Literal[
    "capture-time",
    "container-format",
    "creator",
    "device-make",
    "device-model",
    "gps-latitude",
    "gps-longitude",
]
MediaArtifactState = Literal["observed", "unsupported"]


class MediaObservationModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class MediaFactEvidence(MediaObservationModel):
    """Exact artifact and ExifTool field from which one value was read."""

    artifact_id: str = Field(min_length=1, max_length=160)
    field: str = Field(min_length=1, max_length=240)


class MediaMetadataFact(MediaObservationModel):
    name: MediaFactName
    value: JsonValue
    evidence: MediaFactEvidence


class MediaArtifactFacts(MediaObservationModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        json_schema_extra={
            "allOf": [
                {
                    "if": {
                        "properties": {"state": {"const": "unsupported"}},
                        "required": ["state"],
                    },
                    "then": {"properties": {"facts": {"maxItems": 0}}},
                }
            ]
        },
    )
    artifact_id: str = Field(min_length=1, max_length=160)
    state: MediaArtifactState
    facts: tuple[MediaMetadataFact, ...] = ()

    @model_validator(mode="after")
    def valid_state(self) -> Self:
        if self.state == "unsupported" and self.facts:
            raise ValueError("unsupported media artifacts cannot report metadata facts")
        if any(item.evidence.artifact_id != self.artifact_id for item in self.facts):
            raise ValueError("media fact evidence must bind its containing artifact")
        ordered = tuple(
            sorted(
                self.facts,
                key=lambda item: (
                    item.name,
                    item.evidence.field,
                    canonical_json_bytes(item.value),
                ),
            )
        )
        identities = {
            (item.name, item.evidence.field, canonical_json_bytes(item.value))
            for item in self.facts
        }
        if self.facts != ordered or len(identities) != len(self.facts):
            raise ValueError("media facts must be unique and canonically ordered")
        return self


class MediaMetadataFacts(MediaObservationModel):
    artifacts: tuple[MediaArtifactFacts, ...] = Field(min_length=1)

    @field_validator("artifacts")
    @classmethod
    def canonical_artifacts(
        cls,
        value: tuple[MediaArtifactFacts, ...],
    ) -> tuple[MediaArtifactFacts, ...]:
        ids = [item.artifact_id for item in value]
        if ids != sorted(ids) or len(ids) != len(set(ids)):
            raise ValueError("media artifact facts must be unique and ordered")
        return value


MEDIA_METADATA_OPTIONS_SCHEMA = JsonSchemaDocument.from_schema(
    MEDIA_METADATA_OPTIONS_SCHEMA_ID,
    {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "properties": {},
        "additionalProperties": False,
    },
)

MEDIA_METADATA_FACTS_SCHEMA = JsonSchemaDocument.from_schema(
    MEDIA_METADATA_FACTS_SCHEMA_ID,
    MediaMetadataFacts.model_json_schema(),
)

MEDIA_METADATA_FACTS_CONFORMANCE_VECTORS = SemanticFactsConformanceVectors.model_validate_json(
    files("stove0_media_metadata_observer_contracts")
    .joinpath("vectors/facts-semantics-v1.json")
    .read_text(encoding="utf-8")
)

MEDIA_METADATA_FACTS_SEMANTICS = SemanticValidationProfile.seal(
    SemanticValidationProfilePayload(
        id="stove0.media.metadata-facts-semantics/v1",
        rules=(
            "stove0.media.metadata-facts.canonical-unique-evidence/v1",
            "stove0.media.metadata-facts.complete-request-subjects/v1",
            "stove0.media.metadata-facts.evidence-artifact-binding/v1",
        ),
        conformance_vectors_sha256=MEDIA_METADATA_FACTS_CONFORMANCE_VECTORS.sha256,
    )
)


def validate_media_metadata_facts(
    facts: Mapping[str, object],
    subjects: Sequence[ArtifactSubject],
) -> MediaMetadataFacts:
    """Apply the exact semantic profile advertised by the observer contract."""

    document = MediaMetadataFacts.model_validate(dict(facts))
    if tuple(item.artifact_id for item in document.artifacts) != tuple(
        item.id for item in subjects
    ):
        raise ValueError("media metadata facts must cover the exact request subjects")
    return document


def validate_media_metadata_observation(
    request: ObservationRequest,
    facts: Mapping[str, object],
) -> None:
    """Execute the advertised profile through the generic consumer signature."""

    validate_media_metadata_facts(facts, request.subjects)


MEDIA_METADATA_SEMANTIC_VALIDATOR = SemanticValidatorBinding.from_profile(
    MEDIA_METADATA_FACTS_SEMANTICS,
    validate_media_metadata_observation,
)


MEDIA_METADATA_OBSERVER_CONTRACT = ObserverContract.seal(
    ObserverContractPayload(
        id=MEDIA_METADATA_OBSERVATION_ID,
        options_schema=MEDIA_METADATA_OPTIONS_SCHEMA,
        facts_schema=MEDIA_METADATA_FACTS_SCHEMA,
        facts_semantics=MEDIA_METADATA_FACTS_SEMANTICS,
        maximum_result_bytes=1024 * 1024,
    )
)


__all__ = [
    "MEDIA_METADATA_FACTS_SCHEMA",
    "MEDIA_METADATA_FACTS_SCHEMA_ID",
    "MEDIA_METADATA_FACTS_SEMANTICS",
    "MEDIA_METADATA_FACTS_CONFORMANCE_VECTORS",
    "MEDIA_METADATA_OBSERVATION_ID",
    "MEDIA_METADATA_OBSERVER_CONTRACT",
    "MEDIA_METADATA_OPTIONS_SCHEMA",
    "MEDIA_METADATA_OPTIONS_SCHEMA_ID",
    "MEDIA_METADATA_SEMANTIC_VALIDATOR",
    "MediaArtifactFacts",
    "MediaArtifactState",
    "MediaFactEvidence",
    "MediaFactName",
    "MediaMetadataFact",
    "MediaMetadataFacts",
    "validate_media_metadata_facts",
    "validate_media_metadata_observation",
]
