from __future__ import annotations

from typing import Literal

from pydantic import ConfigDict, Field

from riverhog_api.schemas.archive import ArchiveOut, CollectionManifestOut
from riverhog_api.schemas.common import RiverhogModel


class ArchiveRestoreNotificationStatusOut(RiverhogModel):
    webhook_configured: bool
    reminder_count: int
    next_reminder_at: str | None
    last_notified_at: str | None
    failure_count: int = 0
    last_failure_at: str | None = None
    last_failure: str | None = None


class ArchiveRestoreProgressOut(RiverhogModel):
    archive_verification: Literal["pending", "in_progress", "completed", "failed"]
    extraction: Literal["pending", "in_progress", "completed", "failed"]
    materialization: Literal["pending", "in_progress", "completed", "failed"]


class ArchiveRestoreImageOut(RiverhogModel):
    model_config = ConfigDict(extra="ignore")

    id: str
    filename: str
    collection_ids: list[str] = Field(default_factory=list)
    rebuild_state: Literal[
        "pending",
        "restoring_collections",
        "rebuilding",
        "ready",
        "paused",
        "failed",
        "canceled",
    ] = "pending"


class ArchiveRestoreCollectionOut(RiverhogModel):
    id: str
    archive: ArchiveOut
    collection_manifest: CollectionManifestOut | None = None
    stored_bytes: int


class ArchiveRestoreOut(RiverhogModel):
    model_config = ConfigDict(extra="ignore")

    id: str
    type: Literal["fetch_materialization", "disc_rebuild"] = "disc_rebuild"
    state: Literal[
        "requested",
        "ready",
        "paused",
        "expired",
        "completed",
        "failed",
        "canceled",
    ]
    created_at: str
    requested_at: str | None
    ready_at: str | None
    expires_at: str | None
    completed_at: str | None
    canceled_at: str | None = None
    paused_at: str | None = None
    paused_from_state: str | None = None
    paths: list[str] | None = None
    latest_message: str | None
    warnings: list[str]
    notification: ArchiveRestoreNotificationStatusOut
    progress: ArchiveRestoreProgressOut
    collections: list[ArchiveRestoreCollectionOut] = Field(default_factory=list)
    images: list[ArchiveRestoreImageOut]


class ArchiveRestoreListOut(RiverhogModel):
    page: int
    per_page: int
    total: int
    pages: int
    sort: Literal[
        "created_at",
        "id",
        "type",
        "state",
        "ready_at",
        "expires_at",
    ]
    order: Literal["asc", "desc"]
    terminal: Literal["active", "terminal", "all"] = "all"
    type: Literal["fetch_materialization", "disc_rebuild"] | None
    state: (
        Literal[
            "requested",
            "ready",
            "paused",
            "expired",
            "completed",
            "failed",
            "canceled",
        ]
        | None
    )
    collection: str | None
    image: str | None
    restores: list[ArchiveRestoreOut]
