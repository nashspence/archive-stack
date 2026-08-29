"""Create the exact current v1 Riverhog catalog baseline."""

from alembic import op

revision: str = "v1_0001"
down_revision: str | None = None
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    # The unreleased baseline deliberately follows the current model authority.
    # There is no pre-v1 schema compatibility contract.
    from riverhog_core import catalog_models, catalog_workflow_models  # noqa: PLC0415
    from riverhog_core.catalog_base import Base  # noqa: PLC0415

    _ = (catalog_models, catalog_workflow_models)
    if op.get_bind().dialect.name == "postgresql":
        op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm WITH SCHEMA public")
    Base.metadata.create_all(op.get_bind())


def downgrade() -> None:
    raise RuntimeError("Riverhog state migrations are forward-only")
