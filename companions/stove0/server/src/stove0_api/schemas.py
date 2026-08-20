from __future__ import annotations

from typing import Any, Literal

from http_api_contracts import ErrorResponse, HealthResponse
from pydantic import BaseModel, ConfigDict, Field, JsonValue, field_validator


class Stove0ApiModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class WorkflowPreviewIn(Stove0ApiModel):
    recipe_id: str = Field(min_length=1, max_length=160)
    recipe_revision: int | None = Field(default=None, ge=1)
    collection_ids: tuple[int, ...] = Field(min_length=1, max_length=1000)
    effective_intent: dict[str, JsonValue] = Field(default_factory=dict)

    @field_validator("collection_ids")
    @classmethod
    def canonical_collections(cls, value: tuple[int, ...]) -> tuple[int, ...]:
        if value != tuple(sorted(set(value))) or any(item < 1 for item in value):
            raise ValueError("collection IDs must be positive, unique, and ordered")
        return value


class WorkCreateIn(WorkflowPreviewIn):
    preview_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class WorkCancelIn(Stove0ApiModel):
    reason: str | None = Field(default=None, max_length=1000)


class EvaluationReviewIn(Stove0ApiModel):
    rating: int | None = Field(default=None, ge=1, le=5)
    note: str | None = Field(default=None, max_length=4000)


class SchedulerRunIn(Stove0ApiModel):
    role: Literal["controller", "worker", "combined"] = "combined"
    event_limit: int = Field(default=100, ge=1, le=100)
    work_limit: int = Field(default=25, ge=1, le=100)


class PageOut(Stove0ApiModel):
    page: int = Field(ge=1)
    per_page: int = Field(ge=0)
    total: int = Field(ge=0)
    pages: int = Field(ge=0)
    sort: str
    order: str
    filters: dict[str, Any]


class WorkPageOut(PageOut):
    work: list[dict[str, Any]]


class ArtifactSelectionPageOut(PageOut):
    selection_sha256: str
    total_bytes: int = Field(ge=0)
    artifacts: list[dict[str, Any]]


class EvaluationPageOut(PageOut):
    evaluations: list[dict[str, Any]]


__all__ = [
    "ArtifactSelectionPageOut",
    "EvaluationReviewIn",
    "ErrorResponse",
    "EvaluationPageOut",
    "HealthResponse",
    "SchedulerRunIn",
    "WorkCancelIn",
    "WorkCreateIn",
    "WorkPageOut",
    "WorkflowPreviewIn",
]
