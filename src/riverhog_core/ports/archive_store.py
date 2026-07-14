from __future__ import annotations

from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from typing import Protocol

from riverhog_core.collection_archives import CollectionArchivePackage


@dataclass(frozen=True)
class ArchiveUploadReceipt:
    object_path: str
    stored_bytes: int
    backend: str
    storage_class: str
    uploaded_at: str
    verified_at: str | None = None


@dataclass(frozen=True)
class CollectionArchiveUploadReceipt:
    archive: ArchiveUploadReceipt
    manifest: ArchiveUploadReceipt
    proof: ArchiveUploadReceipt
    archive_sha256: str
    manifest_sha256: str
    proof_sha256: str
    archive_format: str
    compression: str


@dataclass(frozen=True)
class ArchiveMultipartUploadedPart:
    part_number: int
    etag: str
    size: int


@dataclass(frozen=True)
class ArchiveMultipartUploadState:
    upload_id: str
    object_path: str
    part_size: int
    content_length: int
    sha256: str
    total_parts: int | None = None
    encryption_state_json: str | None = None
    parts: tuple[ArchiveMultipartUploadedPart, ...] = ()


class ArchiveMultipartUploadTracker(Protocol):
    def load_multipart_upload(
        self,
        *,
        collection_id: str,
        object_path: str,
        part_size: int,
        content_length: int,
        sha256: str,
    ) -> ArchiveMultipartUploadState | None: ...

    def save_multipart_upload(
        self,
        *,
        collection_id: str,
        state: ArchiveMultipartUploadState,
    ) -> None: ...

    def record_multipart_upload_progress(
        self,
        *,
        collection_id: str,
        state: ArchiveMultipartUploadState,
        part: ArchiveMultipartUploadedPart,
        uploaded_bytes: int,
        uploaded_parts: int,
        total_parts: int,
    ) -> None: ...

    def clear_multipart_upload(
        self,
        *,
        collection_id: str,
        upload_id: str,
    ) -> None: ...


@dataclass(frozen=True)
class ArchiveRestoreStatus:
    state: str
    ready_at: str | None = None
    expires_at: str | None = None
    message: str | None = None


class ArchiveStore(Protocol):
    def upload_collection_archive_package(
        self,
        *,
        collection_id: str,
        package: CollectionArchivePackage,
        archive_storage_prefix: str | None = None,
        multipart_tracker: ArchiveMultipartUploadTracker | None = None,
    ) -> CollectionArchiveUploadReceipt: ...

    def delete_collection_archive_package(
        self,
        *,
        collection_id: str,
        object_path: str,
        manifest_object_path: str,
        proof_object_path: str,
    ) -> None: ...

    def publish_restore_catalog(
        self,
        *,
        entries: Sequence[dict[str, object]],
        generated_at: str,
    ) -> None: ...

    def request_collection_archive_restore(
        self,
        *,
        collection_id: str,
        object_path: str,
        retrieval_tier: str,
        hold_days: int,
        requested_at: str,
        estimated_ready_at: str,
        manifest_object_path: str | None = None,
        proof_object_path: str | None = None,
    ) -> ArchiveRestoreStatus: ...

    def get_collection_archive_restore_status(
        self,
        *,
        collection_id: str,
        object_path: str,
        requested_at: str,
        estimated_ready_at: str | None,
        estimated_expires_at: str | None,
        manifest_object_path: str | None = None,
        proof_object_path: str | None = None,
    ) -> ArchiveRestoreStatus: ...

    def iter_restored_collection_archive(
        self,
        *,
        collection_id: str,
        object_path: str,
    ) -> Iterator[bytes]: ...

    def read_restored_collection_manifest(
        self,
        *,
        collection_id: str,
        object_path: str,
    ) -> bytes: ...

    def read_restored_collection_manifest_proof(
        self,
        *,
        collection_id: str,
        object_path: str,
    ) -> bytes: ...

    def cleanup_collection_archive_restore(
        self,
        *,
        collection_id: str,
        object_path: str,
        manifest_object_path: str | None = None,
        proof_object_path: str | None = None,
    ) -> None: ...
