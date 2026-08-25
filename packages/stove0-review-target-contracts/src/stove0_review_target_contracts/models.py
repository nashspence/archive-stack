"""Target-owned portable review intent models."""

from __future__ import annotations

from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, JsonValue, field_validator, model_validator
from stove0_target_protocol import canonical_json_sha256


class ReviewTargetModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ReviewSampleWindow(ReviewTargetModel):
    artifact_id: str = Field(min_length=1, max_length=160)
    start_ms: int = Field(ge=0)
    duration_ms: int = Field(ge=1)


class ReviewSamplePlanPayload(ReviewTargetModel):
    format: Literal["stove0-review-sample-plan/v1"] = "stove0-review-sample-plan/v1"
    selection_method: Literal["evenly-spaced/v1"] = "evenly-spaced/v1"
    samples_per_artifact: int = Field(ge=1)
    window_duration_ms: int = Field(ge=1)
    windows: tuple[ReviewSampleWindow, ...] = Field(min_length=1)

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

    @model_validator(mode="after")
    def exact_declared_shape(self) -> Self:
        counts: dict[str, int] = {}
        for window in self.windows:
            if window.duration_ms != self.window_duration_ms:
                raise ValueError("review sample window duration differs from its plan")
            counts[window.artifact_id] = counts.get(window.artifact_id, 0) + 1
        if any(count != self.samples_per_artifact for count in counts.values()):
            raise ValueError("review sample count differs from its per-artifact declaration")
        return self


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


class ReviewVariantIntent(ReviewTargetModel):
    id: str = Field(pattern=r"^[a-z0-9](?:[a-z0-9._-]{0,158}[a-z0-9])?$")
    portable_intent: dict[str, JsonValue]


class ReviewMaterializeIntent(ReviewTargetModel):
    sample_plan: ReviewSamplePlan
    variant: ReviewVariantIntent


__all__ = [
    "ReviewMaterializeIntent",
    "ReviewSamplePlan",
    "ReviewSamplePlanPayload",
    "ReviewSampleWindow",
    "ReviewVariantIntent",
]
