"""Seal exact artifact scope into collection work and capabilities."""

from alembic import op
from sqlalchemy import (
    BigInteger,
    Column,
    ForeignKey,
    ForeignKeyConstraint,
    Integer,
    String,
    UniqueConstraint,
)

revision: str = "v1_0004"
down_revision: str | None = "v1_0003"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    collection_id = BigInteger().with_variant(Integer, "sqlite")
    op.create_table(
        "collection_processing_claim_artifacts",
        Column("claim_id", String(64), primary_key=True),
        Column("collection_id", collection_id, primary_key=True),
        Column("path", String(), primary_key=True),
        Column("bytes", BigInteger(), nullable=False),
        Column("sha256", String(64), nullable=False),
        ForeignKeyConstraint(
            ["claim_id", "collection_id"],
            [
                "collection_processing_claim_inputs.claim_id",
                "collection_processing_claim_inputs.collection_id",
            ],
            ondelete="CASCADE",
        ),
    )
    op.create_index(
        "ix_collection_processing_claim_artifacts_collection",
        "collection_processing_claim_artifacts",
        ["collection_id", "path", "claim_id"],
    )
    op.create_table(
        "collection_transform_capability_artifacts",
        Column(
            "capability_id",
            String(32),
            ForeignKey("collection_transform_capabilities.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        Column("collection_id", collection_id, primary_key=True),
        Column("path", String(), primary_key=True),
        Column("bytes", BigInteger(), nullable=False),
        Column("sha256", String(64), nullable=False),
    )
    op.create_index(
        "ix_collection_transform_capability_artifacts_collection",
        "collection_transform_capability_artifacts",
        ["collection_id", "path", "capability_id"],
    )
    op.create_table(
        "collection_processing_outcomes",
        Column(
            "claim_id",
            String(64),
            ForeignKey("collection_processing_claims.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        Column("outcome_id", String(160), primary_key=True),
        Column(
            "source_claim_id",
            String(64),
            ForeignKey("collection_processing_claims.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        Column("collection_id", collection_id, nullable=False),
        Column("manifest_sha256", String(64), nullable=False),
        Column("content_etag", String(64), nullable=False),
        Column("derivation_sha256", String(64), nullable=False),
        Column("created_at", String(), nullable=False),
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
    )
    op.create_index(
        "ix_collection_processing_outcomes_collection",
        "collection_processing_outcomes",
        ["collection_id", "claim_id"],
    )


def downgrade() -> None:
    raise RuntimeError("Riverhog state migrations are forward-only")
