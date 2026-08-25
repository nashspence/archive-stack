"""Portable media-metadata observation contract and authoring models."""

from __future__ import annotations

from typing import Final, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator
from stove0_observer_protocol import (
    JsonSchemaDocument,
    ObserverContract,
    ObserverContractPayload,
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
        return self


class MediaMetadataFacts(MediaObservationModel):
    artifacts: tuple[MediaArtifactFacts, ...] = Field(min_length=1)


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
