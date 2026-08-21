from __future__ import annotations

import builtins
import hashlib
from collections.abc import Sequence
from dataclasses import dataclass

from riverhog_age import encrypt_age_scrypt
from riverhog_protocol.paths import normalize_relpath

from riverhog_core.archive_formats import ROOT_MANIFEST_STORAGE_FORMAT
from riverhog_core.archive_manifest import (
    build_collection_archive_manifest,
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


@dataclass(frozen=True, slots=True)
class SealedArchiveRoot:
    object_path: str
    relative_path: str
    revision: str
    plaintext_bytes: int
    plaintext_sha256: str
    stored_bytes: int
    stored_sha256: str
    tree_sha256: str
    files: int
    bytes: int
    completed_at: str
    manifest_bytes: builtins.bytes


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
        manifest = build_collection_archive_manifest(
            files=files,
            packs=packs,
            raw_volumes=raw_volumes,
            verified_raw_files=verified_raw_files,
            provenance_identity=provenance_identity,
            provenance_objects=provenance_objects,
        )
        plaintext_sha256 = hashlib.sha256(manifest).hexdigest()
        tree = collection_tree_identity(files)
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
            identity_metadata={
                "riverhog-format": ROOT_MANIFEST_STORAGE_FORMAT,
                "riverhog-plaintext-bytes": str(len(manifest)),
                "riverhog-plaintext-sha256": plaintext_sha256,
                "riverhog-tree-sha256": str(tree["sha256"]),
            },
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
        )
