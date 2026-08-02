from __future__ import annotations

from typing import Any, Literal

from http_api_contracts import ErrorBody, ErrorResponse, HealthResponse
from lifecycle_events import EventPage
from pydantic import BaseModel, ConfigDict, Field, StrictBool, model_validator


class JebModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ConfigCheckOut(JebModel):
    status: Literal["ok"]
    source_count: int


class SourceCreateIn(JebModel):
    id: str = Field(min_length=1)
    adapters: list[str] = Field(min_length=1)
    target_config: dict[str, Any]
    credential: str | None = None
    enabled: StrictBool = True
    stable_seconds: int = 600
    include_extensions: list[str] = Field(default_factory=list)
    target: str = "munchy"
    threshold_bytes: int = 0
    cleanup: Literal["never", "after_target_success"] = "after_target_success"
    cadence: Literal["weekly", "monthly", "seasonal", "manual"] = "weekly"
    weekday: int = 0
    hour: int = 3
    minute: int = 0


class SourceUpdateIn(JebModel):
    adapters: list[str] | None = None
    stable_seconds: int | None = None
    include_extensions: list[str] | None = None
    target: str | None = None
    target_config: dict[str, Any] | None = None
    threshold_bytes: int | None = None
    cleanup: Literal["never", "after_target_success"] | None = None
    cadence: Literal["weekly", "monthly", "seasonal", "manual"] | None = None
    weekday: int | None = None
    hour: int | None = None
    minute: int | None = None

    @model_validator(mode="before")
    @classmethod
    def reject_null_updates(cls, value: Any) -> Any:
        if isinstance(value, dict):
            null_fields = sorted(key for key, item in value.items() if item is None)
            if null_fields:
                raise ValueError("source settings cannot be null: " + ", ".join(null_fields))
        return value


class CredentialRotateIn(JebModel):
    credential: str | None = None


class SourceRemovalPlanIn(JebModel):
    purge: StrictBool = False


class SourceRemovalIn(JebModel):
    challenge: str = Field(min_length=1)


class ArchiveNowIn(JebModel):
    source: str = Field(min_length=1)
    process: StrictBool = True
    dry_run: StrictBool = False


class SourceOut(JebModel):
    id: str
    enabled: bool
    adapters: list[str]
    stable_seconds: int
    include_extensions: list[str]
    target: str
    target_config: dict[str, Any]
    threshold_bytes: int
    cleanup: Literal["never", "after_target_success"]
    cadence: Literal["weekly", "monthly", "seasonal", "manual"]
    weekday: int
    hour: int
    minute: int


class SourceCreatedOut(JebModel):
    source: SourceOut
    credential: str | None = None


class SourceListItemOut(JebModel):
    id: str
    enabled: bool
    adapters: list[str]
    target: str
    cadence: Literal["weekly", "monthly", "seasonal", "manual"]
    target_config: dict[str, Any]
    created_at: str
    updated_at: str


class SourceFiltersOut(JebModel):
    enabled: bool | None
    adapter: str | None
    target: str | None


class SourcePageOut(JebModel):
    page: int
    per_page: int
    total: int
    pages: int
    sort: str
    order: Literal["asc", "desc"]
    query: str | None
    filters: SourceFiltersOut
    sources: list[SourceListItemOut]


class AttemptOut(JebModel):
    attempt_id: str
    batch_id: str
    attempt_number: int
    state: str
    source_id: str
    target_name: str
    run_id: str
    cleanup: str
    manifest_digest: str
    target_submission_id: str | None
    created_at: str
    updated_at: str
    last_error: str | None
    emitted_error_at: str | None
    file_count: int
    total_bytes: int
    staged_file_count: int


class AttemptFiltersOut(JebModel):
    source: str | None
    state: str | None
    states: list[str] | None
    target: str | None


class AttemptPageOut(JebModel):
    page: int
    per_page: int
    total: int
    pages: int
    sort: str
    order: Literal["asc", "desc"]
    resolution: Literal["unresolved", "resolved", "all"]
    query: str | None
    filters: AttemptFiltersOut
    attempts: list[AttemptOut]


class OperationOut(JebModel):
    id: str
    operation: str
    started_at: str
    state: str
    source: str | None = None
    attempt_id: str | None = None
    completed_at: str | None = None
    failure: str | None = None


class OperationFiltersOut(JebModel):
    state: str | None = None


class OperationPageOut(JebModel):
    page: int
    per_page: int
    total: int
    pages: int
    sort: str
    order: Literal["asc", "desc"]
    query: str | None
    filters: OperationFiltersOut
    operations: list[OperationOut]


class OperationStartedOut(JebModel):
    status: Literal["started"]
    operation: OperationOut


class StatusOut(JebModel):
    sources: list[dict[str, Any]]
    batches: dict[str, Any]
    unresolved_attempts: AttemptPageOut
    recent_failures: AttemptPageOut
    target_preflight_failures: dict[str, Any]
    incomplete_tus_uploads: dict[str, Any]
    active_operation: OperationOut | None


__all__ = [
    "ArchiveNowIn",
    "AttemptOut",
    "AttemptPageOut",
    "ConfigCheckOut",
    "CredentialRotateIn",
    "ErrorBody",
    "ErrorResponse",
    "EventPage",
    "HealthResponse",
    "OperationOut",
    "OperationPageOut",
    "OperationStartedOut",
    "SourceCreateIn",
    "SourceCreatedOut",
    "SourceOut",
    "SourcePageOut",
    "SourceRemovalIn",
    "SourceRemovalPlanIn",
    "SourceUpdateIn",
    "StatusOut",
]
