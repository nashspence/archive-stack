"""Add content-addressed Stove0 artifact selections."""

from alembic import op
from sqlalchemy import Column, String, Text

revision: str = "v1_0002"
down_revision: str | None = "v1_0001"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "stove0_artifact_selections",
        Column("selection_sha256", String(64), primary_key=True),
        Column("document_json", Text(), nullable=False),
    )


def downgrade() -> None:
    raise RuntimeError("stove0 state migrations are forward-only")
