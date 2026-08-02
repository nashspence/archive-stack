from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class MunchyModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PageResponse(MunchyModel):
    page: int = Field(ge=1)
    pages: int = Field(ge=0)
    per_page: int = Field(ge=0)
    total: int = Field(ge=0)
    sort: str
    order: Literal["asc", "desc"]
    query: str | None


class AppPageResponse(PageResponse):
    active: bool | None
    apps: list[dict[str, Any]]


class AppKeyPageResponse(PageResponse):
    active: bool | None
    app: str
    keys: list[dict[str, Any]]


class JobTemplatePageResponse(PageResponse):
    filters: dict[str, Any]
    templates: list[dict[str, Any]]


class JobPageResponse(PageResponse):
    terminal: str
    filters: dict[str, Any]
    jobs: list[dict[str, Any]]


class JobDiagnosticPageResponse(PageResponse):
    diagnostics: list[dict[str, Any]]


__all__ = [
    "AppKeyPageResponse",
    "AppPageResponse",
    "JobDiagnosticPageResponse",
    "JobPageResponse",
    "JobTemplatePageResponse",
]
