from __future__ import annotations

import hashlib
import secrets
from collections.abc import Iterator, Sequence
from datetime import datetime

from riverhog_age import encrypt_age_scrypt, iter_decrypt_age_scrypt
from riverhog_storage_adapter_protocol import ObjectLocator, ReadRequest

from riverhog_core.archive_attestations import (
    ATTESTATION_FILENAMES,
    ATTESTATION_OBJECT_KINDS,
)
from riverhog_core.archive_formats import (
    ROOT_PROOF_STORAGE_FORMAT,
    archive_object_storage_format,
)
from riverhog_core.ports.archive_objects import ImmutableObjectReceipt
from riverhog_core.ports.archive_store import (
    ArchiveArtifactRead,
    ArchiveObjectIdentity,
    ArchiveObjectUploadReceipt,
    ArchiveReadStatus,
    ArchiveVerificationError,
    CollectionArchiveIdentity,
    CollectionArchiveUploadReceipt,
    MutableManifestReceipt,
    StorageExecutionEvidence,
)
from riverhog_core.ports.download_allowance import DownloadAllowance, DownloadAttribution
from riverhog_core.ports.retrieval_cache import RetrievalCache
from riverhog_core.runtime_config import ArchiveStoreConfig, RuntimeConfig
from riverhog_core.stores.storage_adapter_object_store import (
    StorageAdapterObjectStore,
    StorageAdapterRuntime,
)

_OPAQUE_ARCHIVE_ID_BYTES = 16


class StorageAdapterArchiveStore:
    """Riverhog archive semantics over one opaque object-storage adapter."""

    def __init__(
        self,
        config: RuntimeConfig,
        store: ArchiveStoreConfig,
        runtime: StorageAdapterRuntime,
        *,
        retrieval_cache: RetrievalCache | None = None,
        download_allowance: DownloadAllowance | None = None,
    ) -> None:
        self._config = config
        self._store = store
        self._runtime = runtime
        self._objects = StorageAdapterObjectStore(runtime)
        self._retrieval_cache = retrieval_cache
        self._download_allowance = download_allowance
        if store.monthly_download_allowance_bytes is not None and download_allowance is None:
            raise ValueError("configured archive download allowance requires its service")

    def read_mode(self) -> str:
        return self._runtime.registration.expected_profile_read_mode

    def storage_execution_evidence(self) -> StorageExecutionEvidence:
        descriptor = self._runtime.refresh_descriptor()
        profile = descriptor.profile
        return StorageExecutionEvidence(
            storage_adapter=self._store.storage_adapter,
            storage_profile_id=profile.profile_id,
            storage_profile_contract_sha256=profile.profile_contract_sha256,
            egress_accounting_id=profile.egress_accounting_id,
            read_mode=profile.read_mode,
            adapter_implementation_id=descriptor.implementation_id,
            adapter_implementation_version=descriptor.implementation_version,
            adapter_source_revision=descriptor.source_revision,
            adapter_runtime_descriptor_sha256=descriptor.runtime_descriptor_sha256,
        )

    def new_collection_archive_storage_prefix(self) -> str:
        return f"archives/{secrets.token_hex(_OPAQUE_ARCHIVE_ID_BYTES)}"

    def abort_incomplete_multipart_uploads(
        self,
        *,
        initiated_before: datetime,
    ) -> int:
        if initiated_before.tzinfo is None:
            raise ValueError("multipart upload cutoff must be timezone-aware")
        return self._runtime.client.abort_incomplete_uploads(
            initiated_before=initiated_before.isoformat().replace("+00:00", "Z")
        ).affected

    def discard_collection_archive_upload(self, *, archive_storage_prefix: str) -> None:
        prefix = _archive_prefix(archive_storage_prefix)
        self._objects.delete_prefix(prefix)

    def verify_collection_archive(
        self,
        *,
        collection_id: int,
        archive: CollectionArchiveIdentity,
    ) -> None:
        for expected in archive.objects:
            try:
                receipt = self._objects.object_metadata(
                    object_path=expected.object_path,
                    revision=expected.revision,
                )
                if (
                    receipt.object_path != expected.object_path
                    or receipt.stored_bytes != expected.stored_bytes
                    or expected.stored_sha256 is None
                    or receipt.stored_sha256 != expected.stored_sha256
                ):
                    raise RuntimeError("object receipt differs from archive identity")
                self.stored_archive_object_sha256(
                    collection_id=collection_id,
                    object=expected,
                )
            except Exception as exc:
                raise ArchiveVerificationError(
                    f"archive object does not match its durable identity: {expected.object_path}"
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
            raise ValueError("collection archive objects do not share one archive prefix")
        for current in objects:
            self._objects.delete_object(
                object_path=current.object_path,
                revision=current.revision,
            )

    def publish_collection_metadata(
        self,
        *,
        collection_id: int,
        archive_storage_prefix: str,
        manifest: bytes,
        prior_revision: str | None = None,
    ) -> MutableManifestReceipt:
        self.storage_execution_evidence()
        self._put_archive_root_guidance()
        plaintext_sha256 = hashlib.sha256(manifest).hexdigest()
        ciphertext = encrypt_age_scrypt(
            manifest,
            self._config.archive_passphrase,
            log_n=self._config.archive_scrypt_work_factor,
        )
        receipt = self._objects.put_object(
            object_path=f"{_archive_prefix(archive_storage_prefix)}/metadata.json.age",
            content=ciphertext,
            content_type="application/vnd.riverhog.collection-metadata+age",
            identity_metadata={
                "riverhog-format": "riverhog-collection-metadata/v1",
                "riverhog-collection-id": str(collection_id),
                "riverhog-plaintext-bytes": str(len(manifest)),
                "riverhog-plaintext-sha256": plaintext_sha256,
            },
            prior_revision=prior_revision,
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
    ) -> ArchiveArtifactRead:
        if object.kind not in {"manifest", "proof"}:
            raise ValueError("only collection manifests and proofs are archive artifacts")
        content = b"".join(
            self.iter_archive_object(collection_id=collection_id, object=object)
        )
        _verify_plaintext(object, content)
        return ArchiveArtifactRead(
            receipt=self._receipt(object, verified=True),
            content=content,
        )

    def replace_archive_proof(
        self,
        *,
        collection_id: int,
        object: ArchiveObjectIdentity,
        proof_bytes: bytes,
    ) -> ArchiveObjectUploadReceipt:
        self.storage_execution_evidence()
        _ = collection_id
        if object.object_id != "proof" or object.kind != "proof":
            raise ValueError("archive proof replacement requires the proof object")
        if not object.object_path.endswith("/manifest.json.ots.age"):
            raise ValueError("archive proof path is not canonical")
        ciphertext = encrypt_age_scrypt(
            proof_bytes,
            self._config.archive_passphrase,
            log_n=self._config.archive_scrypt_work_factor,
        )
        stored = self._objects.put_object(
            object_path=object.object_path,
            content=ciphertext,
            content_type="application/vnd.riverhog.collection-manifest-proof+age",
            identity_metadata={
                "riverhog-format": ROOT_PROOF_STORAGE_FORMAT,
                "riverhog-plaintext-bytes": str(len(proof_bytes)),
                "riverhog-plaintext-sha256": hashlib.sha256(proof_bytes).hexdigest(),
            },
            prior_revision=object.revision,
        )
        return self._receipt_from_stored(
            object_id="proof",
            kind="proof",
            plaintext_bytes=len(proof_bytes),
            plaintext_sha256=hashlib.sha256(proof_bytes).hexdigest(),
            stored=stored,
        )

    def stored_archive_object_sha256(
        self,
        *,
        collection_id: int,
        object: ArchiveObjectIdentity,
    ) -> str:
        digest = hashlib.sha256()
        received = 0
        for chunk in self.iter_stored_archive_object(
            collection_id=collection_id,
            object=object,
        ):
            digest.update(chunk)
            received += len(chunk)
        if received != object.stored_bytes:
            raise RuntimeError(f"archive object byte count changed: {object.object_path}")
        actual = digest.hexdigest()
        if object.stored_sha256 is not None and actual != object.stored_sha256:
            raise RuntimeError(f"archive object SHA-256 changed: {object.object_path}")
        return actual

    def publish_archive_attestation(
        self,
        *,
        collection_id: int,
        archive_storage_prefix: str,
        checksums: bytes,
        signature: bytes,
        proof: bytes,
    ) -> CollectionArchiveUploadReceipt:
        self.storage_execution_evidence()
        _ = collection_id
        self._put_archive_root_guidance()
        rows = (
            ("checksums", checksums),
            ("signature", signature),
            ("signature-proof", proof),
        )
        return CollectionArchiveUploadReceipt(
            objects=tuple(
                self._put_plaintext_attestation(
                    object_id=object_id,
                    object_path=(
                        f"{_archive_prefix(archive_storage_prefix)}/"
                        f"{ATTESTATION_FILENAMES[object_id]}"
                    ),
                    content=content,
                    prior_revision=None,
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
        _ = collection_id
        if object.kind not in ATTESTATION_OBJECT_KINDS:
            raise ValueError("archive artifact is not an attestation artifact")
        content = b"".join(
            self.iter_stored_archive_object(collection_id=collection_id, object=object)
        )
        _verify_stored(object, content)
        return ArchiveArtifactRead(receipt=self._receipt(object, verified=True), content=content)

    def replace_archive_attestation_proof(
        self,
        *,
        collection_id: int,
        object: ArchiveObjectIdentity,
        proof_bytes: bytes,
    ) -> ArchiveObjectUploadReceipt:
        self.storage_execution_evidence()
        _ = collection_id
        if object.object_id != "signature-proof" or object.kind != "signature-proof":
            raise ValueError("archive attestation proof replacement requires its proof object")
        return self._put_plaintext_attestation(
            object_id="signature-proof",
            object_path=object.object_path,
            content=proof_bytes,
            prior_revision=object.revision,
        )

    def prepare_archive_objects_read(
        self,
        *,
        collection_id: int,
        objects: Sequence[ArchiveObjectIdentity],
    ) -> ArchiveReadStatus:
        _ = collection_id
        status = self._runtime.client.prepare_read(_read_request(objects))
        return ArchiveReadStatus(
            state=status.state,
            ready_at=status.ready_at,
            expires_at=status.expires_at,
            message=status.message,
        )

    def get_archive_objects_read_status(
        self,
        *,
        collection_id: int,
        objects: Sequence[ArchiveObjectIdentity],
    ) -> ArchiveReadStatus:
        _ = collection_id
        status = self._runtime.client.read_status(_read_request(objects))
        return ArchiveReadStatus(
            state=status.state,
            ready_at=status.ready_at,
            expires_at=status.expires_at,
            message=status.message,
        )

    def iter_archive_object(
        self,
        *,
        collection_id: int,
        object: ArchiveObjectIdentity,
        attribution: DownloadAttribution | None = None,
    ) -> Iterator[bytes]:
        return iter_decrypt_age_scrypt(
            self.iter_stored_archive_object(
                collection_id=collection_id,
                object=object,
                attribution=attribution,
            ),
            self._config.archive_passphrase,
        )

    def iter_stored_archive_object(
        self,
        *,
        collection_id: int,
        object: ArchiveObjectIdentity,
        attribution: DownloadAttribution | None = None,
    ) -> Iterator[bytes]:
        _ = collection_id
        metadata = self._objects.object_metadata(
            object_path=object.object_path,
            revision=object.revision,
        )
        if (
            metadata.stored_bytes != object.stored_bytes
            or object.stored_sha256 is None
            or metadata.stored_sha256 != object.stored_sha256
        ):
            raise RuntimeError(f"archive object receipt changed: {object.object_path}")
        status = self._runtime.client.read_status(_read_request((object,)))
        if status.state != "ready":
            raise RuntimeError(f"archive object is not readable: {object.object_path}")
        content = self._objects.iter_object(
            object_path=object.object_path,
            revision=object.revision,
        )
        if self._download_allowance is None:
            return content
        return self._download_allowance.track(
            store=self._store.name,
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
        self._runtime.client.cleanup_read(_read_request(objects))

    def _put_plaintext_attestation(
        self,
        *,
        object_id: str,
        object_path: str,
        content: bytes,
        prior_revision: str | None,
    ) -> ArchiveObjectUploadReceipt:
        sha256 = hashlib.sha256(content).hexdigest()
        stored = self._objects.put_object(
            object_path=object_path,
            content=content,
            content_type="application/octet-stream",
            identity_metadata={
                "riverhog-format": archive_object_storage_format(object_id),
                "riverhog-plaintext-bytes": str(len(content)),
                "riverhog-plaintext-sha256": sha256,
            },
            prior_revision=prior_revision,
        )
        return self._receipt_from_stored(
            object_id=object_id,
            kind=object_id,
            plaintext_bytes=len(content),
            plaintext_sha256=sha256,
            stored=stored,
        )

    def _put_archive_root_guidance(self) -> None:
        content = (
            b"Riverhog v1 archive root. Preserve relative paths and use the exact "
            b"riverhog-recover release artifact for independent recovery.\n"
        )
        self._objects.put_immutable_object(
            object_path="README.txt",
            content=content,
            content_type="text/plain; charset=utf-8",
            identity_metadata={
                "riverhog-format": "riverhog-archive-guidance/v1",
                "riverhog-stored-sha256": hashlib.sha256(content).hexdigest(),
            },
        )

    def _receipt(
        self,
        object: ArchiveObjectIdentity,
        *,
        verified: bool,
    ) -> ArchiveObjectUploadReceipt:
        evidence = self.storage_execution_evidence()
        return ArchiveObjectUploadReceipt(
            object_id=object.object_id,
            kind=object.kind,
            object_path=object.object_path,
            plaintext_bytes=object.plaintext_bytes,
            stored_bytes=object.stored_bytes,
            sha256=object.sha256,
            stored_sha256=object.stored_sha256,
            revision=object.revision,
            storage_adapter=evidence.storage_adapter,
            storage_profile_id=evidence.storage_profile_id,
            storage_profile_contract_sha256=evidence.storage_profile_contract_sha256,
            egress_accounting_id=evidence.egress_accounting_id,
            adapter_implementation_id=evidence.adapter_implementation_id,
            adapter_implementation_version=evidence.adapter_implementation_version,
            adapter_source_revision=evidence.adapter_source_revision,
            adapter_runtime_descriptor_sha256=(
                evidence.adapter_runtime_descriptor_sha256
            ),
            read_mode=evidence.read_mode,
            uploaded_at="",
            verified_at=(datetime.now().astimezone().isoformat() if verified else None),
        )

    def _receipt_from_stored(
        self,
        *,
        object_id: str,
        kind: str,
        plaintext_bytes: int,
        plaintext_sha256: str,
        stored: ImmutableObjectReceipt,
    ) -> ArchiveObjectUploadReceipt:
        evidence = self.storage_execution_evidence()
        return ArchiveObjectUploadReceipt(
            object_id=object_id,
            kind=kind,
            object_path=stored.object_path,
            plaintext_bytes=plaintext_bytes,
            stored_bytes=stored.stored_bytes,
            sha256=plaintext_sha256,
            stored_sha256=stored.stored_sha256,
            revision=stored.revision,
            storage_adapter=evidence.storage_adapter,
            storage_profile_id=evidence.storage_profile_id,
            storage_profile_contract_sha256=evidence.storage_profile_contract_sha256,
            egress_accounting_id=evidence.egress_accounting_id,
            adapter_implementation_id=evidence.adapter_implementation_id,
            adapter_implementation_version=evidence.adapter_implementation_version,
            adapter_source_revision=evidence.adapter_source_revision,
            adapter_runtime_descriptor_sha256=(
                evidence.adapter_runtime_descriptor_sha256
            ),
            read_mode=evidence.read_mode,
            uploaded_at=stored.completed_at,
            verified_at=stored.completed_at,
        )

def _read_request(objects: Sequence[ArchiveObjectIdentity]) -> ReadRequest:
    locators = tuple(
        sorted(
            (
                ObjectLocator(object_path=current.object_path, revision=current.revision)
                for current in objects
            ),
            key=lambda current: (current.object_path, current.revision),
        )
    )
    if not locators:
        raise ValueError("archive read requires at least one object")
    return ReadRequest(objects=locators)


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
