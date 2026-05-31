from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from riverhog_core.archive_compliance import (
    copy_counts_as_verified,
    normalize_required_copy_count,
)
from riverhog_core.catalog_models import (
    CollectionFileRecord,
    FileCopyRecord,
    FinalizedImageCoveredPathRecord,
    FinalizedImageRecord,
    ImageCopyRecord,
)


def file_has_registered_disc_coverage(
    session: Session,
    *,
    collection_id: str,
    path: str,
) -> bool:
    return (
        session.scalar(
            select(FileCopyRecord.id)
            .where(
                FileCopyRecord.collection_id == collection_id,
                FileCopyRecord.path == path,
            )
            .limit(1)
        )
        is not None
    )


def file_is_fully_compliant(
    session: Session,
    *,
    collection_id: str,
    path: str,
) -> bool:
    image_ids = set(
        session.scalars(
            select(FinalizedImageCoveredPathRecord.image_id).where(
                FinalizedImageCoveredPathRecord.collection_id == collection_id,
                FinalizedImageCoveredPathRecord.path == path,
            )
        ).all()
    )
    if not image_ids:
        return False
    required_by_image = {
        image.image_id: normalize_required_copy_count(image.required_copy_count)
        for image in session.scalars(
            select(FinalizedImageRecord).where(FinalizedImageRecord.image_id.in_(image_ids))
        ).all()
    }
    if set(required_by_image) != image_ids:
        return False
    verified_counts: dict[str, int] = {image_id: 0 for image_id in image_ids}
    for copy in session.scalars(
        select(ImageCopyRecord).where(ImageCopyRecord.image_id.in_(image_ids))
    ).all():
        if copy_counts_as_verified(
            state=copy.state,
            verification_state=copy.verification_state,
        ):
            verified_counts[copy.image_id] = verified_counts.get(copy.image_id, 0) + 1
    return all(
        verified_counts.get(image_id, 0) >= required
        for image_id, required in required_by_image.items()
    )


def collection_is_fully_compliant(session: Session, *, collection_id: str) -> bool:
    files = session.scalars(
        select(CollectionFileRecord).where(CollectionFileRecord.collection_id == collection_id)
    ).all()
    return bool(files) and all(
        file_is_fully_compliant(
            session,
            collection_id=file.collection_id,
            path=file.path,
        )
        for file in files
    )
