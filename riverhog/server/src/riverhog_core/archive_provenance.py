from __future__ import annotations

import hashlib
from dataclasses import dataclass

from riverhog_age import encrypt_age_scrypt
from riverhog_provenance import ProvenanceArchive

from riverhog_core.archive_formats import (
    PROVENANCE_BUNDLE_STORAGE_FORMAT,
    PROVENANCE_INDEX_STORAGE_FORMAT,
)
from riverhog_core.domain.archive import SealedProvenanceObject
from riverhog_core.ports.archive_objects import ImmutableArchiveObjectStore


@dataclass(frozen=True, slots=True)
class SealedArchiveProvenance:
    identity: str
    index: SealedProvenanceObject
    bundles: tuple[SealedProvenanceObject, ...]


class ArchiveProvenancePublisher:
    """Publish immutable encrypted provenance dependencies before the root manifest."""

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
        provenance: ProvenanceArchive,
    ) -> SealedArchiveProvenance:
        prefix = archive_storage_prefix.strip("/")
        if not prefix:
            raise ValueError("archive storage prefix must not be empty")
        bundles = tuple(
            self._put(
                prefix=prefix,
                object_id=bundle.bundle_id,
                kind="provenance-bundle",
                relative_path=bundle.relative_path,
                content=bundle.content,
                plaintext_sha256=bundle.sha256,
                storage_format=PROVENANCE_BUNDLE_STORAGE_FORMAT,
            )
            for bundle in provenance.bundles
        )
        index = self._put(
            prefix=prefix,
            object_id="provenance-index",
            kind="provenance-index",
            relative_path="provenance/index.json.age",
            content=provenance.index_bytes,
            plaintext_sha256=provenance.identity,
            storage_format=PROVENANCE_INDEX_STORAGE_FORMAT,
        )
        return SealedArchiveProvenance(
            identity=provenance.identity,
            index=index,
            bundles=bundles,
        )

    def _put(
        self,
        *,
        prefix: str,
        object_id: str,
        kind: str,
        relative_path: str,
        content: bytes,
        plaintext_sha256: str,
        storage_format: str,
    ) -> SealedProvenanceObject:
        if hashlib.sha256(content).hexdigest() != plaintext_sha256:
            raise ValueError("provenance plaintext identity changed before publication")
        ciphertext = encrypt_age_scrypt(
            content,
            self._passphrase,
            log_n=self._scrypt_log_n,
        )
        object_path = f"{prefix}/{relative_path}"
        receipt = self._object_store.put_immutable_object(
            object_path=object_path,
            content=ciphertext,
            content_type=(
                "application/vnd.riverhog.provenance-index+age"
                if kind == "provenance-index"
                else "application/vnd.riverhog.provenance-bundle+age"
            ),
            identity_metadata={
                "riverhog-format": storage_format,
                "riverhog-plaintext-bytes": str(len(content)),
                "riverhog-plaintext-sha256": plaintext_sha256,
            },
            placement="immediate",
        )
        return SealedProvenanceObject(
            object_id=object_id,
            kind=kind,
            relative_path=relative_path,
            plaintext_bytes=len(content),
            plaintext_sha256=plaintext_sha256,
            stored_bytes=receipt.stored_bytes,
            stored_sha256=receipt.stored_sha256,
            version_id=receipt.version_id,
            completed_at=receipt.completed_at,
        )
