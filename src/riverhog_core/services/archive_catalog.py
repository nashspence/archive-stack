from __future__ import annotations

from collections.abc import Collection
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from riverhog_core.archive_object_paths import archive_storage_prefix_from_object_path
from riverhog_core.catalog_db import session_scope
from riverhog_core.catalog_models import CollectionArchiveRecord
from riverhog_core.ports.archive_store import ArchiveStore


def publish_archive_restore_catalog(
    *,
    archive_store: ArchiveStore,
    session_factory: sessionmaker[Session],
    excluded_collection_ids: Collection[str] = (),
) -> int:
    publish = getattr(archive_store, "publish_restore_catalog", None)
    if not callable(publish):
        return 0
    with session_scope(session_factory) as session:
        statement = (
            select(CollectionArchiveRecord)
            .where(CollectionArchiveRecord.state == "uploaded")
            .where(CollectionArchiveRecord.object_path.is_not(None))
            .order_by(CollectionArchiveRecord.collection_id)
        )
        if excluded_collection_ids:
            statement = statement.where(
                ~CollectionArchiveRecord.collection_id.in_(excluded_collection_ids)
            )
        archives = list(session.scalars(statement))
        entries = [
            {
                "collection_id": archive.collection_id,
                "archive_storage_prefix": archive.archive_storage_prefix
                or archive_storage_prefix_from_object_path(archive.object_path),
                "archive_key": archive.object_path,
                "manifest_key": archive.manifest_object_path,
                "proof_key": archive.ots_object_path,
                "archive_stored_bytes": archive.stored_bytes,
                "archive_plaintext_sha256": archive.sha256,
                "manifest_sha256": archive.manifest_sha256,
                "proof_sha256": archive.ots_sha256,
                "backend": archive.backend,
                "archive_storage_class": archive.storage_class,
                "archive_format": archive.archive_format,
                "compression": archive.compression,
                "uploaded_at": archive.last_uploaded_at,
                "verified_at": archive.last_verified_at,
            }
            for archive in archives
        ]
    publish(
        entries=entries,
        generated_at=datetime.now(UTC).isoformat().replace("+00:00", "Z"),
    )
    return len(entries)
