"""Catalog models for collection workflow claims and derivations."""

from __future__ import annotations

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from riverhog_core.catalog_base import Base

_COLLECTION_ID_TYPE = BigInteger().with_variant(Integer, "sqlite")


class CollectionProcessingClaimRecord(Base):
    __tablename__ = "collection_processing_claims"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    transform_id: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    consumer_app: Mapped[str] = mapped_column(String, nullable=False)
    consumer_key_id: Mapped[str | None] = mapped_column(String, nullable=True)
    purpose: Mapped[str] = mapped_column(String, nullable=False)
    intent_json: Mapped[str] = mapped_column(Text, nullable=False)
    recipe_id: Mapped[str] = mapped_column(String, nullable=False)
    recipe_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    recipe_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    operation_id: Mapped[str] = mapped_column(String, nullable=False)
    operation_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    output_tags_json: Mapped[str] = mapped_column(Text, nullable=False)
    retirement_policy: Mapped[str] = mapped_column(String, nullable=False)
    retirement_grace_seconds: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    state: Mapped[str] = mapped_column(String, nullable=False)
    fence: Mapped[int] = mapped_column(BigInteger, nullable=False)
    expires_at: Mapped[str] = mapped_column(String, nullable=False)
    output_collection_id: Mapped[int | None] = mapped_column(
        _COLLECTION_ID_TYPE,
        ForeignKey("collections.id", ondelete="SET NULL"),
        nullable=True,
    )
    created_at: Mapped[str] = mapped_column(String, nullable=False)
    updated_at: Mapped[str] = mapped_column(String, nullable=False)
    settled_at: Mapped[str | None] = mapped_column(String, nullable=True)
    released_at: Mapped[str | None] = mapped_column(String, nullable=True)

    __table_args__ = (
        CheckConstraint(
            "state IN ('active','settled','retiring','released')",
            name="ck_collection_processing_claims_state",
        ),
        CheckConstraint("fence >= 1", name="ck_collection_processing_claims_fence"),
        CheckConstraint(
            "retirement_grace_seconds >= 0",
            name="ck_collection_processing_claims_grace",
        ),
        Index(
            "ix_collection_processing_claims_owner_state",
            "consumer_app",
            "state",
            "updated_at",
        ),
        Index(
            "ix_collection_processing_claims_expiry",
            "state",
            "expires_at",
        ),
    )


class CollectionProcessingClaimInputRecord(Base):
    __tablename__ = "collection_processing_claim_inputs"

    claim_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("collection_processing_claims.id", ondelete="CASCADE"),
        primary_key=True,
    )
    collection_id: Mapped[int] = mapped_column(
        _COLLECTION_ID_TYPE,
        ForeignKey("collections.id", ondelete="CASCADE"),
        primary_key=True,
    )
    collection_order: Mapped[int] = mapped_column(Integer, nullable=False)
    manifest_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    content_etag: Mapped[str] = mapped_column(String(64), nullable=False)

    __table_args__ = (
        UniqueConstraint(
            "claim_id",
            "collection_order",
            name="uq_collection_processing_claim_inputs_order",
        ),
        Index(
            "ix_collection_processing_claim_inputs_collection",
            "collection_id",
            "claim_id",
        ),
    )


class CollectionTransformCapabilityRecord(Base):
    __tablename__ = "collection_transform_capabilities"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    claim_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("collection_processing_claims.id", ondelete="CASCADE"),
        nullable=False,
    )
    fence: Mapped[int] = mapped_column(BigInteger, nullable=False)
    token_sha256: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    actions_json: Mapped[str] = mapped_column(Text, nullable=False)
    state: Mapped[str] = mapped_column(String, nullable=False)
    expires_at: Mapped[str] = mapped_column(String, nullable=False)
    created_at: Mapped[str] = mapped_column(String, nullable=False)
    revoked_at: Mapped[str | None] = mapped_column(String, nullable=True)

    __table_args__ = (
        CheckConstraint(
            "state IN ('active','revoked')",
            name="ck_collection_transform_capabilities_state",
        ),
        CheckConstraint("fence >= 1", name="ck_collection_transform_capabilities_fence"),
        Index(
            "ix_collection_transform_capabilities_claim_state",
            "claim_id",
            "state",
            "expires_at",
        ),
    )


class CollectionDerivationRecord(Base):
    __tablename__ = "collection_derivations"

    collection_id: Mapped[int] = mapped_column(
        _COLLECTION_ID_TYPE,
        ForeignKey("collections.id", ondelete="CASCADE"),
        primary_key=True,
    )
    transform_id: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    claim_id: Mapped[str] = mapped_column(String(64), nullable=False)
    fence: Mapped[int] = mapped_column(BigInteger, nullable=False)
    document_json: Mapped[str] = mapped_column(Text, nullable=False)
    document_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[str] = mapped_column(String, nullable=False)

    __table_args__ = (
        ForeignKeyConstraint(
            ["claim_id"],
            ["collection_processing_claims.id"],
            ondelete="RESTRICT",
        ),
        CheckConstraint("fence >= 1", name="ck_collection_derivations_fence"),
        Index("ix_collection_derivations_claim", "claim_id", "collection_id"),
    )


__all__ = [
    "CollectionDerivationRecord",
    "CollectionProcessingClaimInputRecord",
    "CollectionProcessingClaimRecord",
    "CollectionTransformCapabilityRecord",
]
