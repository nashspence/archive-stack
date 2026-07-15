from __future__ import annotations

from typing import Any

from sqlalchemy import case, func, literal, or_, select, union_all
from sqlalchemy.orm import Session, selectinload

from riverhog_core.catalog_db import make_session_factory, session_scope
from riverhog_core.catalog_models import (
    ArchiveUsageSnapshotRecord,
    CollectionArchiveCopyRecord,
    CollectionFileRecord,
    CollectionRecord,
    CollectionUploadFileRecord,
    CollectionUploadRecord,
)
from riverhog_core.domain.enums import ArchiveState
from riverhog_core.domain.models import (
    ArchiveCopyStatus,
    ArchiveUsageCollection,
    ArchiveUsageReport,
    ArchiveUsageSnapshot,
    ArchiveUsageTotals,
    CollectionManifestStatus,
)
from riverhog_core.domain.types import CollectionId
from riverhog_core.runtime_config import RuntimeConfig
from riverhog_core.timestamps import format_utc_timestamp, utc_now


class SqlAlchemyArchiveReportingService:
    def __init__(self, config: RuntimeConfig) -> None:
        self._session_factory = make_session_factory(config.database_url)

    def get_report(self, *, collection: str | None = None) -> ArchiveUsageReport:
        measured_at = format_utc_timestamp(utc_now())
        with session_scope(self._session_factory) as session:
            collection_reports = tuple(
                _collection_usage_reports(session, collection_filter=collection)
            )
            totals = _archive_usage_totals(session, collection_filter=collection)
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
    _ensure_usage_snapshot(
        session,
        totals=_archive_usage_totals(session, collection_filter=None),
    )


def _collection_usage_reports(
    session: Session,
    *,
    collection_filter: str | None,
) -> list[ArchiveUsageCollection]:
    file_stats = (
        select(
            CollectionFileRecord.collection_id.label("collection_id"),
            func.coalesce(func.sum(CollectionFileRecord.bytes), 0).label("bytes"),
        )
        .group_by(CollectionFileRecord.collection_id)
        .subquery()
    )
    archive_stats = (
        select(
            CollectionArchiveCopyRecord.collection_id.label("collection_id"),
            func.coalesce(func.sum(_measured_storage_bytes_expression()), 0).label(
                "measured_storage_bytes"
            ),
        )
        .group_by(CollectionArchiveCopyRecord.collection_id)
        .subquery()
    )
    collection_query = (
        select(
            CollectionRecord,
            func.coalesce(file_stats.c.bytes, 0).label("bytes"),
            func.coalesce(archive_stats.c.measured_storage_bytes, 0).label(
                "measured_storage_bytes"
            ),
        )
        .options(selectinload(CollectionRecord.archive_copies))
        .outerjoin(file_stats, file_stats.c.collection_id == CollectionRecord.id)
        .outerjoin(archive_stats, archive_stats.c.collection_id == CollectionRecord.id)
        .order_by(CollectionRecord.id.asc())
    )
    if collection_filter is not None:
        collection_query = collection_query.where(CollectionRecord.id == collection_filter)
    reports = [
        ArchiveUsageCollection(
            id=CollectionId(row[0].id),
            bytes=int(row.bytes),
            measured_storage_bytes=int(row.measured_storage_bytes),
            archive_copies=tuple(
                _collection_archive_status(copy)
                for copy in sorted(row[0].archive_copies, key=lambda item: item.store)
            ),
        )
        for row in session.execute(collection_query).all()
    ]

    upload_file_stats = (
        select(
            CollectionUploadFileRecord.collection_id.label("collection_id"),
            func.coalesce(func.sum(CollectionUploadFileRecord.bytes), 0).label("bytes"),
        )
        .group_by(CollectionUploadFileRecord.collection_id)
        .subquery()
    )
    accepted_collection = (
        select(CollectionRecord.id)
        .where(CollectionRecord.id == CollectionUploadRecord.collection_id)
        .exists()
    )
    upload_query = (
        select(
            CollectionUploadRecord.collection_id.label("collection_id"),
            func.coalesce(upload_file_stats.c.bytes, 0).label("bytes"),
            _upload_archive_state_expression().label("archive_state"),
            CollectionUploadRecord.archive_failure.label("archive_failure"),
            CollectionUploadRecord.archive_store.label("archive_store"),
        )
        .outerjoin(
            upload_file_stats,
            upload_file_stats.c.collection_id == CollectionUploadRecord.collection_id,
        )
        .where(
            _reportable_upload_expression(),
            ~accepted_collection,
        )
        .order_by(CollectionUploadRecord.collection_id.asc())
    )
    if collection_filter is not None:
        upload_query = upload_query.where(CollectionUploadRecord.collection_id == collection_filter)
    reports.extend(
        ArchiveUsageCollection(
            id=CollectionId(row.collection_id),
            bytes=int(row.bytes),
            measured_storage_bytes=0,
            archive_copies=(
                ArchiveCopyStatus(
                    store=row.archive_store,
                    state=ArchiveState(row.archive_state),
                    failure=row.archive_failure,
                ),
            ),
        )
        for row in session.execute(upload_query).all()
    )
    return reports


def _archive_usage_totals(
    session: Session,
    *,
    collection_filter: str | None,
) -> ArchiveUsageTotals:
    accepted = (
        select(
            CollectionRecord.id.label("collection_id"),
            case(
                (
                    func.sum(
                        case(
                            (
                                CollectionArchiveCopyRecord.state == ArchiveState.UPLOADED.value,
                                1,
                            ),
                            else_=0,
                        )
                    )
                    > 0,
                    1,
                ),
                else_=0,
            ).label("uploaded"),
            func.coalesce(func.sum(_measured_storage_bytes_expression()), 0).label(
                "measured_storage_bytes"
            ),
        )
        .outerjoin(
            CollectionArchiveCopyRecord,
            CollectionArchiveCopyRecord.collection_id == CollectionRecord.id,
        )
        .group_by(CollectionRecord.id)
    )
    pending = select(
        CollectionUploadRecord.collection_id.label("collection_id"),
        literal(0).label("uploaded"),
        literal(0).label("measured_storage_bytes"),
    ).where(
        _reportable_upload_expression(),
        ~select(CollectionRecord.id)
        .where(CollectionRecord.id == CollectionUploadRecord.collection_id)
        .exists(),
    )
    if collection_filter is not None:
        accepted = accepted.where(CollectionRecord.id == collection_filter)
        pending = pending.where(CollectionUploadRecord.collection_id == collection_filter)
    usage = union_all(accepted, pending).subquery()
    row = session.execute(
        select(
            func.count(usage.c.collection_id).label("collections"),
            func.coalesce(func.sum(usage.c.uploaded), 0).label("uploaded_collections"),
            func.coalesce(func.sum(usage.c.measured_storage_bytes), 0).label(
                "measured_storage_bytes"
            ),
        )
    ).one()
    return ArchiveUsageTotals(
        collections=int(row.collections),
        uploaded_collections=int(row.uploaded_collections),
        measured_storage_bytes=int(row.measured_storage_bytes),
    )


def _measured_storage_bytes_expression() -> Any:
    return case(
        (
            CollectionArchiveCopyRecord.state == ArchiveState.UPLOADED.value,
            func.coalesce(CollectionArchiveCopyRecord.stored_bytes, 0)
            + func.coalesce(CollectionArchiveCopyRecord.manifest_stored_bytes, 0)
            + func.coalesce(CollectionArchiveCopyRecord.ots_stored_bytes, 0),
        ),
        else_=0,
    )


def _upload_archive_state_expression() -> Any:
    return case(
        (CollectionUploadRecord.state == "failed", ArchiveState.FAILED.value),
        (CollectionUploadRecord.archive_phase == "retry_wait", ArchiveState.RETRYING.value),
        (CollectionUploadRecord.state == "archiving", ArchiveState.UPLOADING.value),
        else_=ArchiveState.PENDING.value,
    )


def _reportable_upload_expression() -> Any:
    return or_(
        CollectionUploadRecord.state.is_(None),
        ~CollectionUploadRecord.state.in_(("canceled", "expired")),
    )


def _collection_archive_status(archive: CollectionArchiveCopyRecord) -> ArchiveCopyStatus:
    return ArchiveCopyStatus(
        store=archive.store,
        state=ArchiveState(archive.state),
        object_path=archive.object_path,
        stored_bytes=archive.stored_bytes,
        backend=archive.backend,
        storage_class=archive.storage_class,
        last_uploaded_at=archive.last_uploaded_at,
        last_verified_at=archive.last_verified_at,
        failure=archive.failure,
        collection_manifest=_collection_manifest_status(archive),
        archive_format=archive.archive_format,
        compression=archive.compression,
    )


def _collection_manifest_status(
    archive: CollectionArchiveCopyRecord,
) -> CollectionManifestStatus:
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


def _ensure_usage_snapshot(session: Session, *, totals: ArchiveUsageTotals) -> None:
    latest = session.scalar(
        select(ArchiveUsageSnapshotRecord).order_by(ArchiveUsageSnapshotRecord.captured_at.desc())
    )
    if latest is not None and _snapshot_matches(latest, totals=totals):
        return
    session.add(
        ArchiveUsageSnapshotRecord(
            captured_at=format_utc_timestamp(utc_now()),
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
