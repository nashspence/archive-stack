from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy import func, literal, select, union_all
from sqlalchemy.orm import Session

from riverhog_core.catalog_models import (
    CollectionArchiveCopyRecord,
    CollectionArchiveObjectRecord,
    CollectionMetadataPublicationRecord,
)
from riverhog_core.ports.archive_store import (
    ArchiveObjectIdentity,
    CollectionArchiveIdentity,
)


def archive_copy_is_complete(copy: CollectionArchiveCopyRecord) -> bool:
    object_ids = {current.object_id for current in copy.objects}
    return bool(
        copy.state == "uploaded"
        and copy.last_verified_at
        and {"manifest", "proof"}.issubset(object_ids)
        and any(current.kind in {"pack", "segment"} for current in copy.objects)
        and all(current.verified_at for current in copy.objects)
    )


def archive_copy_identity(copy: CollectionArchiveCopyRecord) -> CollectionArchiveIdentity:
    return CollectionArchiveIdentity(
        objects=tuple(
            ArchiveObjectIdentity(
                object_id=current.object_id,
                kind=current.kind,
                object_path=current.object_path,
                plaintext_bytes=current.plaintext_bytes,
                stored_bytes=current.stored_bytes,
                sha256=current.sha256,
                stored_sha256=current.stored_sha256,
                version_id=current.version_id,
            )
            for current in sorted(copy.objects, key=lambda item: item.object_order)
        )
    )


def archive_copy_owned_identity(copy: CollectionArchiveCopyRecord) -> CollectionArchiveIdentity:
    immutable = archive_copy_identity(copy)
    publication = copy.metadata_publication
    if publication is None or publication.object_path is None:
        return immutable
    return CollectionArchiveIdentity(
        objects=(
            *immutable.objects,
            ArchiveObjectIdentity(
                object_id="metadata",
                kind="metadata",
                object_path=publication.object_path,
                plaintext_bytes=0,
                stored_bytes=publication.stored_bytes or 0,
                sha256=publication.stored_sha256 or "0" * 64,
                stored_sha256=publication.stored_sha256 or "0" * 64,
                version_id=publication.version_id,
            ),
        )
    )


ArchiveCopyAggregate = tuple[int, int]


def archive_copy_aggregates(
    session: Session,
    *,
    collection_ids: Sequence[int] | None = None,
) -> dict[tuple[int, str], ArchiveCopyAggregate]:
    object_rows = select(
        CollectionArchiveObjectRecord.collection_id.label("collection_id"),
        CollectionArchiveObjectRecord.store.label("store"),
        literal(1).label("object_count"),
        CollectionArchiveObjectRecord.stored_bytes.label("stored_bytes"),
    )
    metadata_rows = select(
        CollectionMetadataPublicationRecord.collection_id.label("collection_id"),
        CollectionMetadataPublicationRecord.store.label("store"),
        literal(1).label("object_count"),
        func.coalesce(CollectionMetadataPublicationRecord.stored_bytes, 0).label("stored_bytes"),
    ).where(CollectionMetadataPublicationRecord.object_path.is_not(None))
    combined = union_all(object_rows, metadata_rows).subquery()
    stmt = (
        select(
            combined.c.collection_id,
            combined.c.store,
            func.sum(combined.c.object_count),
            func.coalesce(func.sum(combined.c.stored_bytes), 0),
        )
        .group_by(
            combined.c.collection_id,
            combined.c.store,
        )
        .order_by(
            combined.c.collection_id,
            combined.c.store,
        )
    )
    if collection_ids is not None:
        if not collection_ids:
            return {}
        stmt = stmt.where(combined.c.collection_id.in_(collection_ids))
    return {
        (int(collection_id), str(store)): (int(object_count), int(stored_bytes))
        for collection_id, store, object_count, stored_bytes in session.execute(stmt)
    }
