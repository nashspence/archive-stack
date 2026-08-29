"""Create the exact current v1 Mango Fish cursor-state baseline."""

from alembic import op

revision: str = "v1_0001"
down_revision: str | None = None
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    from mango_fish.schema import MANGO_FISH_STATE_METADATA  # noqa: PLC0415

    MANGO_FISH_STATE_METADATA.create_all(op.get_bind())


def downgrade() -> None:
    raise RuntimeError("Mango Fish state migrations are forward-only")
