"""Munchy-owned v1 transform operation contracts and portable intents."""

from __future__ import annotations

from typing import Final

from munchy_workflows.profiles import ArchiveEncodeProfile, SourcePreservationProfile
from pydantic import BaseModel, ConfigDict, Field, JsonValue

from munchy_target_support.protocol import (
    InputArtifactContract,
    JsonSchemaDocument,
    OperationContract,
    OperationContractPayload,
    OutputArtifactContract,
)

SOURCE_ROLE: Final = "munchy.source/v1"
SOURCE_ARTIFACTS_ROLE: Final = "munchy.source-artifacts/v1"
VIDEO_ARCHIVE_ROLE: Final = "munchy.video.archive/v1"
VIDEO_REVIEW_ROLE: Final = "munchy.video.review/v1"
AUDIO_ARCHIVE_ROLE: Final = "munchy.audio.archive/v1"
AUDIO_REVIEW_ROLE: Final = "munchy.audio.review/v1"
REVIEW_PLAN_ROLE: Final = "munchy.review-plan/v1"

VIDEO_ARCHIVE_OPERATION: Final = "munchy.video.archive/v1"
VIDEO_REVIEW_OPERATION: Final = "munchy.video.review/v1"
AUDIO_ARCHIVE_OPERATION: Final = "munchy.audio.archive/v1"
AUDIO_REVIEW_OPERATION: Final = "munchy.audio.review/v1"


class SourceArtifactSidecarIntent(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    artifact_id: str = Field(min_length=1, max_length=160)
    sidecar_id: str = Field(default="", max_length=200)
    format: str = Field(default="opaque", min_length=1, max_length=120)
    arcname: str = Field(min_length=1, max_length=4096)
    source_rel_path: str = Field(default="", max_length=4096)


class OperationIntent(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    archive: ArchiveEncodeProfile
    source: SourcePreservationProfile | None = None
    run_id: str | None = Field(default=None, min_length=1, max_length=64)
    container_metadata_required: bool = True
    container_metadata: dict[str, dict[str, JsonValue]] = Field(default_factory=dict)
    source_artifact_sidecars: dict[str, tuple[SourceArtifactSidecarIntent, ...]] = Field(
        default_factory=dict
    )


class ReviewClipIntent(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    target_seconds: int | None = Field(default=None, ge=1)
    min_seconds: int | None = Field(default=None, ge=1)
    max_seconds: int | None = Field(default=None, ge=1)


class VideoArchiveIntent(OperationIntent):
    pass


class VideoReviewIntent(OperationIntent):
    review_clip: ReviewClipIntent | None = None
    review_plan: dict[str, JsonValue] | None = None


class AudioArchiveIntent(OperationIntent):
    pass


class AudioReviewIntent(OperationIntent):
    review_clip: ReviewClipIntent | None = None
    review_plan: dict[str, JsonValue] | None = None


INTENT_MODELS: Final[dict[str, type[OperationIntent]]] = {
    VIDEO_ARCHIVE_OPERATION: VideoArchiveIntent,
    VIDEO_REVIEW_OPERATION: VideoReviewIntent,
    AUDIO_ARCHIVE_OPERATION: AudioArchiveIntent,
    AUDIO_REVIEW_OPERATION: AudioReviewIntent,
}


def _intent_schema(operation_id: str, model: type[OperationIntent]) -> JsonSchemaDocument:
    return JsonSchemaDocument.from_schema(
        f"{operation_id.removesuffix('/v1')}.intent/v1",
        model.model_json_schema(),
    )


def _operation(
    operation_id: str,
    model: type[OperationIntent],
    *,
    output_role: str,
    output_minimum: int = 1,
    output_maximum: int | None = None,
    publishes_review_plan: bool = False,
) -> OperationContract:
    outputs = [
        OutputArtifactContract(
            role=output_role,
            minimum=output_minimum,
            maximum=output_maximum,
            derived_from_roles=(SOURCE_ROLE,),
        ),
        OutputArtifactContract(
            role=SOURCE_ARTIFACTS_ROLE,
            minimum=0,
            derived_from_roles=(SOURCE_ROLE, SOURCE_ARTIFACTS_ROLE),
        ),
    ]
    if publishes_review_plan:
        outputs.append(
            OutputArtifactContract(
                role=REVIEW_PLAN_ROLE,
                derived_from_roles=(SOURCE_ROLE,),
            )
        )
    return OperationContract.seal(
        OperationContractPayload(
            id=operation_id,
            intent_schema=_intent_schema(operation_id, model),
            inputs=(
                InputArtifactContract(role=SOURCE_ROLE, minimum=1),
                InputArtifactContract(role=SOURCE_ARTIFACTS_ROLE, minimum=0),
            ),
            outputs=tuple(outputs),
        )
    )


OPERATION_CONTRACTS: Final[dict[str, OperationContract]] = {
    VIDEO_ARCHIVE_OPERATION: _operation(
        VIDEO_ARCHIVE_OPERATION,
        VideoArchiveIntent,
        output_role=VIDEO_ARCHIVE_ROLE,
    ),
    VIDEO_REVIEW_OPERATION: _operation(
        VIDEO_REVIEW_OPERATION,
        VideoReviewIntent,
        output_role=VIDEO_REVIEW_ROLE,
        output_maximum=1,
        publishes_review_plan=True,
    ),
    AUDIO_ARCHIVE_OPERATION: _operation(
        AUDIO_ARCHIVE_OPERATION,
        AudioArchiveIntent,
        output_role=AUDIO_ARCHIVE_ROLE,
    ),
    AUDIO_REVIEW_OPERATION: _operation(
        AUDIO_REVIEW_OPERATION,
        AudioReviewIntent,
        output_role=AUDIO_REVIEW_ROLE,
        output_maximum=1,
        publishes_review_plan=True,
    ),
}

TASK_OPERATIONS: Final[dict[str, str]] = {
    "archive_video": VIDEO_ARCHIVE_OPERATION,
    "qcut_video": VIDEO_REVIEW_OPERATION,
    "archive_audio": AUDIO_ARCHIVE_OPERATION,
    "audio_review": AUDIO_REVIEW_OPERATION,
}


def operation_contract(operation_id: str) -> OperationContract:
    try:
        return OPERATION_CONTRACTS[operation_id]
    except KeyError as exc:
        raise ValueError(f"unknown Munchy operation: {operation_id}") from exc


def validate_operation_intent(operation_id: str, value: object) -> OperationIntent:
    try:
        model = INTENT_MODELS[operation_id]
    except KeyError as exc:
        raise ValueError(f"unknown Munchy operation: {operation_id}") from exc
    return model.model_validate(value)


__all__ = [
    "AUDIO_ARCHIVE_OPERATION",
    "AUDIO_ARCHIVE_ROLE",
    "AUDIO_REVIEW_OPERATION",
    "AUDIO_REVIEW_ROLE",
    "OPERATION_CONTRACTS",
    "REVIEW_PLAN_ROLE",
    "SOURCE_ARTIFACTS_ROLE",
    "SOURCE_ROLE",
    "TASK_OPERATIONS",
    "VIDEO_ARCHIVE_OPERATION",
    "VIDEO_ARCHIVE_ROLE",
    "VIDEO_REVIEW_OPERATION",
    "VIDEO_REVIEW_ROLE",
    "AudioArchiveIntent",
    "AudioReviewIntent",
    "OperationIntent",
    "ReviewClipIntent",
    "SourceArtifactSidecarIntent",
    "VideoArchiveIntent",
    "VideoReviewIntent",
    "operation_contract",
    "validate_operation_intent",
]
