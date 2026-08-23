"""Maintained, target-independent media operation contracts."""

from __future__ import annotations

from typing import Final, Literal

from pydantic import BaseModel, ConfigDict, Field
from stove0_protocol import JsonSchemaDocument
from stove0_target_protocol import (
    InputArtifactContract,
    OperationContract,
    OperationContractPayload,
    OutputArtifactContract,
)

SOURCE_ROLE: Final = "stove0.media.source/v1"
AUDIO_ARCHIVE_ROLE: Final = "stove0.media.audio-archive/v1"
AV1_OPUS_ARCHIVE_ROLE: Final = "stove0.media.av1-opus-archive/v1"
SOURCE_ARTIFACT_ROLE: Final = "stove0.media.source-artifact/v1"

AUDIO_ARCHIVE_OPERATION_ID: Final = "stove0.media.audio-archive/v1"
AV1_OPUS_ARCHIVE_OPERATION_ID: Final = "stove0.media.av1-opus-archive/v1"


class IntentModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class AudioArchiveIntent(IntentModel):
    codec: Literal["opus"] = "opus"
    container: Literal["opus"] = "opus"
    bitrate_kbps: int = Field(default=128, ge=16, le=512)


class Av1OpusArchiveIntent(IntentModel):
    codec: Literal["av1"] = "av1"
    container: Literal["mkv"] = "mkv"
    quality: int = Field(default=23, ge=0, le=63)
    max_height: int | None = Field(default=None, ge=144, le=8640)
    audio_bitrate_kbps: int = Field(default=128, ge=16, le=512)
    salvage: Literal["off", "safe-remux"] = "safe-remux"


def _schema(identifier: str, model: type[IntentModel]) -> JsonSchemaDocument:
    return JsonSchemaDocument.from_schema(identifier, model.model_json_schema())


AUDIO_ARCHIVE_OPERATION = OperationContract.seal(
    OperationContractPayload(
        id=AUDIO_ARCHIVE_OPERATION_ID,
        intent_schema=_schema("stove0.media.audio-archive-intent/v1", AudioArchiveIntent),
        inputs=(
            InputArtifactContract(
                role=SOURCE_ROLE,
                minimum=1,
                allowed_dispositions=("transformed",),
            ),
        ),
        outputs=(
            OutputArtifactContract(
                role=AUDIO_ARCHIVE_ROLE,
                minimum=1,
                derived_from_roles=(SOURCE_ROLE,),
            ),
        ),
        source_retirement_permitted=False,
    )
)

AV1_OPUS_ARCHIVE_OPERATION = OperationContract.seal(
    OperationContractPayload(
        id=AV1_OPUS_ARCHIVE_OPERATION_ID,
        intent_schema=_schema("stove0.media.av1-opus-archive-intent/v1", Av1OpusArchiveIntent),
        inputs=(
            InputArtifactContract(
                role=SOURCE_ROLE,
                minimum=1,
                allowed_dispositions=("transformed",),
            ),
        ),
        outputs=(
            OutputArtifactContract(
                role=AV1_OPUS_ARCHIVE_ROLE,
                minimum=1,
                derived_from_roles=(SOURCE_ROLE,),
            ),
            OutputArtifactContract(
                role=SOURCE_ARTIFACT_ROLE,
                minimum=1,
                derived_from_roles=(SOURCE_ROLE,),
            ),
        ),
        source_retirement_permitted=True,
    )
)

OPERATIONS: Final = {
    operation.id: operation for operation in (AUDIO_ARCHIVE_OPERATION, AV1_OPUS_ARCHIVE_OPERATION)
}


def operation_contract(operation_id: str) -> OperationContract:
    try:
        return OPERATIONS[operation_id]
    except KeyError as exc:
        raise ValueError(f"unknown maintained media operation: {operation_id}") from exc


__all__ = [
    "AUDIO_ARCHIVE_OPERATION",
    "AUDIO_ARCHIVE_OPERATION_ID",
    "AUDIO_ARCHIVE_ROLE",
    "AudioArchiveIntent",
    "OPERATIONS",
    "SOURCE_ARTIFACT_ROLE",
    "SOURCE_ROLE",
    "AV1_OPUS_ARCHIVE_OPERATION",
    "AV1_OPUS_ARCHIVE_OPERATION_ID",
    "AV1_OPUS_ARCHIVE_ROLE",
    "Av1OpusArchiveIntent",
    "operation_contract",
]
