"""Hard-cut provider-shaped storage state to storage-adapter evidence."""

from alembic import op
from sqlalchemy import Column, String

revision: str = "v1_0005"
down_revision: str | None = "v1_0004"
branch_labels: str | None = None
depends_on: str | None = None

_UNRESUMABLE_RUNTIME_PIN = "0" * 64


def upgrade() -> None:
    with op.batch_alter_table("collection_archive_copies") as batch:
        batch.drop_column("backend")
        batch.drop_column("storage_class")
        batch.add_column(Column("storage_adapter", String(), nullable=True))
        batch.add_column(Column("storage_profile_id", String(), nullable=True))
        batch.add_column(
            Column("storage_profile_contract_sha256", String(64), nullable=True)
        )
        batch.add_column(Column("egress_accounting_id", String(), nullable=True))
        batch.add_column(Column("read_mode", String(), nullable=True))
        batch.add_column(Column("adapter_implementation_id", String(), nullable=True))
        batch.add_column(Column("adapter_implementation_version", String(), nullable=True))
        batch.add_column(Column("adapter_source_revision", String(), nullable=True))
        batch.add_column(
            Column("adapter_runtime_descriptor_sha256", String(64), nullable=True)
        )

    with op.batch_alter_table("collection_archive_objects") as batch:
        batch.alter_column("version_id", new_column_name="revision")
        batch.drop_column("backend")
        batch.drop_column("storage_class")

    with op.batch_alter_table("collection_metadata_publications") as batch:
        batch.alter_column("version_id", new_column_name="revision")

    with op.batch_alter_table("retrieval_cache_objects") as batch:
        batch.alter_column("version_id", new_column_name="revision")

    with op.batch_alter_table("archive_copy_object_uploads") as batch:
        batch.alter_column("multipart_upload_id", new_column_name="multipart_transfer_id")
        batch.add_column(
            Column("adapter_runtime_descriptor_sha256", String(64), nullable=True)
        )

    with op.batch_alter_table("archive_copy_jobs") as batch:
        batch.add_column(
            Column(
                "storage_adapter_runtime_descriptor_sha256",
                String(64),
                nullable=False,
                server_default=_UNRESUMABLE_RUNTIME_PIN,
            )
        )
        batch.alter_column(
            "storage_adapter_runtime_descriptor_sha256",
            server_default=None,
        )

    with op.batch_alter_table("collection_uploads") as batch:
        batch.add_column(
            Column(
                "storage_adapter_runtime_descriptor_sha256",
                String(64),
                nullable=False,
                server_default=_UNRESUMABLE_RUNTIME_PIN,
            )
        )
        batch.alter_column(
            "storage_adapter_runtime_descriptor_sha256",
            server_default=None,
        )


def downgrade() -> None:
    raise RuntimeError("Riverhog state migrations are forward-only")
