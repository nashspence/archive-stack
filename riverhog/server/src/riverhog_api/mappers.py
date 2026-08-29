from __future__ import annotations

from riverhog_core.domain.models import (
    ArchiveCopyStatus,
    ArchiveDownloadAllowance,
    ArchiveRootPublicationStatus,
    ArchiveStoreListPage,
    ArchiveStoreSummary,
    CollectionListPage,
    CollectionSummary,
)


def map_archive(summary: ArchiveCopyStatus) -> dict[str, object]:
    return {
        "store": summary.store,
        "state": summary.state.value,
        "storage_prefix": summary.storage_prefix,
        "object_count": summary.object_count,
        "stored_bytes": summary.stored_bytes,
        "last_uploaded_at": summary.last_uploaded_at,
        "last_verified_at": summary.last_verified_at,
        "failure": summary.failure,
        "archive_root": map_archive_root_publication(summary.archive_root),
    }


def map_archive_root_publication(
    summary: ArchiveRootPublicationStatus | None,
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


def map_archive_download_allowance(
    summary: ArchiveDownloadAllowance | None,
) -> dict[str, object] | None:
    if summary is None:
        return None
    return {
        "store": summary.store,
        "state": summary.state,
        "month_started_at": summary.month_started_at,
        "resets_at": summary.resets_at,
        "allowance_bytes": summary.allowance_bytes,
        "safety_buffer_bytes": summary.safety_buffer_bytes,
        "effective_limit_bytes": summary.effective_limit_bytes,
        "accounted_bytes": summary.accounted_bytes,
        "reserved_bytes": summary.reserved_bytes,
        "remaining_bytes": summary.remaining_bytes,
    }


def map_archive_store(summary: ArchiveStoreSummary) -> dict[str, object]:
    return {
        "store": summary.store,
        "read_mode": summary.read_mode,
        "read_priority": summary.read_priority,
        "write_target": summary.write_target,
        "collections": summary.collections,
        "objects": summary.objects,
        "stored_bytes": summary.stored_bytes,
        "download_allowance": map_archive_download_allowance(summary.download_allowance),
    }


def map_archive_store_list(summary: ArchiveStoreListPage) -> dict[str, object]:
    return {
        "page": summary.page,
        "per_page": summary.per_page,
        "total": summary.total,
        "pages": summary.pages,
        "sort": summary.sort,
        "order": summary.order,
        "query": summary.query,
        "stores": [map_archive_store(item) for item in summary.stores],
    }


def map_collection(summary: CollectionSummary) -> dict[str, object]:
    return {
        "id": summary.id,
        "created_at": summary.created_at,
        "tag_count": summary.tag_count,
        "content_identity": summary.content_identity,
        "archive_root_sha256": summary.archive_root_sha256,
        "encryption_format": summary.encryption_format,
        "passphrase_id": summary.passphrase_id,
        "files": summary.files,
        "bytes": summary.bytes,
        "remote_storage_bytes": summary.remote_storage_bytes,
        "archive_copy_count": summary.archive_copy_count,
    }


def map_collection_list_page(summary: CollectionListPage) -> dict[str, object]:
    return {
        "page": summary.page,
        "per_page": summary.per_page,
        "total": summary.total,
        "pages": summary.pages,
        "sort": summary.sort,
        "order": summary.order,
        "query": summary.query,
        "tag": summary.tag,
        "encryption_format": summary.encryption_format,
        "passphrase_id": summary.passphrase_id,
        "collections": [map_collection(collection) for collection in summary.collections],
    }
