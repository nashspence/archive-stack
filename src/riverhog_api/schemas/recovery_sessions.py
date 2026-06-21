from __future__ import annotations

from typing import Literal

from pydantic import ConfigDict, Field

from riverhog_api.schemas.archive import CollectionManifestOut, GlacierArchiveOut
from riverhog_api.schemas.common import RiverhogModel


class RecoveryNotificationStatusOut(RiverhogModel):
    webhook_configured: bool
    reminder_count: int
    next_reminder_at: str | None
    last_notified_at: str | None
    failure_count: int = 0
    last_failure_at: str | None = None
    last_failure: str | None = None


class RecoverySessionProgressOut(RiverhogModel):
    archive_verification: Literal["pending", "in_progress", "completed", "failed"]
    extraction: Literal["pending", "in_progress", "completed", "failed"]
    materialization: Literal["pending", "in_progress", "completed", "failed"]


class RecoverySessionImageOut(RiverhogModel):
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


class RecoverySessionCollectionOut(RiverhogModel):
    id: str
    glacier: GlacierArchiveOut
    collection_manifest: CollectionManifestOut | None = None
    stored_bytes: int


class RecoverySessionOut(RiverhogModel):
    model_config = ConfigDict(extra="ignore")

    id: str
    type: Literal["collection_restore", "image_rebuild"] = "image_rebuild"
    state: Literal[
        "restore_requested",
        "ready",
        "paused",
        "expired",
        "completed",
        "failed",
        "canceled",
    ]
    created_at: str
    restore_requested_at: str | None
    restore_ready_at: str | None
    restore_expires_at: str | None
    completed_at: str | None
    canceled_at: str | None = None
    paused_at: str | None = None
    paused_from_state: str | None = None
    restore_paths: list[str] | None = None
    latest_message: str | None
    warnings: list[str]
    notification: RecoveryNotificationStatusOut
    progress: RecoverySessionProgressOut
    collections: list[RecoverySessionCollectionOut] = Field(default_factory=list)
    images: list[RecoverySessionImageOut]


class RecoverySessionListOut(RiverhogModel):
    page: int
    per_page: int
    total: int
    pages: int
    sort: Literal[
        "created_at",
        "id",
        "type",
        "state",
        "restore_ready_at",
        "restore_expires_at",
    ]
    order: Literal["asc", "desc"]
    type: Literal["collection_restore", "image_rebuild"] | None
    state: (
        Literal[
            "restore_requested",
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
    sessions: list[RecoverySessionOut]
