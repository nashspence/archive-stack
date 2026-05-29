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
    state: str
    bytes: int = 0
    failure: str | None = None


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
