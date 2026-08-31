from __future__ import annotations

import builtins
import hashlib
from collections.abc import Sequence
from dataclasses import dataclass

from riverhog_age import encrypt_age_scrypt
from riverhog_archive_contracts import (
    CollectionArchiveManifest,
    CollectionArchiveTerminalDocument,
    CollectionArchiveVolumeDocument,
    format_archive_sequence,
)
from riverhog_protocol.paths import normalize_relpath

from riverhog_core.archive_formats import (
    ROOT_MANIFEST_STORAGE_FORMAT,
    VOLUME_METADATA_STORAGE_FORMAT,
)
from riverhog_core.archive_manifest import (
    build_collection_archive_authority,
    build_collection_archive_terminal_document,
    collection_tree_identity,
)
from riverhog_core.domain.archive import (
    ArchiveFile,
    PackVolumePlan,
    SealedPackVolume,
    SealedProvenanceObject,
    SealedRawVolume,
    VerifiedRawFile,
)
from riverhog_core.ports.archive_objects import ImmutableArchiveObjectStore

ROOT_MANIFEST_RELATIVE_PATH = "manifest.json.age"
ROOT_MANIFEST_CONTENT_TYPE = "application/vnd.riverhog.collection-manifest+age"
VOLUME_METADATA_CONTENT_TYPE = "application/vnd.riverhog.collection-volume+age"


@dataclass(frozen=True, slots=True)
class SealedArchiveVolumeMetadata:
    sequence: int
    object_path: str
    relative_path: str
    revision: str | None
    plaintext_bytes: int
    plaintext_sha256: str
    stored_bytes: int
    stored_sha256: str
    completed_at: str


@dataclass(frozen=True, slots=True)
class SealedArchiveRoot:
    object_path: str
    relative_path: str
    revision: str | None
    plaintext_bytes: int
    plaintext_sha256: str
    stored_bytes: int
    stored_sha256: str
    tree_sha256: str
    files: int
    bytes: int
    completed_at: str
    manifest_bytes: builtins.bytes
    volume_metadata: tuple[SealedArchiveVolumeMetadata, ...]


class ArchiveRootPublisher:
    """Write the immutable root once after all referenced volumes are sealed.

    Idempotency is based on the plaintext manifest digest, not randomized age ciphertext.
    The object-store adapter may therefore recover a successful earlier write after a lost
    response without replacing the object.
    """

    def __init__(
        self,
        *,
        object_store: ImmutableArchiveObjectStore,
        passphrase: str,
        scrypt_log_n: int,
    ) -> None:
        if not passphrase:
            raise ValueError("archive passphrase must not be empty")
        self._object_store = object_store
        self._passphrase = passphrase
        self._scrypt_log_n = scrypt_log_n

    def publish(
        self,
        *,
        archive_generation: str,
        archive_storage_prefix: str,
        files: Sequence[ArchiveFile],
        packs: Sequence[tuple[PackVolumePlan, SealedPackVolume]],
        raw_volumes: Sequence[SealedRawVolume] = (),
        verified_raw_files: Sequence[VerifiedRawFile] = (),
        provenance_identity: str | None = None,
        provenance_objects: Sequence[SealedProvenanceObject] = (),
    ) -> SealedArchiveRoot:
        prefix = archive_storage_prefix.strip("/")
        if not prefix:
            raise ValueError("archive storage prefix must not be empty")
        manifest, volume_documents = build_collection_archive_authority(
            archive_generation=archive_generation,
            files=files,
            packs=packs,
            raw_volumes=raw_volumes,
            verified_raw_files=verified_raw_files,
            provenance_identity=provenance_identity,
            provenance_objects=provenance_objects,
        )
        plaintext_sha256 = hashlib.sha256(manifest).hexdigest()
        tree = collection_tree_identity(files)
        sealed_metadata: list[SealedArchiveVolumeMetadata] = []
        for document in volume_documents:
            metadata = document.to_json_bytes()
            metadata_sha256 = hashlib.sha256(metadata).hexdigest()
            sequence = document.volume.sequence
            sequence_token = format_archive_sequence(sequence)
            relative_path = f"metadata/volume-{sequence_token}.json.age"
            metadata_object_path = f"{prefix}/{relative_path}"
            metadata_ciphertext = encrypt_age_scrypt(
                metadata,
                self._passphrase,
                log_n=self._scrypt_log_n,
            )
            metadata_receipt = self._object_store.put_immutable_object(
                object_path=metadata_object_path,
                content=metadata_ciphertext,
                content_type=VOLUME_METADATA_CONTENT_TYPE,
                required_identity_assertions={
                    "riverhog-format": VOLUME_METADATA_STORAGE_FORMAT,
                    "riverhog-plaintext-bytes": str(len(metadata)),
                    "riverhog-plaintext-sha256": metadata_sha256,
                    "riverhog-volume-sequence": sequence_token,
                },
                placement="immediate",
            )
            sealed_metadata.append(
                SealedArchiveVolumeMetadata(
                    sequence=sequence,
                    object_path=metadata_receipt.object_path,
                    relative_path=relative_path,
                    revision=metadata_receipt.revision,
                    plaintext_bytes=len(metadata),
                    plaintext_sha256=metadata_sha256,
                    stored_bytes=metadata_receipt.stored_bytes,
                    stored_sha256=metadata_receipt.stored_sha256,
                    completed_at=metadata_receipt.completed_at,
                )
            )
        terminal = build_collection_archive_terminal_document(
            archive_generation=archive_generation,
            tree_sha256=str(tree["sha256"]),
            sequence=len(volume_documents),
        )
        sealed_metadata.append(
            self.publish_terminal_metadata(
                archive_storage_prefix=prefix,
                document=terminal,
            )
        )
        ciphertext = encrypt_age_scrypt(
            manifest,
            self._passphrase,
            log_n=self._scrypt_log_n,
        )
        object_path = f"{prefix}/{ROOT_MANIFEST_RELATIVE_PATH}"
        receipt = self._object_store.put_immutable_object(
            object_path=object_path,
            content=ciphertext,
            content_type=ROOT_MANIFEST_CONTENT_TYPE,
            required_identity_assertions={
                "riverhog-format": ROOT_MANIFEST_STORAGE_FORMAT,
                "riverhog-plaintext-bytes": str(len(manifest)),
                "riverhog-plaintext-sha256": plaintext_sha256,
                "riverhog-tree-sha256": str(tree["sha256"]),
            },
            placement="immediate",
        )
        if receipt.object_path != object_path or receipt.stored_bytes <= 0:
            raise RuntimeError("immutable root store returned an inconsistent receipt")
        return SealedArchiveRoot(
            object_path=receipt.object_path,
            relative_path=normalize_relpath(ROOT_MANIFEST_RELATIVE_PATH),
            revision=receipt.revision,
            plaintext_bytes=len(manifest),
            plaintext_sha256=plaintext_sha256,
            stored_bytes=receipt.stored_bytes,
            stored_sha256=receipt.stored_sha256,
            tree_sha256=str(tree["sha256"]),
            files=tree["files"],
            bytes=tree["bytes"],
            completed_at=receipt.completed_at,
            manifest_bytes=manifest,
            volume_metadata=tuple(sealed_metadata),
        )

    def publish_volume_metadata(
        self,
        *,
        archive_storage_prefix: str,
        document: CollectionArchiveVolumeDocument,
    ) -> SealedArchiveVolumeMetadata:
        """Publish one bounded volume document before the immutable root exists."""

        prefix = archive_storage_prefix.strip("/")
        if not prefix:
            raise ValueError("archive storage prefix must not be empty")
        metadata = document.to_json_bytes()
        metadata_sha256 = hashlib.sha256(metadata).hexdigest()
        sequence = document.volume.sequence
        sequence_token = format_archive_sequence(sequence)
        relative_path = f"metadata/volume-{sequence_token}.json.age"
        object_path = f"{prefix}/{relative_path}"
        receipt = self._object_store.put_immutable_object(
            object_path=object_path,
            content=encrypt_age_scrypt(
                metadata,
                self._passphrase,
                log_n=self._scrypt_log_n,
            ),
            content_type=VOLUME_METADATA_CONTENT_TYPE,
            required_identity_assertions={
                "riverhog-format": VOLUME_METADATA_STORAGE_FORMAT,
                "riverhog-plaintext-bytes": str(len(metadata)),
                "riverhog-plaintext-sha256": metadata_sha256,
                "riverhog-volume-sequence": sequence_token,
            },
            placement="immediate",
        )
        return SealedArchiveVolumeMetadata(
            sequence=sequence,
            object_path=receipt.object_path,
            relative_path=relative_path,
            revision=receipt.revision,
            plaintext_bytes=len(metadata),
            plaintext_sha256=metadata_sha256,
            stored_bytes=receipt.stored_bytes,
            stored_sha256=receipt.stored_sha256,
            completed_at=receipt.completed_at,
        )

    def publish_terminal_metadata(
        self,
        *,
        archive_storage_prefix: str,
        document: CollectionArchiveTerminalDocument,
    ) -> SealedArchiveVolumeMetadata:
        """Publish the bounded authenticated terminator before the root."""

        prefix = archive_storage_prefix.strip("/")
        if not prefix:
            raise ValueError("archive storage prefix must not be empty")
        metadata = document.to_json_bytes()
        metadata_sha256 = hashlib.sha256(metadata).hexdigest()
        sequence = document.sequence
        sequence_token = format_archive_sequence(sequence)
        relative_path = f"metadata/volume-{sequence_token}.json.age"
        object_path = f"{prefix}/{relative_path}"
        receipt = self._object_store.put_immutable_object(
            object_path=object_path,
            content=encrypt_age_scrypt(
                metadata,
                self._passphrase,
                log_n=self._scrypt_log_n,
            ),
            content_type=VOLUME_METADATA_CONTENT_TYPE,
            required_identity_assertions={
                "riverhog-format": VOLUME_METADATA_STORAGE_FORMAT,
                "riverhog-plaintext-bytes": str(len(metadata)),
                "riverhog-plaintext-sha256": metadata_sha256,
                "riverhog-volume-sequence": sequence_token,
            },
            placement="immediate",
        )
        return SealedArchiveVolumeMetadata(
            sequence=sequence,
            object_path=receipt.object_path,
            relative_path=relative_path,
            revision=receipt.revision,
            plaintext_bytes=len(metadata),
            plaintext_sha256=metadata_sha256,
            stored_bytes=receipt.stored_bytes,
            stored_sha256=receipt.stored_sha256,
            completed_at=receipt.completed_at,
        )

    def publish_root_manifest(
        self,
        *,
        archive_storage_prefix: str,
        manifest: bytes,
    ) -> SealedArchiveRoot:
        """Publish only the small final authority after its exact sequence is durable."""

        prefix = archive_storage_prefix.strip("/")
        if not prefix:
            raise ValueError("archive storage prefix must not be empty")
        authority = CollectionArchiveManifest.from_json_bytes(manifest)
        plaintext_sha256 = hashlib.sha256(manifest).hexdigest()
        object_path = f"{prefix}/{ROOT_MANIFEST_RELATIVE_PATH}"
        receipt = self._object_store.put_immutable_object(
            object_path=object_path,
            content=encrypt_age_scrypt(
                manifest,
                self._passphrase,
                log_n=self._scrypt_log_n,
            ),
            content_type=ROOT_MANIFEST_CONTENT_TYPE,
            required_identity_assertions={
                "riverhog-format": ROOT_MANIFEST_STORAGE_FORMAT,
                "riverhog-plaintext-bytes": str(len(manifest)),
                "riverhog-plaintext-sha256": plaintext_sha256,
                "riverhog-tree-sha256": authority.tree_sha256,
            },
            placement="immediate",
        )
        if receipt.object_path != object_path or receipt.stored_bytes <= 0:
            raise RuntimeError("immutable root store returned an inconsistent receipt")
        return SealedArchiveRoot(
            object_path=receipt.object_path,
            relative_path=normalize_relpath(ROOT_MANIFEST_RELATIVE_PATH),
            revision=receipt.revision,
            plaintext_bytes=len(manifest),
            plaintext_sha256=plaintext_sha256,
            stored_bytes=receipt.stored_bytes,
            stored_sha256=receipt.stored_sha256,
            tree_sha256=authority.tree_sha256,
            files=authority.files,
            bytes=authority.bytes,
            completed_at=receipt.completed_at,
            manifest_bytes=manifest,
            volume_metadata=(),
        )
