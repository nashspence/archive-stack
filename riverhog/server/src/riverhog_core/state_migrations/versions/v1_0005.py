"""Remove provider ontology from Riverhog archive state."""

from alembic import op

revision: str = "v1_0005"
down_revision: str | None = "v1_0004"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    with op.batch_alter_table("collection_archive_objects") as batch:
        batch.drop_column("storage_class")
        batch.drop_column("backend")
    with op.batch_alter_table("collection_archive_copies") as batch:
        batch.drop_column("storage_class")
        batch.drop_column("backend")


def downgrade() -> None:
    raise RuntimeError("Riverhog state migrations are forward-only")
