from __future__ import annotations

from riverhog_core.domain.models import (
    ArchiveRestoreCollection,
    ArchiveRestoreListPage,
    ArchiveRestoreNotificationStatus,
    ArchiveRestoreProgress,
    ArchiveRestoreSummary,
    ArchiveStatus,
    ArchiveUsageCollection,
    ArchiveUsageReport,
    ArchiveUsageSnapshot,
    ArchiveUsageTotals,
    CollectionListPage,
    CollectionManifestStatus,
    CollectionSummary,
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
        "ots_sha256": summary.ots_sha256,
    }


def map_archive_usage_totals(summary: ArchiveUsageTotals) -> dict[str, object]:
    return {
        "collections": summary.collections,
        "uploaded_collections": summary.uploaded_collections,
        "measured_storage_bytes": summary.measured_storage_bytes,
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
        "collections": [map_archive_usage_collection(item) for item in summary.collections],
        "history": [map_archive_usage_snapshot(item) for item in summary.history],
    }


def map_collection(summary: CollectionSummary) -> dict[str, object]:
    return {
        "id": str(summary.id),
        "files": summary.files,
        "bytes": summary.bytes,
        "hot_bytes": summary.hot_bytes,
        "archive": map_archive(summary.archive),
        "collection_manifest": map_collection_manifest(summary.collection_manifest),
        "archive_format": summary.archive_format,
        "compression": summary.compression,
    }


def map_collection_list_page(summary: CollectionListPage) -> dict[str, object]:
    return {
        "page": summary.page,
        "per_page": summary.per_page,
        "total": summary.total,
        "pages": summary.pages,
        "collections": [map_collection(collection) for collection in summary.collections],
    }


def map_archive_restore_notification(
    summary: ArchiveRestoreNotificationStatus,
) -> dict[str, object]:
    return {
        "webhook_configured": summary.webhook_configured,
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
        "state": summary.state.value,
        "created_at": summary.created_at,
        "requested_at": summary.requested_at,
        "ready_at": summary.ready_at,
        "expires_at": summary.expires_at,
        "completed_at": summary.completed_at,
        "canceled_at": summary.canceled_at,
        "latest_message": summary.latest_message,
        "warnings": list(summary.warnings),
        "notification": map_archive_restore_notification(summary.notification),
        "progress": map_archive_restore_progress(summary.progress),
        "collections": [
            map_archive_restore_collection(collection) for collection in summary.collections
        ],
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
        "state": summary.state,
        "collection": summary.collection,
        "restores": [map_archive_restore(restore) for restore in summary.restores],
    }


def map_fetch(summary: FetchSummary) -> dict[str, object]:
    return {
        "id": str(summary.id),
        "name": summary.name,
        "collections": [str(collection) for collection in summary.collections],
        "state": summary.state.value,
        "files": summary.files,
        "bytes": summary.bytes,
        "hot_files": summary.hot_files,
        "hot_bytes": summary.hot_bytes,
        "missing_files": summary.missing_files,
        "missing_bytes": summary.missing_bytes,
    }


def map_fetch_list(summary: FetchListPage) -> dict[str, object]:
    return {
        "page": summary.page,
        "per_page": summary.per_page,
        "total": summary.total,
        "pages": summary.pages,
        "fetches": [map_fetch(fetch) for fetch in summary.fetches],
    }
