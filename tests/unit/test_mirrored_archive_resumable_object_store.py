from __future__ import annotations

import hashlib
import threading
from dataclasses import dataclass, field

import pytest
from riverhog_core.ports.archive_objects import (
    CompletedObjectReceipt,
    ResumableWriteConstraints,
    WriteSegmentReceipt,
    WriteSession,
)
from riverhog_core.ports.retrieval_cache import RetrievalCacheAdmission, RetrievalCacheReceipt
from riverhog_core.stores.mirrored_archive_resumable_object_store import (
    MirroredArchiveResumableObjectStore,
)

NOW = "2026-08-13T00:00:00Z"


@dataclass
class _ResumableStore:
    name: str
    object_path: str | None = None
    write_token: str | None = None
    content_type: str = "application/vnd.riverhog.pack+age"
    segments: dict[int, bytes] = field(default_factory=dict)
    completed: bytes | None = None
    events: list[str] = field(default_factory=list)
    fail_completion_once: bool = False
    fail_segment_once: bool = False
    segment_barrier: threading.Barrier | None = None

    def write_constraints(self) -> ResumableWriteConstraints:
        return ResumableWriteConstraints(1, None, None)

    def begin_write(  # type: ignore[no-untyped-def]
        self, *, object_path, expected_bytes, content_type, metadata
    ):
        _ = metadata
        self.object_path = object_path
        self.content_type = content_type
        self.write_token = f"{self.name}-write"
        self.events.append(f"{self.name}:begin")
        return WriteSession(object_path, self.write_token, expected_bytes)

    def write_segment(self, *, session, number, content):  # type: ignore[no-untyped-def]
        assert session.write_token == self.write_token
        if self.segment_barrier is not None:
            self.segment_barrier.wait()
        if self.fail_segment_once:
            self.fail_segment_once = False
            raise RuntimeError(f"{self.name} segment interrupted")
        self.segments[number] = content
        self.events.append(f"{self.name}:segment:{number}")
        return WriteSegmentReceipt(
            number,
            f"{self.name}-{number}",
            len(content),
            hashlib.sha256(content).hexdigest(),
        )

    def list_segments(self, *, session):  # type: ignore[no-untyped-def]
        assert session.write_token == self.write_token
        assert self.completed is None, "completed writes must reconcile without multipart state"
        return tuple(
            WriteSegmentReceipt(number, f"{self.name}-{number}", len(content))
            for number, content in sorted(self.segments.items())
        )

    def complete_write(
        self,
        *,
        session,
        segments,
        expected_bytes,
        expected_content_type,
        expected_metadata,
    ):  # type: ignore[no-untyped-def]
        _ = expected_metadata
        if self.name != "cache":
            assert expected_content_type == self.content_type
        assert session.write_token == self.write_token
        if self.fail_completion_once:
            self.fail_completion_once = False
            raise RuntimeError(f"{self.name} completion interrupted")
        self.completed = b"".join(self.segments[current.number] for current in segments)
        assert len(self.completed) == expected_bytes
        self.events.append(f"{self.name}:complete")
        return self._completed_receipt()

    def find_completed_write(  # type: ignore[no-untyped-def]
        self, *, object_path, expected_bytes, expected_content_type, expected_metadata
    ):
        _ = object_path, expected_metadata
        if self.completed is None:
            return None
        assert expected_content_type == self.content_type
        assert len(self.completed) == expected_bytes
        return self._completed_receipt()

    def abort_write(self, *, session):  # type: ignore[no-untyped-def]
        assert session.write_token == self.write_token
        self.events.append(f"{self.name}:abort")

    def _completed_receipt(self) -> CompletedObjectReceipt:
        assert self.completed is not None
        assert self.object_path is not None
        cache_receipt = (
            RetrievalCacheReceipt(
                cache_store="local",
                object_path=self.object_path,
                revision=f"{self.name}-version",
                stored_bytes=len(self.completed),
                stored_sha256=None,
                cached_at=NOW,
                verified_at=NOW,
            )
            if self.name == "cache"
            else None
        )
        return CompletedObjectReceipt(
            object_path=self.object_path,
            revision=f"{self.name}-version",
            entity_token=f"{self.name}-entity",
            bytes=len(self.completed),
            completed_at=NOW,
            retrieval_cache=cache_receipt,
        )


class _Cache:
    def __init__(self, store: _ResumableStore) -> None:
        self.store = store
        self.active = False

    def admit(
        self,
        *,
        owner: str,
        source_store: str,
        collection_id: int,
        object_id: str,
        expected_bytes: int,
    ) -> RetrievalCacheAdmission:
        self.active = True
        object_path = f"cache/{source_store}/{collection_id}/{object_id}"
        if self.store.completed is not None:
            completed = self.store._completed_receipt().retrieval_cache
            assert completed is not None
            return RetrievalCacheAdmission(
                owner=owner,
                cache_store="local",
                source_store=source_store,
                collection_id=collection_id,
                object_id=object_id,
                object_path=object_path,
                expected_bytes=expected_bytes,
                write_token=None,
                admitted_at=NOW,
                completed=completed,
            )
        session = self.store.begin_write(
            object_path=object_path,
            expected_bytes=expected_bytes,
            content_type="application/octet-stream",
            metadata={},
        )
        return RetrievalCacheAdmission(
            owner=owner,
            cache_store="local",
            source_store=source_store,
            collection_id=collection_id,
            object_id=object_id,
            object_path=object_path,
            expected_bytes=expected_bytes,
            write_token=session.write_token,
            admitted_at=NOW,
        )

    def resumable_object_store(self, **_: object) -> _ResumableStore:
        return self.store

    def delete(self, *, cache_store: str, object_path: str, revision: str | None) -> None:
        assert cache_store == "local"
        assert object_path == self.store.object_path
        assert revision == "cache-version"
        self.store.completed = None
        self.store.events.append("cache:delete")

    def release(self, *, owner: str) -> int:
        _ = owner
        self.active = False
        self.store.events.append("cache:release")
        if self.store.completed is None:
            return 0
        self.store.completed = None
        self.store.events.append("cache:delete")
        return 1

    def is_current(self, *, admission: RetrievalCacheAdmission) -> bool:
        _ = admission
        return self.active

    def reap_abandoned_populations(self, *, limit: int = 100) -> int:
        _ = limit
        return 0


def _mirror(
    archive: _ResumableStore,
    cache: _ResumableStore,
) -> MirroredArchiveResumableObjectStore:
    return MirroredArchiveResumableObjectStore(
        archive=archive,
        cache=_Cache(cache),  # type: ignore[arg-type]
        source_store="deep",
        collection_id=42,
        object_id="pack-000000000000",
        owner="test:pack-000000000000",
    )


def test_encrypted_segments_complete_in_cache_before_archive_authority() -> None:
    events: list[str] = []
    archive = _ResumableStore("archive", events=events)
    cache = _ResumableStore("cache", events=events)
    mirror = _mirror(archive, cache)
    session = mirror.begin_write(
        object_path="archives/one/volumes/pack.tar.age",
        expected_bytes=9,
        content_type="application/vnd.riverhog.pack+age",
        metadata={"riverhog-format": "riverhog-pack-volume/v1"},
    )
    receipt = mirror.write_segment(session=session, number=1, content=b"encrypted")
    completed = mirror.complete_write(
        session=session,
        segments=(receipt,),
        expected_bytes=9,
        expected_content_type="application/vnd.riverhog.pack+age",
        expected_metadata={"riverhog-format": "riverhog-pack-volume/v1"},
    )

    assert archive.completed == cache.completed == b"encrypted"
    assert completed.retrieval_cache is not None
    assert completed.retrieval_cache.stored_sha256 is None
    assert events.index("cache:complete") < events.index("archive:complete")


def test_archive_and_cache_segment_writes_start_concurrently() -> None:
    barrier = threading.Barrier(2, timeout=2)
    archive = _ResumableStore("archive", segment_barrier=barrier)
    cache = _ResumableStore("cache", segment_barrier=barrier)
    mirror = _mirror(archive, cache)
    session = mirror.begin_write(
        object_path="archives/one/volumes/pack.tar.age",
        expected_bytes=9,
        content_type="application/vnd.riverhog.pack+age",
        metadata={"riverhog-format": "riverhog-pack-volume/v1"},
    )

    receipt = mirror.write_segment(session=session, number=1, content=b"encrypted")

    assert receipt.bytes == 9
    assert archive.segments == cache.segments == {1: b"encrypted"}


def test_cache_segment_failure_never_blocks_archive_completion() -> None:
    events: list[str] = []
    archive = _ResumableStore("archive", events=events)
    cache = _ResumableStore("cache", events=events, fail_segment_once=True)
    mirror = _mirror(archive, cache)
    session = mirror.begin_write(
        object_path="archives/one/volumes/pack.tar.age",
        expected_bytes=9,
        content_type="application/vnd.riverhog.pack+age",
        metadata={"riverhog-format": "riverhog-pack-volume/v1"},
    )

    receipt = mirror.write_segment(session=session, number=1, content=b"encrypted")
    completed = mirror.complete_write(
        session=session,
        segments=(receipt,),
        expected_bytes=9,
        expected_content_type="application/vnd.riverhog.pack+age",
        expected_metadata={"riverhog-format": "riverhog-pack-volume/v1"},
    )

    assert archive.completed == b"encrypted"
    assert cache.completed is None
    assert completed.retrieval_cache is None
    assert "cache:release" in events


def test_completion_resumes_after_cache_sealed_before_archive() -> None:
    archive = _ResumableStore("archive", fail_completion_once=True)
    cache = _ResumableStore("cache")
    mirror = _mirror(archive, cache)
    session = mirror.begin_write(
        object_path="archives/one/volumes/segment.bin.age",
        expected_bytes=10,
        content_type="application/vnd.riverhog.raw-segment+age",
        metadata={"riverhog-format": "riverhog-raw-volume/v1"},
    )
    receipt = mirror.write_segment(session=session, number=1, content=b"ciphertext")

    with pytest.raises(RuntimeError, match="archive completion interrupted"):
        mirror.complete_write(
            session=session,
            segments=(receipt,),
            expected_bytes=10,
            expected_content_type="application/vnd.riverhog.raw-segment+age",
            expected_metadata={"riverhog-format": "riverhog-raw-volume/v1"},
        )

    completed = mirror.complete_write(
        session=session,
        segments=(receipt,),
        expected_bytes=10,
        expected_content_type="application/vnd.riverhog.raw-segment+age",
        expected_metadata={"riverhog-format": "riverhog-raw-volume/v1"},
    )
    assert completed.retrieval_cache is not None
    assert cache.events.count("cache:complete") == 1


def test_completed_archive_remains_authoritative_without_cache() -> None:
    archive = _ResumableStore(
        "archive",
        object_path="archives/one/volumes/pack.tar.age",
        completed=b"encrypted",
    )
    cache = _ResumableStore("cache")

    completed = _mirror(archive, cache).find_completed_write(
        object_path="archives/one/volumes/pack.tar.age",
        expected_bytes=9,
        expected_content_type="application/vnd.riverhog.pack+age",
        expected_metadata={"riverhog-format": "riverhog-pack-volume/v1"},
    )
    assert completed is not None and completed.retrieval_cache is None


def test_completed_archive_reconciles_a_sealed_cache_after_restart() -> None:
    archive = _ResumableStore(
        "archive",
        object_path="archives/one/volumes/pack.tar.age",
        completed=b"encrypted",
    )
    cache = _ResumableStore(
        "cache",
        object_path="cache/deep/42/pack-000000000000",
        completed=b"encrypted",
    )

    completed = _mirror(archive, cache).find_completed_write(
        object_path="archives/one/volumes/pack.tar.age",
        expected_bytes=9,
        expected_content_type="application/vnd.riverhog.pack+age",
        expected_metadata={"riverhog-format": "riverhog-pack-volume/v1"},
    )

    assert completed is not None and completed.retrieval_cache is not None
    assert completed.retrieval_cache.object_path == "cache/deep/42/pack-000000000000"


def test_abort_removes_a_sealed_cache_orphan_when_archive_is_incomplete() -> None:
    archive = _ResumableStore("archive")
    cache = _ResumableStore(
        "cache",
        object_path="cache/deep/42/pack-000000000000",
        completed=b"encrypted",
    )
    mirror = _mirror(archive, cache)
    session = mirror.begin_write(
        object_path="archives/one/volumes/pack.tar.age",
        expected_bytes=9,
        content_type="application/vnd.riverhog.pack+age",
        metadata={"riverhog-format": "riverhog-pack-volume/v1"},
    )
    mirror.abort_write(session=session)

    assert cache.completed is None
    assert "cache:delete" in cache.events
