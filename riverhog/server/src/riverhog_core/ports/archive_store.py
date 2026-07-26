from __future__ import annotations

from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from riverhog_core.archive_objects import CollectionArchive
from riverhog_core.ports.download_allowance import DownloadAttribution
from riverhog_core.ports.retrieval_cache import RetrievalCacheReceipt


@dataclass(frozen=True, slots=True)
class ArchiveObjectUploadReceipt:
    object_id: str
    kind: str
    object_path: str
    plaintext_bytes: int
    stored_bytes: int
    sha256: str
    backend: str
    storage_class: str
    uploaded_at: str
    verified_at: str | None = None
    ingestion_cache: RetrievalCacheReceipt | None = None


@dataclass(frozen=True, slots=True)
class CollectionArchiveUploadReceipt:
    objects: tuple[ArchiveObjectUploadReceipt, ...]

    def require_object(self, object_id: str) -> ArchiveObjectUploadReceipt:
        for current in self.objects:
            if current.object_id == object_id:
                return current
        raise KeyError(object_id)


@dataclass(frozen=True, slots=True)
class MutableManifestReceipt:
    object_path: str
    version_id: str | None
    stored_bytes: int
    stored_sha256: str
    published_at: str


@dataclass(frozen=True, slots=True)
class ArchiveObjectIdentity:
    object_id: str
    kind: str
    object_path: str
    plaintext_bytes: int
    stored_bytes: int
    sha256: str


@dataclass(frozen=True, slots=True)
class CollectionArchiveIdentity:
    objects: tuple[ArchiveObjectIdentity, ...]

    def require_object(self, object_id: str) -> ArchiveObjectIdentity:
        for current in self.objects:
            if current.object_id == object_id:
                return current
        raise KeyError(object_id)

    @property
    def data_objects(self) -> tuple[ArchiveObjectIdentity, ...]:
        return tuple(
            current for current in self.objects if current.kind in {"pack", "file", "segment"}
        )


class ArchiveVerificationError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class ArchiveMultipartUploadedPart:
    part_number: int
    etag: str
    size: int


@dataclass(frozen=True, slots=True)
class ArchiveMultipartUploadState:
    object_id: str
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
        collection_id: int,
        object_id: str,
        object_path: str,
        part_size: int,
        content_length: int,
        sha256: str,
    ) -> ArchiveMultipartUploadState | None: ...

    def save_multipart_upload(
        self,
        *,
        collection_id: int,
        state: ArchiveMultipartUploadState,
    ) -> None: ...

    def record_multipart_upload_progress(
        self,
        *,
        collection_id: int,
        state: ArchiveMultipartUploadState,
        part: ArchiveMultipartUploadedPart,
        uploaded_bytes: int,
        uploaded_parts: int,
        total_parts: int,
    ) -> None: ...

    def clear_multipart_upload(
        self,
        *,
        collection_id: int,
        object_id: str,
        upload_id: str,
    ) -> None: ...

    def load_ingestion_cache(
        self,
        *,
        collection_id: int,
        object_id: str,
    ) -> RetrievalCacheReceipt | None: ...

    def save_ingestion_cache(
        self,
        *,
        collection_id: int,
        object_id: str,
        receipt: RetrievalCacheReceipt,
    ) -> None: ...


@dataclass(frozen=True, slots=True)
class ArchiveReadStatus:
    state: str
    ready_at: str | None = None
    expires_at: str | None = None
    message: str | None = None


class ArchiveStore(Protocol):
    def read_mode(self) -> str: ...
    def new_collection_archive_storage_prefix(self) -> str: ...

    def max_plaintext_object_bytes(self) -> int: ...

    def abort_incomplete_multipart_uploads(
        self,
        *,
        initiated_before: datetime,
    ) -> int: ...

    def upload_collection_archive(
        self,
        *,
        collection_id: int,
        archive: CollectionArchive,
        archive_storage_prefix: str | None = None,
        multipart_tracker: ArchiveMultipartUploadTracker | None = None,
    ) -> CollectionArchiveUploadReceipt: ...

    def verify_collection_archive(
        self,
        *,
        collection_id: int,
        archive: CollectionArchiveIdentity,
    ) -> None: ...

    def delete_collection_archive(
        self,
        *,
        collection_id: int,
        objects: Sequence[ArchiveObjectIdentity],
    ) -> None: ...

    def publish_collection_metadata(
        self,
        *,
        collection_id: int,
        archive_storage_prefix: str,
        manifest: bytes,
    ) -> MutableManifestReceipt: ...

    def prepare_archive_objects_read(
        self,
        *,
        collection_id: int,
        objects: Sequence[ArchiveObjectIdentity],
        retrieval_tier: str,
        hold_days: int,
        requested_at: str,
        estimated_ready_at: str,
    ) -> ArchiveReadStatus: ...

    def get_archive_objects_read_status(
        self,
        *,
        collection_id: int,
        objects: Sequence[ArchiveObjectIdentity],
        requested_at: str,
        estimated_ready_at: str | None,
        estimated_expires_at: str | None,
    ) -> ArchiveReadStatus: ...

    def iter_archive_object(
        self,
        *,
        collection_id: int,
        object: ArchiveObjectIdentity,
        attribution: DownloadAttribution | None = None,
    ) -> Iterator[bytes]: ...

    def iter_stored_archive_object(
        self,
        *,
        collection_id: int,
        object: ArchiveObjectIdentity,
        attribution: DownloadAttribution | None = None,
    ) -> Iterator[bytes]: ...

    def cleanup_archive_objects_read(
        self,
        *,
        collection_id: int,
        objects: Sequence[ArchiveObjectIdentity],
    ) -> None: ...
