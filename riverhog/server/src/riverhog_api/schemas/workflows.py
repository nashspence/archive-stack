from __future__ import annotations

from typing import Any, Literal

from pydantic import Field

from riverhog_api.schemas.common import RiverhogModel


def _default_capability_actions() -> list[Literal["read-inputs", "write-output"]]:
    return ["read-inputs"]


class CollectionRootIdentityIn(RiverhogModel):
    collection_id: int = Field(ge=1)
    manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    content_etag: str = Field(pattern=r"^[0-9a-f]{64}$")


class OperationIdentityIn(RiverhogModel):
    id: str = Field(min_length=1, max_length=160)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class ProcessingClaimCreateIn(RiverhogModel):
    work_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    work_document: dict[str, Any]
    work_document_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    inputs: list[CollectionRootIdentityIn] = Field(min_length=1, max_length=1000)
    lease_seconds: int = Field(default=1800, ge=30, le=86400)
    purpose: str = Field(default="collection-work/v1", min_length=1, max_length=160)


class ProcessingClaimRenewIn(RiverhogModel):
    fence: int = Field(ge=1)
    lease_seconds: int = Field(default=1800, ge=30, le=86400)


class ProcessingClaimRestartIn(RiverhogModel):
    fence: int = Field(ge=1)
    lease_seconds: int = Field(default=1800, ge=30, le=86400)


class ProcessingClaimPlanSealIn(RiverhogModel):
    fence: int = Field(ge=1)
    execution_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    controller_evidence: dict[str, Any]
    controller_evidence_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    operation: OperationIdentityIn
    output_tags: list[str] = Field(min_length=1, max_length=100)
    retirement_policy: Literal["retain", "retire-after-verified-output"] = "retain"
    retirement_grace_seconds: int = Field(default=0, ge=0)


class TransformCapabilityCreateIn(RiverhogModel):
    fence: int = Field(ge=1)
    audience: str = Field(pattern=r"^[a-z0-9][a-z0-9._:/-]{0,299}$")
    actions: list[Literal["read-inputs", "write-output"]] = Field(
        default_factory=_default_capability_actions,
        min_length=1,
    )
    ttl_seconds: int = Field(default=900, ge=30, le=86400)


class ProcessingClaimSettleIn(RiverhogModel):
    fence: int = Field(ge=1)
    output_collection_id: int = Field(ge=1)
    derivation: dict[str, Any]


class ProcessingClaimFenceIn(RiverhogModel):
    fence: int = Field(ge=1)


class ProcessingClaimAbandonIn(ProcessingClaimFenceIn):
    reason: str = Field(min_length=1, max_length=1000)


class ProcessingClaimOut(RiverhogModel):
    format: Literal["riverhog-processing-claim/v1"]
    id: str
    work_id: str
    consumer: dict[str, Any]
    purpose: str
    state: Literal["active", "settled", "retiring", "abandoned", "released"]
    fence: int
    expires_at: str
    created_at: str
    updated_at: str
    settled_at: str | None = None
    abandoned_at: str | None = None
    abandonment_reason: str | None = None
    released_at: str | None = None
    output_collection_id: int | None = None
    work_document: dict[str, Any]
    work_document_sha256: str
    inputs: list[dict[str, Any]]
    plan: dict[str, Any] | None = None


class ProcessingClaimPageOut(RiverhogModel):
    page: int
    per_page: int
    total: int
    pages: int
    sort: str
    order: str
    filters: dict[str, Any]
    claims: list[ProcessingClaimOut]


class TransformCapabilityOut(RiverhogModel):
    format: Literal["riverhog-transform-capability/v1"]
    id: str
    claim_id: str
    fence: int
    audience: str
    actions: list[str]
    principal_app: str
    expires_at: str
    token: str


class CollectionDerivationOut(RiverhogModel):
    collection_id: int
    document_sha256: str
    derivation: dict[str, Any]
