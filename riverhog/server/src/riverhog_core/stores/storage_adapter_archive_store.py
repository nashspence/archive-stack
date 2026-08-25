from __future__ import annotations

import hashlib
import secrets
from collections.abc import Iterator, Sequence
from datetime import datetime, timedelta
from typing import Literal

from riverhog_age import encrypt_age_scrypt, iter_decrypt_age_scrypt
from riverhog_storage_adapter_protocol import (
    AbortIncompleteWritesRequest,
    AdapterDescriptor,
    DeleteObjectRequest,
    DeletePrefixRequest,
    ImmutableObjectReceipt,
    ObjectHeadRequest,
    ObjectLocator,
    ObjectMetadataReceipt,
    ObjectPlacement,
    ObjectReadRequest,
    ReadPreparationRequest,
    SmallObjectWriteRequest,
    StorageAdapterPort,
)
from time_formats import format_utc_timestamp, utc_now, utc_timestamp_now

from riverhog_core.archive_attestations import (
    ATTESTATION_FILENAMES,
    ATTESTATION_OBJECT_KINDS,
)
from riverhog_core.archive_formats import ROOT_PROOF_STORAGE_FORMAT, archive_object_storage_format
from riverhog_core.archive_safety import archive_agents_guidance, archive_recovery_readme
from riverhog_core.ports.archive_store import (
    ArchiveArtifactRead,
    ArchiveObjectIdentity,
    ArchiveObjectUploadReceipt,
    ArchiveReadStatus,
    ArchiveVerificationError,
    CollectionArchiveIdentity,
    CollectionArchiveUploadReceipt,
    MutableManifestReceipt,
)
from riverhog_core.ports.download_allowance import DownloadAllowance, DownloadAttribution
from riverhog_core.runtime_config import RuntimeConfig

_OPAQUE_ARCHIVE_ID_BYTES = 16
_PLAINTEXT_BYTES_METADATA = "riverhog-plaintext-bytes"
_PLAINTEXT_SHA256_METADATA = "riverhog-plaintext-sha256"


class StorageAdapterArchiveStore:
    """Riverhog archive semantics over provider-neutral opaque-object effects."""

    def __init__(
        self,
        config: RuntimeConfig,
        *,
        name: str,
        adapter: StorageAdapterPort,
        download_allowance: DownloadAllowance | None = None,
    ) -> None:
        normalized_name = name.strip().casefold()
        if not normalized_name:
            raise ValueError("storage adapter registration name must be nonempty")
        self._config = config
        self._name = normalized_name
        self._adapter = adapter
        self._descriptor = adapter.descriptor()
        self._download_allowance = download_allowance

    @property
    def descriptor(self) -> AdapterDescriptor:
        return self._descriptor

    def read_mode(self) -> str:
        return self._descriptor.read_mode

    def new_collection_archive_storage_prefix(self) -> str:
        return f"archives/{secrets.token_hex(_OPAQUE_ARCHIVE_ID_BYTES)}"

    def abort_incomplete_writes(
        self,
        *,
        initiated_before: datetime,
    ) -> int:
        if initiated_before.tzinfo is None:
            raise ValueError("resumable-write cutoff must be timezone-aware")
        return self._adapter.abort_incomplete_writes(
            AbortIncompleteWritesRequest(
                object_prefix="archives/",
                initiated_before=format_utc_timestamp(initiated_before),
            )
        )

    def discard_collection_archive_upload(self, *, archive_storage_prefix: str) -> None:
        prefix = _archive_prefix(archive_storage_prefix)
        self._adapter.abort_incomplete_writes(
            AbortIncompleteWritesRequest(
                object_prefix=f"{prefix}/",
                initiated_before=format_utc_timestamp(utc_now() + timedelta(seconds=1)),
            )
        )
        self._adapter.delete_prefix(DeletePrefixRequest(object_prefix=f"{prefix}/"))

    def verify_collection_archive(
        self,
        *,
        collection_id: int,
        archive: CollectionArchiveIdentity,
    ) -> None:
        _ = collection_id
        for expected in archive.objects:
            try:
                metadata = self._metadata(expected)
                _verify_metadata(expected, metadata)
            except Exception as exc:
                raise ArchiveVerificationError(
                    f"remote collection {expected.kind} object does not match its upload record"
                ) from exc

    def delete_collection_archive(
        self,
        *,
        collection_id: int,
        objects: Sequence[ArchiveObjectIdentity],
    ) -> None:
        _ = collection_id
        if not objects:
            raise ValueError("collection archive has no objects")
        prefixes = {_archive_prefix_from_object(current.object_path) for current in objects}
        if len(prefixes) != 1:
            raise ValueError("collection archive paths are outside one owned archive prefix")
        for current in objects:
            self._adapter.delete_object(
                DeleteObjectRequest(
                    object=ObjectLocator(object_path=current.object_path),
                    mode="all_versions",
                )
            )
        remaining = [
            current.object_path
            for current in objects
            if self._head(
                object_path=current.object_path,
                revision=None,
                placement=_object_placement(current.kind),
            )
            is not None
        ]
        if remaining:
            raise RuntimeError(
                "collection archive deletion could not be verified: " + ", ".join(remaining)
            )

    def publish_collection_metadata(
        self,
        *,
        collection_id: int,
        archive_storage_prefix: str,
        manifest: bytes,
        passphrase_id: str,
    ) -> MutableManifestReceipt:
        self._put_archive_root_guidance()
        object_path = f"{_archive_prefix(archive_storage_prefix)}/metadata.json.age"
        plaintext_sha256 = hashlib.sha256(manifest).hexdigest()
        identity = {
            "collection-metadata-format": "riverhog-collection-metadata/v1",
            "riverhog-collection-id": str(collection_id),
            "riverhog-encryption": "age-v1-scrypt",
            "riverhog-passphrase-id": passphrase_id,
            _PLAINTEXT_BYTES_METADATA: str(len(manifest)),
            _PLAINTEXT_SHA256_METADATA: plaintext_sha256,
        }
        existing = self._head(
            object_path=object_path,
            revision=None,
            placement="immediate",
        )
        if existing is not None and _metadata_contains(existing, identity):
            stored_sha256 = _required_stored_sha256(existing, object_path=object_path)
            return MutableManifestReceipt(
                object_path=existing.object_path,
                revision=existing.revision,
                stored_bytes=existing.stored_bytes,
                stored_sha256=stored_sha256,
                published_at=existing.completed_at,
            )
        ciphertext = encrypt_age_scrypt(
            manifest,
            self._config.archive_passphrase_for(passphrase_id),
            log_n=self._config.archive_scrypt_work_factor,
        )
        receipt = self._put_small(
            object_path=object_path,
            content=ciphertext,
            content_type="application/vnd.riverhog.collection-metadata+age",
            identity=identity,
            mode="replace_current",
        )
        return MutableManifestReceipt(
            object_path=receipt.object_path,
            revision=receipt.revision,
            stored_bytes=receipt.stored_bytes,
            stored_sha256=receipt.stored_sha256,
            published_at=receipt.completed_at,
        )

    def read_archive_artifact(
        self,
        *,
        collection_id: int,
        object: ArchiveObjectIdentity,
        passphrase_id: str,
    ) -> ArchiveArtifactRead:
        if object.kind not in {"manifest", "proof"}:
            raise ValueError("only collection archive roots and proofs are archive artifacts")
        metadata = self._metadata(object)
        _verify_metadata(object, metadata)
        content = b"".join(
            self.iter_archive_object(
                collection_id=collection_id,
                object=object,
                passphrase_id=passphrase_id,
            )
        )
        _verify_plaintext(object, content)
        return ArchiveArtifactRead(
            receipt=self._receipt_from_identity(object, metadata=metadata, verified=True),
            content=content,
        )

    def replace_archive_proof(
        self,
        *,
        collection_id: int,
        object: ArchiveObjectIdentity,
        proof_bytes: bytes,
        passphrase_id: str,
    ) -> ArchiveObjectUploadReceipt:
        _ = collection_id
        if object.object_id != "proof" or object.kind != "proof":
            raise ValueError("archive proof replacement requires the proof object")
        if not object.object_path.endswith("/manifest.json.ots.age"):
            raise ValueError("archive proof path is not canonical")
        _archive_prefix_from_object(object.object_path)
        existing = self._head(
            object_path=object.object_path,
            revision=None,
            placement="immediate",
        )
        plaintext_sha256 = hashlib.sha256(proof_bytes).hexdigest()
        identity = {
            "riverhog-format": ROOT_PROOF_STORAGE_FORMAT,
            "riverhog-passphrase-id": passphrase_id,
            _PLAINTEXT_BYTES_METADATA: str(len(proof_bytes)),
            _PLAINTEXT_SHA256_METADATA: plaintext_sha256,
        }
        if existing is not None and (
            archive_root_sha256 := existing.identity_metadata.get("riverhog-archive-root-sha256")
        ):
            identity["riverhog-archive-root-sha256"] = archive_root_sha256
        ciphertext = encrypt_age_scrypt(
            proof_bytes,
            self._config.archive_passphrase_for(passphrase_id),
            log_n=self._config.archive_scrypt_work_factor,
        )
        stored = self._put_small(
            object_path=object.object_path,
            content=ciphertext,
            content_type="application/vnd.riverhog.collection-manifest-proof+age",
            identity=identity,
            mode="replace_current",
        )
        return self._receipt_from_small(
            object_id="proof",
            kind="proof",
            plaintext_bytes=len(proof_bytes),
            plaintext_sha256=plaintext_sha256,
            stored=stored,
        )

    def publish_archive_attestation(
        self,
        *,
        collection_id: int,
        archive_storage_prefix: str,
        checksums: bytes,
        signature: bytes,
        proof: bytes,
    ) -> CollectionArchiveUploadReceipt:
        _ = collection_id
        self._put_archive_root_guidance()
        prefix = _archive_prefix(archive_storage_prefix)
        rows = (
            ("checksums", checksums),
            ("signature", signature),
            ("signature-proof", proof),
        )
        return CollectionArchiveUploadReceipt(
            objects=tuple(
                self._put_plaintext_attestation(
                    object_id=object_id,
                    object_path=f"{prefix}/{ATTESTATION_FILENAMES[object_id]}",
                    content=content,
                    accept_existing=False,
                    replace=True,
                )
                for object_id, content in rows
            )
        )

    def read_archive_attestation_artifact(
        self,
        *,
        collection_id: int,
        object: ArchiveObjectIdentity,
    ) -> ArchiveArtifactRead:
        if object.kind not in ATTESTATION_OBJECT_KINDS:
            raise ValueError("archive artifact is not an attestation artifact")
        expected_filename = ATTESTATION_FILENAMES[object.object_id]
        if not object.object_path.endswith(f"/{expected_filename}"):
            raise ValueError("archive attestation artifact path is not canonical")
        metadata = self._metadata(object)
        _verify_metadata(object, metadata)
        content = b"".join(
            self.iter_stored_archive_object(collection_id=collection_id, object=object)
        )
        _verify_stored(object, content)
        return ArchiveArtifactRead(
            receipt=self._receipt_from_identity(object, metadata=metadata, verified=True),
            content=content,
        )

    def replace_archive_attestation_proof(
        self,
        *,
        collection_id: int,
        object: ArchiveObjectIdentity,
        proof_bytes: bytes,
    ) -> ArchiveObjectUploadReceipt:
        _ = collection_id
        if object.object_id != "signature-proof" or object.kind != "signature-proof":
            raise ValueError("archive attestation proof replacement requires its proof object")
        if not object.object_path.endswith("/SHA256SUMS.minisig.ots"):
            raise ValueError("archive attestation proof path is not canonical")
        return self._put_plaintext_attestation(
            object_id="signature-proof",
            object_path=object.object_path,
            content=proof_bytes,
            accept_existing=False,
            replace=True,
        )

    def prepare_archive_objects_read(
        self,
        *,
        collection_id: int,
        objects: Sequence[ArchiveObjectIdentity],
    ) -> ArchiveReadStatus:
        _ = collection_id
        status = self._adapter.prepare_read(_read_request(objects))
        return ArchiveReadStatus(
            state=status.state,
            ready_at=status.ready_at,
            expires_at=status.expires_at,
        )

    def get_archive_objects_read_status(
        self,
        *,
        collection_id: int,
        objects: Sequence[ArchiveObjectIdentity],
    ) -> ArchiveReadStatus:
        _ = collection_id
        status = self._adapter.read_status(_read_request(objects))
        return ArchiveReadStatus(
            state=status.state,
            ready_at=status.ready_at,
            expires_at=status.expires_at,
        )

    def iter_archive_object(
        self,
        *,
        collection_id: int,
        object: ArchiveObjectIdentity,
        passphrase_id: str,
        attribution: DownloadAttribution | None = None,
    ) -> Iterator[bytes]:
        return iter_decrypt_age_scrypt(
            self.iter_stored_archive_object(
                collection_id=collection_id,
                object=object,
                attribution=attribution,
            ),
            self._config.archive_passphrase_for(passphrase_id),
        )

    def iter_stored_archive_object(
        self,
        *,
        collection_id: int,
        object: ArchiveObjectIdentity,
        attribution: DownloadAttribution | None = None,
    ) -> Iterator[bytes]:
        _ = collection_id
        metadata = self._metadata(object)
        _verify_metadata(object, metadata)
        status = self._adapter.read_status(_read_request((object,)))
        if status.state != "ready":
            raise RuntimeError(f"Archive object is not readable yet: {object.object_path}")
        content = self._adapter.iter_object(
            ObjectReadRequest(
                object=ObjectLocator(
                    object_path=object.object_path,
                    revision=object.revision,
                ),
                expected_bytes=object.stored_bytes,
            )
        )
        if self._download_allowance is None:
            return content
        return self._download_allowance.track(
            store=self._name,
            expected_bytes=object.stored_bytes,
            content=content,
            attribution=attribution,
        )

    def cleanup_archive_objects_read(
        self,
        *,
        collection_id: int,
        objects: Sequence[ArchiveObjectIdentity],
    ) -> None:
        _ = collection_id
        self._adapter.cleanup_read(_read_request(objects))

    def _metadata(self, object: ArchiveObjectIdentity) -> ObjectMetadataReceipt:
        metadata = self._head(
            object_path=object.object_path,
            revision=object.revision,
            placement=_object_placement(object.kind),
        )
        if metadata is None:
            raise RuntimeError(f"Archive object is missing: {object.object_path}")
        return metadata

    def _head(
        self,
        *,
        object_path: str,
        revision: str | None,
        placement: ObjectPlacement,
    ) -> ObjectMetadataReceipt | None:
        return self._adapter.head_object(
            ObjectHeadRequest(
                object=ObjectLocator(object_path=object_path, revision=revision),
                expected_placement=placement,
            )
        )

    def _put_small(
        self,
        *,
        object_path: str,
        content: bytes,
        content_type: str,
        identity: dict[str, str],
        mode: Literal["create_only", "replace_current"],
    ) -> ImmutableObjectReceipt:
        return self._adapter.put_small_object(
            SmallObjectWriteRequest(
                object_path=object_path,
                content_type=content_type,
                identity_metadata=identity,
                placement="immediate",
                mode=mode,
                stored_bytes=len(content),
                stored_sha256=hashlib.sha256(content).hexdigest(),
            ),
            content,
        )

    def _put_plaintext_attestation(
        self,
        *,
        object_id: str,
        object_path: str,
        content: bytes,
        accept_existing: bool,
        replace: bool,
    ) -> ArchiveObjectUploadReceipt:
        sha256 = hashlib.sha256(content).hexdigest()
        identity = {
            "riverhog-format": archive_object_storage_format(object_id),
            _PLAINTEXT_BYTES_METADATA: str(len(content)),
            _PLAINTEXT_SHA256_METADATA: sha256,
        }
        existing = self._head(
            object_path=object_path,
            revision=None,
            placement="immediate",
        )
        if existing is not None:
            if _metadata_contains(existing, identity) or (accept_existing and not replace):
                return self._receipt_from_metadata(
                    object_id=object_id,
                    kind=object_id,
                    plaintext_bytes=int(
                        existing.identity_metadata.get(
                            _PLAINTEXT_BYTES_METADATA,
                            existing.stored_bytes,
                        )
                    ),
                    plaintext_sha256=existing.identity_metadata.get(
                        _PLAINTEXT_SHA256_METADATA,
                        _required_stored_sha256(existing, object_path=object_path),
                    ),
                    metadata=existing,
                    verified=False,
                )
            if not replace:
                raise RuntimeError("archive attestation artifact differs from its durable copy")
        stored = self._put_small(
            object_path=object_path,
            content=content,
            content_type="application/octet-stream",
            identity=identity,
            mode="replace_current" if replace else "create_only",
        )
        return self._receipt_from_small(
            object_id=object_id,
            kind=object_id,
            plaintext_bytes=len(content),
            plaintext_sha256=sha256,
            stored=stored,
        )

    def _put_archive_root_guidance(self) -> None:
        artifacts: list[tuple[str, bytes, str]] = [
            (
                "README.md",
                archive_recovery_readme().encode("utf-8"),
                "riverhog-archive-readme/v1",
            ),
            (
                "AGENTS.md",
                archive_agents_guidance().encode("utf-8"),
                "riverhog-archive-agents/v1",
            ),
        ]
        if self._config.attestation_public_key_file is not None:
            public_key = self._config.attestation_public_key_file.read_bytes()
            if not public_key.strip():
                raise RuntimeError("attestation public key is empty")
            artifacts.append(("minisign.pub", public_key, "riverhog-attestation-public-key/v1"))
        for object_path, content, format_name in artifacts:
            self._put_small(
                object_path=object_path,
                content=content,
                content_type="text/plain; charset=utf-8",
                identity={
                    "riverhog-format": format_name,
                    "riverhog-content-sha256": hashlib.sha256(content).hexdigest(),
                },
                mode="replace_current",
            )

    def _receipt_from_identity(
        self,
        object: ArchiveObjectIdentity,
        *,
        metadata: ObjectMetadataReceipt,
        verified: bool,
    ) -> ArchiveObjectUploadReceipt:
        return self._receipt_from_metadata(
            object_id=object.object_id,
            kind=object.kind,
            plaintext_bytes=object.plaintext_bytes,
            plaintext_sha256=object.sha256,
            metadata=metadata,
            verified=verified,
        )

    def _receipt_from_small(
        self,
        *,
        object_id: str,
        kind: str,
        plaintext_bytes: int,
        plaintext_sha256: str | None,
        stored: ImmutableObjectReceipt,
    ) -> ArchiveObjectUploadReceipt:
        return ArchiveObjectUploadReceipt(
            object_id=object_id,
            kind=kind,
            object_path=stored.object_path,
            plaintext_bytes=plaintext_bytes,
            stored_bytes=stored.stored_bytes,
            sha256=plaintext_sha256,
            stored_sha256=stored.stored_sha256,
            revision=stored.revision,
            uploaded_at=stored.completed_at,
            verified_at=stored.completed_at,
        )

    def _receipt_from_metadata(
        self,
        *,
        object_id: str,
        kind: str,
        plaintext_bytes: int,
        plaintext_sha256: str | None,
        metadata: ObjectMetadataReceipt,
        verified: bool,
    ) -> ArchiveObjectUploadReceipt:
        return ArchiveObjectUploadReceipt(
            object_id=object_id,
            kind=kind,
            object_path=metadata.object_path,
            plaintext_bytes=plaintext_bytes,
            stored_bytes=metadata.stored_bytes,
            sha256=plaintext_sha256,
            stored_sha256=(
                metadata.stored_sha256 or metadata.identity_metadata.get("riverhog-stored-sha256")
            ),
            revision=metadata.revision,
            uploaded_at=metadata.completed_at,
            verified_at=utc_timestamp_now() if verified else None,
        )


def _read_request(objects: Sequence[ArchiveObjectIdentity]) -> ReadPreparationRequest:
    locators = tuple(
        sorted(
            (
                ObjectLocator(
                    object_path=current.object_path,
                    revision=current.revision,
                )
                for current in objects
            ),
            key=lambda current: (current.object_path, current.revision or ""),
        )
    )
    if not locators:
        raise ValueError("archive read requires at least one object")
    return ReadPreparationRequest(objects=locators)


def _object_placement(kind: str) -> ObjectPlacement:
    return "archive" if kind in {"pack", "file", "segment"} else "immediate"


def _verify_metadata(
    expected: ArchiveObjectIdentity,
    actual: ObjectMetadataReceipt,
) -> None:
    if actual.object_path != expected.object_path or actual.stored_bytes != expected.stored_bytes:
        raise RuntimeError("archive object metadata differs from its durable identity")
    if expected.revision is not None and actual.revision != expected.revision:
        raise RuntimeError("archive object revision differs from its durable identity")
    if expected.stored_sha256 is not None and actual.stored_sha256 is not None:
        if actual.stored_sha256 != expected.stored_sha256:
            raise RuntimeError("archive object stored digest differs from its durable identity")
    required = {
        "riverhog-format": archive_object_storage_format(expected.kind),
        _PLAINTEXT_BYTES_METADATA: str(expected.plaintext_bytes),
    }
    if expected.sha256 is not None:
        required[_PLAINTEXT_SHA256_METADATA] = expected.sha256
    if not _metadata_contains(actual, required):
        raise RuntimeError("archive object identity metadata differs from its durable identity")


def _metadata_contains(receipt: ObjectMetadataReceipt, expected: dict[str, str]) -> bool:
    return all(receipt.identity_metadata.get(key) == value for key, value in expected.items())


def _required_stored_sha256(receipt: ObjectMetadataReceipt, *, object_path: str) -> str:
    value = receipt.stored_sha256 or receipt.identity_metadata.get("riverhog-stored-sha256")
    if (
        value is None
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise RuntimeError(f"archive object is missing its stored sha256: {object_path}")
    return value


def _archive_prefix(value: str) -> str:
    normalized = value.strip("/")
    parts = normalized.split("/")
    if len(parts) != 2 or parts[0] != "archives" or not parts[1]:
        raise ValueError("archive storage prefix is not canonical")
    return normalized


def _archive_prefix_from_object(object_path: str) -> str:
    parts = object_path.split("/")
    if len(parts) < 3:
        raise ValueError("archive object path is not beneath an archive prefix")
    return _archive_prefix("/".join(parts[:2]))


def _verify_stored(object: ArchiveObjectIdentity, content: bytes) -> None:
    if len(content) != object.stored_bytes:
        raise RuntimeError("archive object stored byte count changed")
    if object.stored_sha256 is None or hashlib.sha256(content).hexdigest() != object.stored_sha256:
        raise RuntimeError("archive object stored SHA-256 changed")


def _verify_plaintext(object: ArchiveObjectIdentity, content: bytes) -> None:
    if len(content) != object.plaintext_bytes:
        raise RuntimeError("archive object plaintext byte count changed")
    if object.sha256 is None or hashlib.sha256(content).hexdigest() != object.sha256:
        raise RuntimeError("archive object plaintext SHA-256 changed")


__all__ = ["StorageAdapterArchiveStore"]
