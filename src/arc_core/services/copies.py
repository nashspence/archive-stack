from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select

from arc_core.catalog_models import (
    CollectionFileRecord,
    FinalizedImageCoveredPathRecord,
    FinalizedImageRecord,
    ImageCopyRecord,
)
from arc_core.domain.errors import Conflict, NotFound, NotYetImplemented
from arc_core.domain.models import CopySummary
from arc_core.domain.types import CopyId
from arc_core.runtime_config import RuntimeConfig
from arc_core.sqlite_db import make_session_factory, session_scope


class SqlAlchemyCopyService:
    def __init__(self, config: RuntimeConfig) -> None:
        self._session_factory = make_session_factory(str(config.sqlite_path))

    def register(self, image_id: str, copy_id: str, location: str) -> CopySummary:
        with session_scope(self._session_factory) as session:
            image = session.get(FinalizedImageRecord, image_id)
            if image is None:
                raise NotFound(f"image not found: {image_id}")
            existing = session.get(ImageCopyRecord, {"image_id": image_id, "copy_id": copy_id})
            if existing is not None:
                raise Conflict(f"copy already exists for volume: {copy_id}")
            created_at = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
            session.add(
                ImageCopyRecord(
                    image_id=image_id,
                    copy_id=copy_id,
                    location=location,
                    created_at=created_at,
                )
            )
            covered = session.scalars(
                select(FinalizedImageCoveredPathRecord).where(
                    FinalizedImageCoveredPathRecord.image_id == image_id
                )
            ).all()
            for cp in covered:
                file_record = session.get(
                    CollectionFileRecord,
                    {"collection_id": cp.collection_id, "path": cp.path},
                )
                if file_record is not None:
                    file_record.archived = True
            return CopySummary(
                id=CopyId(copy_id),
                volume_id=image_id,
                location=location,
                created_at=created_at,
            )


class StubCopyService:
    def register(self, image_id: str, copy_id: str, location: str) -> object:
        raise NotYetImplemented("StubCopyService is not implemented yet")
