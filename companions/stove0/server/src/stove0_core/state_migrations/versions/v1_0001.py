"""Create the exact current v1 Stove0 control-state baseline."""

from alembic import op

revision: str = "v1_0001"
down_revision: str | None = None
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    # The unreleased baseline deliberately follows the current model authority.
    # There is no pre-v1 schema compatibility contract.
    from stove0_core.persistence import _Base  # noqa: PLC0415

    if op.get_bind().dialect.name == "postgresql":
        op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm WITH SCHEMA public")
    _Base.metadata.create_all(op.get_bind())


def downgrade() -> None:
    raise RuntimeError("stove0 state migrations are forward-only")
