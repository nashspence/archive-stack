"""Create the exact current v1 Mango Fish cursor-state baseline."""

from alembic import op

revision: str = "v1_0001"
down_revision: str | None = None
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.get_bind().exec_driver_sql(
        "CREATE TABLE source_cursors (source TEXT PRIMARY KEY, cursor TEXT NOT NULL)"
    )


def downgrade() -> None:
    raise RuntimeError("Mango Fish state migrations are forward-only")
