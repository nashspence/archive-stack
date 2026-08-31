"""Catalog models for generic collection work claims and derivations."""

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
    work_id: Mapped[str] = mapped_column(String(64), nullable=False)
    consumer_app: Mapped[str] = mapped_column(String, nullable=False)
    consumer_key_id: Mapped[str | None] = mapped_column(String, nullable=True)
    purpose: Mapped[str] = mapped_column(String, nullable=False)
    work_document_json: Mapped[str] = mapped_column(Text, nullable=False)
    work_document_sha256: Mapped[str] = mapped_column(String(64), nullable=False)

    execution_id: Mapped[str | None] = mapped_column(String(64), nullable=True, unique=True)
    controller_evidence_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    controller_evidence_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    operation_id: Mapped[str | None] = mapped_column(String, nullable=True)
    operation_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    input_count: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    input_hash_state: Mapped[str | None] = mapped_column(Text, nullable=True)
    input_set_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    inputs_sealed_at: Mapped[str | None] = mapped_column(String, nullable=True)
    artifact_count: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    artifact_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    artifact_hash_state: Mapped[str | None] = mapped_column(Text, nullable=True)
    artifact_set_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    artifacts_sealed_at: Mapped[str | None] = mapped_column(String, nullable=True)
    output_tag_count: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    output_tag_hash_state: Mapped[str | None] = mapped_column(Text, nullable=True)
    output_tag_set_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    output_tags_sealed_at: Mapped[str | None] = mapped_column(String, nullable=True)
    outcome_count: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    outcome_state: Mapped[str] = mapped_column(String, nullable=False, default="receiving")
    outcome_hash_state: Mapped[str | None] = mapped_column(Text, nullable=True)
    outcome_validation_cursor: Mapped[str | None] = mapped_column(String, nullable=True)
    outcome_validation_count: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    outcome_set_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    outcome_failure: Mapped[str | None] = mapped_column(Text, nullable=True)
    outcomes_sealed_at: Mapped[str | None] = mapped_column(String, nullable=True)
    retirement_policy: Mapped[str | None] = mapped_column(String, nullable=True)
    retirement_grace_seconds: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    plan_sealed_at: Mapped[str | None] = mapped_column(String, nullable=True)

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
    abandoned_at: Mapped[str | None] = mapped_column(String, nullable=True)
    abandonment_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    released_at: Mapped[str | None] = mapped_column(String, nullable=True)

    __table_args__ = (
        UniqueConstraint(
            "consumer_app",
            "purpose",
            "work_id",
            name="uq_collection_processing_claims_owner_work",
        ),
        CheckConstraint(
            "state IN ('active','settled','retiring','abandoned','released')",
            name="ck_collection_processing_claims_state",
        ),
        CheckConstraint(
            "outcome_state IN ('receiving','sealing','sealed','failed')",
            name="ck_collection_processing_claims_outcome_state",
        ),
        CheckConstraint("fence >= 1", name="ck_collection_processing_claims_fence"),
        CheckConstraint(
            "retirement_grace_seconds >= 0",
            name="ck_collection_processing_claims_grace",
        ),
        CheckConstraint(
            "input_count >= 0 AND artifact_count >= 0 AND artifact_bytes >= 0 "
            "AND output_tag_count >= 0 AND outcome_count >= 0 "
            "AND outcome_validation_count >= 0",
            name="ck_collection_processing_claims_artifact_count",
        ),
        Index(
            "ix_collection_processing_claims_owner_state",
            "consumer_app",
            "state",
            "updated_at",
        ),
        Index(
            "ix_collection_processing_claims_owner_state_id",
            "consumer_app",
            "state",
            "id",
        ),
        Index(
            "ix_collection_processing_claims_expiry",
            "state",
            "expires_at",
        ),
        Index(
            "ix_collection_processing_claims_work",
            "work_id",
            "consumer_app",
        ),
        Index(
            "ix_collection_processing_claims_owner_created",
            "consumer_app",
            "created_at",
            "id",
        ),
        Index(
            "ix_collection_processing_claims_owner_updated",
            "consumer_app",
            "updated_at",
            "id",
        ),
        Index(
            "ix_collection_processing_claims_owner_expires",
            "consumer_app",
            "expires_at",
            "id",
        ),
        Index(
            "ix_collection_processing_claims_owner_work_id",
            "consumer_app",
            "work_id",
            "id",
        ),
        Index(
            "ix_collection_processing_claims_owner_execution",
            "consumer_app",
            "execution_id",
            "id",
        ),
        CheckConstraint("length(id) = 64", name="ck_collection_processing_claims_id"),
        CheckConstraint("length(work_id) = 64", name="ck_collection_processing_claims_work_id"),
        CheckConstraint(
            "length(work_document_sha256) = 64",
            name="ck_collection_processing_claims_document_sha256",
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
        primary_key=True,
    )
    collection_order: Mapped[int] = mapped_column(Integer, nullable=False)
    archive_root_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    content_identity: Mapped[str] = mapped_column(String(64), nullable=False)

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
        CheckConstraint("collection_order >= 0", name="ck_processing_claim_inputs_order"),
        CheckConstraint("length(archive_root_sha256) = 64", name="ck_claim_inputs_archive_root"),
        CheckConstraint("length(content_identity) = 64", name="ck_claim_inputs_content_identity"),
    )


class CollectionProcessingClaimArtifactRecord(Base):
    __tablename__ = "collection_processing_claim_artifacts"

    claim_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    collection_id: Mapped[int] = mapped_column(_COLLECTION_ID_TYPE, primary_key=True)
    path: Mapped[str] = mapped_column(String, primary_key=True)
    artifact_order: Mapped[int] = mapped_column(BigInteger, nullable=False)
    bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)

    __table_args__ = (
        ForeignKeyConstraint(
            ["claim_id", "collection_id"],
            [
                "collection_processing_claim_inputs.claim_id",
                "collection_processing_claim_inputs.collection_id",
            ],
            ondelete="CASCADE",
        ),
        Index(
            "ix_collection_processing_claim_artifacts_order",
            "claim_id",
            "artifact_order",
            unique=True,
        ),
        Index(
            "ix_collection_processing_claim_artifacts_collection",
            "collection_id",
            "path",
            "claim_id",
        ),
        CheckConstraint("bytes >= 0", name="ck_processing_claim_artifacts_bytes"),
        CheckConstraint("artifact_order >= 0", name="ck_processing_claim_artifacts_order"),
        CheckConstraint("length(sha256) = 64", name="ck_processing_claim_artifacts_sha256"),
    )


class CollectionProcessingClaimOutputTagRecord(Base):
    __tablename__ = "collection_processing_claim_output_tags"

    claim_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("collection_processing_claims.id", ondelete="CASCADE"),
        primary_key=True,
    )
    tag: Mapped[str] = mapped_column(String, primary_key=True)
    tag_order: Mapped[int] = mapped_column(BigInteger, nullable=False)

    __table_args__ = (
        Index(
            "ix_collection_processing_claim_output_tags_order",
            "claim_id",
            "tag_order",
            unique=True,
        ),
        CheckConstraint("tag_order >= 0", name="ck_processing_claim_output_tags_order"),
    )


class CollectionProcessingDispositionSetRecord(Base):
    __tablename__ = "collection_processing_disposition_sets"

    claim_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("collection_processing_claims.id", ondelete="CASCADE"),
        primary_key=True,
    )
    state: Mapped[str] = mapped_column(String, nullable=False)
    disposition_count: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    output_edge_count: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    output_artifact_count: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    transformed_count: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    transformed_with_outputs_count: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0
    )
    validation_phase: Mapped[str | None] = mapped_column(String, nullable=True)
    validation_collection_id: Mapped[int | None] = mapped_column(_COLLECTION_ID_TYPE, nullable=True)
    validation_input_path: Mapped[str | None] = mapped_column(String, nullable=True)
    validation_output_path: Mapped[str | None] = mapped_column(String, nullable=True)
    validation_output_collection_id: Mapped[int | None] = mapped_column(
        _COLLECTION_ID_TYPE, nullable=True
    )
    validation_output_input_path: Mapped[str | None] = mapped_column(String, nullable=True)
    disposition_hash_state: Mapped[str | None] = mapped_column(Text, nullable=True)
    output_hash_state: Mapped[str | None] = mapped_column(Text, nullable=True)
    disposition_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    output_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    identity_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    failure: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[str] = mapped_column(String, nullable=False)
    updated_at: Mapped[str] = mapped_column(String, nullable=False)
    sealed_at: Mapped[str | None] = mapped_column(String, nullable=True)

    __table_args__ = (
        CheckConstraint(
            "state IN ('receiving','sealing','sealed','failed')",
            name="ck_processing_disposition_sets_state",
        ),
        CheckConstraint(
            "validation_phase IS NULL OR validation_phase IN ('dispositions','outputs')",
            name="ck_processing_disposition_sets_phase",
        ),
        CheckConstraint(
            "disposition_count >= 0 AND output_edge_count >= 0 "
            "AND output_artifact_count >= 0 AND transformed_count >= 0 "
            "AND transformed_with_outputs_count >= 0",
            name="ck_processing_disposition_sets_counts",
        ),
        CheckConstraint(
            "output_artifact_count <= output_edge_count",
            name="ck_processing_disposition_sets_output_counts",
        ),
        Index("ix_processing_disposition_sets_state", "state", "updated_at", "claim_id"),
    )


class CollectionProcessingDispositionRecord(Base):
    __tablename__ = "collection_processing_dispositions"

    claim_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    collection_id: Mapped[int] = mapped_column(_COLLECTION_ID_TYPE, primary_key=True)
    path: Mapped[str] = mapped_column(String, primary_key=True)
    status: Mapped[str] = mapped_column(String, nullable=False)
    failure_code: Mapped[str | None] = mapped_column(String, nullable=True)
    failure_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        ForeignKeyConstraint(
            ["claim_id", "collection_id", "path"],
            [
                "collection_processing_claim_artifacts.claim_id",
                "collection_processing_claim_artifacts.collection_id",
                "collection_processing_claim_artifacts.path",
            ],
            ondelete="CASCADE",
        ),
        CheckConstraint(
            "status IN ('transformed','preserved','omitted','rejected')",
            name="ck_processing_dispositions_status",
        ),
        Index(
            "ix_processing_dispositions_order",
            "claim_id",
            "collection_id",
            "path",
        ),
    )


class CollectionProcessingDispositionOutputRecord(Base):
    __tablename__ = "collection_processing_disposition_outputs"

    claim_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    output_path: Mapped[str] = mapped_column(String, primary_key=True)
    input_collection_id: Mapped[int] = mapped_column(_COLLECTION_ID_TYPE, primary_key=True)
    input_path: Mapped[str] = mapped_column(String, primary_key=True)

    __table_args__ = (
        ForeignKeyConstraint(
            ["claim_id", "input_collection_id", "input_path"],
            [
                "collection_processing_dispositions.claim_id",
                "collection_processing_dispositions.collection_id",
                "collection_processing_dispositions.path",
            ],
            ondelete="CASCADE",
        ),
        Index(
            "ix_processing_disposition_outputs_source",
            "claim_id",
            "input_collection_id",
            "input_path",
            "output_path",
        ),
        Index(
            "ix_processing_disposition_outputs_order",
            "claim_id",
            "output_path",
            "input_collection_id",
            "input_path",
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
    audience: Mapped[str] = mapped_column(String(300), nullable=False)
    token_sha256: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    actions_json: Mapped[str] = mapped_column(Text, nullable=False)
    artifact_count: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    artifact_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    artifact_hash_state: Mapped[str | None] = mapped_column(Text, nullable=True)
    artifact_set_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    artifacts_sealed_at: Mapped[str | None] = mapped_column(String, nullable=True)
    state: Mapped[str] = mapped_column(String, nullable=False)
    expires_at: Mapped[str] = mapped_column(String, nullable=False)
    created_at: Mapped[str] = mapped_column(String, nullable=False)
    revoked_at: Mapped[str | None] = mapped_column(String, nullable=True)

    __table_args__ = (
        CheckConstraint(
            "state IN ('receiving','active','revoked')",
            name="ck_collection_transform_capabilities_state",
        ),
        CheckConstraint("fence >= 1", name="ck_collection_transform_capabilities_fence"),
        CheckConstraint(
            "artifact_count >= 0 AND artifact_bytes >= 0",
            name="ck_collection_transform_capabilities_artifact_totals",
        ),
        Index(
            "ix_collection_transform_capabilities_claim_state",
            "claim_id",
            "state",
            "expires_at",
        ),
    )


class CollectionTransformCapabilityArtifactRecord(Base):
    __tablename__ = "collection_transform_capability_artifacts"

    capability_id: Mapped[str] = mapped_column(
        String(32),
        ForeignKey("collection_transform_capabilities.id", ondelete="CASCADE"),
        primary_key=True,
    )
    collection_id: Mapped[int] = mapped_column(_COLLECTION_ID_TYPE, primary_key=True)
    path: Mapped[str] = mapped_column(String, primary_key=True)
    artifact_order: Mapped[int] = mapped_column(BigInteger, nullable=False)
    bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)

    __table_args__ = (
        Index(
            "ix_collection_transform_capability_artifacts_order",
            "capability_id",
            "artifact_order",
            unique=True,
        ),
        Index(
            "ix_collection_transform_capability_artifacts_collection",
            "collection_id",
            "path",
            "capability_id",
        ),
        CheckConstraint("bytes >= 0", name="ck_capability_artifacts_bytes"),
        CheckConstraint("artifact_order >= 0", name="ck_capability_artifacts_order"),
        CheckConstraint("length(sha256) = 64", name="ck_capability_artifacts_sha256"),
    )


class CollectionProcessingOutcomeRecord(Base):
    __tablename__ = "collection_processing_outcomes"

    claim_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("collection_processing_claims.id", ondelete="CASCADE"),
        primary_key=True,
    )
    outcome_id: Mapped[str] = mapped_column(String(160), primary_key=True)
    source_claim_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("collection_processing_claims.id", ondelete="RESTRICT"),
        nullable=False,
    )
    collection_id: Mapped[int] = mapped_column(_COLLECTION_ID_TYPE, nullable=False)
    archive_root_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    content_identity: Mapped[str] = mapped_column(String(64), nullable=False)
    derivation_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    outcome_order: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    created_at: Mapped[str] = mapped_column(String, nullable=False)

    __table_args__ = (
        UniqueConstraint(
            "claim_id",
            "source_claim_id",
            name="uq_collection_processing_outcomes_source_claim",
        ),
        UniqueConstraint(
            "claim_id",
            "collection_id",
            name="uq_collection_processing_outcomes_output",
        ),
        Index(
            "ix_collection_processing_outcomes_collection",
            "collection_id",
            "claim_id",
        ),
        Index(
            "ix_collection_processing_outcomes_order",
            "claim_id",
            "outcome_order",
            unique=True,
        ),
        CheckConstraint(
            "outcome_order IS NULL OR outcome_order >= 0",
            name="ck_collection_processing_outcomes_order",
        ),
    )


class CollectionDerivationRecord(Base):
    __tablename__ = "collection_derivations"

    collection_id: Mapped[int] = mapped_column(
        _COLLECTION_ID_TYPE,
        ForeignKey("collections.id", ondelete="CASCADE"),
        primary_key=True,
    )
    execution_id: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
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
    "CollectionProcessingDispositionOutputRecord",
    "CollectionProcessingDispositionRecord",
    "CollectionProcessingDispositionSetRecord",
    "CollectionProcessingClaimArtifactRecord",
    "CollectionProcessingClaimInputRecord",
    "CollectionProcessingClaimRecord",
    "CollectionProcessingClaimOutputTagRecord",
    "CollectionProcessingOutcomeRecord",
    "CollectionTransformCapabilityArtifactRecord",
    "CollectionTransformCapabilityRecord",
]
