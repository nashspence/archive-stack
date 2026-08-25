"""Separate archive layout identity from provider-neutral resumable-write state."""

import hashlib
import json
from typing import Any

from alembic import op
from sqlalchemy import text

revision: str = "v1_0008"
down_revision: str | None = "v1_0007"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    with op.batch_alter_table("collection_metadata_publications") as batch:
        batch.alter_column("version_id", new_column_name="revision")
    with op.batch_alter_table("collection_archive_objects") as batch:
        batch.alter_column("version_id", new_column_name="revision")
        batch.alter_column("part_receipts_json", new_column_name="archive_parts_json")
    with op.batch_alter_table("retrieval_cache_objects") as batch:
        batch.alter_column("version_id", new_column_name="revision")
    with op.batch_alter_table("archive_copy_object_uploads") as batch:
        batch.alter_column("multipart_upload_id", new_column_name="write_token")
        batch.alter_column(
            "multipart_content_length",
            new_column_name="expected_stored_bytes",
        )
        batch.alter_column("multipart_parts_json", new_column_name="write_segments_json")
        batch.alter_column("uploaded_parts", new_column_name="uploaded_segments")
        batch.alter_column("total_parts", new_column_name="total_segments")
    with op.batch_alter_table("collection_archive_object_uploads") as batch:
        batch.alter_column("uploaded_parts", new_column_name="uploaded_units")
        batch.alter_column("total_parts", new_column_name="total_units")
    _upgrade_persisted_json()


def downgrade() -> None:
    raise RuntimeError("Riverhog state migrations are forward-only")


def _upgrade_persisted_json() -> None:
    connection = op.get_bind()
    for row in connection.execute(
        text(
            "SELECT collection_id, store, object_id, archive_parts_json "
            "FROM collection_archive_objects WHERE archive_parts_json IS NOT NULL"
        )
    ).mappings():
        connection.execute(
            text(
                "UPDATE collection_archive_objects SET archive_parts_json = :value "
                "WHERE collection_id = :collection_id AND store = :store "
                "AND object_id = :object_id"
            ),
            {
                **row,
                "value": _canonical_json(
                    [_archive_part(value) for value in _json_list(row["archive_parts_json"])]
                ),
            },
        )

    for row in connection.execute(
        text(
            "SELECT collection_id, object_id, kind, plan_json, checkpoint_json, "
            "sealed_receipt_json "
            "FROM collection_archive_object_uploads "
            "WHERE checkpoint_json IS NOT NULL OR sealed_receipt_json IS NOT NULL"
        )
    ).mappings():
        checkpoint = row["checkpoint_json"]
        sealed = row["sealed_receipt_json"]
        connection.execute(
            text(
                "UPDATE collection_archive_object_uploads "
                "SET checkpoint_json = :checkpoint, sealed_receipt_json = :sealed "
                "WHERE collection_id = :collection_id AND object_id = :object_id"
            ),
            {
                "collection_id": row["collection_id"],
                "object_id": row["object_id"],
                "checkpoint": (
                    _canonical_json(
                        _checkpoint(
                            _json_object(checkpoint),
                            kind=str(row["kind"]),
                            plan=_json_object(row["plan_json"]),
                        )
                    )
                    if checkpoint is not None
                    else None
                ),
                "sealed": (
                    _canonical_json(_sealed_receipt(_json_object(sealed)))
                    if sealed is not None
                    else None
                ),
            },
        )

    for row in connection.execute(
        text(
            "SELECT collection_id, destination_store, object_id, write_segments_json "
            "FROM archive_copy_object_uploads WHERE write_segments_json IS NOT NULL"
        )
    ).mappings():
        segments = [
            {
                "number": value["number"],
                "segment_token": value["etag"],
                "bytes": value["bytes"],
            }
            for value in _json_list(row["write_segments_json"])
        ]
        connection.execute(
            text(
                "UPDATE archive_copy_object_uploads SET write_segments_json = :value "
                "WHERE collection_id = :collection_id "
                "AND destination_store = :destination_store AND object_id = :object_id"
            ),
            {**row, "value": _canonical_json(segments)},
        )


def _checkpoint(
    value: dict[str, Any],
    *,
    kind: str = "pack",
    plan: dict[str, Any] | None = None,
) -> dict[str, Any]:
    parts = _json_list(value.pop("parts"))
    old_token = str(value.pop("upload_id"))
    value["write_token"] = _write_token(
        old_token,
        metadata=_checkpoint_metadata(value, kind=kind, plan=plan),
    )
    value["archive_parts"] = [_archive_part(part) for part in parts]
    value["write_segments"] = [_write_segment(part) for part in parts]
    completed = value.get("completed")
    if completed is not None:
        value["completed"] = _completed(_object(completed))
    return value


def _checkpoint_metadata(
    value: dict[str, Any],
    *,
    kind: str,
    plan: dict[str, Any] | None,
) -> dict[str, str]:
    age_state = _object(value["age_state"])
    age_identity = hashlib.sha256(_canonical_json(age_state).encode()).hexdigest()
    if kind == "pack":
        if plan is None:
            return {}
        return {
            "riverhog-format": "riverhog-pack-volume/v1",
            "riverhog-plan-sha256": str(value["plan_sha256"]),
            "riverhog-plaintext-bytes": str(value["plaintext_bytes"]),
            "riverhog-index-sha256": str(plan["index_sha256"]),
            "riverhog-age-state-sha256": age_identity,
        }
    if kind == "segment":
        return {
            "riverhog-format": "riverhog-raw-volume/v1",
            "riverhog-source-path-sha256": hashlib.sha256(
                str(value["source_path"]).encode()
            ).hexdigest(),
            "riverhog-file-offset": str(value["file_offset"]),
            "riverhog-plaintext-bytes": str(value["plaintext_bytes"]),
            "riverhog-file-sha256": str(value["file_sha256"]),
            "riverhog-age-state-sha256": age_identity,
        }
    raise RuntimeError("v1 state upgrade found an unknown archive upload kind")


def _write_token(value: str, *, metadata: dict[str, str]) -> str:
    try:
        payload = json.loads(value)
    except json.JSONDecodeError:
        return value
    if not isinstance(payload, dict) or payload.get("schema") != "archive-cache-mirror-upload/v1":
        return value
    archive = _object(payload.get("archive"))
    cache_value = payload.get("cache")
    cache = None if cache_value is None else _object(cache_value)
    return _canonical_json(
        {
            "schema": "archive-cache-mirror-write/v1",
            "metadata": dict(sorted(metadata.items())),
            "archive": {
                "object_path": archive["object_path"],
                "write_token": archive["upload_id"],
            },
            "cache": (
                {
                    "object_path": cache["object_path"],
                    "write_token": cache["upload_id"],
                }
                if cache is not None
                else None
            ),
        }
    )


def _sealed_receipt(value: dict[str, Any]) -> dict[str, Any]:
    value["parts"] = [_archive_part(part) for part in _json_list(value["parts"])]
    value["revision"] = value.pop("version_id")
    if value.get("retrieval_cache") is not None:
        value["retrieval_cache"] = _retrieval_cache(_object(value["retrieval_cache"]))
    return value


def _completed(value: dict[str, Any]) -> dict[str, Any]:
    value["revision"] = value.pop("version_id")
    value["entity_token"] = value.pop("etag")
    if value.get("retrieval_cache") is not None:
        value["retrieval_cache"] = _retrieval_cache(_object(value["retrieval_cache"]))
    return value


def _retrieval_cache(value: dict[str, Any]) -> dict[str, Any]:
    value["revision"] = value.pop("version_id")
    return value


def _archive_part(value: object) -> dict[str, Any]:
    row = _object(value)
    row.pop("etag")
    return row


def _write_segment(value: object) -> dict[str, Any]:
    row = _object(value)
    return {
        "number": row["number"],
        "segment_token": row["etag"],
        "bytes": row["stored_bytes"],
        "sha256": row["stored_sha256"],
    }


def _json_object(value: object) -> dict[str, Any]:
    return _object(json.loads(str(value)))


def _json_list(value: object) -> list[Any]:
    parsed = json.loads(str(value)) if isinstance(value, str) else value
    if not isinstance(parsed, list):
        raise RuntimeError("v1 state upgrade expected a JSON list")
    return parsed


def _object(value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise RuntimeError("v1 state upgrade expected a JSON object")
    return dict(value)


def _canonical_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))
