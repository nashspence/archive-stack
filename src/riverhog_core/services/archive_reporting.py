from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import and_, func, select
from sqlalchemy.orm import Session

from riverhog_core.catalog_db import make_session_factory, session_scope
from riverhog_core.catalog_models import (
    ArchiveUsageSnapshotRecord,
    CollectionArchiveRecord,
    CollectionFileRecord,
    CollectionRecord,
    CollectionUploadFileRecord,
    CollectionUploadRecord,
    FinalizedImageCoveredPathRecord,
    FinalizedImageRecord,
)
from riverhog_core.domain.enums import ArchiveState
from riverhog_core.domain.models import (
    ArchiveCollectionContribution,
    ArchiveStatus,
    ArchiveUsageCollection,
    ArchiveUsageImage,
    ArchiveUsageReport,
    ArchiveUsageSnapshot,
    ArchiveUsageTotals,
    CollectionManifestStatus,
)
from riverhog_core.domain.types import CollectionId, ImageId
from riverhog_core.durability import normalize_archive_state
from riverhog_core.runtime_config import RuntimeConfig
from riverhog_core.webhooks import utcnow


@dataclass(frozen=True, slots=True)
class _FinalizedImageSummaryRow:
    image_id: str
    filename: str


class SqlAlchemyArchiveReportingService:
    def __init__(self, config: RuntimeConfig) -> None:
        self._config = config
        self._session_factory = make_session_factory(config.database_url)

    def get_report(
        self,
        *,
        image_id: str | None = None,
        collection: str | None = None,
    ) -> ArchiveUsageReport:
        measured_at = _isoformat_z(utcnow())

        with session_scope(self._session_factory) as session:
            image_rows = _filtered_image_rows(
                session,
                image_id=image_id,
                collection=collection,
            )
            image_ids = [row.image_id for row in image_rows]
            image_filenames = {row.image_id: row.filename for row in image_rows}
            image_reports = _image_usage_reports(session, image_rows)
            collection_reports = tuple(
                _direct_collection_usage_reports(
                    session,
                    image_ids=image_ids,
                    image_filenames=image_filenames,
                    collection_filter=collection,
                )
            )

            totals = _totals_from_collections(collection_reports)

            history: tuple[ArchiveUsageSnapshot, ...] = ()
            if image_id is None and collection is None:
                _ensure_usage_snapshot(session, totals=totals)
                session.flush()
                history = tuple(
                    ArchiveUsageSnapshot(
                        captured_at=record.captured_at,
                        uploaded_collections=record.uploaded_images,
                        measured_storage_bytes=record.measured_storage_bytes,
                    )
                    for record in session.scalars(
                        select(ArchiveUsageSnapshotRecord).order_by(
                            ArchiveUsageSnapshotRecord.captured_at.desc()
                        )
                    ).all()
                )

        return ArchiveUsageReport(
            scope=_scope_name(image_id=image_id, collection=collection),
            measured_at=measured_at,
            totals=totals,
            images=image_reports,
            collections=collection_reports,
            history=history,
        )


def record_archive_usage_snapshot(session: Session, *, config: RuntimeConfig) -> None:
    _ = config
    image_rows = _filtered_image_rows(session, image_id=None, collection=None)
    image_ids = [row.image_id for row in image_rows]
    image_filenames = {row.image_id: row.filename for row in image_rows}
    collection_reports = tuple(
        _direct_collection_usage_reports(
            session,
            image_ids=image_ids,
            image_filenames=image_filenames,
            collection_filter=None,
        )
    )
    totals = _totals_from_collections(collection_reports)
    _ensure_usage_snapshot(session, totals=totals)


def _scope_name(*, image_id: str | None, collection: str | None) -> str:
    if image_id is not None and collection is not None:
        return "filtered"
    if image_id is not None:
        return "image"
    if collection is not None:
        return "collection"
    return "all"


def _filtered_image_rows(
    session: Session,
    *,
    image_id: str | None,
    collection: str | None,
) -> list[_FinalizedImageSummaryRow]:
    image_query = select(FinalizedImageRecord.image_id, FinalizedImageRecord.filename)
    if collection is not None:
        image_query = (
            image_query.join(
                FinalizedImageCoveredPathRecord,
                FinalizedImageCoveredPathRecord.image_id == FinalizedImageRecord.image_id,
            )
            .where(FinalizedImageCoveredPathRecord.collection_id == collection)
            .distinct()
        )
    if image_id is not None:
        image_query = image_query.where(FinalizedImageRecord.image_id == image_id)
    rows = session.execute(image_query.order_by(FinalizedImageRecord.image_id.desc())).all()
    return [
        _FinalizedImageSummaryRow(
            image_id=row.image_id,
            filename=row.filename,
        )
        for row in rows
    ]


def _image_usage_reports(
    session: Session,
    image_rows: Sequence[_FinalizedImageSummaryRow],
) -> tuple[ArchiveUsageImage, ...]:
    image_ids = [row.image_id for row in image_rows]
    if not image_ids:
        return ()
    collection_rows = session.execute(
        select(
            FinalizedImageCoveredPathRecord.image_id,
            FinalizedImageCoveredPathRecord.collection_id,
        )
        .where(FinalizedImageCoveredPathRecord.image_id.in_(image_ids))
        .distinct()
    ).all()
    collection_ids_by_image: dict[str, set[str]] = defaultdict(set)
    for row in collection_rows:
        collection_ids_by_image[row.image_id].add(row.collection_id)
    return tuple(
        ArchiveUsageImage(
            id=ImageId(row.image_id),
            filename=row.filename,
            collection_ids=sorted(collection_ids_by_image.get(row.image_id, set())),
        )
        for row in image_rows
    )


def _direct_collection_usage_reports(
    session: Session,
    *,
    image_ids: Sequence[str],
    image_filenames: Mapping[str, str],
    collection_filter: str | None,
) -> list[ArchiveUsageCollection]:
    collection_query = select(CollectionRecord.id).order_by(CollectionRecord.id.asc())
    if collection_filter is not None:
        collection_query = collection_query.where(CollectionRecord.id == collection_filter)
    collection_ids = list(session.scalars(collection_query))

    bytes_by_collection: dict[str, int] = {}
    archives_by_collection: dict[str, CollectionArchiveRecord] = {}
    if collection_ids:
        byte_rows = session.execute(
            select(
                CollectionFileRecord.collection_id,
                func.sum(CollectionFileRecord.bytes).label("bytes"),
            )
            .where(CollectionFileRecord.collection_id.in_(collection_ids))
            .group_by(CollectionFileRecord.collection_id)
        ).all()
        bytes_by_collection = {row.collection_id: int(row.bytes or 0) for row in byte_rows}
        archive_rows = session.scalars(
            select(CollectionArchiveRecord).where(
                CollectionArchiveRecord.collection_id.in_(collection_ids)
            )
        ).all()
        archives_by_collection = {archive.collection_id: archive for archive in archive_rows}

    image_contributions = _image_contributions_by_collection(
        session,
        image_ids=image_ids,
        image_filenames=image_filenames,
        collection_filter=collection_filter,
    )
    reports: list[ArchiveUsageCollection] = []
    seen: set[str] = set()
    for collection_id in collection_ids:
        seen.add(collection_id)
        archive = archives_by_collection.get(collection_id)
        measured_storage_bytes = _collection_measured_storage_bytes(archive)
        reports.append(
            ArchiveUsageCollection(
                id=CollectionId(collection_id),
                bytes=bytes_by_collection.get(collection_id, 0),
                measured_storage_bytes=measured_storage_bytes,
                images=tuple(image_contributions.get(collection_id, ())),
                archive=_collection_archive_status(archive),
                collection_manifest=_collection_manifest_status(archive),
                archive_format=archive.archive_format if archive is not None else None,
                compression=archive.compression if archive is not None else None,
            )
        )

    upload_query = (
        select(
            CollectionUploadRecord.collection_id,
            CollectionUploadRecord.state,
            CollectionUploadRecord.archive_phase,
            CollectionUploadRecord.archive_failure,
        )
        .where(~CollectionUploadRecord.state.in_(("canceled", "expired")))
        .order_by(CollectionUploadRecord.collection_id.asc())
    )
    if collection_filter is not None:
        upload_query = upload_query.where(CollectionUploadRecord.collection_id == collection_filter)
    upload_rows = session.execute(upload_query).all()
    upload_collection_ids = [
        row.collection_id for row in upload_rows if row.collection_id not in seen
    ]
    upload_bytes_by_collection: dict[str, int] = {}
    if upload_collection_ids:
        upload_byte_rows = session.execute(
            select(
                CollectionUploadFileRecord.collection_id,
                func.sum(CollectionUploadFileRecord.bytes).label("bytes"),
            )
            .where(CollectionUploadFileRecord.collection_id.in_(upload_collection_ids))
            .group_by(CollectionUploadFileRecord.collection_id)
        ).all()
        upload_bytes_by_collection = {
            row.collection_id: int(row.bytes or 0) for row in upload_byte_rows
        }

    for upload in upload_rows:
        if upload.collection_id in seen:
            continue
        reports.append(
            ArchiveUsageCollection(
                id=CollectionId(upload.collection_id),
                bytes=upload_bytes_by_collection.get(upload.collection_id, 0),
                measured_storage_bytes=0,
                images=(),
                archive=ArchiveStatus(
                    state=_upload_archive_state_from_values(
                        state=upload.state,
                        archive_phase=upload.archive_phase,
                    ),
                    failure=upload.archive_failure,
                ),
                collection_manifest=None,
                archive_format=None,
                compression=None,
            )
        )
    return reports


def _image_contributions_by_collection(
    session: Session,
    *,
    image_ids: Sequence[str],
    image_filenames: Mapping[str, str],
    collection_filter: str | None,
) -> dict[str, tuple[ArchiveCollectionContribution, ...]]:
    if not image_ids:
        return {}
    contribution_query = (
        select(
            FinalizedImageCoveredPathRecord.image_id,
            FinalizedImageCoveredPathRecord.collection_id,
            func.coalesce(func.sum(CollectionFileRecord.bytes), 0).label("represented_bytes"),
        )
        .outerjoin(
            CollectionFileRecord,
            and_(
                CollectionFileRecord.collection_id == FinalizedImageCoveredPathRecord.collection_id,
                CollectionFileRecord.path == FinalizedImageCoveredPathRecord.path,
            ),
        )
        .where(FinalizedImageCoveredPathRecord.image_id.in_(image_ids))
        .group_by(
            FinalizedImageCoveredPathRecord.image_id,
            FinalizedImageCoveredPathRecord.collection_id,
        )
    )
    if collection_filter is not None:
        contribution_query = contribution_query.where(
            FinalizedImageCoveredPathRecord.collection_id == collection_filter
        )
    rows = session.execute(contribution_query).all()

    result: dict[str, list[ArchiveCollectionContribution]] = defaultdict(list)
    for row in rows:
        result[row.collection_id].append(
            ArchiveCollectionContribution(
                image_id=ImageId(row.image_id),
                filename=image_filenames.get(row.image_id, row.image_id),
                represented_bytes=int(row.represented_bytes or 0),
            )
        )
    return {
        collection_id: tuple(
            sorted(contributions, key=lambda current: str(current.image_id), reverse=True)
        )
        for collection_id, contributions in result.items()
    }


def _collection_measured_storage_bytes(archive: CollectionArchiveRecord | None) -> int:
    if archive is None or normalize_archive_state(archive.state).value != "uploaded":
        return 0
    return (
        int(archive.stored_bytes or 0)
        + int(archive.manifest_stored_bytes or 0)
        + int(archive.ots_stored_bytes or 0)
    )


def _collection_archive_status(
    archive: CollectionArchiveRecord | None,
) -> ArchiveStatus:
    if archive is None:
        return ArchiveStatus()
    return ArchiveStatus(
        state=normalize_archive_state(archive.state),
        object_path=archive.object_path,
        stored_bytes=archive.stored_bytes,
        backend=archive.backend,
        storage_class=archive.storage_class,
        last_uploaded_at=archive.last_uploaded_at,
        last_verified_at=archive.last_verified_at,
        failure=archive.failure,
    )


def _collection_manifest_status(
    archive: CollectionArchiveRecord | None,
) -> CollectionManifestStatus | None:
    if archive is None:
        return None
    ots_state = "uploaded" if archive.ots_object_path else "pending"
    if normalize_archive_state(archive.state).value == "failed":
        ots_state = "failed"
    return CollectionManifestStatus(
        object_path=archive.manifest_object_path,
        sha256=archive.manifest_sha256,
        ots_object_path=archive.ots_object_path,
        ots_state=ots_state,
        ots_sha256=archive.ots_sha256,
    )


def _upload_archive_state(upload: CollectionUploadRecord) -> ArchiveState:
    return _upload_archive_state_from_values(
        state=upload.state,
        archive_phase=upload.archive_phase,
    )


def _upload_archive_state_from_values(
    *,
    state: str | None,
    archive_phase: str | None,
) -> ArchiveState:
    if state == "failed":
        return ArchiveState.FAILED
    if archive_phase == "retry_wait":
        return ArchiveState.RETRYING
    if state == "archiving":
        return ArchiveState.UPLOADING
    return ArchiveState.PENDING


def _totals_from_collections(collections: tuple[ArchiveUsageCollection, ...]) -> ArchiveUsageTotals:
    return ArchiveUsageTotals(
        collections=len(collections),
        uploaded_collections=sum(
            1 for collection in collections if collection.measured_storage_bytes > 0
        ),
        measured_storage_bytes=sum(collection.measured_storage_bytes for collection in collections),
    )


def _ensure_usage_snapshot(
    session: Session,
    *,
    totals: ArchiveUsageTotals,
) -> None:
    latest = session.scalar(
        select(ArchiveUsageSnapshotRecord).order_by(ArchiveUsageSnapshotRecord.captured_at.desc())
    )
    if latest is not None and _snapshot_matches(latest, totals=totals):
        return
    session.add(
        ArchiveUsageSnapshotRecord(
            captured_at=_isoformat_z(utcnow()),
            uploaded_images=totals.uploaded_collections,
            measured_storage_bytes=totals.measured_storage_bytes,
        )
    )


def _snapshot_matches(
    latest: ArchiveUsageSnapshotRecord,
    *,
    totals: ArchiveUsageTotals,
) -> bool:
    return (
        latest.uploaded_images == totals.uploaded_collections
        and latest.measured_storage_bytes == totals.measured_storage_bytes
    )


def _isoformat_z(value: datetime) -> str:
    return value.strftime("%Y-%m-%dT%H:%M:%SZ")
