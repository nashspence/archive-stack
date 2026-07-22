from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from riverhog_core.archive_object_paths import archive_storage_prefix_from_object_path
from riverhog_core.archive_objects import CollectionArchive
from riverhog_core.catalog_models import (
    CollectionArchiveCopyRecord,
    CollectionArchiveFileObjectRecord,
    CollectionArchiveObjectRecord,
)
from riverhog_core.ports.archive_store import (
    ArchiveObjectIdentity,
    CollectionArchiveIdentity,
    CollectionArchiveUploadReceipt,
)


def apply_archive_receipt(
    copy: CollectionArchiveCopyRecord,
    receipt: CollectionArchiveUploadReceipt,
    archive: CollectionArchive,
) -> None:
    if not receipt.objects:
        raise ValueError("collection archive upload returned no objects")
    receipt_by_id = {current.object_id: current for current in receipt.objects}
    if set(receipt_by_id) != {
        *(current.object_id for current in archive.data_objects),
        "manifest",
        "proof",
    }:
        raise ValueError("collection archive receipt objects do not match its manifest")

    copy.state = "uploaded"
    copy.archive_storage_prefix = archive_storage_prefix_from_object_path(
        receipt.require_object("manifest").object_path
    )
    copy.backend = receipt.objects[0].backend
    copy.storage_class = receipt.objects[0].storage_class
    copy.last_uploaded_at = max(current.uploaded_at for current in receipt.objects)
    verified = [current.verified_at for current in receipt.objects]
    copy.last_verified_at = (
        max(current for current in verified if current) if all(verified) else None
    )
    copy.failure = None
    copy.objects.clear()

    data_by_id = {current.object_id: current for current in archive.data_objects}
    sequence_by_path: dict[str, int] = {}
    for object_order, current in enumerate(receipt.objects):
        object_record = CollectionArchiveObjectRecord(
            collection_id=copy.collection_id,
            store=copy.store,
            object_id=current.object_id,
            object_order=object_order,
            kind=current.kind,
            object_path=current.object_path,
            plaintext_bytes=current.plaintext_bytes,
            stored_bytes=current.stored_bytes,
            sha256=current.sha256,
            backend=current.backend,
            storage_class=current.storage_class,
            uploaded_at=current.uploaded_at,
            verified_at=current.verified_at,
        )
        copy.objects.append(object_record)
        data = data_by_id.get(current.object_id)
        if data is None:
            continue
        for placement in data.placements:
            sequence = sequence_by_path.get(placement.path, 0)
            sequence_by_path[placement.path] = sequence + 1
            object_record.placements.append(
                CollectionArchiveFileObjectRecord(
                    collection_id=copy.collection_id,
                    store=copy.store,
                    path=placement.path,
                    sequence=sequence,
                    object_id=current.object_id,
                    file_offset=placement.file_offset,
                    bytes=placement.bytes,
                    member=placement.member,
                )
            )


def archive_copy_is_complete(copy: CollectionArchiveCopyRecord) -> bool:
    object_ids = {current.object_id for current in copy.objects}
    return bool(
        copy.state == "uploaded"
        and copy.last_verified_at
        and {"manifest", "proof"}.issubset(object_ids)
        and any(current.kind in {"pack", "file", "segment"} for current in copy.objects)
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
            )
            for current in sorted(copy.objects, key=lambda item: item.object_order)
        )
    )


ArchiveCopyAggregate = tuple[int, int]


def archive_copy_aggregates(
    session: Session,
    *,
    collection_ids: Sequence[str] | None = None,
) -> dict[tuple[str, str], ArchiveCopyAggregate]:
    stmt = (
        select(
            CollectionArchiveObjectRecord.collection_id,
            CollectionArchiveObjectRecord.store,
            func.count(CollectionArchiveObjectRecord.object_id),
            func.coalesce(func.sum(CollectionArchiveObjectRecord.stored_bytes), 0),
        )
        .group_by(
            CollectionArchiveObjectRecord.collection_id,
            CollectionArchiveObjectRecord.store,
        )
        .order_by(
            CollectionArchiveObjectRecord.collection_id,
            CollectionArchiveObjectRecord.store,
        )
    )
    if collection_ids is not None:
        if not collection_ids:
            return {}
        stmt = stmt.where(CollectionArchiveObjectRecord.collection_id.in_(collection_ids))
    return {
        (str(collection_id), str(store)): (int(object_count), int(stored_bytes))
        for collection_id, store, object_count, stored_bytes in session.execute(stmt)
    }
