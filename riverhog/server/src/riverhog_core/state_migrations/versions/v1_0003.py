"""Finalize the hard-cut v1 retrieval and collection-workflow schema."""

import hashlib
import json
from typing import Any

from alembic import op
from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Column,
    ForeignKey,
    ForeignKeyConstraint,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)

revision: str = "v1_0003"
down_revision: str | None = "v1_0002"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    connection = op.get_bind()
    rows = list(
        connection.execute(text("SELECT id, constraints_json FROM retrieval_jobs")).mappings()
    )
    for row in rows:
        plan = _normalize_retrieval_plan(json.loads(str(row["constraints_json"])))
        connection.execute(
            text(
                "UPDATE retrieval_jobs "
                "SET plan_etag = :plan_etag, constraints_json = :constraints_json "
                "WHERE id = :job_id"
            ),
            {
                "job_id": str(row["id"]),
                "plan_etag": str(plan["etag"]),
                "constraints_json": json.dumps(plan, sort_keys=True, separators=(",", ":")),
            },
        )
    _create_collection_workflow_schema()


def _create_collection_workflow_schema() -> None:
    collection_id = BigInteger().with_variant(Integer, "sqlite")
    op.create_table(
        "collection_processing_claims",
        Column("id", String(64), primary_key=True),
        Column("work_id", String(64), nullable=False),
        Column("consumer_app", String(), nullable=False),
        Column("consumer_key_id", String(), nullable=True),
        Column("purpose", String(), nullable=False),
        Column("work_document_json", Text(), nullable=False),
        Column("work_document_sha256", String(64), nullable=False),
        Column("execution_id", String(64), nullable=True, unique=True),
        Column("controller_evidence_json", Text(), nullable=True),
        Column("controller_evidence_sha256", String(64), nullable=True),
        Column("operation_id", String(), nullable=True),
        Column("operation_sha256", String(64), nullable=True),
        Column("output_tags_json", Text(), nullable=True),
        Column("retirement_policy", String(), nullable=True),
        Column(
            "retirement_grace_seconds",
            BigInteger(),
            nullable=False,
            server_default="0",
        ),
        Column("plan_sealed_at", String(), nullable=True),
        Column("state", String(), nullable=False),
        Column("fence", BigInteger(), nullable=False),
        Column("expires_at", String(), nullable=False),
        Column(
            "output_collection_id",
            collection_id,
            ForeignKey("collections.id", ondelete="SET NULL"),
            nullable=True,
        ),
        Column("created_at", String(), nullable=False),
        Column("updated_at", String(), nullable=False),
        Column("settled_at", String(), nullable=True),
        Column("abandoned_at", String(), nullable=True),
        Column("abandonment_reason", Text(), nullable=True),
        Column("released_at", String(), nullable=True),
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
        CheckConstraint("fence >= 1", name="ck_collection_processing_claims_fence"),
        CheckConstraint(
            "retirement_grace_seconds >= 0",
            name="ck_collection_processing_claims_grace",
        ),
    )
    op.create_index(
        "ix_collection_processing_claims_owner_state",
        "collection_processing_claims",
        ["consumer_app", "state", "updated_at"],
    )
    op.create_index(
        "ix_collection_processing_claims_expiry",
        "collection_processing_claims",
        ["state", "expires_at"],
    )
    op.create_index(
        "ix_collection_processing_claims_work",
        "collection_processing_claims",
        ["work_id", "consumer_app"],
    )
    op.create_table(
        "collection_processing_claim_inputs",
        Column(
            "claim_id",
            String(64),
            ForeignKey("collection_processing_claims.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        Column(
            "collection_id",
            collection_id,
            primary_key=True,
        ),
        Column("collection_order", Integer(), nullable=False),
        Column("manifest_sha256", String(64), nullable=False),
        Column("content_etag", String(64), nullable=False),
        UniqueConstraint(
            "claim_id",
            "collection_order",
            name="uq_collection_processing_claim_inputs_order",
        ),
    )
    op.create_index(
        "ix_collection_processing_claim_inputs_collection",
        "collection_processing_claim_inputs",
        ["collection_id", "claim_id"],
    )
    op.create_table(
        "collection_transform_capabilities",
        Column("id", String(32), primary_key=True),
        Column(
            "claim_id",
            String(64),
            ForeignKey("collection_processing_claims.id", ondelete="CASCADE"),
            nullable=False,
        ),
        Column("fence", BigInteger(), nullable=False),
        Column("audience", String(300), nullable=False),
        Column("token_sha256", String(64), nullable=False, unique=True),
        Column("actions_json", Text(), nullable=False),
        Column("state", String(), nullable=False),
        Column("expires_at", String(), nullable=False),
        Column("created_at", String(), nullable=False),
        Column("revoked_at", String(), nullable=True),
        CheckConstraint(
            "state IN ('active','revoked')",
            name="ck_collection_transform_capabilities_state",
        ),
        CheckConstraint(
            "fence >= 1",
            name="ck_collection_transform_capabilities_fence",
        ),
    )
    op.create_index(
        "ix_collection_transform_capabilities_claim_state",
        "collection_transform_capabilities",
        ["claim_id", "state", "expires_at"],
    )
    op.create_table(
        "collection_derivations",
        Column(
            "collection_id",
            collection_id,
            ForeignKey("collections.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        Column("execution_id", String(64), nullable=False, unique=True),
        Column("claim_id", String(64), nullable=False),
        Column("fence", BigInteger(), nullable=False),
        Column("document_json", Text(), nullable=False),
        Column("document_sha256", String(64), nullable=False),
        Column("created_at", String(), nullable=False),
        ForeignKeyConstraint(
            ["claim_id"], ["collection_processing_claims.id"], ondelete="RESTRICT"
        ),
        CheckConstraint("fence >= 1", name="ck_collection_derivations_fence"),
    )
    op.create_index(
        "ix_collection_derivations_claim",
        "collection_derivations",
        ["claim_id", "collection_id"],
    )


def _normalize_retrieval_plan(value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("persisted retrieval plan must be an object")
    plan = dict(value)
    objects = plan.get("objects")
    if not isinstance(objects, list) or any(not isinstance(current, dict) for current in objects):
        raise ValueError("persisted retrieval plan objects are invalid")
    plan.pop("etag", None)
    plan["restore_policy"] = "allow"
    plan["requires_restore"] = any(
        current.get("read_mode") == "restore_required" for current in objects
    )
    plan["etag"] = hashlib.sha256(
        json.dumps(plan, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return plan


def downgrade() -> None:
    raise RuntimeError("Riverhog state migrations are forward-only")
