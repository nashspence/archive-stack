"""Closed Riverhog list-control vocabularies shared by servers and clients."""

from __future__ import annotations

from typing import Literal

type SortOrder = Literal["asc", "desc"]
type CollectionSort = Literal["id", "created_at", "bytes", "files"]
type CollectionUploadState = Literal[
    "open", "uploading", "finalizing", "failed", "orphaned", "discarding"
]
type CollectionUploadSort = Literal["id", "created_at", "state", "bytes", "files"]
type RetrievalCacheState = Literal["ready", "delete_pending", "deleting"]
type RetrievalCacheProtection = Literal["protected", "unleased"]
type RetrievalCacheSort = Literal[
    "collection_id",
    "source_store",
    "object_id",
    "stored_bytes",
    "cached_at",
    "verified_at",
    "protected_until",
]
type SearchSort = Literal["file_ref", "collection_id", "path", "bytes"]
type ProvenanceStatus = Literal["captured", "omitted"]
type ProvenanceSort = Literal["path", "bytes", "status"]
type ArchiveStoreSort = Literal[
    "store", "read_mode", "read_priority", "collections", "objects", "stored_bytes"
]
type ApplicationSort = Literal["name", "keys", "active_keys", "last_used_at"]
type ApplicationKeySort = Literal["id", "created_at", "expires_at", "last_used_at"]
type ApplicationAccessSort = Literal["app", "key_id", "permission", "resource", "created_at"]
type TagSort = Literal["id", "created_at", "collections"]
type DownloadQuotaSort = Literal[
    "app",
    "key_id",
    "monthly_bytes",
    "accounted_bytes",
    "reserved_bytes",
    "remaining_bytes",
]
type ArchiveCopyState = Literal[
    "requested",
    "waiting",
    "checking",
    "copying",
    "canceling",
    "completed",
    "failed",
    "canceled",
]
type ArchiveCopySort = Literal[
    "collection_id", "source_store", "destination_store", "state", "requested_at"
]

__all__ = [
    "ApplicationAccessSort",
    "ApplicationKeySort",
    "ApplicationSort",
    "ArchiveCopySort",
    "ArchiveCopyState",
    "ArchiveStoreSort",
    "CollectionSort",
    "CollectionUploadSort",
    "CollectionUploadState",
    "DownloadQuotaSort",
    "ProvenanceSort",
    "ProvenanceStatus",
    "RetrievalCacheProtection",
    "RetrievalCacheSort",
    "RetrievalCacheState",
    "SearchSort",
    "SortOrder",
    "TagSort",
]
