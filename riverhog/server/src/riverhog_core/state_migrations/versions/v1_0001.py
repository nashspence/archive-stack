"""Establish the first supported v1 catalog schema revision."""

revision: str = "v1_0001"
down_revision: str | None = None
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    """The exact v1 baseline is created before this revision is stamped."""


def downgrade() -> None:
    raise RuntimeError("Riverhog state migrations are forward-only")
