from __future__ import annotations

from typing import Any, Literal

from pydantic import ConfigDict, field_validator, model_validator
from riverhog_protocol import SortOrder, TagSort
from riverhog_protocol.paths import CanonicalTag

from riverhog_api.schemas.common import RiverhogModel


class CreateTagRequest(RiverhogModel):
    id: CanonicalTag


class TagOut(RiverhogModel):
    id: CanonicalTag
    created_by_app: str
    created_by_key_id: str | None
    created_at: str
    collections: int


class TagListOut(RiverhogModel):
    page: int
    per_page: int
    total: int
    pages: int
    sort: TagSort
    order: SortOrder
    query: str | None
    tags: list[TagOut]


class ReplaceCollectionTagsRequest(RiverhogModel):
    tags: list[CanonicalTag]
    event_context: dict[str, Any] | None = None

    @field_validator("tags")
    @classmethod
    def validate_unique_tags(cls, value: list[str]) -> list[str]:
        if len(value) != len(set(value)):
            raise ValueError("collection tags must not contain duplicates")
        return value


class CollectionTagsOut(RiverhogModel):
    collection_id: int
    metadata_revision: int
    record_etag: str
    tags: list[CanonicalTag]


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
    model_config = ConfigDict(
        json_schema_extra={
            "oneOf": [
                {
                    "properties": {
                        "status": {"const": "blocked"},
                        "challenge": {"type": "null"},
                        "blockers": {"minItems": 1},
                    }
                },
                {
                    "properties": {
                        "status": {"const": "ready"},
                        "challenge": {"type": "string", "minLength": 1},
                        "blockers": {"maxItems": 0},
                    }
                },
            ]
        }
    )

    status: Literal["ready", "blocked"]
    tag: CanonicalTag
    warning: str
    expires_at: str
    challenge: str | None
    dependencies: TagDependenciesOut
    blockers: list[str]

    @model_validator(mode="after")
    def validate_plan_state(self) -> TagDeletionPlanOut:
        if self.status == "blocked":
            if self.challenge is not None or not self.blockers:
                raise ValueError("blocked tag deletion requires blockers and no challenge")
        elif not self.challenge or self.blockers:
            raise ValueError("ready tag deletion requires a challenge and no blockers")
        return self


class DeleteTagRequest(RiverhogModel):
    challenge: str


class TagDeletionResultOut(RiverhogModel):
    status: Literal["deleted", "already_absent"]
    tag: CanonicalTag


class MutateCollectionTagRequest(RiverhogModel):
    event_context: dict[str, Any] | None = None
