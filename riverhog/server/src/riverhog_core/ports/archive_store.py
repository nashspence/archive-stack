from __future__ import annotations

from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from typing import Protocol

from riverhog_core.ports.download_allowance import DownloadAttribution
from riverhog_core.ports.retrieval_cache import RetrievalCacheReceipt


@dataclass(frozen=True, slots=True)
class ArchiveObjectUploadReceipt:
    object_id: str
    kind: str
    object_path: str
    plaintext_bytes: int
    stored_bytes: int
    sha256: str | None
    stored_sha256: str | None
    revision: str | None
    uploaded_at: str
    verified_at: str | None = None
    retrieval_cache: RetrievalCacheReceipt | None = None


@dataclass(frozen=True, slots=True)
class ArchiveArtifactRead:
    receipt: ArchiveObjectUploadReceipt
    content: bytes


@dataclass(frozen=True, slots=True)
class CollectionDescriptionReceipt:
    object_path: str
    revision: str | None
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
    sha256: str | None
    stored_sha256: str | None
    revision: str | None


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
        return tuple(current for current in self.objects if current.kind in {"pack", "segment"})


class ArchiveVerificationError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class ArchiveReadStatus:
    state: str
    ready_at: str | None = None
    expires_at: str | None = None
    message: str | None = None


class ArchiveStore(Protocol):
    def read_mode(self) -> str: ...
    def new_collection_archive_storage_prefix(self) -> str: ...

    def discard_collection_archive_upload(self, *, archive_storage_prefix: str) -> None: ...

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

    def publish_collection_description(
        self,
        *,
        collection_id: int,
        archive_storage_prefix: str,
        document: bytes,
        passphrase_id: str,
    ) -> CollectionDescriptionReceipt: ...

    def delete_collection_description(
        self,
        *,
        collection_id: int,
        archive_storage_prefix: str,
    ) -> None: ...

    def read_archive_artifact(
        self,
        *,
        collection_id: int,
        object: ArchiveObjectIdentity,
        passphrase_id: str,
    ) -> ArchiveArtifactRead: ...

    def prepare_archive_objects_read(
        self,
        *,
        collection_id: int,
        objects: Sequence[ArchiveObjectIdentity],
    ) -> ArchiveReadStatus: ...

    def get_archive_objects_read_status(
        self,
        *,
        collection_id: int,
        objects: Sequence[ArchiveObjectIdentity],
    ) -> ArchiveReadStatus: ...

    def iter_archive_object(
        self,
        *,
        collection_id: int,
        object: ArchiveObjectIdentity,
        passphrase_id: str,
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
