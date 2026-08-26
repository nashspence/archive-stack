"""Target-owned operation and intent contracts for maintained media archives."""

from __future__ import annotations

from collections.abc import Mapping
from importlib.resources import files
from typing import Final, Literal

from pydantic import BaseModel, ConfigDict, Field
from stove0_protocol import (
    JsonSchemaDocument,
    SemanticValidationProfile,
    SemanticValidationProfilePayload,
)
from stove0_target_protocol import (
    InputArtifactContract,
    OperationContract,
    OperationContractPayload,
    OutputArtifactContract,
    SemanticIntentConformanceVectors,
)

from stove0_media_archive_target_contracts.projection_policy import (
    MediaFieldPreference,
    MediaGps,
    MediaProjectionFieldName,
    MediaProjectionPolicy,
)
from stove0_media_archive_target_contracts.roles import (
    AUDIO_ARCHIVE_ROLE,
    AV1_OPUS_ARCHIVE_ROLE,
    METADATA_XMP_ROLE,
    SOURCE_ARTIFACT_ROLE,
    SOURCE_ROLE,
    XMP_SOURCE_ROLE,
)

AUDIO_ARCHIVE_OPERATION_ID: Final = "stove0.media.audio-archive/v1"
AV1_OPUS_ARCHIVE_OPERATION_ID: Final = "stove0.media.av1-opus-archive/v1"


class IntentModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class AudioArchiveIntent(IntentModel):
    codec: Literal["opus"] = "opus"
    container: Literal["opus"] = "opus"
    bitrate_kbps: int = Field(default=128, ge=16, le=512)
    metadata_projection: MediaProjectionPolicy = Field(default_factory=MediaProjectionPolicy)


class Av1OpusArchiveIntent(IntentModel):
    codec: Literal["av1"] = "av1"
    container: Literal["mkv"] = "mkv"
    quality: int = Field(default=23, ge=0, le=63)
    max_height: int | None = Field(default=None, ge=144, le=8640)
    audio_bitrate_kbps: int = Field(default=128, ge=16, le=512)
    salvage: Literal["off", "safe-remux"] = "safe-remux"
    metadata_projection: MediaProjectionPolicy = Field(default_factory=MediaProjectionPolicy)


def _schema(identifier: str, model: type[IntentModel]) -> JsonSchemaDocument:
    return JsonSchemaDocument.from_schema(identifier, model.model_json_schema())


AUDIO_ARCHIVE_INTENT_CONFORMANCE_VECTORS = SemanticIntentConformanceVectors.model_validate_json(
    files("stove0_media_archive_target_contracts")
    .joinpath("vectors/audio-archive-intent-v1.json")
    .read_text(encoding="utf-8")
)
AV1_OPUS_ARCHIVE_INTENT_CONFORMANCE_VECTORS = SemanticIntentConformanceVectors.model_validate_json(
    files("stove0_media_archive_target_contracts")
    .joinpath("vectors/av1-opus-archive-intent-v1.json")
    .read_text(encoding="utf-8")
)

AUDIO_ARCHIVE_INTENT_SEMANTICS = SemanticValidationProfile.seal(
    SemanticValidationProfilePayload(
        id="stove0.media.audio-archive-intent-semantics/v1",
        rules=(
            "stove0.media.audio-archive-intent.typed-model/v1",
            "stove0.media.projection-policy.semantic-validation/v1",
        ),
        conformance_vectors_sha256=AUDIO_ARCHIVE_INTENT_CONFORMANCE_VECTORS.sha256,
    )
)
AV1_OPUS_ARCHIVE_INTENT_SEMANTICS = SemanticValidationProfile.seal(
    SemanticValidationProfilePayload(
        id="stove0.media.av1-opus-archive-intent-semantics/v1",
        rules=(
            "stove0.media.av1-opus-archive-intent.typed-model/v1",
            "stove0.media.projection-policy.semantic-validation/v1",
        ),
        conformance_vectors_sha256=AV1_OPUS_ARCHIVE_INTENT_CONFORMANCE_VECTORS.sha256,
    )
)


def validate_audio_archive_intent(intent: Mapping[str, object]) -> None:
    AudioArchiveIntent.model_validate(dict(intent))


def validate_av1_opus_archive_intent(intent: Mapping[str, object]) -> None:
    Av1OpusArchiveIntent.model_validate(dict(intent))


AUDIO_ARCHIVE_OPERATION = OperationContract.seal(
    OperationContractPayload(
        id=AUDIO_ARCHIVE_OPERATION_ID,
        intent_schema=_schema("stove0.media.audio-archive-intent/v1", AudioArchiveIntent),
        intent_semantics=AUDIO_ARCHIVE_INTENT_SEMANTICS,
        inputs=(
            InputArtifactContract(
                role=SOURCE_ROLE,
                minimum=1,
                allowed_dispositions=("transformed",),
            ),
            InputArtifactContract(
                role=XMP_SOURCE_ROLE,
                minimum=0,
                allowed_dispositions=("transformed",),
            ),
        ),
        outputs=(
            OutputArtifactContract(
                role=AUDIO_ARCHIVE_ROLE,
                minimum=1,
                derived_from_roles=(SOURCE_ROLE, XMP_SOURCE_ROLE),
            ),
            OutputArtifactContract(
                role=METADATA_XMP_ROLE,
                minimum=1,
                derived_from_roles=(SOURCE_ROLE, XMP_SOURCE_ROLE),
            ),
            OutputArtifactContract(
                role=SOURCE_ARTIFACT_ROLE,
                minimum=0,
                derived_from_roles=(XMP_SOURCE_ROLE,),
            ),
        ),
        source_retirement_permitted=False,
    )
)

AV1_OPUS_ARCHIVE_OPERATION = OperationContract.seal(
    OperationContractPayload(
        id=AV1_OPUS_ARCHIVE_OPERATION_ID,
        intent_schema=_schema("stove0.media.av1-opus-archive-intent/v1", Av1OpusArchiveIntent),
        intent_semantics=AV1_OPUS_ARCHIVE_INTENT_SEMANTICS,
        inputs=(
            InputArtifactContract(
                role=SOURCE_ROLE,
                minimum=1,
                allowed_dispositions=("transformed",),
            ),
            InputArtifactContract(
                role=XMP_SOURCE_ROLE,
                minimum=0,
                allowed_dispositions=("transformed",),
            ),
        ),
        outputs=(
            OutputArtifactContract(
                role=AV1_OPUS_ARCHIVE_ROLE,
                minimum=1,
                derived_from_roles=(SOURCE_ROLE, XMP_SOURCE_ROLE),
            ),
            OutputArtifactContract(
                role=METADATA_XMP_ROLE,
                minimum=1,
                derived_from_roles=(SOURCE_ROLE, XMP_SOURCE_ROLE),
            ),
            OutputArtifactContract(
                role=SOURCE_ARTIFACT_ROLE,
                minimum=1,
                derived_from_roles=(SOURCE_ROLE, XMP_SOURCE_ROLE),
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
    "AUDIO_ARCHIVE_INTENT_CONFORMANCE_VECTORS",
    "AUDIO_ARCHIVE_OPERATION_ID",
    "AUDIO_ARCHIVE_INTENT_SEMANTICS",
    "AUDIO_ARCHIVE_ROLE",
    "AudioArchiveIntent",
    "METADATA_XMP_ROLE",
    "MediaFieldPreference",
    "MediaGps",
    "MediaProjectionFieldName",
    "MediaProjectionPolicy",
    "OPERATIONS",
    "SOURCE_ARTIFACT_ROLE",
    "SOURCE_ROLE",
    "XMP_SOURCE_ROLE",
    "AV1_OPUS_ARCHIVE_OPERATION",
    "AV1_OPUS_ARCHIVE_INTENT_CONFORMANCE_VECTORS",
    "AV1_OPUS_ARCHIVE_OPERATION_ID",
    "AV1_OPUS_ARCHIVE_INTENT_SEMANTICS",
    "AV1_OPUS_ARCHIVE_ROLE",
    "Av1OpusArchiveIntent",
    "operation_contract",
    "validate_audio_archive_intent",
    "validate_av1_opus_archive_intent",
]
