from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

import yaml
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from arc_core.archive_compliance import normalize_glacier_state
from arc_core.catalog_models import (
    CollectionFileRecord,
    FinalizedImageRecord,
    GlacierUsageSnapshotRecord,
)
from arc_core.domain.models import (
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
from arc_core.planner.manifest import MANIFEST_FILENAME
from arc_core.recovery_payloads import decrypt_recovery_payload
from arc_core.runtime_config import RuntimeConfig
from arc_core.services.glacier_billing import resolve_glacier_billing
from arc_core.services.glacier_pricing import resolve_glacier_pricing
from arc_core.sqlite_db import make_session_factory, session_scope
from arc_core.webhooks import utcnow

_BYTES_PER_GIB = Decimal(1024**3)
_USD_QUANTUM = Decimal("0.000000000001")


@dataclass(frozen=True)
class _ImageCollectionAttribution:
    image_id: str
    filename: str
    collection_id: str
    total_collection_bytes: int
    represented_bytes: int
    glacier: GlacierArchiveStatus
    derived_stored_bytes: int | None
    derived_billable_bytes: int | None
    estimated_monthly_cost_usd: float | None
    represented_fraction: float | None


class SqlAlchemyGlacierReportingService:
    def __init__(self, config: RuntimeConfig) -> None:
        self._config = config
        self._session_factory = make_session_factory(str(config.sqlite_path))

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
            collection_files = session.scalars(select(CollectionFileRecord)).all()
            file_bytes = {
                (record.collection_id, record.path): record.bytes for record in collection_files
            }
            collection_total_bytes = defaultdict(int)
            for record in collection_files:
                collection_total_bytes[record.collection_id] += record.bytes

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

            image_reports = tuple(
                _image_usage_report(record, pricing_basis=pricing_basis)
                for record in filtered_images
            )
            collection_reports = tuple(
                _collection_usage_reports(
                    filtered_images=filtered_images,
                    collection_filter=collection,
                    file_bytes=file_bytes,
                    collection_total_bytes=dict(collection_total_bytes),
                    pricing_basis=pricing_basis,
                )
            )

            if collection is None:
                totals = _totals_from_images(image_reports)
            else:
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
                        uploaded_images=record.uploaded_images,
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


def record_glacier_usage_snapshot(session, *, config: RuntimeConfig) -> None:
    pricing_basis = _pricing_basis(config)
    image_records = session.scalars(select(FinalizedImageRecord)).all()
    totals = _totals_from_images(
        tuple(_image_usage_report(record, pricing_basis=pricing_basis) for record in image_records)
    )
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


def _image_usage_report(
    record: FinalizedImageRecord,
    *,
    pricing_basis: GlacierPricingBasis,
) -> GlacierUsageImage:
    glacier = _glacier_archive_status(record)
    measured_storage_bytes = (
        int(record.glacier_stored_bytes)
        if glacier.state.value == "uploaded" and record.glacier_stored_bytes is not None
        else 0
    )
    estimated_billable_bytes = _billable_bytes_for_object(
        measured_storage_bytes,
        pricing_basis=pricing_basis,
    )
    estimated_monthly_cost_usd = _estimate_monthly_cost_usd(
        measured_storage_bytes,
        object_count=1 if measured_storage_bytes > 0 else 0,
        pricing_basis=pricing_basis,
    )
    collection_ids = sorted({covered_path.collection_id for covered_path in record.covered_paths})
    return GlacierUsageImage(
        id=record.image_id,
        filename=record.filename,
        collection_ids=collection_ids,
        glacier=glacier,
        measured_storage_bytes=measured_storage_bytes,
        estimated_billable_bytes=estimated_billable_bytes,
        estimated_monthly_cost_usd=estimated_monthly_cost_usd,
    )


def _collection_usage_reports(
    *,
    filtered_images: list[FinalizedImageRecord],
    collection_filter: str | None,
    file_bytes: dict[tuple[str, str], int],
    collection_total_bytes: dict[str, int],
    pricing_basis: GlacierPricingBasis,
) -> list[GlacierUsageCollection]:
    collections: dict[str, list[_ImageCollectionAttribution]] = defaultdict(list)

    for image in filtered_images:
        represented_by_collection, attribution_available = _represented_bytes_by_collection(
            image_root=image.image_root,
            file_bytes=file_bytes,
        )
        if collection_filter is not None:
            represented_by_collection = {
                collection_id: represented
                for collection_id, represented in represented_by_collection.items()
                if collection_id == collection_filter
            }
        total_represented_bytes = sum(represented_by_collection.values())
        glacier = _glacier_archive_status(image)
        image_stored_bytes = (
            int(image.glacier_stored_bytes)
            if glacier.state.value == "uploaded" and image.glacier_stored_bytes is not None
            else 0
        )

        for collection_id, represented_bytes in represented_by_collection.items():
            represented_fraction = (
                represented_bytes / total_represented_bytes
                if attribution_available and total_represented_bytes > 0
                else None
            )
            if represented_fraction is None or image_stored_bytes <= 0:
                derived_stored_bytes = None
                derived_billable_bytes = None
                estimated_monthly_cost_usd = None
            else:
                derived_stored_bytes = _round_int(image_stored_bytes * represented_fraction)
                derived_billable_bytes = _round_int(
                    _billable_bytes_for_object(
                        image_stored_bytes,
                        pricing_basis=pricing_basis,
                    )
                    * represented_fraction
                )
                estimated_monthly_cost_usd = _round_usd(
                    _estimate_monthly_cost_usd(
                        derived_stored_bytes,
                        object_count=0,
                        pricing_basis=pricing_basis,
                        archived_metadata_bytes=pricing_basis.archived_metadata_bytes_per_object
                        * represented_fraction,
                        standard_metadata_bytes=pricing_basis.standard_metadata_bytes_per_object
                        * represented_fraction,
                    )
                )
            collections[collection_id].append(
                _ImageCollectionAttribution(
                    image_id=image.image_id,
                    filename=image.filename,
                    collection_id=collection_id,
                    total_collection_bytes=collection_total_bytes.get(collection_id, 0),
                    represented_bytes=represented_bytes,
                    glacier=glacier,
                    derived_stored_bytes=derived_stored_bytes,
                    derived_billable_bytes=derived_billable_bytes,
                    estimated_monthly_cost_usd=estimated_monthly_cost_usd,
                    represented_fraction=represented_fraction,
                )
            )

    result: list[GlacierUsageCollection] = []
    for collection_id in sorted(collections):
        contributions = collections[collection_id]
        result.append(
            GlacierUsageCollection(
                id=collection_id,
                bytes=contributions[0].total_collection_bytes,
                represented_bytes=sum(item.represented_bytes for item in contributions),
                attribution_state=(
                    "derived"
                    if any(item.derived_stored_bytes is not None for item in contributions)
                    else "unavailable"
                ),
                derived_stored_bytes=sum(item.derived_stored_bytes or 0 for item in contributions),
                derived_billable_bytes=sum(
                    item.derived_billable_bytes or 0 for item in contributions
                ),
                estimated_monthly_cost_usd=_round_usd(
                    sum(item.estimated_monthly_cost_usd or 0.0 for item in contributions)
                ),
                images=tuple(
                    GlacierCollectionContribution(
                        image_id=item.image_id,
                        filename=item.filename,
                        glacier=item.glacier,
                        represented_bytes=item.represented_bytes,
                        represented_fraction=item.represented_fraction,
                        derived_stored_bytes=item.derived_stored_bytes,
                        derived_billable_bytes=item.derived_billable_bytes,
                        estimated_monthly_cost_usd=item.estimated_monthly_cost_usd,
                    )
                    for item in sorted(
                        contributions,
                        key=lambda current: current.image_id,
                        reverse=True,
                    )
                ),
            )
        )
    return result


def _represented_bytes_by_collection(
    *,
    image_root: str,
    file_bytes: dict[tuple[str, str], int],
) -> tuple[dict[str, int], bool]:
    manifest_path = Path(image_root) / MANIFEST_FILENAME
    try:
        manifest = yaml.safe_load(decrypt_recovery_payload(manifest_path.read_bytes()))
    except Exception:
        return {}, False

    represented_by_collection: dict[str, int] = defaultdict(int)
    for collection in manifest.get("collections", []):
        collection_id = str(collection["id"])
        for file_entry in collection.get("files", []):
            path = str(file_entry["path"]).lstrip("/")
            total_bytes = file_bytes.get((collection_id, path))
            if total_bytes is None:
                return {}, False
            parts_block = file_entry.get("parts")
            if parts_block is None:
                represented_by_collection[collection_id] += total_bytes
                continue
            part_count = int(parts_block["count"])
            for present in parts_block.get("present", []):
                represented_by_collection[collection_id] += _split_part_length(
                    total_bytes,
                    part_count=part_count,
                    part_index=int(present["index"]) - 1,
                )
    return dict(represented_by_collection), True


def _split_part_length(total_bytes: int, *, part_count: int, part_index: int) -> int:
    base, remainder = divmod(total_bytes, part_count)
    return base + int(part_index < remainder)


def _glacier_archive_status(image: FinalizedImageRecord) -> GlacierArchiveStatus:
    return GlacierArchiveStatus(
        state=normalize_glacier_state(image.glacier_state),
        object_path=image.glacier_object_path,
        stored_bytes=image.glacier_stored_bytes,
        backend=image.glacier_backend,
        storage_class=image.glacier_storage_class,
        last_uploaded_at=image.glacier_last_uploaded_at,
        last_verified_at=image.glacier_last_verified_at,
        failure=image.glacier_failure,
    )


def _totals_from_images(images: tuple[GlacierUsageImage, ...]) -> GlacierUsageTotals:
    return GlacierUsageTotals(
        images=len(images),
        uploaded_images=sum(1 for image in images if image.measured_storage_bytes > 0),
        measured_storage_bytes=sum(image.measured_storage_bytes for image in images),
        estimated_billable_bytes=sum(image.estimated_billable_bytes for image in images),
        estimated_monthly_cost_usd=_round_usd(
            sum(image.estimated_monthly_cost_usd for image in images)
        ),
    )


def _totals_from_collections(collections: tuple[GlacierUsageCollection, ...]) -> GlacierUsageTotals:
    uploaded_images = {
        contribution.image_id
        for collection in collections
        for contribution in collection.images
        if contribution.derived_stored_bytes is not None
    }
    return GlacierUsageTotals(
        images=len(
            {
                contribution.image_id
                for collection in collections
                for contribution in collection.images
            }
        ),
        uploaded_images=len(uploaded_images),
        measured_storage_bytes=sum(collection.derived_stored_bytes for collection in collections),
        estimated_billable_bytes=sum(
            collection.derived_billable_bytes for collection in collections
        ),
        estimated_monthly_cost_usd=_round_usd(
            sum(collection.estimated_monthly_cost_usd for collection in collections)
        ),
    )


def _billable_bytes_for_object(
    measured_storage_bytes: int, *, pricing_basis: GlacierPricingBasis
) -> int:
    if measured_storage_bytes <= 0:
        return 0
    return (
        measured_storage_bytes
        + pricing_basis.archived_metadata_bytes_per_object
        + pricing_basis.standard_metadata_bytes_per_object
    )


def _estimate_monthly_cost_usd(
    measured_storage_bytes: int,
    *,
    object_count: int,
    pricing_basis: GlacierPricingBasis,
    archived_metadata_bytes: float | None = None,
    standard_metadata_bytes: float | None = None,
) -> float:
    archived_bytes = Decimal(measured_storage_bytes) + Decimal(
        str(
            archived_metadata_bytes
            if archived_metadata_bytes is not None
            else pricing_basis.archived_metadata_bytes_per_object * object_count
        )
    )
    standard_bytes = Decimal(
        str(
            standard_metadata_bytes
            if standard_metadata_bytes is not None
            else pricing_basis.standard_metadata_bytes_per_object * object_count
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
    session,
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
            uploaded_images=totals.uploaded_images,
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
        latest.uploaded_images == totals.uploaded_images
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


def _round_int(value: float) -> int:
    return int(Decimal(str(value)).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def _isoformat_z(value) -> str:
    return value.strftime("%Y-%m-%dT%H:%M:%SZ")
