from __future__ import annotations

import hashlib
import secrets
from collections.abc import Iterator, Sequence
from typing import Literal

from riverhog_age import encrypt_age_scrypt, iter_decrypt_age_scrypt
from riverhog_protocol import (
    COLLECTION_DESCRIPTION_DOCUMENT_FORMAT,
    COLLECTION_DESCRIPTION_RELATIVE_PATH,
    COLLECTION_TAG_HEAD_FORMAT,
    COLLECTION_TAG_HEAD_RELATIVE_PATH,
    COLLECTION_TAG_NODE_FORMAT,
    CollectionDescriptionDocument,
    CollectionTagHeadDocument,
    collection_tag_node_digest,
    collection_tag_node_path,
    decode_collection_tag_node,
)
from riverhog_storage_adapter_protocol import (
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
    validated_storage_adapter,
)
from time_formats import utc_timestamp_now

from riverhog_core.archive_formats import archive_object_storage_format
from riverhog_core.archive_safety import archive_agents_guidance, archive_recovery_readme
from riverhog_core.ports.archive_store import (
    ArchiveArtifactRead,
    ArchiveObjectIdentity,
    ArchiveObjectUploadReceipt,
    ArchiveReadStatus,
    ArchiveVerificationError,
    CollectionArchiveIdentity,
    CollectionDescriptionReceipt,
    CollectionTagObjectReceipt,
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
        self._adapter = validated_storage_adapter(adapter)
        self._descriptor = self._adapter.descriptor()
        self._download_allowance = download_allowance

    @property
    def descriptor(self) -> AdapterDescriptor:
        return self._descriptor

    def read_mode(self) -> str:
        return self._descriptor.read_mode

    def new_collection_archive_storage_prefix(self) -> str:
        return f"archives/{secrets.token_hex(_OPAQUE_ARCHIVE_ID_BYTES)}"

    def discard_collection_archive_upload(self, *, archive_storage_prefix: str) -> None:
        prefix = _archive_prefix(archive_storage_prefix)
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

    def publish_collection_description(
        self,
        *,
        collection_id: int,
        archive_storage_prefix: str,
        document: bytes,
        passphrase_id: str,
        expected_current_stored_sha256: str | None = None,
    ) -> CollectionDescriptionReceipt:
        description = CollectionDescriptionDocument.from_json_bytes(document)
        object_path = (
            f"{_archive_prefix(archive_storage_prefix)}/{COLLECTION_DESCRIPTION_RELATIVE_PATH}"
        )
        plaintext_sha256 = hashlib.sha256(document).hexdigest()
        identity = {
            "riverhog-format": COLLECTION_DESCRIPTION_DOCUMENT_FORMAT,
            "riverhog-archive-root-sha256": description.archive_root_sha256,
            "riverhog-description-identity": description.description_identity,
            "riverhog-description-revision": str(description.revision),
            "riverhog-encryption": "age-v1-scrypt",
            "riverhog-passphrase-id": passphrase_id,
            _PLAINTEXT_BYTES_METADATA: str(len(document)),
            _PLAINTEXT_SHA256_METADATA: plaintext_sha256,
        }
        existing = self._head(
            object_path=object_path,
            revision=None,
            placement="immediate",
        )
        if existing is not None and _metadata_contains(existing, identity):
            return CollectionDescriptionReceipt(
                object_path=existing.object_path,
                revision=existing.revision,
                stored_bytes=existing.stored_bytes,
                stored_sha256=_required_stored_sha256(existing, object_path=object_path),
                published_at=existing.completed_at,
            )
        ciphertext = encrypt_age_scrypt(
            document,
            self._config.archive_passphrase_for(passphrase_id),
            log_n=self._config.archive_scrypt_work_factor,
        )
        receipt = self._put_small(
            object_path=object_path,
            content=ciphertext,
            content_type="application/vnd.riverhog.collection-description.v1+age",
            identity=identity,
            mode=("create_only" if expected_current_stored_sha256 is None else "replace_current"),
            expected_current_stored_sha256=expected_current_stored_sha256,
        )
        return CollectionDescriptionReceipt(
            object_path=receipt.object_path,
            revision=receipt.revision,
            stored_bytes=receipt.stored_bytes,
            stored_sha256=receipt.stored_sha256,
            published_at=receipt.completed_at,
        )

    def delete_collection_description(
        self,
        *,
        collection_id: int,
        archive_storage_prefix: str,
    ) -> None:
        _ = collection_id
        object_path = (
            f"{_archive_prefix(archive_storage_prefix)}/{COLLECTION_DESCRIPTION_RELATIVE_PATH}"
        )
        self._adapter.delete_object(
            DeleteObjectRequest(
                object=ObjectLocator(object_path=object_path),
                mode="all_versions",
            )
        )
        if self._head(object_path=object_path, revision=None, placement="immediate") is not None:
            raise RuntimeError("collection description deletion could not be verified")

    def publish_collection_tag_node(
        self,
        *,
        collection_id: int,
        archive_storage_prefix: str,
        digest: str,
        encoded: bytes,
        passphrase_id: str,
    ) -> CollectionTagObjectReceipt:
        _ = collection_id
        decode_collection_tag_node(encoded)
        if collection_tag_node_digest(encoded) != digest:
            raise ValueError("collection tag node digest differs")
        relative_path = collection_tag_node_path(digest)
        object_path = f"{_archive_prefix(archive_storage_prefix)}/{relative_path}"
        identity = {
            "riverhog-format": COLLECTION_TAG_NODE_FORMAT,
            "riverhog-tag-node-sha256": digest,
            "riverhog-encryption": "age-v1-scrypt",
            "riverhog-passphrase-id": passphrase_id,
            _PLAINTEXT_BYTES_METADATA: str(len(encoded)),
            _PLAINTEXT_SHA256_METADATA: hashlib.sha256(encoded).hexdigest(),
        }
        existing = self._head(object_path=object_path, revision=None, placement="immediate")
        if existing is not None and _metadata_contains(existing, identity):
            return _tag_receipt(existing)
        ciphertext = encrypt_age_scrypt(
            encoded,
            self._config.archive_passphrase_for(passphrase_id),
            log_n=self._config.archive_scrypt_work_factor,
        )
        receipt = self._put_small(
            object_path=object_path,
            content=ciphertext,
            content_type="application/vnd.riverhog.collection-tag-node.v1+age",
            identity=identity,
            mode="create_only",
        )
        return _tag_receipt(receipt)

    def publish_collection_tag_head(
        self,
        *,
        collection_id: int,
        archive_storage_prefix: str,
        document: bytes,
        passphrase_id: str,
        expected_current_stored_sha256: str | None = None,
    ) -> CollectionTagObjectReceipt:
        _ = collection_id
        head = CollectionTagHeadDocument.from_json_bytes(document)
        object_path = (
            f"{_archive_prefix(archive_storage_prefix)}/{COLLECTION_TAG_HEAD_RELATIVE_PATH}"
        )
        identity = {
            "riverhog-format": COLLECTION_TAG_HEAD_FORMAT,
            "riverhog-archive-root-sha256": head.archive_root_sha256,
            "riverhog-tag-head-identity": head.head_identity,
            "riverhog-tag-set-identity": head.tag_set_identity,
            "riverhog-tag-revision": str(head.revision),
            "riverhog-encryption": "age-v1-scrypt",
            "riverhog-passphrase-id": passphrase_id,
            _PLAINTEXT_BYTES_METADATA: str(len(document)),
            _PLAINTEXT_SHA256_METADATA: hashlib.sha256(document).hexdigest(),
        }
        existing = self._head(object_path=object_path, revision=None, placement="immediate")
        if existing is not None and _metadata_contains(existing, identity):
            return _tag_receipt(existing)
        ciphertext = encrypt_age_scrypt(
            document,
            self._config.archive_passphrase_for(passphrase_id),
            log_n=self._config.archive_scrypt_work_factor,
        )
        receipt = self._put_small(
            object_path=object_path,
            content=ciphertext,
            content_type="application/vnd.riverhog.collection-tag-head.v1+age",
            identity=identity,
            mode=("create_only" if expected_current_stored_sha256 is None else "replace_current"),
            expected_current_stored_sha256=expected_current_stored_sha256,
        )
        return _tag_receipt(receipt)

    def delete_collection_tags(
        self,
        *,
        collection_id: int,
        archive_storage_prefix: str,
    ) -> None:
        _ = collection_id
        prefix = _archive_prefix(archive_storage_prefix)
        for relative_path in (COLLECTION_TAG_HEAD_RELATIVE_PATH, "tags/nodes"):
            object_prefix = f"{prefix}/{relative_path}"
            self._adapter.delete_prefix(DeletePrefixRequest(object_prefix=object_prefix))

    def delete_collection_tag_node(
        self,
        *,
        collection_id: int,
        archive_storage_prefix: str,
        digest: str,
        expected_current_stored_sha256: str,
        provider_revision: str | None,
    ) -> None:
        _ = collection_id
        object_path = (
            f"{_archive_prefix(archive_storage_prefix)}/{collection_tag_node_path(digest)}"
        )
        self._adapter.delete_object(
            DeleteObjectRequest(
                object=ObjectLocator(object_path=object_path),
                mode="current",
                expected_current_stored_sha256=expected_current_stored_sha256,
            )
        )
        if self._head(object_path=object_path, revision=None, placement="immediate") is not None:
            raise RuntimeError("collection tag-node deletion could not be verified")
        if provider_revision is None:
            return
        self._adapter.delete_object(
            DeleteObjectRequest(
                object=ObjectLocator(
                    object_path=object_path,
                    revision=provider_revision,
                ),
                mode="exact_revision",
            )
        )
        if (
            self._head(
                object_path=object_path,
                revision=provider_revision,
                placement="immediate",
            )
            is not None
        ):
            raise RuntimeError("collection tag-node provider revision was not reclaimed")

    def read_archive_artifact(
        self,
        *,
        collection_id: int,
        object: ArchiveObjectIdentity,
        passphrase_id: str,
    ) -> ArchiveArtifactRead:
        if object.kind != "manifest":
            raise ValueError("only collection archive roots are archive artifacts")
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

    def prepare_archive_objects_read(
        self,
        *,
        collection_id: int,
        objects: Sequence[ArchiveObjectIdentity],
    ) -> ArchiveReadStatus:
        _ = collection_id
        status = self._adapter.prepare_read(_read_request(objects))
        readiness = status.readiness
        return ArchiveReadStatus(
            state=readiness.state,
            ready_at=(readiness.estimated_ready_at if readiness.state == "requested" else None),
            expires_at=(readiness.available_until if readiness.state == "ready" else None),
        )

    def get_archive_objects_read_status(
        self,
        *,
        collection_id: int,
        objects: Sequence[ArchiveObjectIdentity],
    ) -> ArchiveReadStatus:
        _ = collection_id
        status = self._adapter.read_status(_read_request(objects))
        readiness = status.readiness
        return ArchiveReadStatus(
            state=readiness.state,
            ready_at=(readiness.estimated_ready_at if readiness.state == "requested" else None),
            expires_at=(readiness.available_until if readiness.state == "ready" else None),
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
        if status.readiness.state != "ready":
            raise RuntimeError(f"Archive object is not readable yet: {object.object_path}")
        content = self._adapter.read_object(
            ObjectReadRequest(
                object=ObjectLocator(
                    object_path=object.object_path,
                    revision=object.revision,
                ),
                expected_bytes=object.stored_bytes,
            )
        ).content
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
        expected_current_stored_sha256: str | None = None,
    ) -> ImmutableObjectReceipt:
        return self._adapter.put_small_object(
            SmallObjectWriteRequest(
                object_path=object_path,
                content_type=content_type,
                required_identity_assertions=identity,
                placement="immediate",
                mode=mode,
                expected_current_stored_sha256=expected_current_stored_sha256,
                stored_bytes=len(content),
                stored_sha256=hashlib.sha256(content).hexdigest(),
            ),
            content,
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
            stored_sha256=(metadata.stored_sha256),
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
        raise RuntimeError(
            "archive object required identity assertions differs from its durable identity"
        )


def _metadata_contains(receipt: ObjectMetadataReceipt, expected: dict[str, str]) -> bool:
    return all(
        receipt.observed_identity_assertions.get(key) == value for key, value in expected.items()
    )


def _required_stored_sha256(
    receipt: ObjectMetadataReceipt | ImmutableObjectReceipt, *, object_path: str
) -> str:
    value = receipt.stored_sha256
    if (
        value is None
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise RuntimeError(f"archive object is missing its stored sha256: {object_path}")
    return value


def _tag_receipt(
    receipt: ObjectMetadataReceipt | ImmutableObjectReceipt,
) -> CollectionTagObjectReceipt:
    return CollectionTagObjectReceipt(
        object_path=receipt.object_path,
        revision=receipt.revision,
        stored_bytes=receipt.stored_bytes,
        stored_sha256=_required_stored_sha256(receipt, object_path=receipt.object_path),
        published_at=receipt.completed_at,
    )


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
