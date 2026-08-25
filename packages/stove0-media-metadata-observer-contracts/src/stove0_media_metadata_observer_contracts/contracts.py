"""Portable media-metadata observation contract and authoring models."""

from __future__ import annotations

from typing import Final, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, JsonValue, field_validator, model_validator
from stove0_observer_protocol import (
    JsonSchemaDocument,
    ObserverContract,
    ObserverContractPayload,
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
    artifact_id: str = Field(min_length=1, max_length=160)
    state: MediaArtifactState
    facts: tuple[MediaMetadataFact, ...] = ()

    @model_validator(mode="after")
    def valid_state(self) -> Self:
        if self.state == "unsupported" and self.facts:
            raise ValueError("unsupported media artifacts cannot report metadata facts")
        keys = [
            (
                fact.name,
                fact.evidence.artifact_id,
                fact.evidence.field,
                canonical_json_bytes(fact.value),
            )
            for fact in self.facts
        ]
        if keys != sorted(keys) or len(keys) != len(set(keys)):
            raise ValueError("media metadata facts must be unique and canonically ordered")
        if any(fact.evidence.artifact_id != self.artifact_id for fact in self.facts):
            raise ValueError("media metadata facts must bind their exact observed artifact")
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
            raise ValueError("media artifact facts must be unique and ordered by artifact ID")
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

MEDIA_METADATA_OBSERVER_CONTRACT = ObserverContract.seal(
    ObserverContractPayload(
        id=MEDIA_METADATA_OBSERVATION_ID,
        options_schema=MEDIA_METADATA_OPTIONS_SCHEMA,
        facts_schema=MEDIA_METADATA_FACTS_SCHEMA,
        maximum_result_bytes=1024 * 1024,
    )
)


__all__ = [
    "MEDIA_METADATA_FACTS_SCHEMA",
    "MEDIA_METADATA_FACTS_SCHEMA_ID",
    "MEDIA_METADATA_OBSERVATION_ID",
    "MEDIA_METADATA_OBSERVER_CONTRACT",
    "MEDIA_METADATA_OPTIONS_SCHEMA",
    "MEDIA_METADATA_OPTIONS_SCHEMA_ID",
    "MediaArtifactFacts",
    "MediaArtifactState",
    "MediaFactEvidence",
    "MediaFactName",
    "MediaMetadataFact",
    "MediaMetadataFacts",
]
