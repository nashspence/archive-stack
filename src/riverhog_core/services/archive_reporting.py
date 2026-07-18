from __future__ import annotations

from typing import Any

from sqlalchemy import case, func, literal, or_, select, union_all
from sqlalchemy.orm import Session, selectinload

from riverhog_core.catalog_db import make_session_factory, session_scope
from riverhog_core.catalog_models import (
    ArchiveUsageSnapshotRecord,
    CollectionArchiveCopyRecord,
    CollectionArchiveObjectRecord,
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
from riverhog_core.ports.download_allowance import DownloadAllowance
from riverhog_core.runtime_config import RuntimeConfig
from riverhog_core.services.archive_records import (
    ArchiveCopyAggregate,
    archive_copy_aggregates,
)
from riverhog_core.services.download_allowances import SqlAlchemyDownloadAllowance
from riverhog_core.timestamps import format_utc_timestamp, utc_now


class SqlAlchemyArchiveReportingService:
    def __init__(
        self,
        config: RuntimeConfig,
        *,
        download_allowance: DownloadAllowance | None = None,
    ) -> None:
        self._session_factory = make_session_factory(config.database_url)
        self._download_allowance = download_allowance or SqlAlchemyDownloadAllowance(config)

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
            download_allowances=self._download_allowance.get_statuses(),
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
            func.coalesce(func.sum(CollectionArchiveObjectRecord.stored_bytes), 0).label(
                "measured_storage_bytes"
            ),
        )
        .join(
            CollectionArchiveObjectRecord,
            (
                CollectionArchiveObjectRecord.collection_id
                == CollectionArchiveCopyRecord.collection_id
            )
            & (CollectionArchiveObjectRecord.store == CollectionArchiveCopyRecord.store),
        )
        .where(CollectionArchiveCopyRecord.state == ArchiveState.UPLOADED.value)
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
    collection_rows = session.execute(collection_query).all()
    aggregates = archive_copy_aggregates(
        session,
        collection_ids=[str(row[0].id) for row in collection_rows],
    )
    reports = [
        ArchiveUsageCollection(
            id=CollectionId(row[0].id),
            bytes=int(row.bytes),
            measured_storage_bytes=int(row.measured_storage_bytes),
            archive_copies=tuple(
                _collection_archive_status(copy, aggregates=aggregates)
                for copy in sorted(row[0].archive_copies, key=lambda item: item.store)
            ),
        )
        for row in collection_rows
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
    stored = (
        select(
            CollectionArchiveCopyRecord.collection_id.label("collection_id"),
            func.count(func.distinct(CollectionArchiveCopyRecord.store)).label("copies"),
            func.coalesce(func.sum(CollectionArchiveObjectRecord.stored_bytes), 0).label(
                "stored_bytes"
            ),
        )
        .join(
            CollectionArchiveObjectRecord,
            (
                CollectionArchiveObjectRecord.collection_id
                == CollectionArchiveCopyRecord.collection_id
            )
            & (CollectionArchiveObjectRecord.store == CollectionArchiveCopyRecord.store),
        )
        .where(CollectionArchiveCopyRecord.state == ArchiveState.UPLOADED.value)
        .group_by(CollectionArchiveCopyRecord.collection_id)
        .subquery()
    )
    accepted = select(
        CollectionRecord.id.label("collection_id"),
        case((func.coalesce(stored.c.copies, 0) > 0, 1), else_=0).label("uploaded"),
        func.coalesce(stored.c.stored_bytes, 0).label("measured_storage_bytes"),
    ).outerjoin(stored, stored.c.collection_id == CollectionRecord.id)
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


def _collection_archive_status(
    archive: CollectionArchiveCopyRecord,
    *,
    aggregates: dict[tuple[str, str], ArchiveCopyAggregate],
) -> ArchiveCopyStatus:
    object_count, stored_bytes = aggregates.get((archive.collection_id, archive.store), (0, 0))
    return ArchiveCopyStatus(
        store=archive.store,
        state=ArchiveState(archive.state),
        storage_prefix=archive.archive_storage_prefix,
        object_count=object_count,
        stored_bytes=stored_bytes,
        backend=archive.backend,
        storage_class=archive.storage_class,
        last_uploaded_at=archive.last_uploaded_at,
        last_verified_at=archive.last_verified_at,
        failure=archive.failure,
        collection_manifest=_collection_manifest_status(archive),
    )


def _collection_manifest_status(
    archive: CollectionArchiveCopyRecord,
) -> CollectionManifestStatus:
    manifest = next(
        (current for current in archive.objects if current.object_id == "manifest"),
        None,
    )
    proof = next(
        (current for current in archive.objects if current.object_id == "proof"),
        None,
    )
    proof_state = "uploaded" if proof else "pending"
    if ArchiveState(archive.state) == ArchiveState.FAILED:
        proof_state = "failed"
    return CollectionManifestStatus(
        object_path=manifest.object_path if manifest else None,
        sha256=manifest.sha256 if manifest else None,
        proof_object_path=proof.object_path if proof else None,
        proof_state=proof_state,
        proof_sha256=proof.sha256 if proof else None,
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
