from __future__ import annotations

import hashlib
from dataclasses import dataclass, field

import pytest
from riverhog_core.ports.archive_objects import (
    CompletedObjectReceipt,
    MultipartPartReceipt,
    MultipartUpload,
)
from riverhog_core.ports.retrieval_cache import RetrievalCacheReceipt
from riverhog_core.stores.mirrored_archive_multipart_object_store import (
    MirroredArchiveMultipartObjectStore,
)

NOW = "2026-08-13T00:00:00Z"


@dataclass
class _MultipartStore:
    name: str
    object_path: str | None = None
    upload_id: str | None = None
    parts: dict[int, bytes] = field(default_factory=dict)
    completed: bytes | None = None
    events: list[str] = field(default_factory=list)
    fail_completion_once: bool = False

    def create_multipart_upload(self, *, object_path, content_type, metadata):  # type: ignore[no-untyped-def]
        _ = content_type, metadata
        self.object_path = object_path
        self.upload_id = f"{self.name}-upload"
        self.events.append(f"{self.name}:create")
        return MultipartUpload(object_path, self.upload_id)

    def upload_part(self, *, upload, number, content):  # type: ignore[no-untyped-def]
        assert upload.upload_id == self.upload_id
        self.parts[number] = content
        self.events.append(f"{self.name}:part:{number}")
        return MultipartPartReceipt(
            number,
            f"{self.name}-{number}",
            len(content),
            hashlib.sha256(content).hexdigest(),
        )

    def list_parts(self, *, upload):  # type: ignore[no-untyped-def]
        assert upload.upload_id == self.upload_id
        return tuple(
            MultipartPartReceipt(number, f"{self.name}-{number}", len(content))
            for number, content in sorted(self.parts.items())
        )

    def complete_multipart_upload(
        self,
        *,
        upload,
        parts,
        expected_bytes,
        expected_metadata,
    ):  # type: ignore[no-untyped-def]
        _ = expected_metadata
        assert upload.upload_id == self.upload_id
        if self.fail_completion_once:
            self.fail_completion_once = False
            raise RuntimeError(f"{self.name} completion interrupted")
        self.completed = b"".join(self.parts[current.number] for current in parts)
        assert len(self.completed) == expected_bytes
        self.events.append(f"{self.name}:complete")
        return self._completed_receipt()

    def head_completed_object(self, *, object_path, expected_metadata):  # type: ignore[no-untyped-def]
        _ = object_path, expected_metadata
        return self._completed_receipt() if self.completed is not None else None

    def abort_multipart_upload(self, *, upload):  # type: ignore[no-untyped-def]
        assert upload.upload_id == self.upload_id
        self.events.append(f"{self.name}:abort")

    def _completed_receipt(self) -> CompletedObjectReceipt:
        assert self.completed is not None
        assert self.object_path is not None
        return CompletedObjectReceipt(
            object_path=self.object_path,
            version_id=f"{self.name}-version",
            etag=f"{self.name}-etag",
            bytes=len(self.completed),
            completed_at=NOW,
        )


class _Cache:
    def __init__(self, store: _MultipartStore) -> None:
        self.store = store

    def multipart_object_store(self, **_: object) -> _MultipartStore:
        return self.store

    def verify_multipart_object(
        self,
        *,
        completed: CompletedObjectReceipt,
        parts: tuple[MultipartPartReceipt, ...] = (),
    ) -> RetrievalCacheReceipt:
        assert parts
        assert self.store.completed is not None
        assert completed.bytes == len(self.store.completed)
        self.store.events.append("cache:verify")
        return RetrievalCacheReceipt(
            object_path=completed.object_path,
            version_id=completed.version_id,
            stored_bytes=completed.bytes,
            stored_sha256=hashlib.sha256(self.store.completed).hexdigest(),
            cached_at=completed.completed_at,
            verified_at=NOW,
        )

    def delete(self, *, object_path: str, version_id: str | None) -> None:
        assert object_path == self.store.object_path
        assert version_id == "cache-version"
        self.store.completed = None
        self.store.events.append("cache:delete")


def _mirror(
    archive: _MultipartStore,
    cache: _MultipartStore,
) -> MirroredArchiveMultipartObjectStore:
    return MirroredArchiveMultipartObjectStore(
        archive=archive,
        cache=_Cache(cache),  # type: ignore[arg-type]
        source_store="deep",
        collection_id=42,
        object_id="pack-000000000000",
    )


def test_encrypted_parts_are_verified_in_cache_before_archive_completion() -> None:
    events: list[str] = []
    archive = _MultipartStore("archive", events=events)
    cache = _MultipartStore("cache", events=events)
    mirror = _mirror(archive, cache)
    upload = mirror.create_multipart_upload(
        object_path="archives/one/volumes/pack.tar.age",
        content_type="application/vnd.riverhog.pack+age",
        metadata={"riverhog-format": "riverhog-pack-volume/v1"},
    )
    receipt = mirror.upload_part(upload=upload, number=1, content=b"encrypted")
    completed = mirror.complete_multipart_upload(
        upload=upload,
        parts=(receipt,),
        expected_bytes=9,
        expected_metadata={"riverhog-format": "riverhog-pack-volume/v1"},
    )

    assert archive.completed == cache.completed == b"encrypted"
    assert completed.retrieval_cache is not None
    assert completed.retrieval_cache.stored_sha256 == hashlib.sha256(b"encrypted").hexdigest()
    assert events.index("cache:complete") < events.index("cache:verify")
    assert events.index("cache:verify") < events.index("archive:complete")


def test_completion_resumes_after_cache_sealed_before_archive() -> None:
    archive = _MultipartStore("archive", fail_completion_once=True)
    cache = _MultipartStore("cache")
    mirror = _mirror(archive, cache)
    upload = mirror.create_multipart_upload(
        object_path="archives/one/volumes/segment.bin.age",
        content_type="application/vnd.riverhog.raw-segment+age",
        metadata={"riverhog-format": "riverhog-raw-volume/v1"},
    )
    receipt = mirror.upload_part(upload=upload, number=1, content=b"ciphertext")

    with pytest.raises(RuntimeError, match="archive completion interrupted"):
        mirror.complete_multipart_upload(
            upload=upload,
            parts=(receipt,),
            expected_bytes=10,
            expected_metadata={"riverhog-format": "riverhog-raw-volume/v1"},
        )

    completed = mirror.complete_multipart_upload(
        upload=upload,
        parts=(receipt,),
        expected_bytes=10,
        expected_metadata={"riverhog-format": "riverhog-raw-volume/v1"},
    )
    assert completed.retrieval_cache is not None
    assert cache.events.count("cache:complete") == 1


def test_completed_archive_without_required_cache_is_rejected() -> None:
    archive = _MultipartStore(
        "archive",
        object_path="archives/one/volumes/pack.tar.age",
        completed=b"encrypted",
    )
    cache = _MultipartStore("cache")

    with pytest.raises(RuntimeError, match="missing its required retrieval cache"):
        _mirror(archive, cache).head_completed_object(
            object_path="archives/one/volumes/pack.tar.age",
            expected_metadata={"riverhog-format": "riverhog-pack-volume/v1"},
        )


def test_abort_removes_a_sealed_cache_orphan_when_archive_is_incomplete() -> None:
    archive = _MultipartStore("archive")
    cache = _MultipartStore("cache")
    mirror = _mirror(archive, cache)
    upload = mirror.create_multipart_upload(
        object_path="archives/one/volumes/pack.tar.age",
        content_type="application/vnd.riverhog.pack+age",
        metadata={"riverhog-format": "riverhog-pack-volume/v1"},
    )
    mirror.upload_part(upload=upload, number=1, content=b"encrypted")
    cache.completed = b"encrypted"

    mirror.abort_multipart_upload(upload=upload)

    assert cache.completed is None
    assert "cache:delete" in cache.events
