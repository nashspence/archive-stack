from __future__ import annotations

import hashlib
from dataclasses import dataclass, field

import pytest
from riverhog_core.ports.archive_objects import (
    CompletedObjectReceipt,
    ResumableWriteConstraints,
    WriteSegmentReceipt,
    WriteSession,
)
from riverhog_core.ports.retrieval_cache import RetrievalCacheReceipt
from riverhog_core.stores.mirrored_archive_resumable_object_store import (
    MirroredArchiveResumableObjectStore,
)

NOW = "2026-08-13T00:00:00Z"


@dataclass
class _ResumableStore:
    name: str
    object_path: str | None = None
    write_token: str | None = None
    segments: dict[int, bytes] = field(default_factory=dict)
    completed: bytes | None = None
    events: list[str] = field(default_factory=list)
    fail_completion_once: bool = False

    def write_constraints(self) -> ResumableWriteConstraints:
        return ResumableWriteConstraints(1, None, None)

    def begin_write(self, *, object_path, content_type, metadata):  # type: ignore[no-untyped-def]
        _ = content_type, metadata
        self.object_path = object_path
        self.write_token = f"{self.name}-write"
        self.events.append(f"{self.name}:begin")
        return WriteSession(object_path, self.write_token)

    def write_segment(self, *, session, number, content):  # type: ignore[no-untyped-def]
        assert session.write_token == self.write_token
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
        expected_metadata,
    ):  # type: ignore[no-untyped-def]
        _ = expected_metadata
        assert session.write_token == self.write_token
        if self.fail_completion_once:
            self.fail_completion_once = False
            raise RuntimeError(f"{self.name} completion interrupted")
        self.completed = b"".join(self.segments[current.number] for current in segments)
        assert len(self.completed) == expected_bytes
        self.events.append(f"{self.name}:complete")
        return self._completed_receipt()

    def find_completed_write(self, *, object_path, expected_metadata):  # type: ignore[no-untyped-def]
        _ = object_path, expected_metadata
        return self._completed_receipt() if self.completed is not None else None

    def abort_write(self, *, session):  # type: ignore[no-untyped-def]
        assert session.write_token == self.write_token
        self.events.append(f"{self.name}:abort")

    def _completed_receipt(self) -> CompletedObjectReceipt:
        assert self.completed is not None
        assert self.object_path is not None
        return CompletedObjectReceipt(
            object_path=self.object_path,
            revision=f"{self.name}-version",
            entity_token=f"{self.name}-entity",
            bytes=len(self.completed),
            completed_at=NOW,
        )


class _Cache:
    def __init__(self, store: _ResumableStore) -> None:
        self.store = store

    def resumable_object_store(self, **_: object) -> _ResumableStore:
        return self.store

    def verify_resumable_object(
        self,
        *,
        completed: CompletedObjectReceipt,
        segments: tuple[WriteSegmentReceipt, ...] = (),
    ) -> RetrievalCacheReceipt:
        assert segments
        assert self.store.completed is not None
        assert completed.bytes == len(self.store.completed)
        self.store.events.append("cache:verify")
        return RetrievalCacheReceipt(
            object_path=completed.object_path,
            revision=completed.revision,
            stored_bytes=completed.bytes,
            stored_sha256=hashlib.sha256(self.store.completed).hexdigest(),
            cached_at=completed.completed_at,
            verified_at=NOW,
        )

    def delete(self, *, object_path: str, revision: str | None) -> None:
        assert object_path == self.store.object_path
        assert revision == "cache-version"
        self.store.completed = None
        self.store.events.append("cache:delete")


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
    )


def test_encrypted_segments_are_verified_in_cache_before_archive_completion() -> None:
    events: list[str] = []
    archive = _ResumableStore("archive", events=events)
    cache = _ResumableStore("cache", events=events)
    mirror = _mirror(archive, cache)
    session = mirror.begin_write(
        object_path="archives/one/volumes/pack.tar.age",
        content_type="application/vnd.riverhog.pack+age",
        metadata={"riverhog-format": "riverhog-pack-volume/v1"},
    )
    receipt = mirror.write_segment(session=session, number=1, content=b"encrypted")
    completed = mirror.complete_write(
        session=session,
        segments=(receipt,),
        expected_bytes=9,
        expected_metadata={"riverhog-format": "riverhog-pack-volume/v1"},
    )

    assert archive.completed == cache.completed == b"encrypted"
    assert completed.retrieval_cache is not None
    assert completed.retrieval_cache.stored_sha256 == hashlib.sha256(b"encrypted").hexdigest()
    assert events.index("cache:complete") < events.index("cache:verify")
    assert events.index("cache:verify") < events.index("archive:complete")


def test_completion_resumes_after_cache_sealed_before_archive() -> None:
    archive = _ResumableStore("archive", fail_completion_once=True)
    cache = _ResumableStore("cache")
    mirror = _mirror(archive, cache)
    session = mirror.begin_write(
        object_path="archives/one/volumes/segment.bin.age",
        content_type="application/vnd.riverhog.raw-segment+age",
        metadata={"riverhog-format": "riverhog-raw-volume/v1"},
    )
    receipt = mirror.write_segment(session=session, number=1, content=b"ciphertext")

    with pytest.raises(RuntimeError, match="archive completion interrupted"):
        mirror.complete_write(
            session=session,
            segments=(receipt,),
            expected_bytes=10,
            expected_metadata={"riverhog-format": "riverhog-raw-volume/v1"},
        )

    completed = mirror.complete_write(
        session=session,
        segments=(receipt,),
        expected_bytes=10,
        expected_metadata={"riverhog-format": "riverhog-raw-volume/v1"},
    )
    assert completed.retrieval_cache is not None
    assert cache.events.count("cache:complete") == 1


def test_completed_archive_without_required_cache_is_rejected() -> None:
    archive = _ResumableStore(
        "archive",
        object_path="archives/one/volumes/pack.tar.age",
        completed=b"encrypted",
    )
    cache = _ResumableStore("cache")

    with pytest.raises(RuntimeError, match="missing its required retrieval cache"):
        _mirror(archive, cache).find_completed_write(
            object_path="archives/one/volumes/pack.tar.age",
            expected_metadata={"riverhog-format": "riverhog-pack-volume/v1"},
        )


def test_abort_removes_a_sealed_cache_orphan_when_archive_is_incomplete() -> None:
    archive = _ResumableStore("archive")
    cache = _ResumableStore("cache")
    mirror = _mirror(archive, cache)
    session = mirror.begin_write(
        object_path="archives/one/volumes/pack.tar.age",
        content_type="application/vnd.riverhog.pack+age",
        metadata={"riverhog-format": "riverhog-pack-volume/v1"},
    )
    mirror.write_segment(session=session, number=1, content=b"encrypted")
    cache.completed = b"encrypted"

    mirror.abort_write(session=session)

    assert cache.completed is None
    assert "cache:delete" in cache.events
