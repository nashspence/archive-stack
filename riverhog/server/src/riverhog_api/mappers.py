from __future__ import annotations

from riverhog_core.domain.models import (
    ArchiveCopyStatus,
    ArchiveUsageCollection,
    ArchiveUsageReport,
    ArchiveUsageSnapshot,
    ArchiveUsageTotals,
    CollectionListPage,
    CollectionManifestStatus,
    CollectionSummary,
)


def map_archive(summary: ArchiveCopyStatus) -> dict[str, object]:
    return {
        "store": summary.store,
        "state": summary.state.value,
        "storage_prefix": summary.storage_prefix,
        "object_count": summary.object_count,
        "stored_bytes": summary.stored_bytes,
        "backend": summary.backend,
        "storage_class": summary.storage_class,
        "last_uploaded_at": summary.last_uploaded_at,
        "last_verified_at": summary.last_verified_at,
        "failure": summary.failure,
        "collection_manifest": map_collection_manifest(summary.collection_manifest),
    }


def map_collection_manifest(
    summary: CollectionManifestStatus | None,
) -> dict[str, object] | None:
    if summary is None:
        return None
    return {
        "object_path": summary.object_path,
        "sha256": summary.sha256,
        "proof_object_path": summary.proof_object_path,
        "proof_state": summary.proof_state,
        "proof_sha256": summary.proof_sha256,
    }


def map_archive_usage_totals(summary: ArchiveUsageTotals) -> dict[str, object]:
    return {
        "collections": summary.collections,
        "uploaded_collections": summary.uploaded_collections,
        "measured_storage_bytes": summary.measured_storage_bytes,
    }


def map_archive_usage_collection(summary: ArchiveUsageCollection) -> dict[str, object]:
    return {
        "id": summary.id,
        "bytes": summary.bytes,
        "archive_copies": [map_archive(copy) for copy in summary.archive_copies],
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
        "download_allowances": [
            {
                "store": item.store,
                "state": item.state,
                "month_started_at": item.month_started_at,
                "resets_at": item.resets_at,
                "allowance_bytes": item.allowance_bytes,
                "safety_buffer_bytes": item.safety_buffer_bytes,
                "effective_limit_bytes": item.effective_limit_bytes,
                "accounted_bytes": item.accounted_bytes,
                "reserved_bytes": item.reserved_bytes,
                "remaining_bytes": item.remaining_bytes,
            }
            for item in summary.download_allowances
        ],
    }


def map_collection(summary: CollectionSummary) -> dict[str, object]:
    return {
        "id": summary.id,
        "tags": list(summary.tags),
        "files": summary.files,
        "bytes": summary.bytes,
        "archive_copies": [map_archive(copy) for copy in summary.archive_copies],
    }


def map_collection_list_page(summary: CollectionListPage) -> dict[str, object]:
    return {
        "page": summary.page,
        "per_page": summary.per_page,
        "total": summary.total,
        "pages": summary.pages,
        "collections": [map_collection(collection) for collection in summary.collections],
    }
