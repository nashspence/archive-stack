"""Add the Riverhog v1 provenance catalog and upload projection."""

from alembic import op
from sqlalchemy import (
    BigInteger,
    Column,
    ForeignKeyConstraint,
    LargeBinary,
    PrimaryKeyConstraint,
    String,
    Text,
)

revision: str = "v1_0002"
down_revision: str | None = "v1_0001"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column(
        "collections",
        Column("provenance_mode", String(), nullable=False, server_default="omitted"),
    )
    op.add_column("collections", Column("provenance_etag", String(64), nullable=True))
    op.add_column(
        "collection_uploads",
        Column("provenance_mode", String(), nullable=False, server_default="captured"),
    )
    op.add_column("collection_uploads", Column("provenance_omission_reason", Text(), nullable=True))
    op.add_column("collection_uploads", Column("provenance_etag", String(64), nullable=True))
    op.add_column(
        "collection_upload_files",
        Column("provenance_status", String(), nullable=False, server_default="captured"),
    )
    op.add_column(
        "collection_upload_files", Column("provenance_journal_id", String(), nullable=True)
    )
    op.add_column(
        "collection_upload_files", Column("provenance_current_state_id", String(), nullable=True)
    )
    op.add_column(
        "collection_upload_files", Column("provenance_omission_reason", Text(), nullable=True)
    )

    op.create_table(
        "collection_provenance_journals",
        Column("collection_id", BigInteger(), nullable=False),
        Column("journal_id", String(), nullable=False),
        Column("journal_bytes", LargeBinary(), nullable=False),
        Column("bytes", BigInteger(), nullable=False),
        Column("sha256", String(64), nullable=False),
        Column("current_state_id", String(), nullable=False),
        Column("current_path", String(), nullable=False),
        Column("current_bytes", BigInteger(), nullable=False),
        Column("current_sha256", String(64), nullable=False),
        ForeignKeyConstraint(["collection_id"], ["collections.id"], ondelete="CASCADE"),
        PrimaryKeyConstraint("collection_id", "journal_id"),
    )
    op.create_index(
        "ix_collection_provenance_journals_sha256",
        "collection_provenance_journals",
        ["sha256", "collection_id"],
    )
    op.create_table(
        "collection_file_provenance",
        Column("collection_id", BigInteger(), nullable=False),
        Column("path", String(), nullable=False),
        Column("status", String(), nullable=False),
        Column("journal_id", String(), nullable=True),
        Column("current_state_id", String(), nullable=True),
        Column("omission_reason", Text(), nullable=True),
        ForeignKeyConstraint(
            ["collection_id", "path"],
            ["collection_files.collection_id", "collection_files.path"],
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["collection_id", "journal_id"],
            [
                "collection_provenance_journals.collection_id",
                "collection_provenance_journals.journal_id",
            ],
            ondelete="CASCADE",
        ),
        PrimaryKeyConstraint("collection_id", "path"),
    )
    op.create_index(
        "ix_collection_file_provenance_journal",
        "collection_file_provenance",
        ["collection_id", "journal_id"],
    )
    op.create_table(
        "collection_provenance_entities",
        Column("collection_id", BigInteger(), nullable=False),
        Column("journal_id", String(), nullable=False),
        Column("entity_type", String(), nullable=False),
        Column("entity_id", String(), nullable=False),
        Column("entry_id", String(), nullable=False),
        Column("document_json", Text(), nullable=False),
        ForeignKeyConstraint(
            ["collection_id", "journal_id"],
            [
                "collection_provenance_journals.collection_id",
                "collection_provenance_journals.journal_id",
            ],
            ondelete="CASCADE",
        ),
        PrimaryKeyConstraint("collection_id", "journal_id", "entity_type", "entity_id"),
    )
    op.create_index(
        "ix_collection_provenance_entities_type",
        "collection_provenance_entities",
        ["collection_id", "entity_type", "entity_id"],
    )
    op.create_table(
        "collection_upload_provenance_journals",
        Column("collection_id", BigInteger(), nullable=False),
        Column("journal_id", String(), nullable=False),
        Column("journal_bytes", LargeBinary(), nullable=False),
        Column("bytes", BigInteger(), nullable=False),
        Column("sha256", String(64), nullable=False),
        Column("current_state_id", String(), nullable=False),
        Column("current_path", String(), nullable=False),
        Column("current_bytes", BigInteger(), nullable=False),
        Column("current_sha256", String(64), nullable=False),
        ForeignKeyConstraint(
            ["collection_id"], ["collection_uploads.collection_id"], ondelete="CASCADE"
        ),
        PrimaryKeyConstraint("collection_id", "journal_id"),
    )


def downgrade() -> None:
    raise RuntimeError("Riverhog state migrations are forward-only")
