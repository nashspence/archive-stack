"""Name immutable identities and external-state references precisely."""

import hashlib
import json
from collections import defaultdict
from typing import Any

from alembic import op
from sqlalchemy import text

revision: str = "v1_0006"
down_revision: str | None = "v1_0005"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    with op.batch_alter_table("collections") as batch:
        batch.alter_column("content_etag", new_column_name="content_identity")
        batch.alter_column("provenance_etag", new_column_name="provenance_identity")
    with op.batch_alter_table("collection_uploads") as batch:
        batch.alter_column("provenance_etag", new_column_name="provenance_identity")
    with op.batch_alter_table("collection_processing_claim_inputs") as batch:
        batch.alter_column("content_etag", new_column_name="content_identity")
    with op.batch_alter_table("collection_processing_outcomes") as batch:
        batch.alter_column("content_etag", new_column_name="content_identity")

    op.drop_index(
        "ix_collection_provenance_lineage_edges_target",
        table_name="collection_provenance_lineage_edges",
    )
    op.rename_table(
        "collection_provenance_lineage_edges",
        "collection_provenance_external_state_references",
    )
    op.create_index(
        "ix_collection_provenance_external_state_references_target",
        "collection_provenance_external_state_references",
        ["collection_id", "to_journal_id"],
    )

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
            "SELECT id, content_identity, provenance_mode, provenance_identity, "
            "metadata_revision FROM collections ORDER BY id"
        )
    ).mappings()
    for collection in collections:
        collection_id = int(collection["id"])
        payload = {
            "format": "riverhog-collection/v1",
            "collection": collection_id,
            "content_identity": str(collection["content_identity"]),
            "provenance_mode": str(collection["provenance_mode"]),
            "provenance_identity": collection["provenance_identity"],
            "metadata_revision": int(collection["metadata_revision"]),
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

    connection.execute(
        text(
            "UPDATE collection_metadata_publications SET state = 'pending', "
            "published_revision = NULL, attempt_count = 0, failure = NULL, "
            "next_attempt_at = (SELECT metadata_updated_at FROM collections "
            "WHERE collections.id = collection_metadata_publications.collection_id)"
        )
    )


def downgrade() -> None:
    raise RuntimeError("Riverhog state migrations are forward-only")
