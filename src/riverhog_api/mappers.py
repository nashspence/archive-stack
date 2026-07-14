from __future__ import annotations

from riverhog_core.domain.models import (
    ArchiveCollectionContribution,
    ArchiveRestoreCollection,
    ArchiveRestoreImage,
    ArchiveRestoreListPage,
    ArchiveRestoreNotificationStatus,
    ArchiveRestoreProgress,
    ArchiveRestoreSummary,
    ArchiveStatus,
    ArchiveUsageCollection,
    ArchiveUsageImage,
    ArchiveUsageReport,
    ArchiveUsageSnapshot,
    ArchiveUsageTotals,
    CollectionCoverageImage,
    CollectionListPage,
    CollectionManifestStatus,
    CollectionSummary,
    Coverage,
    DiscHistoryEntry,
    DiscSummary,
    FetchListPage,
    FetchSummary,
)


def map_archive(summary: ArchiveStatus) -> dict[str, object]:
    return {
        "state": summary.state.value,
        "object_path": summary.object_path,
        "stored_bytes": summary.stored_bytes,
        "backend": summary.backend,
        "storage_class": summary.storage_class,
        "last_uploaded_at": summary.last_uploaded_at,
        "last_verified_at": summary.last_verified_at,
        "failure": summary.failure,
    }


def map_collection_manifest(
    summary: CollectionManifestStatus | None,
) -> dict[str, object] | None:
    if summary is None:
        return None
    return {
        "object_path": summary.object_path,
        "sha256": summary.sha256,
        "ots_object_path": summary.ots_object_path,
        "ots_state": summary.ots_state,
    }


def map_archive_usage_totals(summary: ArchiveUsageTotals) -> dict[str, object]:
    return {
        "collections": summary.collections,
        "uploaded_collections": summary.uploaded_collections,
        "measured_storage_bytes": summary.measured_storage_bytes,
    }


def map_archive_usage_image(summary: ArchiveUsageImage) -> dict[str, object]:
    return {
        "id": str(summary.id),
        "filename": summary.filename,
        "collection_ids": list(summary.collection_ids),
    }


def map_archive_collection_contribution(
    summary: ArchiveCollectionContribution,
) -> dict[str, object]:
    return {
        "image_id": str(summary.image_id),
        "filename": summary.filename,
        "represented_bytes": summary.represented_bytes,
    }


def map_archive_usage_collection(summary: ArchiveUsageCollection) -> dict[str, object]:
    return {
        "id": str(summary.id),
        "bytes": summary.bytes,
        "archive": map_archive(summary.archive),
        "collection_manifest": map_collection_manifest(summary.collection_manifest),
        "archive_format": summary.archive_format,
        "compression": summary.compression,
        "measured_storage_bytes": summary.measured_storage_bytes,
        "images": [map_archive_collection_contribution(image) for image in summary.images],
    }


def map_archive_usage_snapshot(summary: ArchiveUsageSnapshot) -> dict[str, object]:
    return {
        "captured_at": summary.captured_at,
        "uploaded_collections": summary.uploaded_collections,
        "measured_storage_bytes": summary.measured_storage_bytes,
    }


def map_archive_usage_report(summary: ArchiveUsageReport) -> dict[str, object]:
    return {
        "scope": summary.scope,
        "measured_at": summary.measured_at,
        "totals": map_archive_usage_totals(summary.totals),
        "images": [map_archive_usage_image(image) for image in summary.images],
        "collections": [map_archive_usage_collection(item) for item in summary.collections],
        "history": [map_archive_usage_snapshot(item) for item in summary.history],
    }


def map_collection(
    summary: CollectionSummary,
    *,
    coverage_path_limit: int | None = None,
) -> dict[str, object]:
    return {
        "id": str(summary.id),
        "files": summary.files,
        "bytes": summary.bytes,
        "hot_bytes": summary.hot_bytes,
        "archive": map_archive(summary.archive),
        "collection_manifest": map_collection_manifest(summary.collection_manifest),
        "archive_format": summary.archive_format,
        "compression": summary.compression,
        "disc_coverage": map_coverage(summary.disc_coverage),
        "disc_redundancy": map_coverage(summary.disc_redundancy),
        "image_coverage": [
            map_collection_coverage_image(image, path_limit=coverage_path_limit)
            for image in summary.image_coverage
        ],
    }


def map_collection_list_item(summary: CollectionSummary) -> dict[str, object]:
    return {
        "id": str(summary.id),
        "files": summary.files,
        "bytes": summary.bytes,
        "hot_bytes": summary.hot_bytes,
        "archive": map_archive(summary.archive),
        "collection_manifest": map_collection_manifest(summary.collection_manifest),
        "archive_format": summary.archive_format,
        "compression": summary.compression,
        "disc_coverage": map_coverage(summary.disc_coverage),
        "disc_redundancy": map_coverage(summary.disc_redundancy),
    }


def map_collection_list_page(summary: CollectionListPage) -> dict[str, object]:
    return {
        "page": summary.page,
        "per_page": summary.per_page,
        "total": summary.total,
        "pages": summary.pages,
        "collections": [map_collection_list_item(collection) for collection in summary.collections],
    }


def map_coverage(summary: Coverage) -> dict[str, object]:
    return {
        "state": summary.state.value,
        "bytes": summary.bytes,
    }


def map_archive_restore_notification(
    summary: ArchiveRestoreNotificationStatus,
) -> dict[str, object]:
    return {
        "webhook_configured": summary.webhook_configured,
        "reminder_count": summary.reminder_count,
        "next_reminder_at": summary.next_reminder_at,
        "last_notified_at": summary.last_notified_at,
        "failure_count": summary.failure_count,
        "last_failure_at": summary.last_failure_at,
        "last_failure": summary.last_failure,
    }


def map_archive_restore_progress(summary: ArchiveRestoreProgress) -> dict[str, object]:
    return {
        "archive_verification": summary.archive_verification,
        "extraction": summary.extraction,
        "materialization": summary.materialization,
    }


def map_archive_restore_image(summary: ArchiveRestoreImage) -> dict[str, object]:
    return {
        "id": str(summary.id),
        "filename": summary.filename,
        "collection_ids": [str(collection_id) for collection_id in summary.collection_ids],
        "rebuild_state": summary.rebuild_state,
    }


def map_archive_restore_collection(summary: ArchiveRestoreCollection) -> dict[str, object]:
    return {
        "id": str(summary.id),
        "archive": map_archive(summary.archive),
        "collection_manifest": map_collection_manifest(summary.collection_manifest),
        "stored_bytes": summary.stored_bytes,
    }


def map_archive_restore(summary: ArchiveRestoreSummary) -> dict[str, object]:
    return {
        "id": summary.id,
        "type": summary.type,
        "state": summary.state.value,
        "created_at": summary.created_at,
        "requested_at": summary.requested_at,
        "ready_at": summary.ready_at,
        "expires_at": summary.expires_at,
        "completed_at": summary.completed_at,
        "canceled_at": summary.canceled_at,
        "paused_at": summary.paused_at,
        "paused_from_state": summary.paused_from_state,
        "paths": None if summary.paths is None else [str(path) for path in summary.paths],
        "latest_message": summary.latest_message,
        "warnings": list(summary.warnings),
        "notification": map_archive_restore_notification(summary.notification),
        "progress": map_archive_restore_progress(summary.progress),
        "collections": [
            map_archive_restore_collection(collection) for collection in summary.collections
        ],
        "images": [map_archive_restore_image(image) for image in summary.images],
    }


def map_archive_restore_list(summary: ArchiveRestoreListPage) -> dict[str, object]:
    return {
        "page": summary.page,
        "per_page": summary.per_page,
        "total": summary.total,
        "pages": summary.pages,
        "sort": summary.sort,
        "order": summary.order,
        "terminal": summary.terminal,
        "type": summary.type,
        "state": summary.state,
        "collection": summary.collection,
        "image": summary.image,
        "restores": [map_archive_restore(restore) for restore in summary.restores],
    }


def map_disc_history(entry: DiscHistoryEntry) -> dict[str, object]:
    return {
        "at": entry.at,
        "event": entry.event,
        "state": entry.state.value,
        "verification_state": entry.verification_state.value,
        "location": entry.location,
    }


def map_disc(summary: DiscSummary) -> dict[str, object]:
    return {
        "disc_id": str(summary.disc_id),
        "image_id": summary.image_id,
        "label_text": summary.label_text,
        "location": summary.location,
        "created_at": summary.created_at,
        "state": summary.state.value,
        "verification_state": summary.verification_state.value,
        "history": [map_disc_history(entry) for entry in summary.history],
    }


def map_collection_coverage_image(
    summary: CollectionCoverageImage,
    *,
    path_limit: int | None = None,
) -> dict[str, object]:
    covered_paths = list(summary.covered_paths)
    if path_limit is not None:
        covered_paths = covered_paths[:path_limit]
    covered_paths_total = (
        summary.covered_paths_total
        if summary.covered_paths_total is not None
        else len(summary.covered_paths)
    )
    return {
        "id": str(summary.id),
        "filename": summary.filename,
        "disc_redundancy_state": summary.disc_redundancy_state.value,
        "discs_required": summary.discs_required,
        "discs_registered": summary.discs_registered,
        "discs_verified": summary.discs_verified,
        "discs_missing": summary.discs_missing,
        "covered_paths": covered_paths,
        "covered_paths_total": covered_paths_total,
        "discs": [map_disc(disc) for disc in summary.discs],
    }


def map_fetch(summary: FetchSummary) -> dict[str, object]:
    return {
        "id": str(summary.id),
        "name": summary.name,
        "targets": [str(target) for target in summary.targets],
        "state": summary.state.value,
        "files": summary.files,
        "bytes": summary.bytes,
        "entries_total": summary.entries_total,
        "entries_pending": summary.entries_pending,
        "entries_partial": summary.entries_partial,
        "entries_byte_complete": summary.entries_byte_complete,
        "entries_uploaded": summary.entries_uploaded,
        "uploaded_bytes": summary.uploaded_bytes,
        "missing_bytes": summary.missing_bytes,
        "upload_state_expires_at": summary.upload_state_expires_at,
        "discs": [
            {
                "disc_id": str(disc.disc_id),
                "image_id": disc.image_id,
                "location": disc.location,
            }
            for disc in summary.discs
        ],
    }


def map_fetch_list(summary: FetchListPage) -> dict[str, object]:
    return {
        "page": summary.page,
        "per_page": summary.per_page,
        "total": summary.total,
        "pages": summary.pages,
        "fetches": [map_fetch(fetch) for fetch in summary.fetches],
    }
