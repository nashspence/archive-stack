from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from riverhog_core.archive_compliance import normalize_glacier_state
from riverhog_core.catalog_db import make_session_factory, session_scope
from riverhog_core.catalog_models import (
    CollectionArchiveRecord,
    CollectionFileRecord,
    CollectionRecord,
    CollectionUploadRecord,
    FinalizedImageRecord,
    GlacierUsageSnapshotRecord,
)
from riverhog_core.domain.enums import GlacierState
from riverhog_core.domain.models import (
    CollectionManifestStatus,
    GlacierArchiveStatus,
    GlacierCollectionContribution,
    GlacierPricingBasis,
    GlacierReportingContext,
    GlacierUsageCollection,
    GlacierUsageImage,
    GlacierUsageReport,
    GlacierUsageSnapshot,
    GlacierUsageTotals,
)
from riverhog_core.domain.types import CollectionId, ImageId
from riverhog_core.runtime_config import RuntimeConfig
from riverhog_core.services.glacier_billing import resolve_glacier_billing
from riverhog_core.services.glacier_pricing import resolve_glacier_pricing
from riverhog_core.webhooks import utcnow

_BYTES_PER_GIB = Decimal(1024**3)
_USD_QUANTUM = Decimal("0.000000000001")


class SqlAlchemyGlacierReportingService:
    def __init__(self, config: RuntimeConfig) -> None:
        self._config = config
        self._session_factory = make_session_factory(config.database_url)

    def get_report(
        self,
        *,
        image_id: str | None = None,
        collection: str | None = None,
    ) -> GlacierUsageReport:
        measured_at = _isoformat_z(utcnow())
        context = _reporting_context(
            self._config,
            include_billing=image_id is None and collection is None,
        )
        pricing_basis = context.pricing_basis
        billing = context.billing

        with session_scope(self._session_factory) as session:
            image_records = session.scalars(
                select(FinalizedImageRecord).options(
                    selectinload(FinalizedImageRecord.covered_paths),
                )
            ).all()

            filtered_images = [
                record
                for record in image_records
                if (image_id is None or record.image_id == image_id)
                and (
                    collection is None
                    or any(path.collection_id == collection for path in record.covered_paths)
                )
            ]
            filtered_images.sort(key=lambda record: record.image_id, reverse=True)

            image_reports = tuple(_image_usage_report(record) for record in filtered_images)
            collection_reports = tuple(
                _direct_collection_usage_reports(
                    session,
                    filtered_images=filtered_images,
                    collection_filter=collection,
                    pricing_basis=pricing_basis,
                )
            )

            totals = _totals_from_collections(collection_reports)

            history: tuple[GlacierUsageSnapshot, ...] = ()
            if image_id is None and collection is None:
                _ensure_usage_snapshot(
                    session,
                    totals=totals,
                    pricing_basis=pricing_basis,
                )
                session.flush()
                history = tuple(
                    GlacierUsageSnapshot(
                        captured_at=record.captured_at,
                        uploaded_collections=record.uploaded_images,
                        measured_storage_bytes=record.measured_storage_bytes,
                        estimated_billable_bytes=record.estimated_billable_bytes,
                        estimated_monthly_cost_usd=_round_usd(record.estimated_monthly_cost_usd),
                    )
                    for record in session.scalars(
                        select(GlacierUsageSnapshotRecord).order_by(
                            GlacierUsageSnapshotRecord.captured_at.desc()
                        )
                    ).all()
                )

        return GlacierUsageReport(
            scope=_scope_name(image_id=image_id, collection=collection),
            measured_at=measured_at,
            pricing_basis=pricing_basis,
            totals=totals,
            images=image_reports,
            collections=collection_reports,
            history=history,
            billing=billing,
        )


def record_glacier_usage_snapshot(session: Session, *, config: RuntimeConfig) -> None:
    pricing_basis = _pricing_basis(config)
    image_records = session.scalars(
        select(FinalizedImageRecord).options(
            selectinload(FinalizedImageRecord.covered_paths),
        )
    ).all()
    collection_reports = tuple(
        _direct_collection_usage_reports(
            session,
            filtered_images=list(image_records),
            collection_filter=None,
            pricing_basis=pricing_basis,
        )
    )
    totals = _totals_from_collections(collection_reports)
    _ensure_usage_snapshot(session, totals=totals, pricing_basis=pricing_basis)


def _scope_name(*, image_id: str | None, collection: str | None) -> str:
    if image_id is not None and collection is not None:
        return "filtered"
    if image_id is not None:
        return "image"
    if collection is not None:
        return "collection"
    return "all"


def _pricing_basis(config: RuntimeConfig) -> GlacierPricingBasis:
    return resolve_glacier_pricing(config)


def _reporting_context(
    config: RuntimeConfig,
    *,
    include_billing: bool,
) -> GlacierReportingContext:
    pricing_basis = _pricing_basis(config)
    billing = resolve_glacier_billing(config, include=include_billing)
    return GlacierReportingContext(
        pricing_basis=pricing_basis,
        billing=billing,
    )


def _image_usage_report(record: FinalizedImageRecord) -> GlacierUsageImage:
    collection_ids = sorted({covered_path.collection_id for covered_path in record.covered_paths})
    return GlacierUsageImage(
        id=ImageId(record.image_id),
        filename=record.filename,
        collection_ids=collection_ids,
    )


def _direct_collection_usage_reports(
    session: Session,
    *,
    filtered_images: list[FinalizedImageRecord],
    collection_filter: str | None,
    pricing_basis: GlacierPricingBasis,
) -> list[GlacierUsageCollection]:
    collections = session.scalars(
        select(CollectionRecord).options(
            selectinload(CollectionRecord.files),
            selectinload(CollectionRecord.archive),
        )
    ).all()
    uploads = session.scalars(
        select(CollectionUploadRecord)
        .options(selectinload(CollectionUploadRecord.files))
        .where(~CollectionUploadRecord.state.in_(("canceled", "expired")))
    ).all()

    file_bytes_by_path = {
        (file.collection_id, file.path): file.bytes
        for file in session.scalars(select(CollectionFileRecord)).all()
    }
    image_contributions = _image_contributions_by_collection(
        filtered_images,
        collection_filter=collection_filter,
        file_bytes_by_path=file_bytes_by_path,
    )
    reports: list[GlacierUsageCollection] = []
    seen: set[str] = set()
    for collection in sorted(collections, key=lambda current: current.id):
        if collection_filter is not None and collection.id != collection_filter:
            continue
        seen.add(collection.id)
        archive = collection.archive
        measured_storage_bytes = _collection_measured_storage_bytes(archive)
        archived_storage_bytes = _collection_archived_storage_bytes(archive)
        standard_storage_bytes = _collection_standard_storage_bytes(archive)
        archived_object_count = _collection_archived_object_count(archive)
        billable_bytes = _billable_bytes_for_storage_classes(
            archived_storage_bytes=archived_storage_bytes,
            standard_storage_bytes=standard_storage_bytes,
            archived_object_count=archived_object_count,
            pricing_basis=pricing_basis,
        )
        reports.append(
            GlacierUsageCollection(
                id=CollectionId(collection.id),
                bytes=sum(file.bytes for file in collection.files),
                measured_storage_bytes=measured_storage_bytes,
                estimated_billable_bytes=billable_bytes,
                estimated_monthly_cost_usd=_estimate_monthly_cost_usd(
                    archived_storage_bytes=archived_storage_bytes,
                    standard_storage_bytes=standard_storage_bytes,
                    archived_object_count=archived_object_count,
                    pricing_basis=pricing_basis,
                ),
                images=tuple(image_contributions.get(collection.id, ())),
                glacier=_collection_glacier_archive_status(archive),
                collection_manifest=_collection_manifest_status(archive),
                archive_format=archive.archive_format if archive is not None else None,
                compression=archive.compression if archive is not None else None,
            )
        )

    for upload in sorted(uploads, key=lambda current: current.collection_id):
        if upload.collection_id in seen:
            continue
        if collection_filter is not None and upload.collection_id != collection_filter:
            continue
        reports.append(
            GlacierUsageCollection(
                id=CollectionId(upload.collection_id),
                bytes=sum(file.bytes for file in upload.files),
                measured_storage_bytes=0,
                estimated_billable_bytes=0,
                estimated_monthly_cost_usd=0.0,
                images=(),
                glacier=GlacierArchiveStatus(
                    state=_upload_glacier_state(upload),
                    failure=upload.archive_failure,
                ),
                collection_manifest=None,
                archive_format=None,
                compression=None,
            )
        )
    return reports


def _image_contributions_by_collection(
    images: list[FinalizedImageRecord],
    *,
    collection_filter: str | None,
    file_bytes_by_path: dict[tuple[str, str], int],
) -> dict[str, tuple[GlacierCollectionContribution, ...]]:
    result: dict[str, list[GlacierCollectionContribution]] = defaultdict(list)
    for image in images:
        represented_by_collection: dict[str, int] = defaultdict(int)
        for path in image.covered_paths:
            represented_by_collection[path.collection_id] += file_bytes_by_path.get(
                (path.collection_id, path.path),
                0,
            )
        for collection_id, represented_bytes in sorted(represented_by_collection.items()):
            if collection_filter is not None and collection_id != collection_filter:
                continue
            result[collection_id].append(
                GlacierCollectionContribution(
                    image_id=ImageId(image.image_id),
                    filename=image.filename,
                    represented_bytes=represented_bytes,
                )
            )
    return {
        collection_id: tuple(
            sorted(contributions, key=lambda current: str(current.image_id), reverse=True)
        )
        for collection_id, contributions in result.items()
    }


def _collection_measured_storage_bytes(archive: CollectionArchiveRecord | None) -> int:
    if archive is None or normalize_glacier_state(archive.state).value != "uploaded":
        return 0
    return (
        int(archive.stored_bytes or 0)
        + int(archive.manifest_stored_bytes or 0)
        + int(archive.ots_stored_bytes or 0)
    )


def _collection_archived_storage_bytes(archive: CollectionArchiveRecord | None) -> int:
    if archive is None or normalize_glacier_state(archive.state).value != "uploaded":
        return 0
    if _is_archival_storage_class(archive.storage_class):
        return int(archive.stored_bytes or 0)
    return 0


def _collection_standard_storage_bytes(archive: CollectionArchiveRecord | None) -> int:
    if archive is None or normalize_glacier_state(archive.state).value != "uploaded":
        return 0
    standard_bytes = int(archive.manifest_stored_bytes or 0) + int(
        archive.ots_stored_bytes or 0
    )
    if not _is_archival_storage_class(archive.storage_class):
        standard_bytes += int(archive.stored_bytes or 0)
    return standard_bytes


def _collection_archived_object_count(archive: CollectionArchiveRecord | None) -> int:
    if archive is None or normalize_glacier_state(archive.state).value != "uploaded":
        return 0
    if archive.object_path and _is_archival_storage_class(archive.storage_class):
        return 1
    return 0


def _is_archival_storage_class(storage_class: str | None) -> bool:
    normalized = str(storage_class or "").strip().upper()
    return normalized not in {"", "STANDARD", "REDUCED_REDUNDANCY", "INTELLIGENT_TIERING"}


def _collection_glacier_archive_status(
    archive: CollectionArchiveRecord | None,
) -> GlacierArchiveStatus:
    if archive is None:
        return GlacierArchiveStatus()
    return GlacierArchiveStatus(
        state=normalize_glacier_state(archive.state),
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
    if normalize_glacier_state(archive.state).value == "failed":
        ots_state = "failed"
    return CollectionManifestStatus(
        object_path=archive.manifest_object_path,
        sha256=archive.manifest_sha256,
        ots_object_path=archive.ots_object_path,
        ots_state=ots_state,
        ots_sha256=archive.ots_sha256,
    )


def _upload_glacier_state(upload: CollectionUploadRecord) -> GlacierState:
    if upload.state == "failed":
        return GlacierState.FAILED
    if upload.archive_phase == "retry_wait":
        return GlacierState.RETRYING
    if upload.state == "archiving":
        return GlacierState.UPLOADING
    return GlacierState.PENDING


def _totals_from_collections(collections: tuple[GlacierUsageCollection, ...]) -> GlacierUsageTotals:
    return GlacierUsageTotals(
        collections=len(collections),
        uploaded_collections=sum(
            1 for collection in collections if collection.measured_storage_bytes > 0
        ),
        measured_storage_bytes=sum(collection.measured_storage_bytes for collection in collections),
        estimated_billable_bytes=sum(
            collection.estimated_billable_bytes for collection in collections
        ),
        estimated_monthly_cost_usd=_round_usd(
            sum(collection.estimated_monthly_cost_usd for collection in collections)
        ),
    )


def _billable_bytes_for_storage_classes(
    *,
    archived_storage_bytes: int,
    standard_storage_bytes: int,
    archived_object_count: int,
    pricing_basis: GlacierPricingBasis,
) -> int:
    if archived_storage_bytes <= 0 and standard_storage_bytes <= 0:
        return 0
    return (
        archived_storage_bytes
        + standard_storage_bytes
        + pricing_basis.archived_metadata_bytes_per_object * archived_object_count
        + pricing_basis.standard_metadata_bytes_per_object * archived_object_count
    )


def _estimate_monthly_cost_usd(
    *,
    archived_storage_bytes: int,
    standard_storage_bytes: int,
    archived_object_count: int,
    pricing_basis: GlacierPricingBasis,
    archived_metadata_bytes: float | None = None,
    standard_metadata_bytes: float | None = None,
) -> float:
    archived_bytes = Decimal(archived_storage_bytes) + Decimal(
        str(
            archived_metadata_bytes
            if archived_metadata_bytes is not None
            else pricing_basis.archived_metadata_bytes_per_object * archived_object_count
        )
    )
    standard_bytes = Decimal(standard_storage_bytes) + Decimal(
        str(
            standard_metadata_bytes
            if standard_metadata_bytes is not None
            else pricing_basis.standard_metadata_bytes_per_object * archived_object_count
        )
    )
    glacier_rate = Decimal(str(pricing_basis.glacier_storage_rate_usd_per_gib_month))
    standard_rate = Decimal(str(pricing_basis.standard_storage_rate_usd_per_gib_month))
    return float(
        (
            (archived_bytes / _BYTES_PER_GIB * glacier_rate)
            + (standard_bytes / _BYTES_PER_GIB * standard_rate)
        ).quantize(_USD_QUANTUM, rounding=ROUND_HALF_UP)
    )


def _ensure_usage_snapshot(
    session: Session,
    *,
    totals: GlacierUsageTotals,
    pricing_basis: GlacierPricingBasis,
) -> None:
    latest = session.scalar(
        select(GlacierUsageSnapshotRecord).order_by(GlacierUsageSnapshotRecord.captured_at.desc())
    )
    if latest is not None and _snapshot_matches(latest, totals=totals, pricing_basis=pricing_basis):
        return
    session.add(
        GlacierUsageSnapshotRecord(
            captured_at=_isoformat_z(utcnow()),
            uploaded_images=totals.uploaded_collections,
            measured_storage_bytes=totals.measured_storage_bytes,
            estimated_billable_bytes=totals.estimated_billable_bytes,
            estimated_monthly_cost_usd=totals.estimated_monthly_cost_usd,
            pricing_label=pricing_basis.label,
            glacier_storage_rate_usd_per_gib_month=pricing_basis.glacier_storage_rate_usd_per_gib_month,
            standard_storage_rate_usd_per_gib_month=pricing_basis.standard_storage_rate_usd_per_gib_month,
            archived_metadata_bytes_per_object=pricing_basis.archived_metadata_bytes_per_object,
            standard_metadata_bytes_per_object=pricing_basis.standard_metadata_bytes_per_object,
            minimum_storage_duration_days=pricing_basis.minimum_storage_duration_days,
        )
    )


def _snapshot_matches(
    latest: GlacierUsageSnapshotRecord,
    *,
    totals: GlacierUsageTotals,
    pricing_basis: GlacierPricingBasis,
) -> bool:
    return (
        latest.uploaded_images == totals.uploaded_collections
        and latest.measured_storage_bytes == totals.measured_storage_bytes
        and latest.estimated_billable_bytes == totals.estimated_billable_bytes
        and _round_usd(latest.estimated_monthly_cost_usd) == totals.estimated_monthly_cost_usd
        and latest.pricing_label == pricing_basis.label
        and _round_usd(latest.glacier_storage_rate_usd_per_gib_month)
        == _round_usd(pricing_basis.glacier_storage_rate_usd_per_gib_month)
        and _round_usd(latest.standard_storage_rate_usd_per_gib_month)
        == _round_usd(pricing_basis.standard_storage_rate_usd_per_gib_month)
        and latest.archived_metadata_bytes_per_object
        == pricing_basis.archived_metadata_bytes_per_object
        and latest.standard_metadata_bytes_per_object
        == pricing_basis.standard_metadata_bytes_per_object
        and latest.minimum_storage_duration_days == pricing_basis.minimum_storage_duration_days
    )


def _round_usd(value: float) -> float:
    return float(Decimal(str(value)).quantize(_USD_QUANTUM, rounding=ROUND_HALF_UP))


def _isoformat_z(value: datetime) -> str:
    return value.strftime("%Y-%m-%dT%H:%M:%SZ")
