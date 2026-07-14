from __future__ import annotations

from riverhog_core.domain.models import (
    CollectionCoverageImage,
    CollectionListPage,
    CollectionManifestStatus,
    CollectionRecoverySummary,
    CollectionSummary,
    CopyHistoryEntry,
    CopySummary,
    FetchListPage,
    FetchSummary,
    GlacierArchiveStatus,
    GlacierCollectionContribution,
    GlacierUsageCollection,
    GlacierUsageImage,
    GlacierUsageReport,
    GlacierUsageSnapshot,
    GlacierUsageTotals,
    RecoveryCoverage,
    RecoveryNotificationStatus,
    RecoverySessionCollection,
    RecoverySessionImage,
    RecoverySessionListPage,
    RecoverySessionProgress,
    RecoverySessionSummary,
)


def map_glacier(summary: GlacierArchiveStatus) -> dict[str, object]:
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


def map_glacier_usage_totals(summary: GlacierUsageTotals) -> dict[str, object]:
    return {
        "collections": summary.collections,
        "uploaded_collections": summary.uploaded_collections,
        "measured_storage_bytes": summary.measured_storage_bytes,
    }


def map_glacier_usage_image(summary: GlacierUsageImage) -> dict[str, object]:
    return {
        "id": str(summary.id),
        "filename": summary.filename,
        "collection_ids": list(summary.collection_ids),
    }


def map_glacier_collection_contribution(
    summary: GlacierCollectionContribution,
) -> dict[str, object]:
    return {
        "image_id": str(summary.image_id),
        "filename": summary.filename,
        "represented_bytes": summary.represented_bytes,
    }


def map_glacier_usage_collection(summary: GlacierUsageCollection) -> dict[str, object]:
    return {
        "id": str(summary.id),
        "bytes": summary.bytes,
        "glacier": map_glacier(summary.glacier),
        "collection_manifest": map_collection_manifest(summary.collection_manifest),
        "archive_format": summary.archive_format,
        "compression": summary.compression,
        "measured_storage_bytes": summary.measured_storage_bytes,
        "images": [map_glacier_collection_contribution(image) for image in summary.images],
    }


def map_glacier_usage_snapshot(summary: GlacierUsageSnapshot) -> dict[str, object]:
    return {
        "captured_at": summary.captured_at,
        "uploaded_collections": summary.uploaded_collections,
        "measured_storage_bytes": summary.measured_storage_bytes,
    }


def map_glacier_usage_report(summary: GlacierUsageReport) -> dict[str, object]:
    return {
        "scope": summary.scope,
        "measured_at": summary.measured_at,
        "totals": map_glacier_usage_totals(summary.totals),
        "images": [map_glacier_usage_image(image) for image in summary.images],
        "collections": [map_glacier_usage_collection(item) for item in summary.collections],
        "history": [map_glacier_usage_snapshot(item) for item in summary.history],
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
        "archived_bytes": summary.archived_bytes,
        "pending_bytes": summary.pending_bytes,
        "glacier": map_glacier(summary.glacier),
        "collection_manifest": map_collection_manifest(summary.collection_manifest),
        "archive_format": summary.archive_format,
        "compression": summary.compression,
        "disc_coverage": map_collection_disc_coverage(summary.recovery.verified_physical),
        "protection_state": map_collection_protection_state(summary),
        "protected_bytes": summary.protected_bytes,
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
        "archived_bytes": summary.archived_bytes,
        "pending_bytes": summary.pending_bytes,
        "glacier": map_glacier(summary.glacier),
        "collection_manifest": map_collection_manifest(summary.collection_manifest),
        "archive_format": summary.archive_format,
        "compression": summary.compression,
        "disc_coverage": map_collection_disc_coverage(summary.recovery.verified_physical),
        "protection_state": map_collection_protection_state(summary),
        "protected_bytes": summary.protected_bytes,
    }


def map_collection_list_page(summary: CollectionListPage) -> dict[str, object]:
    return {
        "page": summary.page,
        "per_page": summary.per_page,
        "total": summary.total,
        "pages": summary.pages,
        "collections": [map_collection_list_item(collection) for collection in summary.collections],
    }


def map_recovery_coverage(summary: RecoveryCoverage) -> dict[str, object]:
    return {
        "state": summary.state.value,
        "bytes": summary.bytes,
    }


def map_collection_recovery(summary: CollectionRecoverySummary) -> dict[str, object]:
    return {
        "verified_physical": map_recovery_coverage(summary.verified_physical),
        "glacier": map_recovery_coverage(summary.glacier),
        "available": list(summary.available),
    }


def map_collection_disc_coverage(summary: RecoveryCoverage) -> dict[str, object]:
    return {
        "state": summary.state.value,
        "covered_bytes": summary.bytes,
        "verified_physical_bytes": summary.bytes,
    }


def map_collection_protection_state(summary: CollectionSummary) -> str:
    state = summary.protection_state.value
    if state == "protected":
        return "fully_protected"
    if state == "partially_protected":
        return "under_protected"
    return "cloud_only"


def map_recovery_notification(summary: RecoveryNotificationStatus) -> dict[str, object]:
    return {
        "webhook_configured": summary.webhook_configured,
        "reminder_count": summary.reminder_count,
        "next_reminder_at": summary.next_reminder_at,
        "last_notified_at": summary.last_notified_at,
        "failure_count": summary.failure_count,
        "last_failure_at": summary.last_failure_at,
        "last_failure": summary.last_failure,
    }


def map_recovery_session_progress(summary: RecoverySessionProgress) -> dict[str, object]:
    return {
        "archive_verification": summary.archive_verification,
        "extraction": summary.extraction,
        "materialization": summary.materialization,
    }


def map_recovery_session_image(summary: RecoverySessionImage) -> dict[str, object]:
    return {
        "id": str(summary.id),
        "filename": summary.filename,
        "collection_ids": [str(collection_id) for collection_id in summary.collection_ids],
        "rebuild_state": summary.rebuild_state,
    }


def map_recovery_session_collection(summary: RecoverySessionCollection) -> dict[str, object]:
    return {
        "id": str(summary.id),
        "glacier": map_glacier(summary.glacier),
        "collection_manifest": map_collection_manifest(summary.collection_manifest),
        "stored_bytes": summary.stored_bytes,
    }


def map_recovery_session(summary: RecoverySessionSummary) -> dict[str, object]:
    return {
        "id": summary.id,
        "type": summary.type,
        "state": summary.state.value,
        "created_at": summary.created_at,
        "restore_requested_at": summary.restore_requested_at,
        "restore_ready_at": summary.restore_ready_at,
        "restore_expires_at": summary.restore_expires_at,
        "completed_at": summary.completed_at,
        "canceled_at": summary.canceled_at,
        "paused_at": summary.paused_at,
        "paused_from_state": summary.paused_from_state,
        "restore_paths": None
        if summary.restore_paths is None
        else [str(path) for path in summary.restore_paths],
        "latest_message": summary.latest_message,
        "warnings": list(summary.warnings),
        "notification": map_recovery_notification(summary.notification),
        "progress": map_recovery_session_progress(summary.progress),
        "collections": [
            map_recovery_session_collection(collection) for collection in summary.collections
        ],
        "images": [map_recovery_session_image(image) for image in summary.images],
    }


def map_recovery_session_list(summary: RecoverySessionListPage) -> dict[str, object]:
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
        "sessions": [map_recovery_session(session) for session in summary.sessions],
    }


def map_copy_history(entry: CopyHistoryEntry) -> dict[str, object]:
    return {
        "at": entry.at,
        "event": entry.event,
        "state": entry.state.value,
        "verification_state": entry.verification_state.value,
        "location": entry.location,
    }


def map_copy(summary: CopySummary) -> dict[str, object]:
    return {
        "id": str(summary.id),
        "volume_id": summary.volume_id,
        "label_text": summary.label_text,
        "location": summary.location,
        "created_at": summary.created_at,
        "state": summary.state.value,
        "verification_state": summary.verification_state.value,
        "history": [map_copy_history(entry) for entry in summary.history],
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
        "physical_protection_state": summary.protection_state.value,
        "physical_copies_required": summary.physical_copies_required,
        "physical_copies_registered": summary.physical_copies_registered,
        "physical_copies_verified": summary.physical_copies_verified,
        "physical_copies_missing": summary.physical_copies_missing,
        "covered_paths": covered_paths,
        "covered_paths_total": covered_paths_total,
        "copies": [map_copy(copy) for copy in summary.copies],
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
        "copies": [
            {"id": str(c.id), "volume_id": c.volume_id, "location": c.location}
            for c in summary.copies
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
