"""Bounded native catalog synchronization documents."""

from __future__ import annotations

from typing import Annotated, Literal, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    model_validator,
)

from riverhog_protocol.collection_description import (
    MAX_COLLECTION_DESCRIPTION_REVISION,
    CollectionDescription,
)
from riverhog_protocol.paths import CollectionId
from riverhog_protocol.transport import CATALOG_SYNC_PAGE_SIZE_MAX

CATALOG_SYNC_FORMAT: Literal["riverhog-catalog-sync/v1"] = "riverhog-catalog-sync/v1"
MAX_CATALOG_SYNC_REVISION = 8_999_999_999_999_999_999
CATALOG_SYNC_CURSOR_BYTES_MAX = 4096
_POSITION_PATTERN = r"^(?:0|[1-9][0-9]{0,17}|[1-8][0-9]{18})$"
_REVISION_PATTERN = r"^(?:[1-9][0-9]{0,17}|[1-8][0-9]{18})$"


CatalogSyncPosition = Annotated[
    str,
    StringConstraints(min_length=1, max_length=19, pattern=_POSITION_PATTERN),
]


CatalogSyncRevision = Annotated[
    str,
    StringConstraints(min_length=1, max_length=19, pattern=_REVISION_PATTERN),
]
CatalogSyncCursor = Annotated[
    str,
    StringConstraints(min_length=1, max_length=CATALOG_SYNC_CURSOR_BYTES_MAX),
]
CatalogSyncIdentity = Annotated[
    str,
    StringConstraints(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$"),
]


class CatalogSyncModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class CatalogSyncDescriptor(CatalogSyncModel):
    collection_id: CollectionId
    archive_root_sha256: CatalogSyncIdentity
    content_identity: CatalogSyncIdentity
    description: CollectionDescription | None
    description_revision: int = Field(
        ge=0,
        le=MAX_COLLECTION_DESCRIPTION_REVISION,
        strict=True,
    )
    description_identity: CatalogSyncIdentity
    revision: CatalogSyncRevision


class CatalogSyncUpsert(CatalogSyncDescriptor):
    operation: Literal["upsert"] = "upsert"


class CatalogSyncDelete(CatalogSyncModel):
    operation: Literal["delete"] = "delete"
    collection_id: CollectionId
    revision: CatalogSyncRevision


CatalogSyncChange = Annotated[
    CatalogSyncUpsert | CatalogSyncDelete,
    Field(discriminator="operation"),
]


class CatalogSyncEnvelope(CatalogSyncModel):
    format: Literal["riverhog-catalog-sync/v1"] = CATALOG_SYNC_FORMAT
    source_identity: CatalogSyncIdentity
    authorization_view_identity: CatalogSyncIdentity


class CatalogSyncCheckpoint(CatalogSyncEnvelope):
    catalog_cursor: CatalogSyncCursor


class CatalogSyncCollectionPage(CatalogSyncEnvelope):
    collections: list[CatalogSyncDescriptor] = Field(max_length=CATALOG_SYNC_PAGE_SIZE_MAX)
    next_cursor: CatalogSyncCursor | None = None
    changes_cursor: CatalogSyncCursor | None = None

    @model_validator(mode="after")
    def validate_continuation(self) -> Self:
        if (self.next_cursor is None) == (self.changes_cursor is None):
            raise ValueError("catalog page must contain exactly one continuation")
        if self.next_cursor is not None and not self.collections:
            raise ValueError("continued catalog page must contain a collection")
        return self


class CatalogSyncChangePage(CatalogSyncEnvelope):
    changes: list[CatalogSyncChange] = Field(max_length=CATALOG_SYNC_PAGE_SIZE_MAX)
    next_cursor: CatalogSyncCursor
    caught_up: bool
    through_revision: CatalogSyncPosition


__all__ = [
    "CATALOG_SYNC_CURSOR_BYTES_MAX",
    "CATALOG_SYNC_FORMAT",
    "CATALOG_SYNC_PAGE_SIZE_MAX",
    "MAX_CATALOG_SYNC_REVISION",
    "CatalogSyncChange",
    "CatalogSyncChangePage",
    "CatalogSyncCheckpoint",
    "CatalogSyncCollectionPage",
    "CatalogSyncCursor",
    "CatalogSyncDelete",
    "CatalogSyncDescriptor",
    "CatalogSyncIdentity",
    "CatalogSyncPosition",
    "CatalogSyncRevision",
    "CatalogSyncUpsert",
]
