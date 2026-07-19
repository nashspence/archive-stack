from __future__ import annotations

from collections.abc import Collection

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker
from time_formats import utc_timestamp_now

from riverhog_core.catalog_db import session_scope
from riverhog_core.catalog_models import CollectionArchiveCopyRecord
from riverhog_core.ports.archive_store import ArchiveStore


def publish_archive_catalog(
    *,
    store_name: str,
    archive_store: ArchiveStore,
    session_factory: sessionmaker[Session],
    excluded_collection_ids: Collection[str] = (),
) -> int:
    with session_scope(session_factory) as session:
        statement = (
            select(CollectionArchiveCopyRecord)
            .where(CollectionArchiveCopyRecord.state == "uploaded")
            .where(CollectionArchiveCopyRecord.store == store_name)
        )
        if excluded_collection_ids:
            statement = statement.where(
                ~CollectionArchiveCopyRecord.collection_id.in_(excluded_collection_ids)
            )
        total = int(session.scalar(select(func.count()).select_from(statement.subquery())) or 0)
        archives = session.scalars(
            statement.order_by(CollectionArchiveCopyRecord.collection_id)
        ).all()
        entries: list[dict[str, object]] = [
            {
                "collection_id": archive.collection_id,
                "archive_storage_prefix": archive.archive_storage_prefix,
                "objects": [
                    {
                        "id": current.object_id,
                        "kind": current.kind,
                        "key": current.object_path,
                        "plaintext_bytes": current.plaintext_bytes,
                        "stored_bytes": current.stored_bytes,
                        "sha256": current.sha256,
                        "storage_class": current.storage_class,
                    }
                    for current in sorted(
                        archive.objects,
                        key=lambda item: item.object_order,
                    )
                ],
                "backend": archive.backend,
                "uploaded_at": archive.last_uploaded_at,
                "verified_at": archive.last_verified_at,
            }
            for archive in archives
        ]
    archive_store.publish_archive_catalog(
        entries=entries,
        generated_at=utc_timestamp_now(),
    )
    return total
