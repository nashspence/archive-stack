from __future__ import annotations

import hashlib
from dataclasses import dataclass

from riverhog_age import encrypt_age_scrypt
from riverhog_provenance import (
    ProvenanceRootDocument,
    ProvenanceTerminalDocument,
    ProvenanceVolumeDocument,
    format_provenance_sequence,
)

from riverhog_core.archive_formats import (
    PROVENANCE_BINDING_SEGMENT_STORAGE_FORMAT,
    PROVENANCE_JOURNAL_SEGMENT_STORAGE_FORMAT,
    PROVENANCE_ROOT_STORAGE_FORMAT,
    PROVENANCE_TERMINAL_STORAGE_FORMAT,
    PROVENANCE_VOLUME_METADATA_STORAGE_FORMAT,
)
from riverhog_core.domain.archive import SealedProvenanceObject
from riverhog_core.ports.archive_objects import ImmutableArchiveObjectStore


@dataclass(frozen=True, slots=True)
class SealedArchiveProvenanceVolume:
    sequence: int
    payload: SealedProvenanceObject
    metadata: SealedProvenanceObject


@dataclass(frozen=True, slots=True)
class SealedArchiveProvenance:
    identity: str
    root: SealedProvenanceObject


class ArchiveProvenancePublisher:
    """Publish one bounded provenance object at a time before its immutable root."""

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

    def publish_volume(
        self,
        *,
        archive_storage_prefix: str,
        document: ProvenanceVolumeDocument,
        payload: bytes,
    ) -> SealedArchiveProvenanceVolume:
        prefix = _prefix(archive_storage_prefix)
        if (
            len(payload) != document.payload.bytes
            or hashlib.sha256(payload).hexdigest() != document.payload.sha256
        ):
            raise ValueError("provenance payload identity changed before publication")
        payload_object = self._put(
            prefix=prefix,
            object_id=f"provenance-payload-{format_provenance_sequence(document.sequence)}",
            kind=(
                "provenance-bindings"
                if document.payload.kind == "bindings"
                else "provenance-journal-segment"
            ),
            relative_path=document.payload.path,
            content=payload,
            storage_format=(
                PROVENANCE_BINDING_SEGMENT_STORAGE_FORMAT
                if document.payload.kind == "bindings"
                else PROVENANCE_JOURNAL_SEGMENT_STORAGE_FORMAT
            ),
        )
        metadata_object = self._put(
            prefix=prefix,
            object_id=f"provenance-volume-{format_provenance_sequence(document.sequence)}",
            kind="provenance-volume-metadata",
            relative_path=document.metadata_path,
            content=document.to_json_bytes(),
            storage_format=PROVENANCE_VOLUME_METADATA_STORAGE_FORMAT,
        )
        return SealedArchiveProvenanceVolume(
            sequence=document.sequence,
            payload=payload_object,
            metadata=metadata_object,
        )

    def publish_root(
        self,
        *,
        archive_storage_prefix: str,
        root: ProvenanceRootDocument,
    ) -> SealedArchiveProvenance:
        sealed = self._put(
            prefix=_prefix(archive_storage_prefix),
            object_id="provenance-root",
            kind="provenance-root",
            relative_path="provenance/root.json.age",
            content=root.to_json_bytes(),
            storage_format=PROVENANCE_ROOT_STORAGE_FORMAT,
        )
        if sealed.plaintext_sha256 != root.identity:
            raise RuntimeError("published provenance root identity changed")
        return SealedArchiveProvenance(identity=root.identity, root=sealed)

    def publish_terminal(
        self,
        *,
        archive_storage_prefix: str,
        terminal: ProvenanceTerminalDocument,
    ) -> SealedProvenanceObject:
        """Publish the authenticated sequence terminator before the root."""

        return self._put(
            prefix=_prefix(archive_storage_prefix),
            object_id=f"provenance-terminal-{format_provenance_sequence(terminal.sequence)}",
            kind="provenance-terminal",
            relative_path=terminal.metadata_path,
            content=terminal.to_json_bytes(),
            storage_format=PROVENANCE_TERMINAL_STORAGE_FORMAT,
        )

    def _put(
        self,
        *,
        prefix: str,
        object_id: str,
        kind: str,
        relative_path: str,
        content: bytes,
        storage_format: str,
    ) -> SealedProvenanceObject:
        plaintext_sha256 = hashlib.sha256(content).hexdigest()
        ciphertext = encrypt_age_scrypt(
            content,
            self._passphrase,
            log_n=self._scrypt_log_n,
        )
        receipt = self._object_store.put_immutable_object(
            object_path=f"{prefix}/{relative_path}",
            content=ciphertext,
            content_type=f"application/vnd.{storage_format.replace('/', '.').replace('+', '.')}",
            required_identity_assertions={
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
            revision=receipt.revision,
            completed_at=receipt.completed_at,
        )


def _prefix(value: str) -> str:
    prefix = value.strip("/")
    if not prefix:
        raise ValueError("archive storage prefix must not be empty")
    return prefix


__all__ = [
    "ArchiveProvenancePublisher",
    "SealedArchiveProvenance",
    "SealedArchiveProvenanceVolume",
]
