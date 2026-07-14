from __future__ import annotations

from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from riverhog_core.catalog_db import make_session_factory, session_scope
from riverhog_core.catalog_models import (
    ArchiveUsageSnapshotRecord,
    CollectionArchiveRecord,
    CollectionFileRecord,
    CollectionRecord,
    CollectionUploadFileRecord,
    CollectionUploadRecord,
)
from riverhog_core.domain.enums import ArchiveState
from riverhog_core.domain.models import (
    ArchiveStatus,
    ArchiveUsageCollection,
    ArchiveUsageReport,
    ArchiveUsageSnapshot,
    ArchiveUsageTotals,
    CollectionManifestStatus,
)
from riverhog_core.domain.types import CollectionId
from riverhog_core.runtime_config import RuntimeConfig
from riverhog_core.webhooks import utcnow


class SqlAlchemyArchiveReportingService:
    def __init__(self, config: RuntimeConfig) -> None:
        self._session_factory = make_session_factory(config.database_url)

    def get_report(self, *, collection: str | None = None) -> ArchiveUsageReport:
        measured_at = _isoformat_z(utcnow())
        with session_scope(self._session_factory) as session:
            collection_reports = tuple(
                _collection_usage_reports(session, collection_filter=collection)
            )
            totals = _totals_from_collections(collection_reports)
            history: tuple[ArchiveUsageSnapshot, ...] = ()
            if collection is None:
                _ensure_usage_snapshot(session, totals=totals)
                session.flush()
                history = tuple(
                    ArchiveUsageSnapshot(
                        captured_at=record.captured_at,
                        uploaded_collections=record.uploaded_collections,
                        measured_storage_bytes=record.measured_storage_bytes,
                    )
                    for record in session.scalars(
                        select(ArchiveUsageSnapshotRecord).order_by(
                            ArchiveUsageSnapshotRecord.captured_at.desc()
                        )
                    ).all()
                )

        return ArchiveUsageReport(
            scope="collection" if collection is not None else "all",
            measured_at=measured_at,
            totals=totals,
            collections=collection_reports,
            history=history,
        )


def record_archive_usage_snapshot(session: Session, *, config: RuntimeConfig) -> None:
    _ = config
    reports = tuple(_collection_usage_reports(session, collection_filter=None))
    _ensure_usage_snapshot(session, totals=_totals_from_collections(reports))


def _collection_usage_reports(
    session: Session,
    *,
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

    reports: list[ArchiveUsageCollection] = []
    seen: set[str] = set()
    for collection_id in collection_ids:
        seen.add(collection_id)
        archive = archives_by_collection.get(collection_id)
        reports.append(
            ArchiveUsageCollection(
                id=CollectionId(collection_id),
                bytes=bytes_by_collection.get(collection_id, 0),
                measured_storage_bytes=_collection_measured_storage_bytes(archive),
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
    pending_ids = [row.collection_id for row in upload_rows if row.collection_id not in seen]
    pending_bytes: dict[str, int] = {}
    if pending_ids:
        rows = session.execute(
            select(
                CollectionUploadFileRecord.collection_id,
                func.sum(CollectionUploadFileRecord.bytes).label("bytes"),
            )
            .where(CollectionUploadFileRecord.collection_id.in_(pending_ids))
            .group_by(CollectionUploadFileRecord.collection_id)
        ).all()
        pending_bytes = {row.collection_id: int(row.bytes or 0) for row in rows}

    for upload in upload_rows:
        if upload.collection_id in seen:
            continue
        reports.append(
            ArchiveUsageCollection(
                id=CollectionId(upload.collection_id),
                bytes=pending_bytes.get(upload.collection_id, 0),
                measured_storage_bytes=0,
                archive=ArchiveStatus(
                    state=_upload_archive_state(
                        state=upload.state,
                        archive_phase=upload.archive_phase,
                    ),
                    failure=upload.archive_failure,
                ),
            )
        )
    return reports


def _collection_measured_storage_bytes(archive: CollectionArchiveRecord | None) -> int:
    if archive is None or ArchiveState(archive.state) != ArchiveState.UPLOADED:
        return 0
    return (
        int(archive.stored_bytes or 0)
        + int(archive.manifest_stored_bytes or 0)
        + int(archive.ots_stored_bytes or 0)
    )


def _collection_archive_status(archive: CollectionArchiveRecord | None) -> ArchiveStatus:
    if archive is None:
        return ArchiveStatus()
    return ArchiveStatus(
        state=ArchiveState(archive.state),
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
    if ArchiveState(archive.state) == ArchiveState.FAILED:
        ots_state = "failed"
    return CollectionManifestStatus(
        object_path=archive.manifest_object_path,
        sha256=archive.manifest_sha256,
        ots_object_path=archive.ots_object_path,
        ots_state=ots_state,
        ots_sha256=archive.ots_sha256,
    )


def _upload_archive_state(*, state: str | None, archive_phase: str | None) -> ArchiveState:
    if state == "failed":
        return ArchiveState.FAILED
    if archive_phase == "retry_wait":
        return ArchiveState.RETRYING
    if state == "archiving":
        return ArchiveState.UPLOADING
    return ArchiveState.PENDING


def _totals_from_collections(
    collections: tuple[ArchiveUsageCollection, ...],
) -> ArchiveUsageTotals:
    return ArchiveUsageTotals(
        collections=len(collections),
        uploaded_collections=sum(
            1 for collection in collections if collection.archive.state == ArchiveState.UPLOADED
        ),
        measured_storage_bytes=sum(collection.measured_storage_bytes for collection in collections),
    )


def _ensure_usage_snapshot(session: Session, *, totals: ArchiveUsageTotals) -> None:
    latest = session.scalar(
        select(ArchiveUsageSnapshotRecord).order_by(ArchiveUsageSnapshotRecord.captured_at.desc())
    )
    if latest is not None and _snapshot_matches(latest, totals=totals):
        return
    session.add(
        ArchiveUsageSnapshotRecord(
            captured_at=_isoformat_z(utcnow()),
            uploaded_collections=totals.uploaded_collections,
            measured_storage_bytes=totals.measured_storage_bytes,
        )
    )


def _snapshot_matches(
    latest: ArchiveUsageSnapshotRecord,
    *,
    totals: ArchiveUsageTotals,
) -> bool:
    return (
        latest.uploaded_collections == totals.uploaded_collections
        and latest.measured_storage_bytes == totals.measured_storage_bytes
    )


def _isoformat_z(value: datetime) -> str:
    return value.strftime("%Y-%m-%dT%H:%M:%SZ")
