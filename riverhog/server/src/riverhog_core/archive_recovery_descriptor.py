from __future__ import annotations

import builtins
import hashlib
from dataclasses import dataclass

from riverhog_archive_contracts import (
    RECOVERY_DESCRIPTOR_PATH,
    ArchiveRootCiphertextIdentity,
    CollectionEncryptionBinding,
    RecoveryDescriptor,
)

from riverhog_core.archive_formats import RECOVERY_DESCRIPTOR_STORAGE_FORMAT
from riverhog_core.archive_root import SealedArchiveRoot
from riverhog_core.ports.archive_objects import ImmutableArchiveObjectStore

RECOVERY_DESCRIPTOR_CONTENT_TYPE = "application/vnd.riverhog.recovery-descriptor+json"


def build_recovery_descriptor(
    *,
    encryption: CollectionEncryptionBinding,
    root_relative_path: str,
    root_stored_bytes: int,
    root_stored_sha256: str,
) -> bytes:
    """Return the canonical plaintext recovery descriptor for an encrypted root."""

    return RecoveryDescriptor(
        encryption=encryption,
        root=ArchiveRootCiphertextIdentity(
            path=root_relative_path,
            stored_bytes=root_stored_bytes,
            stored_sha256=root_stored_sha256,
        ),
    ).to_json_bytes()


@dataclass(frozen=True, slots=True)
class SealedRecoveryDescriptor:
    object_path: str
    relative_path: str
    version_id: str | None
    bytes: int
    sha256: str
    completed_at: str
    content: builtins.bytes


class ArchiveRecoveryDescriptorPublisher:
    """Publish the plaintext key-selection contract for one immutable root."""

    def __init__(self, *, object_store: ImmutableArchiveObjectStore) -> None:
        self._object_store = object_store

    def publish(
        self,
        *,
        archive_storage_prefix: str,
        root: SealedArchiveRoot,
        encryption: CollectionEncryptionBinding,
    ) -> SealedRecoveryDescriptor:
        prefix = archive_storage_prefix.strip("/")
        if not prefix:
            raise ValueError("archive storage prefix must not be empty")
        content = build_recovery_descriptor(
            encryption=encryption,
            root_relative_path=root.relative_path,
            root_stored_bytes=root.stored_bytes,
            root_stored_sha256=root.stored_sha256,
        )
        sha256 = hashlib.sha256(content).hexdigest()
        object_path = f"{prefix}/{RECOVERY_DESCRIPTOR_PATH}"
        receipt = self._object_store.put_immutable_object(
            object_path=object_path,
            content=content,
            content_type=RECOVERY_DESCRIPTOR_CONTENT_TYPE,
            identity_metadata={
                "riverhog-format": RECOVERY_DESCRIPTOR_STORAGE_FORMAT,
                "riverhog-sha256": sha256,
                "riverhog-root-stored-sha256": root.stored_sha256,
            },
            placement="immediate",
        )
        if receipt.object_path != object_path or receipt.stored_bytes != len(content):
            raise RuntimeError("immutable descriptor store returned an inconsistent receipt")
        if receipt.stored_sha256 != sha256:
            raise RuntimeError("immutable descriptor store changed the recovery descriptor")
        return SealedRecoveryDescriptor(
            object_path=receipt.object_path,
            relative_path=RECOVERY_DESCRIPTOR_PATH,
            version_id=receipt.version_id,
            bytes=len(content),
            sha256=sha256,
            completed_at=receipt.completed_at,
            content=content,
        )
