from __future__ import annotations

from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from riverhog_core.ports.download_allowance import DownloadAttribution
from riverhog_core.ports.retrieval_cache import RetrievalCacheReceipt


@dataclass(frozen=True, slots=True)
class StorageExecutionEvidence:
    storage_adapter: str
    storage_profile_id: str
    storage_profile_contract_sha256: str
    egress_accounting_id: str
    read_mode: str
    adapter_implementation_id: str
    adapter_implementation_version: str
    adapter_source_revision: str
    adapter_runtime_descriptor_sha256: str


@dataclass(frozen=True, slots=True)
class ArchiveObjectUploadReceipt:
    object_id: str
    kind: str
    object_path: str
    plaintext_bytes: int
    stored_bytes: int
    sha256: str | None
    stored_sha256: str | None
    revision: str
    storage_adapter: str
    storage_profile_id: str
    storage_profile_contract_sha256: str
    egress_accounting_id: str
    adapter_implementation_id: str
    adapter_implementation_version: str
    adapter_source_revision: str
    adapter_runtime_descriptor_sha256: str
    read_mode: str
    uploaded_at: str
    verified_at: str | None = None
    retrieval_cache: RetrievalCacheReceipt | None = None


@dataclass(frozen=True, slots=True)
class CollectionArchiveUploadReceipt:
    objects: tuple[ArchiveObjectUploadReceipt, ...]

    def require_object(self, object_id: str) -> ArchiveObjectUploadReceipt:
        for current in self.objects:
            if current.object_id == object_id:
                return current
        raise KeyError(object_id)


@dataclass(frozen=True, slots=True)
class ArchiveArtifactRead:
    receipt: ArchiveObjectUploadReceipt
    content: bytes


@dataclass(frozen=True, slots=True)
class MutableManifestReceipt:
    object_path: str
    revision: str
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
    revision: str


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
    def storage_execution_evidence(self) -> StorageExecutionEvidence: ...
    def read_mode(self) -> str: ...
    def new_collection_archive_storage_prefix(self) -> str: ...

    def abort_incomplete_multipart_uploads(
        self,
        *,
        initiated_before: datetime,
    ) -> int: ...

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

    def publish_collection_metadata(
        self,
        *,
        collection_id: int,
        archive_storage_prefix: str,
        manifest: bytes,
        prior_revision: str | None = None,
    ) -> MutableManifestReceipt: ...

    def read_archive_artifact(
        self,
        *,
        collection_id: int,
        object: ArchiveObjectIdentity,
    ) -> ArchiveArtifactRead: ...

    def replace_archive_proof(
        self,
        *,
        collection_id: int,
        object: ArchiveObjectIdentity,
        proof_bytes: bytes,
    ) -> ArchiveObjectUploadReceipt: ...

    def stored_archive_object_sha256(
        self,
        *,
        collection_id: int,
        object: ArchiveObjectIdentity,
    ) -> str: ...

    def publish_archive_attestation(
        self,
        *,
        collection_id: int,
        archive_storage_prefix: str,
        checksums: bytes,
        signature: bytes,
        proof: bytes,
    ) -> CollectionArchiveUploadReceipt: ...

    def read_archive_attestation_artifact(
        self,
        *,
        collection_id: int,
        object: ArchiveObjectIdentity,
    ) -> ArchiveArtifactRead: ...

    def replace_archive_attestation_proof(
        self,
        *,
        collection_id: int,
        object: ArchiveObjectIdentity,
        proof_bytes: bytes,
    ) -> ArchiveObjectUploadReceipt: ...

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
