"""Create the exact current v1 Riverhog catalog baseline."""

from alembic import op

revision: str = "v1_0001"
down_revision: str | None = None
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    from riverhog_core.state_migrations.v1_ddl import (  # noqa: PLC0415
        POSTGRESQL_DDL,
        SQLITE_DDL,
    )

    dialect = op.get_bind().dialect.name
    statements = POSTGRESQL_DDL if dialect == "postgresql" else SQLITE_DDL
    for statement in statements:
        op.execute(statement)


def downgrade() -> None:
    raise RuntimeError("Riverhog state migrations are forward-only")
