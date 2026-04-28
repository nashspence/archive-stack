from __future__ import annotations

from typing import Literal

from arc_api.schemas.archive import GlacierArchiveOut
from arc_api.schemas.common import ArcModel


class RecoveryCostEstimateOut(ArcModel):
    currency_code: str
    retrieval_tier: Literal["bulk", "standard"]
    hold_days: int
    image_count: int
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


class RecoverySessionImageOut(ArcModel):
    id: str
    filename: str
    glacier: GlacierArchiveOut
    stored_bytes: int


class RecoverySessionOut(ArcModel):
    id: str
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
    images: list[RecoverySessionImageOut]
