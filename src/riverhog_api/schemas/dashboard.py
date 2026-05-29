from __future__ import annotations

from typing import Literal

from pydantic import ConfigDict

from riverhog_api.schemas.common import RiverhogModel


class DashboardRecoveryCoverageOut(RiverhogModel):
    state: Literal["none", "partial", "full"]
    bytes: int


class DashboardRecoveryOut(RiverhogModel):
    available: list[str]
    verified_physical: DashboardRecoveryCoverageOut
    glacier: DashboardRecoveryCoverageOut


class DashboardProtectionMirrorOut(RiverhogModel):
    enabled: bool
    required: bool = False
    state: str
    bytes: int = 0
    failure: str | None = None


class DashboardActiveUploadOut(RiverhogModel):
    model_config = ConfigDict(extra="ignore")

    collection_id: str
    ingest_source: str | None = None
    state: Literal["uploading", "archiving"]
    files_total: int
    files_pending: int
    files_partial: int
    files_uploaded: int
    hot_promoted_files: int = 0
    bytes_total: int
    uploaded_bytes: int
    hot_promoted_bytes: int = 0
    missing_bytes: int
    latest_failure: str | None = None
    archive_phase: str | None = None
    archive_phase_updated_at: str | None = None
    archive_object_path: str | None = None
    archive_attempt_count: int = 0
    archive_next_attempt_at: str | None = None
    archive_uploaded_bytes: int | None = None
    archive_total_bytes: int | None = None
    archive_uploaded_parts: int | None = None
    archive_total_parts: int | None = None


class DashboardCollectionOut(RiverhogModel):
    model_config = ConfigDict(extra="ignore")

    id: str
    files: int
    bytes: int
    hot_bytes: int
    archived_bytes: int
    pending_bytes: int
    protection_state: Literal["cloud_only", "under_protected", "fully_protected"]
    protected_bytes: int
    recovery: DashboardRecoveryOut
    protection_mirror: DashboardProtectionMirrorOut | None = None


class DashboardCollectionsResponse(RiverhogModel):
    collections: list[DashboardCollectionOut]
    active_uploads: list[DashboardActiveUploadOut] = []
