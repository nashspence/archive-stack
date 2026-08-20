"""Typed authoring models for review sampling and evaluation matrices."""

from __future__ import annotations

from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, JsonValue, field_validator, model_validator
from stove0_protocol import (
    CollectionRootRef,
    EvaluationDefinition,
    EvaluationDefinitionPayload,
    EvaluationMatrix,
    EvaluationMatrixPayload,
    EvaluationVariant,
    RecipeRef,
    WorkIdentity,
    canonical_json_sha256,
)


class ReviewModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class SampleableRange(ReviewModel):
    start_ms: int = Field(ge=0)
    duration_ms: int = Field(ge=1)


class MediaSamplingArtifactFacts(ReviewModel):
    artifact_id: str = Field(min_length=1, max_length=160)
    duration_ms: int = Field(ge=1)
    sampleable_ranges: tuple[SampleableRange, ...] = Field(min_length=1, max_length=4096)

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


class MediaSamplingFacts(ReviewModel):
    artifacts: tuple[MediaSamplingArtifactFacts, ...] = Field(min_length=1, max_length=128)

    @field_validator("artifacts")
    @classmethod
    def canonical_artifacts(
        cls, value: tuple[MediaSamplingArtifactFacts, ...]
    ) -> tuple[MediaSamplingArtifactFacts, ...]:
        ids = [item.artifact_id for item in value]
        if ids != sorted(ids) or len(ids) != len(set(ids)):
            raise ValueError("sampling facts must be unique and ordered by artifact ID")
        return value


class ReviewSampleWindow(ReviewModel):
    artifact_id: str = Field(min_length=1, max_length=160)
    start_ms: int = Field(ge=0)
    duration_ms: int = Field(ge=1)


class ReviewSamplePlanPayload(ReviewModel):
    format: Literal["stove0-review-sample-plan/v1"] = "stove0-review-sample-plan/v1"
    selection_method: Literal["evenly-spaced/v1"] = "evenly-spaced/v1"
    samples_per_artifact: int = Field(ge=1, le=64)
    window_duration_ms: int = Field(ge=1, le=60 * 60 * 1000)
    windows: tuple[ReviewSampleWindow, ...] = Field(min_length=1, max_length=8192)

    @field_validator("windows")
    @classmethod
    def canonical_windows(
        cls, value: tuple[ReviewSampleWindow, ...]
    ) -> tuple[ReviewSampleWindow, ...]:
        ordered = tuple(sorted(value, key=lambda item: (item.artifact_id, item.start_ms)))
        identities = {(item.artifact_id, item.start_ms) for item in value}
        if value != ordered or len(identities) != len(value):
            raise ValueError("review sample windows must be unique and canonically ordered")
        return value


class ReviewSamplePlan(ReviewSamplePlanPayload):
    sample_plan_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def verify_digest(self) -> Self:
        document = self.model_dump(
            mode="json",
            exclude={"sample_plan_sha256"},
            exclude_none=True,
        )
        if canonical_json_sha256(document) != self.sample_plan_sha256:
            raise ValueError("review sample-plan digest does not match its canonical payload")
        return self

    @classmethod
    def seal(cls, payload: ReviewSamplePlanPayload) -> ReviewSamplePlan:
        document = payload.model_dump(mode="json", exclude_none=True)
        return cls(**document, sample_plan_sha256=canonical_json_sha256(document))


class ReviewVariant(ReviewModel):
    id: str = Field(pattern=r"^[a-z0-9](?:[a-z0-9._-]{0,158}[a-z0-9])?$")
    portable_intent: dict[str, JsonValue] = Field(default_factory=dict)
    target_options: dict[str, JsonValue] = Field(default_factory=dict)


def evenly_spaced_sample_plan(
    facts: MediaSamplingFacts,
    *,
    samples_per_artifact: int,
    window_duration_ms: int,
) -> ReviewSamplePlan:
    if samples_per_artifact < 1 or samples_per_artifact > 64:
        raise ValueError("samples_per_artifact must be between 1 and 64")
    if window_duration_ms < 1:
        raise ValueError("window_duration_ms must be positive")
    windows: list[ReviewSampleWindow] = []
    for artifact in facts.artifacts:
        domains: list[tuple[int, int]] = []
        total_start_positions = 0
        for span in artifact.sampleable_ranges:
            positions = span.duration_ms - window_duration_ms + 1
            if positions <= 0:
                continue
            domains.append((span.start_ms, positions))
            total_start_positions += positions
        if total_start_positions < samples_per_artifact:
            raise ValueError(
                f"artifact has insufficient sampleable duration: {artifact.artifact_id}"
            )
        selected_positions: tuple[int, ...] = (
            (total_start_positions // 2,)
            if samples_per_artifact == 1
            else tuple(
                (index * (total_start_positions - 1)) // (samples_per_artifact - 1)
                for index in range(samples_per_artifact)
            )
        )
        if len(set(selected_positions)) != samples_per_artifact:
            raise RuntimeError("sample-position selection was not unique")
        for position in selected_positions:
            start = _map_position(domains, position)
            windows.append(
                ReviewSampleWindow(
                    artifact_id=artifact.artifact_id,
                    start_ms=start,
                    duration_ms=window_duration_ms,
                )
            )
    return ReviewSamplePlan.seal(
        ReviewSamplePlanPayload(
            samples_per_artifact=samples_per_artifact,
            window_duration_ms=window_duration_ms,
            windows=tuple(sorted(windows, key=lambda item: (item.artifact_id, item.start_ms))),
        )
    )


def review_evaluation_definition(
    *,
    recipe: RecipeRef,
    inputs: tuple[CollectionRootRef, ...],
    sample_plan: ReviewSamplePlan,
    variants: tuple[ReviewVariant, ...],
    purpose: Literal["trial", "evaluation"] = "evaluation",
    common_intent: dict[str, JsonValue] | None = None,
) -> EvaluationDefinition:
    ordered = tuple(sorted(variants, key=lambda item: item.id))
    if not ordered or len({item.id for item in ordered}) != len(ordered):
        raise ValueError("review variants must be nonempty and unique")
    matrix = EvaluationMatrix.seal(
        EvaluationMatrixPayload(
            variants=tuple(
                EvaluationVariant(
                    id=item.id,
                    parameters={
                        "review_variant": {
                            "id": item.id,
                            "portable_intent": item.portable_intent,
                            "target_options": item.target_options,
                        }
                    },
                )
                for item in ordered
            )
        )
    )
    intent: dict[str, Any] = dict(common_intent or {})
    intent["review_sample_plan"] = sample_plan.model_dump(mode="json")
    return EvaluationDefinition.seal(
        EvaluationDefinitionPayload(
            purpose=purpose,
            recipe=recipe,
            inputs=inputs,
            common_intent=intent,
            matrix=matrix,
        )
    )


def review_operation_intent(work: WorkIdentity) -> dict[str, JsonValue]:
    """Compile one review evaluation child into the portable operation intent."""

    binding = work.evaluation
    if binding is None:
        raise ValueError("review operation intent requires evaluation-bound work")
    raw_plan = work.effective_intent.get("review_sample_plan")
    raw_variant = binding.parameters.get("review_variant")
    if not isinstance(raw_plan, dict) or not isinstance(raw_variant, dict):
        raise ValueError("review work is missing its sample plan or variant")
    variant_id = raw_variant.get("id")
    portable_intent = raw_variant.get("portable_intent")
    if variant_id != binding.variant_id or not isinstance(portable_intent, dict):
        raise ValueError("review variant parameters differ from the evaluation binding")
    return {
        "sample_plan": dict(raw_plan),
        "variant": {
            "id": binding.variant_id,
            "portable_intent": dict(portable_intent),
        },
    }


def review_target_options(work: WorkIdentity) -> dict[str, JsonValue]:
    """Return target-owned options without adding them to portable intent."""

    binding = work.evaluation
    if binding is None:
        raise ValueError("review target options require evaluation-bound work")
    raw_variant = binding.parameters.get("review_variant")
    if not isinstance(raw_variant, dict):
        raise ValueError("review work is missing its variant parameters")
    options = raw_variant.get("target_options")
    if not isinstance(options, dict):
        raise ValueError("review variant target options must be a JSON object")
    return dict(options)


def _map_position(domains: list[tuple[int, int]], position: int) -> int:
    remaining = position
    for start, positions in domains:
        if remaining < positions:
            return start + remaining
        remaining -= positions
    raise RuntimeError("sample position exceeded its canonical domain")


__all__ = [
    "MediaSamplingArtifactFacts",
    "MediaSamplingFacts",
    "ReviewSamplePlan",
    "ReviewSamplePlanPayload",
    "ReviewSampleWindow",
    "ReviewVariant",
    "SampleableRange",
    "evenly_spaced_sample_plan",
    "review_evaluation_definition",
    "review_operation_intent",
    "review_target_options",
]
