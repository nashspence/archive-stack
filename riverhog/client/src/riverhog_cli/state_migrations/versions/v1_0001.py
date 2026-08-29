"""Create the exact current v1 Riverhog client local-state baseline."""

from alembic import op

revision: str = "v1_0001"
down_revision: str | None = None
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    from riverhog_cli.local_state import LOCAL_STATE_METADATA  # noqa: PLC0415

    LOCAL_STATE_METADATA.create_all(op.get_bind())


def downgrade() -> None:
    raise RuntimeError("Riverhog local-state migrations are forward-only")
