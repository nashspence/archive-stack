from __future__ import annotations

import hashlib
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime

from riverhog_age import decrypt_age_scrypt
from riverhog_core.ports.archive_store import (
    ArchiveObjectIdentity,
    CollectionArchiveIdentity,
)
from riverhog_core.runtime_config import RuntimeConfig
from riverhog_core.stores.storage_adapter_archive_store import StorageAdapterArchiveStore
from riverhog_storage_adapter_protocol import (
    AbortIncompleteUploadsRequest,
    AdapterDescriptor,
    CompletedObjectReceipt,
    DeleteObjectRequest,
    DeletePrefixRequest,
    ImmutableObjectReceipt,
    MultipartCompleteRequest,
    MultipartCreateRequest,
    MultipartHeadRequest,
    MultipartPartReceipt,
    MultipartUpload,
    ObjectHeadRequest,
    ObjectMetadataReceipt,
    ObjectReadRequest,
    ReadPreparationRequest,
    ReadStatus,
    SmallObjectWriteRequest,
    StorageAdapterRejection,
)


@dataclass
class _Stored:
    content: bytes
    identity: dict[str, str]
    placement: str
    revision: str
    completed_at: str = "2026-08-21T00:00:00Z"


class _MemoryAdapter:
    def __init__(self, *, read_mode: str = "immediate") -> None:
        self.read_mode = read_mode
        self.objects: dict[str, _Stored] = {}
        self.reads = 0
        self.preparations: list[ReadPreparationRequest] = []
        self.deleted: list[DeleteObjectRequest] = []
        self.aborted: list[AbortIncompleteUploadsRequest] = []

    def descriptor(self) -> AdapterDescriptor:
        return AdapterDescriptor(
            implementation_id="fixture.storage/v1",
            implementation_version="1.0.0",
            read_mode=self.read_mode,  # type: ignore[arg-type]
            minimum_nonfinal_part_bytes=1,
            maximum_part_bytes=1024 * 1024,
            maximum_part_count=10_000,
        )

    def put_small_object(
        self,
        request: SmallObjectWriteRequest,
        content: bytes,
    ) -> ImmutableObjectReceipt:
        existing = self.objects.get(request.object_path)
        if existing is not None and existing.identity == request.identity_metadata:
            return self._immutable(request.object_path, existing)
        if existing is not None and request.mode == "create_only":
            raise StorageAdapterRejection("identity_conflict", "different identity")
        revision = f"revision-{len(self.objects) + 1}"
        stored = _Stored(
            content=content,
            identity=dict(request.identity_metadata),
            placement=request.placement,
            revision=revision,
        )
        self.objects[request.object_path] = stored
        return self._immutable(request.object_path, stored)

    def head_object(self, request: ObjectHeadRequest) -> ObjectMetadataReceipt | None:
        stored = self.objects.get(request.object.object_path)
        if stored is None:
            return None
        assert stored.placement == request.expected_placement
        if request.object.revision is not None:
            assert stored.revision == request.object.revision
        return ObjectMetadataReceipt(
            object_path=request.object.object_path,
            revision=stored.revision,
            entity_token=f"entity-{stored.revision}",
            content_type="application/octet-stream",
            stored_bytes=len(stored.content),
            stored_sha256=hashlib.sha256(stored.content).hexdigest(),
            identity_metadata=stored.identity,
            completed_at=stored.completed_at,
        )

    def iter_object(self, request: ObjectReadRequest) -> Iterator[bytes]:
        self.reads += 1
        stored = self.objects[request.object.object_path]
        assert len(stored.content) == request.expected_bytes
        content = stored.content
        if request.offset is not None and request.size is not None:
            content = content[request.offset : request.offset + request.size]
        yield content

    def prepare_read(self, request: ReadPreparationRequest) -> ReadStatus:
        self.preparations.append(request)
        return ReadStatus(state="requested" if self.read_mode == "restore_required" else "ready")

    def read_status(self, request: ReadPreparationRequest) -> ReadStatus:
        self.preparations.append(request)
        return ReadStatus(state="ready")

    def cleanup_read(self, request: ReadPreparationRequest) -> None:
        self.preparations.append(request)

    def delete_object(self, request: DeleteObjectRequest) -> None:
        self.deleted.append(request)
        self.objects.pop(request.object.object_path, None)

    def delete_prefix(self, request: DeletePrefixRequest) -> int:
        paths = [path for path in self.objects if path.startswith(request.object_prefix)]
        for path in paths:
            del self.objects[path]
        return len(paths)

    def abort_incomplete_uploads(self, request: AbortIncompleteUploadsRequest) -> int:
        self.aborted.append(request)
        return 0

    def create_multipart_upload(self, request: MultipartCreateRequest) -> MultipartUpload:
        raise NotImplementedError(request)

    def upload_part(
        self,
        *,
        upload: MultipartUpload,
        number: int,
        content: bytes,
    ) -> MultipartPartReceipt:
        raise NotImplementedError(upload, number, content)

    def list_parts(self, upload: MultipartUpload) -> tuple[MultipartPartReceipt, ...]:
        raise NotImplementedError(upload)

    def complete_multipart_upload(
        self,
        request: MultipartCompleteRequest,
    ) -> CompletedObjectReceipt:
        raise NotImplementedError(request)

    def head_completed_object(
        self,
        request: MultipartHeadRequest,
    ) -> CompletedObjectReceipt | None:
        raise NotImplementedError(request)

    def abort_multipart_upload(self, upload: MultipartUpload) -> None:
        raise NotImplementedError(upload)

    def _immutable(self, object_path: str, stored: _Stored) -> ImmutableObjectReceipt:
        return ImmutableObjectReceipt(
            object_path=object_path,
            revision=stored.revision,
            entity_token=f"entity-{stored.revision}",
            stored_bytes=len(stored.content),
            stored_sha256=hashlib.sha256(stored.content).hexdigest(),
            completed_at=stored.completed_at,
        )


def _store(adapter: _MemoryAdapter) -> StorageAdapterArchiveStore:
    return StorageAdapterArchiveStore(
        RuntimeConfig(),
        name="primary",
        adapter=adapter,
    )


def test_metadata_publication_keeps_archive_semantics_in_riverhog() -> None:
    adapter = _MemoryAdapter()
    store = _store(adapter)
    manifest = b'{"format":"fixture/v1"}'
    published = store.publish_collection_metadata(
        collection_id=17,
        archive_storage_prefix="archives/opaque",
        manifest=manifest,
    )
    stored = adapter.objects[published.object_path]

    assert decrypt_age_scrypt(stored.content, RuntimeConfig().archive_passphrase) == manifest
    assert stored.placement == "immediate"
    assert store.read_mode() == "immediate"
    assert store.new_collection_archive_storage_prefix().startswith("archives/")
    assert not store.new_collection_archive_storage_prefix().startswith("archive/archives/")
    assert set(adapter.objects) == {
        "AGENTS.md",
        "README.md",
        "archives/opaque/metadata.json.age",
    }
    assert adapter.objects["README.md"].placement == "immediate"
    assert adapter.objects["AGENTS.md"].placement == "immediate"


def test_encrypted_archive_artifact_write_and_read_stay_in_riverhog() -> None:
    adapter = _MemoryAdapter()
    store = _store(adapter)
    proof_path = "archives/opaque/manifest.json.ots.age"
    previous = ArchiveObjectIdentity(
        object_id="proof",
        kind="proof",
        object_path=proof_path,
        plaintext_bytes=0,
        stored_bytes=0,
        sha256=hashlib.sha256(b"").hexdigest(),
        stored_sha256=None,
        version_id=None,
    )

    receipt = store.replace_archive_proof(
        collection_id=17,
        object=previous,
        proof_bytes=b"proof-bytes",
    )
    identity = ArchiveObjectIdentity(
        object_id=receipt.object_id,
        kind=receipt.kind,
        object_path=receipt.object_path,
        plaintext_bytes=receipt.plaintext_bytes,
        stored_bytes=receipt.stored_bytes,
        sha256=receipt.sha256,
        stored_sha256=receipt.stored_sha256,
        version_id=receipt.version_id,
    )

    artifact = store.read_archive_artifact(collection_id=17, object=identity)

    assert artifact.content == b"proof-bytes"
    assert adapter.objects[proof_path].placement == "immediate"


def test_verification_is_metadata_only_and_explicit_hashing_reads_once() -> None:
    adapter = _MemoryAdapter()
    content = b"opaque-encrypted-volume"
    path = "archives/opaque/volumes/pack-000000000000.tar.age"
    adapter.objects[path] = _Stored(
        content=content,
        identity={
            "riverhog-format": "riverhog-pack-volume/v1",
            "riverhog-plaintext-bytes": "1024",
            "riverhog-plaintext-sha256": "a" * 64,
        },
        placement="archive",
        revision="version-pack",
    )
    identity = ArchiveObjectIdentity(
        object_id="pack-000000000000",
        kind="pack",
        object_path=path,
        plaintext_bytes=1024,
        stored_bytes=len(content),
        sha256="a" * 64,
        stored_sha256=hashlib.sha256(content).hexdigest(),
        version_id="version-pack",
    )
    store = _store(adapter)

    store.verify_collection_archive(
        collection_id=17,
        archive=CollectionArchiveIdentity(objects=(identity,)),
    )
    assert adapter.reads == 0

    assert store.stored_archive_object_sha256(collection_id=17, object=identity) == (
        hashlib.sha256(content).hexdigest()
    )
    assert adapter.reads == 1


def test_read_preparation_carries_only_exact_opaque_objects() -> None:
    adapter = _MemoryAdapter(read_mode="restore_required")
    store = _store(adapter)
    identity = ArchiveObjectIdentity(
        object_id="segment-000000000000",
        kind="segment",
        object_path="archives/opaque/volumes/segment-000000000000.bin.age",
        plaintext_bytes=1,
        stored_bytes=2,
        sha256="a" * 64,
        stored_sha256="b" * 64,
        version_id="version-segment",
    )

    assert (
        store.prepare_archive_objects_read(
            collection_id=17,
            objects=(identity,),
        ).state
        == "requested"
    )

    payload = adapter.preparations[0].model_dump(mode="json")
    assert payload == {
        "objects": [
            {
                "object_path": identity.object_path,
                "revision": identity.version_id,
            }
        ]
    }
    assert "tier" not in str(payload).casefold()
    assert "hold" not in str(payload).casefold()


def test_deletion_uses_all_versions_and_verifies_current_absence() -> None:
    adapter = _MemoryAdapter()
    path = "archives/opaque/manifest.json.age"
    content = b"ciphertext"
    adapter.objects[path] = _Stored(
        content=content,
        identity={
            "riverhog-format": "riverhog-collection-manifest/v1",
            "riverhog-plaintext-bytes": "1",
            "riverhog-plaintext-sha256": "a" * 64,
        },
        placement="immediate",
        revision="version-manifest",
    )
    identity = ArchiveObjectIdentity(
        object_id="manifest",
        kind="manifest",
        object_path=path,
        plaintext_bytes=1,
        stored_bytes=len(content),
        sha256="a" * 64,
        stored_sha256=hashlib.sha256(content).hexdigest(),
        version_id="version-manifest",
    )

    _store(adapter).delete_collection_archive(collection_id=17, objects=(identity,))

    assert len(adapter.deleted) == 1
    assert adapter.deleted[0].mode == "all_versions"


def test_plaintext_attestations_round_trip_and_the_proof_is_replaceable() -> None:
    adapter = _MemoryAdapter()
    store = _store(adapter)

    published = store.publish_archive_attestation(
        collection_id=17,
        archive_storage_prefix="archives/opaque",
        checksums=b"checksums",
        signature=b"signature",
        proof=b"proof",
    )
    signature = published.require_object("signature")
    signature_identity = ArchiveObjectIdentity(
        object_id=signature.object_id,
        kind=signature.kind,
        object_path=signature.object_path,
        plaintext_bytes=signature.plaintext_bytes,
        stored_bytes=signature.stored_bytes,
        sha256=signature.sha256,
        stored_sha256=signature.stored_sha256,
        version_id=signature.version_id,
    )
    proof = published.require_object("signature-proof")
    proof_identity = ArchiveObjectIdentity(
        object_id=proof.object_id,
        kind=proof.kind,
        object_path=proof.object_path,
        plaintext_bytes=proof.plaintext_bytes,
        stored_bytes=proof.stored_bytes,
        sha256=proof.sha256,
        stored_sha256=proof.stored_sha256,
        version_id=proof.version_id,
    )

    assert (
        store.read_archive_attestation_artifact(
            collection_id=17,
            object=signature_identity,
        ).content
        == b"signature"
    )
    replaced = store.replace_archive_attestation_proof(
        collection_id=17,
        object=proof_identity,
        proof_bytes=b"mature-proof",
    )
    assert adapter.objects[replaced.object_path].content == b"mature-proof"
    assert adapter.objects[replaced.object_path].placement == "immediate"


def test_incomplete_sweep_and_discard_stay_scoped_to_the_archive_namespace() -> None:
    adapter = _MemoryAdapter()
    store = _store(adapter)
    adapter.objects["archives/opaque/partial"] = _Stored(
        content=b"partial",
        identity={},
        placement="archive",
        revision="partial-version",
    )

    assert (
        store.abort_incomplete_multipart_uploads(
            initiated_before=datetime(2026, 8, 21, tzinfo=UTC),
        )
        == 0
    )
    store.discard_collection_archive_upload(archive_storage_prefix="archives/opaque")

    assert adapter.aborted[0].object_prefix == "archives/"
    assert adapter.aborted[1].object_prefix == "archives/opaque/"
    assert "archives/opaque/partial" not in adapter.objects
