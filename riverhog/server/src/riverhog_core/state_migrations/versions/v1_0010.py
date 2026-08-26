"""Retain exact collection-upload creation identity after finalization."""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from typing import Any

from alembic import op
from sqlalchemy import Column, String, text

revision: str = "v1_0010"
down_revision: str | None = "v1_0009"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    with op.batch_alter_table("collection_uploads") as batch:
        batch.add_column(Column("creation_identity_sha256", String(64), nullable=True))
    with op.batch_alter_table("collections") as batch:
        batch.add_column(Column("creation_identity_sha256", String(64), nullable=True))
        batch.add_column(Column("creation_custody_mode", String(), nullable=True))

    connection = op.get_bind()
    tags: dict[int, list[str]] = defaultdict(list)
    for row in connection.execute(
        text(
            "SELECT collection_id, tag_id FROM collection_upload_tags "
            "ORDER BY collection_id, tag_id"
        )
    ).mappings():
        tags[int(row["collection_id"])].append(str(row["tag_id"]))

    uploads = connection.execute(
        text(
            "SELECT collection_id, ingest_source, archive_store, event_context_json, "
            "provenance_mode, provenance_omission_reason, custody_mode "
            "FROM collection_uploads ORDER BY collection_id"
        )
    ).mappings()
    for row in uploads:
        collection_id = int(row["collection_id"])
        payload: dict[str, Any] = {
            "format": "riverhog-collection-upload-creation/v1",
            "tags": tags[collection_id],
            "archive_store": str(row["archive_store"]),
            "provenance_mode": str(row["provenance_mode"]),
            "custody_mode": str(row["custody_mode"]),
        }
        if row["ingest_source"] is not None:
            payload["ingest_source"] = str(row["ingest_source"])
        if row["event_context_json"] is not None:
            payload["event_context"] = json.loads(str(row["event_context_json"]))
        if row["provenance_omission_reason"] is not None:
            payload["provenance_omission_reason"] = str(row["provenance_omission_reason"])
        connection.execute(
            text(
                "UPDATE collection_uploads SET creation_identity_sha256 = :identity "
                "WHERE collection_id = :collection_id"
            ),
            {
                "collection_id": collection_id,
                "identity": _canonical_sha256(payload),
            },
        )

    for row in connection.execute(text("SELECT id FROM collections ORDER BY id")).mappings():
        collection_id = int(row["id"])
        unavailable_identity = hashlib.sha256(
            f"riverhog-migration-unrecorded-upload-identity\0{collection_id}".encode()
        ).hexdigest()
        connection.execute(
            text(
                "UPDATE collections SET creation_identity_sha256 = :identity, "
                "creation_custody_mode = 'producer-retained' WHERE id = :collection_id"
            ),
            {"collection_id": collection_id, "identity": unavailable_identity},
        )

    with op.batch_alter_table("collection_uploads") as batch:
        batch.alter_column(
            "creation_identity_sha256",
            existing_type=String(64),
            nullable=False,
        )
    with op.batch_alter_table("collections") as batch:
        batch.alter_column(
            "creation_identity_sha256",
            existing_type=String(64),
            nullable=False,
        )
        batch.alter_column(
            "creation_custody_mode",
            existing_type=String(),
            nullable=False,
        )


def _canonical_sha256(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def downgrade() -> None:
    raise RuntimeError("Riverhog state migrations are forward-only")
