"""Pure authoring helpers bridging review observations to target intents."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue
from stove0_media_sampling_observer_contracts import MediaSamplingFacts
from stove0_protocol import (
    CollectionRootRef,
    EvaluationDefinition,
    EvaluationDefinitionPayload,
    EvaluationMatrix,
    EvaluationMatrixPayload,
    EvaluationVariant,
    RecipeRef,
)
from stove0_review_target_contracts import (
    ReviewSamplePlan,
    ReviewSamplePlanPayload,
    ReviewSampleWindow,
)


class ReviewPlanningModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ReviewVariant(ReviewPlanningModel):
    id: str = Field(pattern=r"^[a-z0-9](?:[a-z0-9._-]{0,158}[a-z0-9])?$")
    portable_intent: dict[str, JsonValue] = Field(default_factory=dict)
    target_options: dict[str, JsonValue] = Field(default_factory=dict)


def evenly_spaced_sample_plan(
    facts: MediaSamplingFacts,
    *,
    samples_per_artifact: int,
    window_duration_ms: int,
) -> ReviewSamplePlan:
    if samples_per_artifact < 1:
        raise ValueError("samples_per_artifact must be positive")
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
            windows.append(
                ReviewSampleWindow(
                    artifact_id=artifact.artifact_id,
                    start_ms=_map_position(domains, position),
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


def _map_position(domains: list[tuple[int, int]], position: int) -> int:
    remaining = position
    for start, positions in domains:
        if remaining < positions:
            return start + remaining
        remaining -= positions
    raise RuntimeError("sample position exceeded its canonical domain")


__all__ = ["ReviewVariant", "evenly_spaced_sample_plan", "review_evaluation_definition"]
