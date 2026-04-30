from __future__ import annotations

from typing import Literal

from pydantic import AliasChoices, ConfigDict, Field

from arc_api.schemas.archive import CollectionArchiveManifestOut, GlacierArchiveOut
from arc_api.schemas.common import ArcModel


class RecoveryCostEstimateOut(ArcModel):
    model_config = ConfigDict(extra="ignore")

    currency_code: str
    retrieval_tier: Literal["bulk", "standard"]
    hold_days: int
    collection_count: int = Field(
        default=0,
        validation_alias=AliasChoices("collection_count", "image_count"),
    )
    total_bytes: int
    restore_request_count: int
    retrieval_rate_usd_per_gib: float
    request_rate_usd_per_1000: float
    standard_storage_rate_usd_per_gib_month: float
    retrieval_cost_usd: float
    request_fees_usd: float
    temporary_storage_cost_usd: float
    total_estimated_cost_usd: float
    assumptions: list[str]


class RecoveryNotificationStatusOut(ArcModel):
    webhook_configured: bool
    reminder_count: int
    next_reminder_at: str | None
    last_notified_at: str | None


class RecoverySessionProgressOut(ArcModel):
    archive_verification: Literal["pending", "in_progress", "completed", "failed"]
    extraction: Literal["pending", "in_progress", "completed", "failed"]
    materialization: Literal["pending", "in_progress", "completed", "failed"]


class RecoverySessionImageOut(ArcModel):
    model_config = ConfigDict(extra="ignore")

    id: str
    filename: str
    collection_ids: list[str] = Field(default_factory=list)
    rebuild_state: Literal[
        "pending", "restoring_collections", "rebuilding", "ready", "failed"
    ] = "pending"


class RecoverySessionCollectionOut(ArcModel):
    id: str
    glacier: GlacierArchiveOut
    archive_manifest: CollectionArchiveManifestOut | None = None
    stored_bytes: int


class RecoverySessionOut(ArcModel):
    model_config = ConfigDict(extra="ignore")

    id: str
    type: Literal["collection_restore", "image_rebuild"] = "image_rebuild"
    state: Literal["pending_approval", "restore_requested", "ready", "expired", "completed"]
    created_at: str
    approved_at: str | None
    restore_requested_at: str | None
    restore_ready_at: str | None
    restore_expires_at: str | None
    completed_at: str | None
    latest_message: str | None
    warnings: list[str]
    cost_estimate: RecoveryCostEstimateOut
    notification: RecoveryNotificationStatusOut
    progress: RecoverySessionProgressOut
    collections: list[RecoverySessionCollectionOut] = Field(default_factory=list)
    images: list[RecoverySessionImageOut]


class CollectionRestoreMaterializeRequest(ArcModel):
    paths: list[str]
