"""Bind collection archive encryption identity before the v1 release."""

import hashlib
import json
import os
from collections import defaultdict
from typing import Any

from alembic import op
from riverhog_archive_contracts import ARCHIVE_ENCRYPTION_FORMAT, normalize_passphrase_id
from sqlalchemy import Column, String, text

revision: str = "v1_0007"
down_revision: str | None = "v1_0006"
branch_labels: str | None = None
depends_on: str | None = None

_CUTOVER_PASSPHRASE_ID_ENV = "RIVERHOG_STATE_UPGRADE_PASSPHRASE_ID"


def upgrade() -> None:
    for table in ("collections", "collection_uploads"):
        op.add_column(table, Column("encryption_format", String(), nullable=True))
        op.add_column(table, Column("passphrase_id", String(), nullable=True))

    connection = op.get_bind()
    populated = (
        connection.execute(text("SELECT 1 FROM collections LIMIT 1")).first() is not None
        or connection.execute(text("SELECT 1 FROM collection_uploads LIMIT 1")).first() is not None
    )
    passphrase_id = os.environ.get(_CUTOVER_PASSPHRASE_ID_ENV, "").strip()
    if populated:
        if not passphrase_id:
            raise RuntimeError(
                f"{_CUTOVER_PASSPHRASE_ID_ENV} is required to bind pre-v1 archive state"
            )
        normalize_passphrase_id(passphrase_id)
        values = {
            "encryption_format": ARCHIVE_ENCRYPTION_FORMAT,
            "passphrase_id": passphrase_id,
        }
        connection.execute(
            text(
                "UPDATE collections SET encryption_format = :encryption_format, "
                "passphrase_id = :passphrase_id"
            ),
            values,
        )
        connection.execute(
            text(
                "UPDATE collection_uploads SET encryption_format = :encryption_format, "
                "passphrase_id = :passphrase_id"
            ),
            values,
        )
        _refresh_collection_identities(
            encryption_format=ARCHIVE_ENCRYPTION_FORMAT,
            passphrase_id=passphrase_id,
        )

    for table in ("collections", "collection_uploads"):
        with op.batch_alter_table(table) as batch:
            batch.alter_column("encryption_format", existing_type=String(), nullable=False)
            batch.alter_column("passphrase_id", existing_type=String(), nullable=False)

    op.create_index(
        "ix_collections_encryption_format",
        "collections",
        ["encryption_format", "id"],
    )
    op.create_index(
        "ix_collections_passphrase_id",
        "collections",
        ["passphrase_id", "id"],
    )


def downgrade() -> None:
    raise RuntimeError("Riverhog state migrations are forward-only")


def _refresh_collection_identities(*, encryption_format: str, passphrase_id: str) -> None:
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
            "encryption_format": encryption_format,
            "passphrase_id": passphrase_id,
            "provenance_mode": str(collection["provenance_mode"]),
            "provenance_identity": collection["provenance_identity"],
            "metadata_revision": int(collection["metadata_revision"]),
            "tags": tags[collection_id],
            "files": files[collection_id],
        }
        # passphrase_id is an opaque public identifier, not passphrase material.
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
