"""Add explicit custody-transfer upload leases and artifact receipts."""

from alembic import op
from sqlalchemy import Column, String, Text

revision: str = "v1_0009"
down_revision: str | None = "v1_0008"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    with op.batch_alter_table("collection_uploads") as batch:
        batch.add_column(
            Column(
                "custody_mode",
                String(),
                nullable=False,
                server_default="producer-retained",
            )
        )
        batch.add_column(Column("lease_expires_at", String(), nullable=True))
        batch.add_column(Column("orphaned_at", String(), nullable=True))
    with op.batch_alter_table("collection_upload_files") as batch:
        batch.add_column(Column("custodied_at", String(), nullable=True))
        batch.add_column(Column("custody_receipt_json", Text(), nullable=True))


def downgrade() -> None:
    raise RuntimeError("Riverhog state migrations are forward-only")
