"""Add the Riverhog v1 provenance catalog and upload projection."""

import hashlib
import json
from collections import defaultdict
from typing import Any

from alembic import op
from sqlalchemy import (
    BigInteger,
    Column,
    ForeignKeyConstraint,
    LargeBinary,
    PrimaryKeyConstraint,
    String,
    Text,
    text,
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
        Column("entries", BigInteger(), nullable=False),
        Column("agent_ids_json", Text(), nullable=False),
        Column("entity_counts_json", Text(), nullable=False),
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
        "collection_provenance_lineage_edges",
        Column("collection_id", BigInteger(), nullable=False),
        Column("from_journal_id", String(), nullable=False),
        Column("to_journal_id", String(), nullable=False),
        Column("entry_id", String(), nullable=False),
        Column("state_id", String(), nullable=False),
        Column("entry_json_sha256", String(64), nullable=False),
        ForeignKeyConstraint(
            ["collection_id", "from_journal_id"],
            [
                "collection_provenance_journals.collection_id",
                "collection_provenance_journals.journal_id",
            ],
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["collection_id", "to_journal_id"],
            [
                "collection_provenance_journals.collection_id",
                "collection_provenance_journals.journal_id",
            ],
            ondelete="CASCADE",
        ),
        PrimaryKeyConstraint(
            "collection_id",
            "from_journal_id",
            "to_journal_id",
            "entry_id",
            "state_id",
        ),
    )
    op.create_index(
        "ix_collection_provenance_lineage_edges_target",
        "collection_provenance_lineage_edges",
        ["collection_id", "to_journal_id"],
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
    _rebuild_collection_record_etags()


def _rebuild_collection_record_etags() -> None:
    connection = op.get_bind()
    tags: dict[int, list[str]] = defaultdict(list)
    for row in connection.execute(
        text("SELECT collection_id, tag_id FROM collection_tags ORDER BY collection_id, tag_id")
    ).mappings():
        tags[int(row["collection_id"])].append(str(row["tag_id"]))

    files: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for row in connection.execute(
        text(
            "SELECT collection_id, path, bytes, sha256 "
            "FROM collection_files ORDER BY collection_id, path"
        )
    ).mappings():
        files[int(row["collection_id"])].append(
            {
                "path": str(row["path"]),
                "bytes": int(row["bytes"]),
                "sha256": str(row["sha256"]),
            }
        )

    collections = connection.execute(
        text(
            "SELECT id, content_etag, provenance_mode, provenance_etag, metadata_revision "
            "FROM collections ORDER BY id"
        )
    ).mappings()
    for row in collections:
        collection_id = int(row["id"])
        payload = {
            "format": "riverhog-collection/v1",
            "collection": collection_id,
            "content_etag": str(row["content_etag"]),
            "provenance_mode": str(row["provenance_mode"]),
            "provenance_etag": row["provenance_etag"],
            "metadata_revision": int(row["metadata_revision"]),
            "tags": tags[collection_id],
            "files": files[collection_id],
        }
        record_etag = hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        connection.execute(
            text("UPDATE collections SET record_etag = :record_etag WHERE id = :collection_id"),
            {"record_etag": record_etag, "collection_id": collection_id},
        )


def downgrade() -> None:
    raise RuntimeError("Riverhog state migrations are forward-only")
