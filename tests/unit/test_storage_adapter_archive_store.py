from __future__ import annotations

import hashlib
from dataclasses import dataclass

from riverhog_core.ports.archive_store import (
    ArchiveObjectIdentity,
    CollectionArchiveIdentity,
)
from riverhog_core.runtime_config import RuntimeConfig
from riverhog_core.stores.storage_adapter_archive_store import StorageAdapterArchiveStore
from riverhog_storage_adapter_protocol import (
    AdapterDescriptor,
    CompletedObjectReceipt,
    CompletedWriteLookupRequest,
    DeleteObjectRequest,
    DeletePrefixRequest,
    ImmutableObjectReceipt,
    ObjectHeadRequest,
    ObjectLocator,
    ObjectMetadataReceipt,
    ObjectReadReceipt,
    ObjectReadRequest,
    ObjectReadStream,
    ReadPreparationRequest,
    ReadReady,
    ReadRequested,
    ReadStatus,
    SmallObjectWriteRequest,
    StorageAdapterRejection,
    WriteCompleteRequest,
    WriteSegmentReceipt,
    WriteSegmentSet,
    WriteSession,
    WriteStartRequest,
)


@dataclass
class _Stored:
    content: bytes
    content_type: str
    identity: dict[str, str]
    placement: str
    revision: str
    completed_at: str = "2026-08-21T00:00:00.000000Z"


class _MemoryAdapter:
    def __init__(self, *, read_mode: str = "immediate") -> None:
        self.read_mode = read_mode
        self.objects: dict[str, _Stored] = {}
        self.reads = 0
        self.preparations: list[ReadPreparationRequest] = []
        self.deleted: list[DeleteObjectRequest] = []

    def descriptor(self) -> AdapterDescriptor:
        return AdapterDescriptor(
            implementation_id="fixture.storage/v1",
            implementation_version="1.0.0",
            read_mode=self.read_mode,  # type: ignore[arg-type]
            minimum_nonfinal_segment_bytes=1,
            maximum_segment_bytes=1024 * 1024,
            maximum_segment_count=10_000,
        )

    def put_small_object(
        self,
        request: SmallObjectWriteRequest,
        content: bytes,
    ) -> ImmutableObjectReceipt:
        existing = self.objects.get(request.object_path)
        if (
            existing is not None
            and existing.content_type == request.content_type
            and existing.identity == request.required_identity_assertions
        ):
            return self._immutable(request.object_path, existing)
        if existing is not None and request.mode == "create_only":
            raise StorageAdapterRejection("identity_conflict", "different identity")
        revision = f"revision-{len(self.objects) + 1}"
        stored = _Stored(
            content=content,
            content_type=request.content_type,
            identity=dict(request.required_identity_assertions),
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
            content_type=stored.content_type,
            stored_bytes=len(stored.content),
            stored_sha256=hashlib.sha256(stored.content).hexdigest(),
            observed_identity_assertions=stored.identity,
            verified_placement=request.expected_placement,
            completed_at=stored.completed_at,
        )

    def read_object(self, request: ObjectReadRequest) -> ObjectReadStream:
        self.reads += 1
        stored = self.objects[request.object.object_path]
        assert len(stored.content) == request.expected_bytes
        content = stored.content
        if request.offset is not None and request.size is not None:
            content = content[request.offset : request.offset + request.size]
        return ObjectReadStream(
            receipt=ObjectReadReceipt(
                object=ObjectLocator(
                    object_path=request.object.object_path,
                    revision=stored.revision,
                ),
                total_bytes=len(stored.content),
                offset=request.offset or 0,
                read_bytes=len(content),
            ),
            content=iter((content,)) if content else iter(()),
        )

    def prepare_read(self, request: ReadPreparationRequest) -> ReadStatus:
        self.preparations.append(request)
        readiness = ReadRequested() if self.read_mode == "restore_required" else ReadReady()
        return ReadStatus(objects=request.objects, readiness=readiness)

    def read_status(self, request: ReadPreparationRequest) -> ReadStatus:
        self.preparations.append(request)
        return ReadStatus(objects=request.objects, readiness=ReadReady())

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

    def begin_write(self, request: WriteStartRequest) -> WriteSession:
        raise NotImplementedError(request)

    def write_segment(
        self,
        *,
        upload: WriteSession,
        number: int,
        content: bytes,
    ) -> WriteSegmentReceipt:
        raise NotImplementedError(upload, number, content)

    def list_segments(self, upload: WriteSession) -> WriteSegmentSet:
        raise NotImplementedError(upload)

    def complete_write(
        self,
        request: WriteCompleteRequest,
    ) -> CompletedObjectReceipt:
        raise NotImplementedError(request)

    def find_completed_write(
        self,
        request: CompletedWriteLookupRequest,
    ) -> CompletedObjectReceipt | None:
        raise NotImplementedError(request)

    def abort_write(self, upload: WriteSession) -> None:
        raise NotImplementedError(upload)

    def _immutable(self, object_path: str, stored: _Stored) -> ImmutableObjectReceipt:
        return ImmutableObjectReceipt(
            object_path=object_path,
            revision=stored.revision,
            entity_token=f"entity-{stored.revision}",
            stored_bytes=len(stored.content),
            stored_sha256=hashlib.sha256(stored.content).hexdigest(),
            verified_content_type=stored.content_type,
            verified_identity_assertions=stored.identity,
            verified_placement=stored.placement,
            completed_at=stored.completed_at,
        )


def _store(adapter: _MemoryAdapter) -> StorageAdapterArchiveStore:
    return StorageAdapterArchiveStore(
        RuntimeConfig(),
        name="primary",
        adapter=adapter,
    )


def test_archive_store_uses_one_opaque_archive_namespace() -> None:
    adapter = _MemoryAdapter()
    store = _store(adapter)
    assert store.read_mode() == "immediate"
    assert store.new_collection_archive_storage_prefix().startswith("archives/")
    assert not store.new_collection_archive_storage_prefix().startswith("archive/archives/")
    assert adapter.objects == {}


def test_verification_is_metadata_only_and_explicit_hashing_reads_once() -> None:
    adapter = _MemoryAdapter()
    content = b"opaque-encrypted-volume"
    path = "archives/opaque/volumes/pack-000000000000.tar.age"
    adapter.objects[path] = _Stored(
        content=content,
        content_type="application/vnd.riverhog.pack+age",
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
        revision="version-pack",
    )
    store = _store(adapter)

    store.verify_collection_archive(
        collection_id=17,
        archive=CollectionArchiveIdentity(objects=(identity,)),
    )
    assert adapter.reads == 0


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
        revision="version-segment",
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
                "revision": identity.revision,
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
        content_type="application/vnd.riverhog.collection-metadata+age",
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
        revision="version-manifest",
    )

    _store(adapter).delete_collection_archive(collection_id=17, objects=(identity,))

    assert len(adapter.deleted) == 1
    assert adapter.deleted[0].mode == "all_versions"


def test_discard_removes_completed_objects_from_the_exact_archive_namespace() -> None:
    adapter = _MemoryAdapter()
    store = _store(adapter)
    adapter.objects["archives/opaque/partial"] = _Stored(
        content=b"partial",
        content_type="application/octet-stream",
        identity={},
        placement="archive",
        revision="partial-version",
    )

    store.discard_collection_archive_upload(archive_storage_prefix="archives/opaque")

    assert "archives/opaque/partial" not in adapter.objects
