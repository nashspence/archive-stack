from __future__ import annotations

from typing import Any, Literal

from riverhog_api.schemas.common import RiverhogModel


class CreateTagRequest(RiverhogModel):
    id: str


class TagOut(RiverhogModel):
    id: str
    created_by_app: str
    created_by_key_id: str | None
    created_at: str
    collections: int


class TagListOut(RiverhogModel):
    page: int
    per_page: int
    total: int
    pages: int
    sort: str
    order: Literal["asc", "desc"]
    query: str | None
    tags: list[TagOut]


class ReplaceCollectionTagsRequest(RiverhogModel):
    tags: list[str]
    event_context: dict[str, Any] | None = None


class CollectionTagsOut(RiverhogModel):
    collection_id: int
    metadata_revision: int
    record_etag: str
    tags: list[str]


class TagDependencySummaryOut(RiverhogModel):
    count: int
    sample: list[str]
    truncated: bool


class TagDependenciesOut(RiverhogModel):
    collections: TagDependencySummaryOut
    upload_sessions: TagDependencySummaryOut
    app_key_access: TagDependencySummaryOut
    metadata_publications: TagDependencySummaryOut


class TagDeletionPlanOut(RiverhogModel):
    status: Literal["ready", "blocked"]
    tag: str
    warning: str
    expires_at: str
    challenge: str | None
    dependencies: TagDependenciesOut
    blockers: list[str]


class DeleteTagRequest(RiverhogModel):
    challenge: str


class TagDeletionResultOut(RiverhogModel):
    status: Literal["deleted", "already_absent"]
    tag: str


class MutateCollectionTagRequest(RiverhogModel):
    event_context: dict[str, Any] | None = None
