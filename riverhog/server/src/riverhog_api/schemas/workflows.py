from __future__ import annotations

from typing import Any, Literal

from pydantic import Field

from riverhog_api.schemas.common import RiverhogModel


class RecipeIdentityIn(RiverhogModel):
    id: str = Field(min_length=1, max_length=160)
    revision: int = Field(ge=1)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class OperationIdentityIn(RiverhogModel):
    id: str = Field(min_length=1, max_length=160)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class ProcessingClaimCreateIn(RiverhogModel):
    input_collection_ids: list[int] = Field(min_length=1, max_length=1000)
    recipe: RecipeIdentityIn
    operation: OperationIdentityIn
    effective_intent: dict[str, Any] = Field(default_factory=dict)
    output_tags: list[str] = Field(min_length=1, max_length=100)
    retirement_policy: Literal["retain", "retire-after-verified-output"] = "retain"
    retirement_grace_seconds: int = Field(default=0, ge=0)
    lease_seconds: int = Field(default=1800, ge=30, le=86400)
    purpose: str = Field(default="collection-transform/v1", min_length=1, max_length=160)


class ProcessingClaimRenewIn(RiverhogModel):
    fence: int = Field(ge=1)
    lease_seconds: int = Field(default=1800, ge=30, le=86400)


class TransformCapabilityCreateIn(RiverhogModel):
    fence: int = Field(ge=1)
    actions: list[Literal["read-inputs", "write-output"]] = Field(
        default_factory=lambda: ["read-inputs", "write-output"],
        min_length=1,
    )
    ttl_seconds: int = Field(default=900, ge=30, le=86400)


class ProcessingClaimSettleIn(RiverhogModel):
    fence: int = Field(ge=1)
    output_collection_id: int = Field(ge=1)
    derivation: dict[str, Any]


class ProcessingClaimFenceIn(RiverhogModel):
    fence: int = Field(ge=1)


class ProcessingClaimOut(RiverhogModel):
    format: Literal["riverhog-processing-claim/v1"]
    id: str
    transform_id: str
    consumer: dict[str, Any]
    purpose: str
    state: Literal["active", "settled", "retiring", "released"]
    fence: int
    expires_at: str
    created_at: str
    updated_at: str
    settled_at: str | None = None
    released_at: str | None = None
    output_collection_id: int | None = None
    intent: dict[str, Any]
    inputs: list[dict[str, Any]]


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
    actions: list[str]
    expires_at: str
    token: str


class CollectionDerivationOut(RiverhogModel):
    collection_id: int
    document_sha256: str
    derivation: dict[str, Any]
