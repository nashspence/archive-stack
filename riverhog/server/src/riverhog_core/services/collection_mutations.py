from __future__ import annotations

from riverhog_protocol.errors import Conflict
from sqlalchemy import select
from sqlalchemy.orm import Session

from riverhog_core.catalog_models import (
    ArchiveCopyRetirementRecord,
    CollectionDeletionRecord,
    CollectionRecord,
)


def require_collection_archive_idle(session: Session, collection_id: int) -> None:
    session.scalar(
        select(CollectionRecord.id)
        .where(
            CollectionRecord.id == collection_id,
            CollectionRecord.is_published.is_(True),
        )
        .with_for_update()
    )
    if session.get(CollectionDeletionRecord, collection_id) is not None:
        raise Conflict(f"collection deletion is in progress: {collection_id}")
    retirement = session.scalar(
        select(ArchiveCopyRetirementRecord)
        .where(ArchiveCopyRetirementRecord.collection_id == collection_id)
        .order_by(ArchiveCopyRetirementRecord.store)
        .limit(1)
    )
    if retirement is not None:
        raise Conflict(
            f"archive copy retirement is in progress: {collection_id} in {retirement.store}"
        )


def require_collection_mutation_allowed(session: Session, collection_id: int) -> None:
    session.scalar(
        select(CollectionRecord.id)
        .where(
            CollectionRecord.id == collection_id,
            CollectionRecord.is_published.is_(True),
        )
        .with_for_update()
    )
    if session.get(CollectionDeletionRecord, collection_id) is not None:
        raise Conflict(f"collection deletion is in progress: {collection_id}")
    retirement = session.scalar(
        select(ArchiveCopyRetirementRecord)
        .where(ArchiveCopyRetirementRecord.collection_id == collection_id)
        .order_by(ArchiveCopyRetirementRecord.store)
        .limit(1)
    )
    if retirement is not None:
        raise Conflict(
            f"archive copy retirement is in progress: {collection_id} in {retirement.store}"
        )
