from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator
from threading import RLock

from riverhog_storage_adapter_protocol import (
    CompleteUploadRequest,
    ObjectLocator,
    StorageAdapterDescriptor,
    UploadDeclaration,
    UploadDeclarationPayload,
    UploadPartReceipt,
    WriteCondition,
)
from riverhog_storage_adapter_support import (
    StorageAdapterClient,
    StorageAdapterProtocolError,
)

from riverhog_core.ports.archive_objects import (
    ArchiveObjectIdentityConflict,
    CompletedObjectReceipt,
    ImmutableObjectReceipt,
    MultipartPartReceipt,
    MultipartUpload,
)
from riverhog_core.runtime_config import StorageAdapterRegistration


class StorageAdapterRuntime:
    """Lazy registration runtime so unrelated Riverhog APIs survive target outages."""

    def __init__(
        self,
        registration: StorageAdapterRegistration,
        *,
        max_connections: int = 32,
        client: StorageAdapterClient | None = None,
    ) -> None:
        self.registration = registration
        self.max_connections = max_connections
        self._client = client
        self._descriptor: StorageAdapterDescriptor | None = None
        self._lock = RLock()

    @classmethod
    def connect(
        cls,
        registration: StorageAdapterRegistration,
        *,
        max_connections: int = 32,
        client: StorageAdapterClient | None = None,
    ) -> StorageAdapterRuntime:
        return cls(
            registration,
            max_connections=max_connections,
            client=client,
        )

    @property
    def client(self) -> StorageAdapterClient:
        with self._lock:
            if self._client is None:
                self._client = StorageAdapterClient.from_token_file(
                    self.registration.endpoint_url,
                    token_file=self.registration.token_file,
                    allow_insecure_http=self.registration.allow_insecure_http,
                    max_connections=self.max_connections,
                )
            return self._client

    @property
    def descriptor(self) -> StorageAdapterDescriptor:
        with self._lock:
            if self._descriptor is None:
                descriptor = self.client.descriptor()
                _validate_descriptor(self.registration, descriptor)
                self._descriptor = descriptor
            return self._descriptor

    def refresh_descriptor(self) -> StorageAdapterDescriptor:
        descriptor = self.client.descriptor()
        _validate_descriptor(self.registration, descriptor)
        with self._lock:
            self._descriptor = descriptor
        return descriptor


class StorageAdapterObjectStore:
    """Riverhog capability ports over one exact storage-adapter registration."""

    def __init__(self, runtime: StorageAdapterRuntime) -> None:
        self.runtime = runtime

    def create_multipart_upload(
        self,
        *,
        object_path: str,
        content_type: str,
        metadata: dict[str, str],
        expected_bytes: int,
    ) -> MultipartUpload:
        transfer_id = _transfer_id(object_path=object_path, identity=metadata)
        declaration = UploadDeclaration.seal(
            UploadDeclarationPayload(
                transfer_id=transfer_id,
                object_path=object_path,
                content_type=content_type,
                stored_bytes=expected_bytes,
                runtime_descriptor_sha256=(self.runtime.descriptor.runtime_descriptor_sha256),
                condition=WriteCondition(mode="create_only"),
            )
        )
        status = self.runtime.client.put_upload(declaration)
        if status.declaration != declaration:
            raise ArchiveObjectIdentityConflict(
                "storage adapter returned a different upload declaration"
            )
        return MultipartUpload(object_path=object_path, transfer_id=transfer_id)

    def upload_part(
        self,
        *,
        upload: MultipartUpload,
        number: int,
        content: bytes,
    ) -> MultipartPartReceipt:
        receipt = self.runtime.client.put_part(
            transfer_id=upload.transfer_id,
            number=number,
            content=content,
        )
        return _part_receipt(receipt)

    def list_parts(self, *, upload: MultipartUpload) -> tuple[MultipartPartReceipt, ...]:
        status = self.runtime.client.get_upload(upload.transfer_id)
        _require_upload_path(upload, status.declaration.object_path)
        return tuple(_part_receipt(current) for current in status.parts)

    def complete_multipart_upload(
        self,
        *,
        upload: MultipartUpload,
        parts: tuple[MultipartPartReceipt, ...],
        expected_bytes: int,
        expected_metadata: dict[str, str],
    ) -> CompletedObjectReceipt:
        expected_transfer_id = _transfer_id(
            object_path=upload.object_path,
            identity=expected_metadata,
        )
        if upload.transfer_id != expected_transfer_id:
            raise ArchiveObjectIdentityConflict(
                "multipart upload identity differs from its completion"
            )
        expected_sha256 = expected_metadata.get("riverhog-stored-sha256")
        receipt = self.runtime.client.complete_upload(
            transfer_id=upload.transfer_id,
            completion=CompleteUploadRequest(
                parts=tuple(_wire_part(current) for current in parts),
                stored_bytes=expected_bytes,
                stored_sha256=expected_sha256,
            ),
        )
        return _completed_receipt(receipt)

    def head_completed_object(
        self,
        *,
        object_path: str,
        expected_metadata: dict[str, str],
    ) -> CompletedObjectReceipt | None:
        transfer_id = _transfer_id(object_path=object_path, identity=expected_metadata)
        try:
            status = self.runtime.client.get_upload(transfer_id)
        except StorageAdapterProtocolError as exc:
            if exc.code == "not_found" or exc.status == 404:
                return None
            raise
        if status.declaration.object_path != object_path:
            raise ArchiveObjectIdentityConflict(
                "storage adapter upload path differs from its Riverhog identity"
            )
        return _completed_receipt(status.object) if status.object is not None else None

    def abort_multipart_upload(self, *, upload: MultipartUpload) -> None:
        self.runtime.client.delete_upload(upload.transfer_id)

    def put_immutable_object(
        self,
        *,
        object_path: str,
        content: bytes,
        content_type: str,
        identity_metadata: dict[str, str],
    ) -> ImmutableObjectReceipt:
        return self.put_object(
            object_path=object_path,
            content=content,
            content_type=content_type,
            identity_metadata=identity_metadata,
            prior_revision=None,
        )

    def put_object(
        self,
        *,
        object_path: str,
        content: bytes,
        content_type: str,
        identity_metadata: dict[str, str],
        prior_revision: str | None,
    ) -> ImmutableObjectReceipt:
        identity = dict(identity_metadata)
        identity["riverhog-stored-sha256"] = hashlib.sha256(content).hexdigest()
        identity["riverhog-stored-bytes"] = str(len(content))
        if prior_revision is not None:
            identity["riverhog-prior-revision-sha256"] = hashlib.sha256(
                prior_revision.encode("utf-8")
            ).hexdigest()
        transfer_id = _transfer_id(object_path=object_path, identity=identity)
        declaration = UploadDeclaration.seal(
            UploadDeclarationPayload(
                transfer_id=transfer_id,
                object_path=object_path,
                content_type=content_type,
                stored_bytes=len(content),
                runtime_descriptor_sha256=(self.runtime.descriptor.runtime_descriptor_sha256),
                condition=(
                    WriteCondition(mode="create_only")
                    if prior_revision is None
                    else WriteCondition(
                        mode="replace_exact",
                        prior_revision=prior_revision,
                    )
                ),
            )
        )
        status = self.runtime.client.put_upload(declaration)
        if status.object is not None:
            return _immutable_receipt(status.object)
        part_size = self.runtime.descriptor.maximum_part_bytes
        parts: list[UploadPartReceipt] = []
        for offset in range(0, len(content), part_size):
            parts.append(
                self.runtime.client.put_part(
                    transfer_id=transfer_id,
                    number=len(parts) + 1,
                    content=content[offset : offset + part_size],
                )
            )
        receipt = self.runtime.client.complete_upload(
            transfer_id=transfer_id,
            completion=CompleteUploadRequest(
                parts=tuple(parts),
                stored_bytes=len(content),
                stored_sha256=identity["riverhog-stored-sha256"],
            ),
        )
        return _immutable_receipt(receipt)

    def iter_object_range(
        self,
        *,
        object_path: str,
        revision: str,
        offset: int,
        size: int,
    ) -> Iterator[bytes]:
        yield from self.runtime.client.iter_object_content(
            ObjectLocator(object_path=object_path, revision=revision),
            offset=offset,
            size=size,
        )

    def iter_object(
        self,
        *,
        object_path: str,
        revision: str,
    ) -> Iterator[bytes]:
        yield from self.runtime.client.iter_object_content(
            ObjectLocator(object_path=object_path, revision=revision)
        )

    def object_metadata(self, *, object_path: str, revision: str):  # type: ignore[no-untyped-def]
        return self.runtime.client.object_metadata(
            ObjectLocator(object_path=object_path, revision=revision)
        )

    def delete_object(self, *, object_path: str, revision: str) -> None:
        self.runtime.client.delete_object(ObjectLocator(object_path=object_path, revision=revision))

    def delete_prefix(self, object_prefix: str) -> int:
        return self.runtime.client.delete_prefix(object_prefix).affected


def _validate_descriptor(
    registration: StorageAdapterRegistration,
    descriptor: StorageAdapterDescriptor,
) -> None:
    profile = descriptor.profile
    if profile.profile_id != registration.expected_profile_id:
        raise ValueError(
            f"storage adapter {registration.name} profile ID differs from its registration"
        )
    if profile.profile_contract_sha256 != registration.expected_profile_contract_sha256:
        raise ValueError(
            f"storage adapter {registration.name} profile contract differs from its registration"
        )
    expected_implementation = registration.expected_implementation_id
    if (
        expected_implementation is not None
        and descriptor.implementation_id != expected_implementation
    ):
        raise ValueError(
            f"storage adapter {registration.name} implementation differs from its readiness pin"
        )


def _transfer_id(*, object_path: str, identity: dict[str, str]) -> str:
    canonical = json.dumps(
        {
            "format": "riverhog-storage-transfer-identity/v1",
            "object_path": object_path,
            "identity": dict(sorted(identity.items())),
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return "rh-" + hashlib.sha256(canonical).hexdigest()


def _part_receipt(receipt: UploadPartReceipt) -> MultipartPartReceipt:
    return MultipartPartReceipt(
        number=receipt.number,
        part_token=receipt.part_token,
        stored_bytes=receipt.stored_bytes,
        stored_sha256=receipt.stored_sha256,
    )


def _wire_part(receipt: MultipartPartReceipt) -> UploadPartReceipt:
    return UploadPartReceipt(
        number=receipt.number,
        part_token=receipt.part_token,
        stored_bytes=receipt.stored_bytes,
        stored_sha256=receipt.stored_sha256,
    )


def _completed_receipt(receipt) -> CompletedObjectReceipt:  # type: ignore[no-untyped-def]
    if receipt is None:
        raise RuntimeError("completed adapter upload omitted its object receipt")
    return CompletedObjectReceipt(
        object_path=receipt.object_path,
        revision=receipt.revision,
        stored_bytes=receipt.stored_bytes,
        stored_sha256=receipt.stored_sha256,
        completed_at=receipt.completed_at,
    )


def _immutable_receipt(receipt) -> ImmutableObjectReceipt:  # type: ignore[no-untyped-def]
    return ImmutableObjectReceipt(
        object_path=receipt.object_path,
        revision=receipt.revision,
        stored_bytes=receipt.stored_bytes,
        stored_sha256=receipt.stored_sha256,
        completed_at=receipt.completed_at,
    )


def _require_upload_path(upload: MultipartUpload, object_path: str) -> None:
    if upload.object_path != object_path:
        raise ArchiveObjectIdentityConflict(
            "storage adapter upload path differs from its checkpoint"
        )


__all__ = ["StorageAdapterObjectStore", "StorageAdapterRuntime"]
