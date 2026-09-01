from __future__ import annotations

from typing import Literal

from http_api_contracts import BrowsePageToken
from lifecycle_events import EventContext
from pydantic import ConfigDict, Field, model_validator
from riverhog_protocol import CollectionId, SortOrder, TagSort
from riverhog_protocol.paths import CanonicalTag
from riverhog_protocol.transport import TAG_DEPENDENCY_SAMPLE_MAX

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
    page_size: int = Field(ge=1, le=100)
    next_page_token: BrowsePageToken | None
    sort: TagSort
    order: SortOrder
    query: str | None
    tags: list[TagOut]


class CollectionTagSetOut(RiverhogModel):
    collection_id: CollectionId
    metadata_revision: int = Field(ge=1, strict=True)
    inventory_identity: str = Field(pattern=r"^[0-9a-f]{64}$")
    tag_count: int = Field(ge=0, strict=True)


class CollectionTagMembershipOut(RiverhogModel):
    tag: CanonicalTag


class CollectionTagsOut(RiverhogModel):
    collection_id: CollectionId
    metadata_revision: int = Field(ge=1, strict=True)
    inventory_identity: str = Field(pattern=r"^[0-9a-f]{64}$")
    page_size: int = Field(ge=1, le=100, strict=True)
    next_page_token: BrowsePageToken | None
    tags: list[CanonicalTag]


class TagDependencySummaryOut(RiverhogModel):
    count: int
    sample: list[str] = Field(
        max_length=TAG_DEPENDENCY_SAMPLE_MAX,
        json_schema_extra={
            "x-riverhog-extent": {
                "policy": "contract_max",
                "reason": "bounded-diagnostic-sample-with-complete-count",
            }
        },
    )
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
    event_context: EventContext | None = None


class ReplaceCollectionTagsRequest(MutateCollectionTagRequest):
    tags: list[CanonicalTag]

    @model_validator(mode="after")
    def canonical_set(self) -> ReplaceCollectionTagsRequest:
        if self.tags != sorted(set(self.tags)):
            raise ValueError("collection tags must be unique and canonically ordered")
        return self
