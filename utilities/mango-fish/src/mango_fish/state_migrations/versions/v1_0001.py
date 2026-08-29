"""Create the exact current v1 Mango Fish cursor-state baseline."""

from alembic import op

revision: str = "v1_0001"
down_revision: str | None = None
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    from mango_fish.state_migrations.v1_ddl import SQLITE_DDL  # noqa: PLC0415

    for statement in SQLITE_DDL:
        op.execute(statement)


def downgrade() -> None:
    raise RuntimeError("Mango Fish state migrations are forward-only")
