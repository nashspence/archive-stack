"""Linux filesystem implementation of the Riverhog storage-adapter contract.

One adapter process owns one configured root.  Exact-size resumable sessions are
backed by a preallocated payload file, so a successful ``begin_write`` reserves
real filesystem blocks before Riverhog starts an expensive upstream operation.
The adapter keeps provider-private continuation and immutable revision metadata
under the root; Riverhog sees only the provider-neutral v1 contract.
"""

from __future__ import annotations

import errno
import fcntl
import hashlib
import json
import os
import shutil
import stat
import threading
import uuid
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Self, cast

from riverhog_storage_adapter_protocol import (
    AdapterDescriptor,
    BinaryContent,
    CompletedObjectReceipt,
    CompletedWriteLookupRequest,
    DeleteObjectRequest,
    DeletePrefixRequest,
    ImmutableObjectReceipt,
    ObjectHeadRequest,
    ObjectLocator,
    ObjectMetadataReceipt,
    ObjectPlacement,
    ObjectReadReceipt,
    ObjectReadRequest,
    ObjectReadStream,
    ReadPreparationRequest,
    ReadReady,
    ReadStatus,
    SmallObjectWriteRequest,
    StorageAdapterRejection,
    WriteCompleteRequest,
    WriteSegmentReceipt,
    WriteSegmentSet,
    WriteSession,
    WriteStartRequest,
)
from time_formats import format_utc_timestamp, utc_now

_WRITE_SCHEMA = "riverhog-filesystem-write/v1"
_OBJECT_SCHEMA = "riverhog-filesystem-object/v1"
_DEFAULT_SEGMENT_BYTES = 64 * 1024 * 1024
_DEFAULT_READ_CHUNK_BYTES = 8 * 1024 * 1024
_DEFAULT_MINIMUM_FREE_BYTES = 256 * 1024 * 1024
_GATE_SHARDS = 1024
_INTERNAL_MODE = 0o700
_FILE_MODE = 0o600
_CAPACITY_ERRORS = frozenset({errno.ENOSPC, errno.EDQUOT, errno.EFBIG})
_UNSUPPORTED_ALLOCATION_ERRORS = frozenset(
    {
        errno.EINVAL,
        errno.ENOSYS,
        errno.EOPNOTSUPP,
        getattr(errno, "ENOTSUP", errno.EOPNOTSUPP),
    }
)


@dataclass(frozen=True, slots=True)
class FilesystemStorageAdapterConfig:
    """Private configuration for one local Linux filesystem target."""

    root: Path
    implementation_id: str = "riverhog.filesystem/v1"
    implementation_version: str = "0.1.0"
    segment_bytes: int = _DEFAULT_SEGMENT_BYTES
    read_chunk_bytes: int = _DEFAULT_READ_CHUNK_BYTES
    minimum_free_bytes: int = _DEFAULT_MINIMUM_FREE_BYTES

    def __post_init__(self) -> None:
        if os.name != "posix" or not hasattr(os, "posix_fallocate"):
            raise ValueError("filesystem adapter requires Linux posix_fallocate support")
        if not self.implementation_id or not self.implementation_version:
            raise ValueError("filesystem adapter implementation identity must be nonempty")
        if not self.root.is_absolute():
            raise ValueError("filesystem adapter root must be absolute")
        if self.segment_bytes < 64 * 1024:
            raise ValueError("filesystem adapter segment bytes must be at least 64 KiB")
        if self.read_chunk_bytes < 64 * 1024:
            raise ValueError("filesystem adapter read chunk bytes must be at least 64 KiB")
        if self.minimum_free_bytes < 0:
            raise ValueError("filesystem adapter minimum free bytes must be nonnegative")


class _ObjectGate:
    """Process-local reader/writer gate held for the lifetime of streamed reads."""

    def __init__(self) -> None:
        self._condition = threading.Condition()
        self._readers = 0
        self._writer = False

    def acquire_read(self) -> None:
        with self._condition:
            while self._writer:
                self._condition.wait()
            self._readers += 1

    def release_read(self) -> None:
        with self._condition:
            if self._readers < 1:
                raise RuntimeError("filesystem object read gate is unbalanced")
            self._readers -= 1
            if self._readers == 0:
                self._condition.notify_all()

    @contextmanager
    def write(self) -> Iterator[None]:
        with self._condition:
            while self._writer or self._readers:
                self._condition.wait()
            self._writer = True
        try:
            yield
        finally:
            with self._condition:
                self._writer = False
                self._condition.notify_all()


@dataclass(frozen=True, slots=True)
class _PartRecord:
    number: int
    offset: int
    stored_bytes: int
    stored_sha256: str | None

    @classmethod
    def from_json(cls, raw: dict[str, Any]) -> Self:
        if set(raw) != {"number", "offset", "stored_bytes", "stored_sha256"}:
            raise RuntimeError("filesystem object part metadata has an invalid shape")
        number = _required_int(raw, "number", minimum=1)
        offset = _required_int(raw, "offset", minimum=0)
        stored_bytes = _required_int(raw, "stored_bytes", minimum=0)
        digest = _required_sha256(raw, "stored_sha256")
        return cls(
            number=number,
            offset=offset,
            stored_bytes=stored_bytes,
            stored_sha256=digest,
        )

    def as_json(self) -> dict[str, Any]:
        return {
            "number": self.number,
            "offset": self.offset,
            "stored_bytes": self.stored_bytes,
            "stored_sha256": self.stored_sha256,
        }


@dataclass(frozen=True, slots=True)
class _SegmentRecord:
    number: int
    offset: int
    segment_token: str
    stored_bytes: int
    stored_sha256: str

    @classmethod
    def from_json(cls, raw: dict[str, Any]) -> Self:
        if set(raw) != {
            "number",
            "offset",
            "segment_token",
            "stored_bytes",
            "stored_sha256",
        }:
            raise RuntimeError("filesystem write segment metadata has an invalid shape")
        number = _required_int(raw, "number", minimum=1)
        offset = _required_int(raw, "offset", minimum=0)
        stored_bytes = _required_int(raw, "stored_bytes", minimum=1)
        digest = _required_sha256(raw, "stored_sha256")
        token = raw["segment_token"]
        if not isinstance(token, str) or not token:
            raise RuntimeError("filesystem write segment token is invalid")
        return cls(
            number=number,
            offset=offset,
            segment_token=token,
            stored_bytes=stored_bytes,
            stored_sha256=digest,
        )

    def as_json(self) -> dict[str, Any]:
        return {
            "number": self.number,
            "offset": self.offset,
            "segment_token": self.segment_token,
            "stored_bytes": self.stored_bytes,
            "stored_sha256": self.stored_sha256,
        }

    def receipt(self) -> WriteSegmentReceipt:
        return WriteSegmentReceipt(
            number=self.number,
            segment_token=self.segment_token,
            stored_bytes=self.stored_bytes,
            stored_sha256=self.stored_sha256,
        )

    def part(self) -> _PartRecord:
        return _PartRecord(
            number=self.number,
            offset=self.offset,
            stored_bytes=self.stored_bytes,
            stored_sha256=self.stored_sha256,
        )


@dataclass(frozen=True, slots=True)
class _WriteState:
    token: str
    object_path: str
    expected_bytes: int
    content_type: str
    required_identity_assertions: dict[str, str]
    placement: ObjectPlacement
    created_at: str
    segments: dict[int, _SegmentRecord]

    @classmethod
    def from_json(cls, raw: dict[str, Any]) -> Self:
        expected = {
            "schema",
            "token",
            "object_path",
            "expected_bytes",
            "content_type",
            "required_identity_assertions",
            "placement",
            "created_at",
            "segments",
        }
        if set(raw) != expected or raw.get("schema") != _WRITE_SCHEMA:
            raise RuntimeError("filesystem write state has an invalid shape")
        token = raw["token"]
        object_path = raw["object_path"]
        content_type = raw["content_type"]
        assertions = raw["required_identity_assertions"]
        placement = raw["placement"]
        raw_segments = raw["segments"]
        if not isinstance(token, str) or not token:
            raise RuntimeError("filesystem write token is invalid")
        if not isinstance(object_path, str) or not object_path:
            raise RuntimeError("filesystem write object path is invalid")
        if not isinstance(content_type, str) or not content_type:
            raise RuntimeError("filesystem write content type is invalid")
        if not _is_string_mapping(assertions):
            raise RuntimeError("filesystem write assertions are invalid")
        if placement not in {"archive", "immediate"}:
            raise RuntimeError("filesystem write placement is invalid")
        if not isinstance(raw_segments, list):
            raise RuntimeError("filesystem write segment state is invalid")
        expected_bytes = _required_int(raw, "expected_bytes", minimum=1)
        parsed = tuple(
            _SegmentRecord.from_json(_required_dict(item, "filesystem write segment"))
            for item in raw_segments
        )
        numbers = tuple(item.number for item in parsed)
        if numbers != tuple(sorted(set(numbers))):
            raise RuntimeError("filesystem write segment state is not canonical")
        _validate_extents(
            ((item.offset, item.stored_bytes) for item in parsed),
            maximum=expected_bytes,
            label="filesystem write segment",
        )
        if sum(item.stored_bytes for item in parsed) > expected_bytes:
            raise RuntimeError("filesystem write segments exceed the admitted bytes")
        return cls(
            token=token,
            object_path=object_path,
            expected_bytes=expected_bytes,
            content_type=content_type,
            required_identity_assertions=dict(sorted(cast(dict[str, str], assertions).items())),
            placement=cast(ObjectPlacement, placement),
            created_at=_required_string(raw, "created_at"),
            segments={item.number: item for item in parsed},
        )

    def as_json(self) -> dict[str, Any]:
        return {
            "schema": _WRITE_SCHEMA,
            "token": self.token,
            "object_path": self.object_path,
            "expected_bytes": self.expected_bytes,
            "content_type": self.content_type,
            "required_identity_assertions": self.required_identity_assertions,
            "placement": self.placement,
            "created_at": self.created_at,
            "segments": [self.segments[number].as_json() for number in sorted(self.segments)],
        }

    def session(self) -> WriteSession:
        return WriteSession(
            object_path=self.object_path,
            expected_bytes=self.expected_bytes,
            write_token=self.token,
        )

    def matches(self, request: WriteStartRequest) -> bool:
        return (
            self.object_path == request.object_path
            and self.expected_bytes == request.expected_bytes
            and self.content_type == request.content_type
            and self.required_identity_assertions == request.required_identity_assertions
            and self.placement == request.placement
        )

    def with_segment(self, segment: _SegmentRecord) -> _WriteState:
        segments = dict(self.segments)
        segments[segment.number] = segment
        return _WriteState(
            token=self.token,
            object_path=self.object_path,
            expected_bytes=self.expected_bytes,
            content_type=self.content_type,
            required_identity_assertions=self.required_identity_assertions,
            placement=self.placement,
            created_at=self.created_at,
            segments=segments,
        )


@dataclass(frozen=True, slots=True)
class _ObjectRecord:
    object_path: str
    revision: str
    entity_token: str
    stored_bytes: int
    stored_sha256: str | None
    content_type: str
    required_identity_assertions: dict[str, str]
    placement: ObjectPlacement
    completed_at: str
    parts: tuple[_PartRecord, ...]

    @classmethod
    def from_json(cls, raw: dict[str, Any]) -> Self:
        expected = {
            "schema",
            "object_path",
            "revision",
            "entity_token",
            "stored_bytes",
            "stored_sha256",
            "content_type",
            "required_identity_assertions",
            "placement",
            "completed_at",
            "parts",
        }
        if set(raw) != expected or raw.get("schema") != _OBJECT_SCHEMA:
            raise RuntimeError("filesystem object metadata has an invalid shape")
        assertions = raw["required_identity_assertions"]
        raw_parts = raw["parts"]
        placement = raw["placement"]
        if not _is_string_mapping(assertions):
            raise RuntimeError("filesystem object assertions are invalid")
        if not isinstance(raw_parts, list):
            raise RuntimeError("filesystem object parts are invalid")
        if placement not in {"archive", "immediate"}:
            raise RuntimeError("filesystem object placement is invalid")
        parts = tuple(
            _PartRecord.from_json(_required_dict(item, "filesystem object part"))
            for item in raw_parts
        )
        if tuple(part.number for part in parts) != tuple(range(1, len(parts) + 1)):
            raise RuntimeError("filesystem object parts are not contiguous")
        stored_bytes = _required_int(raw, "stored_bytes", minimum=0)
        if sum(part.stored_bytes for part in parts) != stored_bytes:
            raise RuntimeError("filesystem object parts differ from its byte count")
        _validate_extents(
            ((part.offset, part.stored_bytes) for part in parts),
            maximum=stored_bytes,
            label="filesystem object part",
        )
        return cls(
            object_path=_required_string(raw, "object_path"),
            revision=_required_string(raw, "revision"),
            entity_token=_required_string(raw, "entity_token"),
            stored_bytes=stored_bytes,
            stored_sha256=_optional_sha256(raw, "stored_sha256"),
            content_type=_required_string(raw, "content_type"),
            required_identity_assertions=dict(sorted(cast(dict[str, str], assertions).items())),
            placement=cast(ObjectPlacement, placement),
            completed_at=_required_string(raw, "completed_at"),
            parts=parts,
        )

    def as_json(self) -> dict[str, Any]:
        return {
            "schema": _OBJECT_SCHEMA,
            "object_path": self.object_path,
            "revision": self.revision,
            "entity_token": self.entity_token,
            "stored_bytes": self.stored_bytes,
            "stored_sha256": self.stored_sha256,
            "content_type": self.content_type,
            "required_identity_assertions": self.required_identity_assertions,
            "placement": self.placement,
            "completed_at": self.completed_at,
            "parts": [part.as_json() for part in self.parts],
        }


class FilesystemStorageAdapter:
    """Immediate-read local storage with durable exact-size admission."""

    def __init__(self, config: FilesystemStorageAdapterConfig) -> None:
        self._config = config
        if config.root.exists() and config.root.is_symlink():
            raise ValueError("filesystem adapter root must not be a symbolic link")
        self._root = config.root.resolve(strict=False)
        self._capacity_lock = threading.RLock()
        self._gates = tuple(_ObjectGate() for _ in range(_GATE_SHARDS))
        self._closed = False
        self._initialize_root()
        self._instance_fd = os.open(
            self._root / "instance.lock",
            os.O_RDWR | os.O_CREAT | os.O_CLOEXEC | os.O_NOFOLLOW,
            _FILE_MODE,
        )
        try:
            fcntl.flock(self._instance_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            os.close(self._instance_fd)
            raise RuntimeError(
                "filesystem adapter root is already owned by another process"
            ) from exc
        try:
            self._reconcile_private_state()
        except BaseException:
            fcntl.flock(self._instance_fd, fcntl.LOCK_UN)
            os.close(self._instance_fd)
            raise

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        fcntl.flock(self._instance_fd, fcntl.LOCK_UN)
        os.close(self._instance_fd)

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def descriptor(self) -> AdapterDescriptor:
        return AdapterDescriptor(
            implementation_id=self._config.implementation_id,
            implementation_version=self._config.implementation_version,
            read_mode="immediate",
            minimum_nonfinal_segment_bytes=self._config.segment_bytes,
            maximum_segment_bytes=self._config.segment_bytes,
            maximum_segment_count=None,
        )

    def readiness(self) -> None:
        """Verify that the root and its private state remain accessible.

        A full cache remains ready: admission expresses that condition through
        ``insufficient_storage`` so Riverhog may evict or select a fallback.
        """

        self._require_open()
        os.fstat(self._instance_fd)
        os.statvfs(self._root)
        for name in ("objects", "writes", "staging"):
            details = (self._root / name).stat(follow_symlinks=False)
            if not stat.S_ISDIR(details.st_mode):
                raise RuntimeError("filesystem adapter internal path is not a directory")

    def begin_write(self, request: WriteStartRequest) -> WriteSession:
        self._require_open()
        object_key = self._object_key(request.object_path)
        with self._capacity_lock, self._gate(object_key).write():
            write_dir = self._write_dir_by_key(object_key)
            state_path = write_dir / "state.json"
            if write_dir.exists():
                if not state_path.exists():
                    shutil.rmtree(write_dir)
                else:
                    state = self._read_write_state(state_path, request.object_path)
                    self._verify_active_payload(write_dir, state)
                    if not state.matches(request):
                        raise StorageAdapterRejection(
                            "identity_conflict",
                            "another nonterminal write exists for this object path",
                        )
                    return state.session()
            self._check_physical_capacity(request.expected_bytes)
            write_dir.mkdir(parents=True, mode=_INTERNAL_MODE)
            payload_path = write_dir / "payload.data"
            try:
                self._create_reserved_file(payload_path, request.expected_bytes)
                state = _WriteState(
                    token=uuid.uuid4().hex,
                    object_path=request.object_path,
                    expected_bytes=request.expected_bytes,
                    content_type=request.content_type,
                    required_identity_assertions=dict(request.required_identity_assertions),
                    placement=request.placement,
                    created_at=format_utc_timestamp(utc_now()),
                    segments={},
                )
                self._write_json_atomic(state_path, state.as_json())
                self._fsync_dir(write_dir)
            except BaseException:
                shutil.rmtree(write_dir, ignore_errors=True)
                self._prune_empty_parents(write_dir.parent, stop=self._root / "writes")
                raise
            return state.session()

    def write_segment(
        self,
        *,
        session: WriteSession,
        number: int,
        stored_bytes: int,
        content: BinaryContent,
    ) -> WriteSegmentReceipt:
        self._require_open()
        object_key = self._object_key(session.object_path)
        with self._gate(object_key).write():
            write_dir = self._write_dir_by_key(object_key)
            state_path = write_dir / "state.json"
            state = self._require_write_state(state_path, session)
            self._verify_active_payload(write_dir, state)
            if number < 1:
                raise StorageAdapterRejection(
                    "invalid_request", "write segment number must be positive"
                )
            if stored_bytes < 1 or stored_bytes > self._config.segment_bytes:
                raise StorageAdapterRejection(
                    "invalid_request",
                    "write segment size is outside the filesystem adapter limit",
                )
            existing = state.segments.get(number)
            payload_path = write_dir / "payload.data"
            if existing is not None:
                if existing.stored_bytes != stored_bytes:
                    raise StorageAdapterRejection(
                        "identity_conflict",
                        "accepted filesystem segment has a different byte count",
                    )
                self._verify_replayed_segment(payload_path, existing, content)
                return existing.receipt()

            accepted_bytes = sum(item.stored_bytes for item in state.segments.values())
            if accepted_bytes + stored_bytes > state.expected_bytes:
                raise StorageAdapterRejection(
                    "invalid_request",
                    "accepted filesystem segments would exceed the admitted object bytes",
                )
            offset = max(
                (item.offset + item.stored_bytes for item in state.segments.values()),
                default=0,
            )
            if offset + stored_bytes > state.expected_bytes:
                raise RuntimeError("filesystem write extent accounting is inconsistent")
            digest = self._write_payload_range(
                payload_path,
                offset=offset,
                expected_bytes=stored_bytes,
                content=content,
            )
            segment = _SegmentRecord(
                number=number,
                offset=offset,
                segment_token=digest,
                stored_bytes=stored_bytes,
                stored_sha256=digest,
            )
            try:
                self._write_json_atomic(state_path, state.with_segment(segment).as_json())
            except OSError as exc:
                self._raise_post_admission_io(exc)
            return segment.receipt()

    def list_segments(self, session: WriteSession) -> WriteSegmentSet:
        self._require_open()
        object_key = self._object_key(session.object_path)
        with self._gate(object_key).write():
            write_dir = self._write_dir_by_key(object_key)
            state = self._require_write_state(write_dir / "state.json", session)
            self._verify_active_payload(write_dir, state)
            return WriteSegmentSet(
                session=session,
                segments=tuple(
                    state.segments[number].receipt() for number in sorted(state.segments)
                ),
            )

    def complete_write(self, request: WriteCompleteRequest) -> CompletedObjectReceipt:
        self._require_open()
        object_key = self._object_key(request.session.object_path)
        with self._capacity_lock, self._gate(object_key).write():
            existing = self._load_object(request.session.object_path, revision=None)
            if existing is not None:
                self._require_completed_match(
                    existing,
                    expected_bytes=request.expected_bytes,
                    expected_content_type=request.expected_content_type,
                    required_identity_assertions=request.required_identity_assertions,
                    expected_placement=request.expected_placement,
                )
                write_dir = self._write_dir_by_key(object_key)
                state_path = write_dir / "state.json"
                if state_path.exists():
                    state = self._read_write_state(state_path, request.session.object_path)
                    if (
                        state.token == request.session.write_token
                        and state.expected_bytes == request.session.expected_bytes
                    ):
                        self._remove_write_dir(object_key)
                return self._completed_receipt(existing)

            write_dir = self._write_dir_by_key(object_key)
            state = self._require_write_state(write_dir / "state.json", request.session)
            self._verify_active_payload(write_dir, state)
            if (
                state.expected_bytes != request.expected_bytes
                or state.content_type != request.expected_content_type
                or state.required_identity_assertions != request.required_identity_assertions
                or state.placement != request.expected_placement
            ):
                raise StorageAdapterRejection(
                    "identity_conflict",
                    "write completion differs from the admitted filesystem session",
                )
            expected_receipts = tuple(
                state.segments[number].receipt() for number in sorted(state.segments)
            )
            if request.segments != expected_receipts:
                raise StorageAdapterRejection(
                    "identity_conflict",
                    "write completion segment receipts differ from persisted state",
                )
            self._validate_complete_segments(state)
            completed_at = format_utc_timestamp(utc_now())
            record = _ObjectRecord(
                object_path=state.object_path,
                revision=state.token,
                entity_token=state.token,
                stored_bytes=state.expected_bytes,
                stored_sha256=None,
                content_type=state.content_type,
                required_identity_assertions=state.required_identity_assertions,
                placement=state.placement,
                completed_at=completed_at,
                parts=tuple(state.segments[number].part() for number in sorted(state.segments)),
            )
            self._install_write_as_revision(object_key, write_dir, record)
            return self._completed_receipt(record)

    def find_completed_write(
        self,
        request: CompletedWriteLookupRequest,
    ) -> CompletedObjectReceipt | None:
        self._require_open()
        object_key = self._object_key(request.object_path)
        gate = self._gate(object_key)
        gate.acquire_read()
        try:
            record = self._load_object(request.object_path, revision=None)
            if record is None:
                return None
            self._require_completed_match(
                record,
                expected_bytes=request.expected_bytes,
                expected_content_type=request.expected_content_type,
                required_identity_assertions=request.required_identity_assertions,
                expected_placement=request.expected_placement,
            )
            self._verify_object_payload(self._revision_dir(object_key, record.revision), record)
            return self._completed_receipt(record)
        finally:
            gate.release_read()

    def abort_write(self, session: WriteSession) -> None:
        self._require_open()
        object_key = self._object_key(session.object_path)
        with self._capacity_lock, self._gate(object_key).write():
            write_dir = self._write_dir_by_key(object_key)
            state_path = write_dir / "state.json"
            if not state_path.exists():
                return
            self._require_write_state(state_path, session)
            self._remove_write_dir(object_key)

    def put_small_object(
        self,
        request: SmallObjectWriteRequest,
        content: BinaryContent,
    ) -> ImmutableObjectReceipt:
        self._require_open()
        object_key = self._object_key(request.object_path)
        with self._capacity_lock, self._gate(object_key).write():
            current = self._load_object(request.object_path, revision=None)
            if current is not None and self._small_object_matches(current, request):
                _drain_and_verify_content(
                    content,
                    expected_bytes=request.stored_bytes,
                    expected_sha256=request.stored_sha256,
                )
                return self._immutable_receipt(current)
            if current is not None and request.mode == "create_only":
                raise StorageAdapterRejection(
                    "identity_conflict",
                    "filesystem object already exists with a different identity",
                )
            if request.expected_current_stored_sha256 is not None and (
                current is None or current.stored_sha256 != request.expected_current_stored_sha256
            ):
                raise StorageAdapterRejection(
                    "identity_conflict",
                    "filesystem current object differs from the replacement fence",
                )

            revision = self._small_revision(request)
            historical = self._load_object(request.object_path, revision=revision)
            if historical is not None:
                if not self._small_object_matches(historical, request):
                    raise RuntimeError("filesystem deterministic small-object revision conflicts")
                _drain_and_verify_content(
                    content,
                    expected_bytes=request.stored_bytes,
                    expected_sha256=request.stored_sha256,
                )
                object_dir = self._object_dir_by_key(object_key)
                self._write_text_atomic(object_dir / "current", revision)
                return self._immutable_receipt(historical)

            self._check_physical_capacity(request.stored_bytes)
            staging_path = self._root / "staging" / f"small-{uuid.uuid4().hex}.data"
            try:
                self._create_reserved_file(staging_path, request.stored_bytes)
                digest = self._write_payload_range(
                    staging_path,
                    offset=0,
                    expected_bytes=request.stored_bytes,
                    content=content,
                )
                if digest != request.stored_sha256:
                    raise StorageAdapterRejection(
                        "integrity_failure",
                        "filesystem small-object digest differs from its request",
                    )
                record = _ObjectRecord(
                    object_path=request.object_path,
                    revision=revision,
                    entity_token=digest,
                    stored_bytes=request.stored_bytes,
                    stored_sha256=digest,
                    content_type=request.content_type,
                    required_identity_assertions=dict(request.required_identity_assertions),
                    placement=request.placement,
                    completed_at=format_utc_timestamp(utc_now()),
                    parts=(
                        _PartRecord(
                            number=1,
                            offset=0,
                            stored_bytes=request.stored_bytes,
                            stored_sha256=digest,
                        ),
                    ),
                )
                self._install_staged_revision(object_key, staging_path, record)
                return self._immutable_receipt(record)
            finally:
                staging_path.unlink(missing_ok=True)

    def head_object(self, request: ObjectHeadRequest) -> ObjectMetadataReceipt | None:
        self._require_open()
        object_key = self._object_key(request.object.object_path)
        gate = self._gate(object_key)
        gate.acquire_read()
        try:
            record = self._load_object(
                request.object.object_path,
                revision=request.object.revision,
            )
            if record is None:
                return None
            self._require_placement(record, request.expected_placement)
            self._verify_object_payload(self._revision_dir(object_key, record.revision), record)
            return ObjectMetadataReceipt(
                object_path=record.object_path,
                revision=record.revision,
                entity_token=record.entity_token,
                content_type=record.content_type,
                stored_bytes=record.stored_bytes,
                stored_sha256=record.stored_sha256,
                observed_identity_assertions=record.required_identity_assertions,
                verified_placement=record.placement,
                completed_at=record.completed_at,
            )
        finally:
            gate.release_read()

    def read_object(self, request: ObjectReadRequest) -> ObjectReadStream:
        self._require_open()
        object_key = self._object_key(request.object.object_path)
        gate = self._gate(object_key)
        gate.acquire_read()
        released = False

        def release() -> None:
            nonlocal released
            if not released:
                released = True
                gate.release_read()

        try:
            record = self._load_object(
                request.object.object_path,
                revision=request.object.revision,
            )
            if record is None:
                raise StorageAdapterRejection("not_found", "filesystem object was not found")
            if record.stored_bytes != request.expected_bytes:
                raise StorageAdapterRejection(
                    "identity_conflict",
                    "filesystem object bytes differ from the read request",
                )
            revision_dir = self._revision_dir(object_key, record.revision)
            self._verify_object_payload(revision_dir, record)
            offset = request.offset if request.offset is not None else 0
            read_bytes = request.size if request.size is not None else record.stored_bytes
            if offset + read_bytes > record.stored_bytes:
                raise StorageAdapterRejection(
                    "invalid_range",
                    "filesystem object range exceeds the object bytes",
                )
        except BaseException:
            release()
            raise

        def content() -> Iterator[bytes]:
            remaining = read_bytes
            logical_skip = offset
            fd = os.open(
                revision_dir / "payload.data",
                os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW,
            )
            try:
                for part in record.parts:
                    if remaining == 0:
                        break
                    if logical_skip >= part.stored_bytes:
                        logical_skip -= part.stored_bytes
                        continue
                    part_cursor = logical_skip
                    take = min(remaining, part.stored_bytes - part_cursor)
                    absolute = part.offset + part_cursor
                    while take:
                        chunk = os.pread(
                            fd,
                            min(take, self._config.read_chunk_bytes),
                            absolute,
                        )
                        if not chunk:
                            raise StorageAdapterRejection(
                                "integrity_failure",
                                "filesystem object ended before its metadata",
                            )
                        absolute += len(chunk)
                        take -= len(chunk)
                        remaining -= len(chunk)
                        yield chunk
                    logical_skip = 0
                if remaining:
                    raise StorageAdapterRejection(
                        "integrity_failure",
                        "filesystem object ended before its recorded byte count",
                    )
            finally:
                os.close(fd)
                release()

        return ObjectReadStream(
            receipt=ObjectReadReceipt(
                object=ObjectLocator(
                    object_path=record.object_path,
                    revision=record.revision,
                ),
                total_bytes=record.stored_bytes,
                offset=offset,
                read_bytes=read_bytes,
            ),
            content=content(),
            close=release,
        )

    def delete_object(self, request: DeleteObjectRequest) -> None:
        self._require_open()
        object_key = self._object_key(request.object.object_path)
        with self._capacity_lock, self._gate(object_key).write():
            object_dir = self._object_dir_by_key(object_key)
            if not object_dir.exists():
                return
            self._verify_object_path_file(object_dir, request.object.object_path)
            if request.mode == "all_versions":
                shutil.rmtree(object_dir)
                self._prune_empty_parents(object_dir.parent, stop=self._root / "objects")
                return
            if request.mode == "current":
                if request.expected_current_stored_sha256 is not None:
                    current = self._load_object(request.object.object_path, revision=None)
                    if current is None:
                        return
                    if current.stored_sha256 != request.expected_current_stored_sha256:
                        raise StorageAdapterRejection(
                            "identity_conflict",
                            "filesystem current object differs from the deletion fence",
                        )
                (object_dir / "current").unlink(missing_ok=True)
                self._fsync_dir(object_dir)
                return
            revision = request.object.revision
            if revision is None:
                raise StorageAdapterRejection(
                    "invalid_request",
                    "exact revision deletion requires a revision",
                )
            revision_dir = self._revision_dir(object_key, revision)
            if not revision_dir.exists():
                return
            current_revision = self._read_current_revision(object_dir)
            shutil.rmtree(revision_dir)
            if current_revision == revision:
                (object_dir / "current").unlink(missing_ok=True)
            revisions_dir = object_dir / "revisions"
            if not any(revisions_dir.iterdir()):
                shutil.rmtree(object_dir)
                self._prune_empty_parents(object_dir.parent, stop=self._root / "objects")
            else:
                self._fsync_dir(object_dir)

    def delete_prefix(self, request: DeletePrefixRequest) -> int:
        self._require_open()
        affected = 0
        with self._capacity_lock:
            for object_dir in tuple(self._iter_object_dirs()):
                try:
                    object_path = (object_dir / "path").read_text(encoding="utf-8")
                except FileNotFoundError:
                    continue
                if not object_path.startswith(request.object_prefix):
                    continue
                object_key = object_dir.name
                with self._gate(object_key).write():
                    if object_dir.exists():
                        shutil.rmtree(object_dir)
                        affected += 1
            self._prune_empty_tree(self._root / "objects")
        return affected

    def prepare_read(self, request: ReadPreparationRequest) -> ReadStatus:
        return ReadStatus(objects=request.objects, readiness=ReadReady())

    def read_status(self, request: ReadPreparationRequest) -> ReadStatus:
        return ReadStatus(objects=request.objects, readiness=ReadReady())

    def cleanup_read(self, request: ReadPreparationRequest) -> None:
        del request

    def _initialize_root(self) -> None:
        self._root.mkdir(parents=True, mode=_INTERNAL_MODE, exist_ok=True)
        root_details = self._root.stat(follow_symlinks=False)
        if not stat.S_ISDIR(root_details.st_mode):
            raise ValueError("filesystem adapter root must be a real directory")
        os.chmod(self._root, _INTERNAL_MODE)
        for name in ("objects", "writes", "staging"):
            path = self._root / name
            path.mkdir(mode=_INTERNAL_MODE, exist_ok=True)
            details = path.stat(follow_symlinks=False)
            if not stat.S_ISDIR(details.st_mode):
                raise ValueError(f"filesystem adapter internal {name} path must be a directory")
            os.chmod(path, _INTERNAL_MODE)

    def _reconcile_private_state(self) -> None:
        self._remove_atomic_temps(self._root / "objects")
        self._remove_atomic_temps(self._root / "writes")
        for path in tuple((self._root / "staging").iterdir()):
            if path.is_file() or path.is_symlink():
                path.unlink(missing_ok=True)
            elif path.is_dir():
                shutil.rmtree(path)
            else:
                raise RuntimeError("filesystem staging directory contains an unknown entry")

        active_revisions: set[tuple[str, str]] = set()
        for write_dir in tuple((self._root / "writes").glob("*/*/*")):
            if not write_dir.is_dir():
                raise RuntimeError("filesystem writes tree contains a non-directory entry")
            state_path = write_dir / "state.json"
            if not state_path.exists():
                shutil.rmtree(write_dir)
                continue
            state = _WriteState.from_json(self._read_json(state_path))
            if self._object_key(state.object_path) != write_dir.name:
                raise RuntimeError("filesystem write directory differs from its object identity")
            completed = self._load_object(state.object_path, revision=None)
            if completed is not None and completed.revision == state.token:
                self._remove_write_dir(write_dir.name)
                continue
            self._verify_active_payload(write_dir, state)
            installed = self._load_object(state.object_path, revision=state.token)
            if installed is not None:
                payload = write_dir / "payload.data"
                installed_payload = (
                    self._revision_dir(
                        write_dir.name,
                        state.token,
                    )
                    / "payload.data"
                )
                self._require_completed_match(
                    installed,
                    expected_bytes=state.expected_bytes,
                    expected_content_type=state.content_type,
                    required_identity_assertions=state.required_identity_assertions,
                    expected_placement=state.placement,
                )
                if not os.path.samefile(payload, installed_payload):
                    raise RuntimeError(
                        "filesystem installed revision differs from its active write"
                    )
                object_dir = self._object_dir_by_key(write_dir.name)
                self._write_text_atomic(object_dir / "current", state.token)
                self._remove_write_dir(write_dir.name)
                continue
            active_revisions.add((write_dir.name, state.token))
        self._prune_empty_tree(self._root / "writes")

        for object_dir in tuple(self._iter_object_dirs()):
            object_key = object_dir.name
            revisions_dir = object_dir / "revisions"
            if not revisions_dir.is_dir():
                raise RuntimeError("filesystem object revisions directory is missing")
            for revision_dir in tuple(revisions_dir.iterdir()):
                if revision_dir.name.startswith("."):
                    shutil.rmtree(revision_dir)
                    continue
                if not revision_dir.is_dir():
                    raise RuntimeError("filesystem object revision path is not a directory")
                if not (revision_dir / "metadata.json").exists():
                    if (object_key, revision_dir.name) not in active_revisions:
                        shutil.rmtree(revision_dir)
            current = self._read_current_revision(object_dir)
            if current is not None and not (revisions_dir / current / "metadata.json").exists():
                (object_dir / "current").unlink(missing_ok=True)
            if not any(revisions_dir.iterdir()) and not (object_dir / "current").exists():
                shutil.rmtree(object_dir)
        self._prune_empty_tree(self._root / "objects")

    def _require_open(self) -> None:
        if self._closed:
            raise RuntimeError("filesystem storage adapter is closed")

    def _gate(self, object_key: str) -> _ObjectGate:
        shard = int(object_key[:8], 16) % len(self._gates)
        return self._gates[shard]

    @staticmethod
    def _object_key(object_path: str) -> str:
        return hashlib.sha256(object_path.encode("utf-8")).hexdigest()

    def _object_dir_by_key(self, object_key: str) -> Path:
        return self._root / "objects" / object_key[:2] / object_key[2:4] / object_key

    def _write_dir_by_key(self, object_key: str) -> Path:
        return self._root / "writes" / object_key[:2] / object_key[2:4] / object_key

    def _revision_dir(self, object_key: str, revision: str) -> Path:
        if not revision or "/" in revision or "\\" in revision or revision in {".", ".."}:
            raise StorageAdapterRejection("not_found", "filesystem object revision was not found")
        return self._object_dir_by_key(object_key) / "revisions" / revision

    def _check_physical_capacity(self, expected_bytes: int) -> None:
        stats = os.statvfs(self._root)
        available = stats.f_bavail * stats.f_frsize
        if expected_bytes + self._config.minimum_free_bytes > available:
            raise StorageAdapterRejection(
                "insufficient_storage",
                "filesystem adapter cannot preserve its minimum free-space reserve",
            )

    def _create_reserved_file(self, path: Path, expected_bytes: int) -> None:
        fd = os.open(
            path,
            os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
            _FILE_MODE,
        )
        try:
            if expected_bytes:
                try:
                    os.posix_fallocate(fd, 0, expected_bytes)
                except OSError as exc:
                    if exc.errno in _CAPACITY_ERRORS:
                        raise StorageAdapterRejection(
                            "insufficient_storage",
                            "filesystem could not reserve the declared object bytes",
                        ) from exc
                    if exc.errno in _UNSUPPORTED_ALLOCATION_ERRORS:
                        raise RuntimeError(
                            "filesystem does not support required exact block reservation"
                        ) from exc
                    raise
            os.fsync(fd)
        except BaseException:
            os.close(fd)
            path.unlink(missing_ok=True)
            raise
        os.close(fd)
        self._fsync_dir(path.parent)

    def _write_payload_range(
        self,
        path: Path,
        *,
        offset: int,
        expected_bytes: int,
        content: BinaryContent,
    ) -> str:
        fd = os.open(path, os.O_RDWR | os.O_CLOEXEC | os.O_NOFOLLOW)
        digest = hashlib.sha256()
        observed = 0
        try:
            for chunk in _content_chunks(content):
                observed += len(chunk)
                if observed > expected_bytes:
                    raise StorageAdapterRejection(
                        "integrity_failure",
                        "opaque content exceeds its declared byte count",
                    )
                _pwrite_all(fd, chunk, offset + observed - len(chunk))
                digest.update(chunk)
            if observed != expected_bytes:
                raise StorageAdapterRejection(
                    "integrity_failure",
                    "opaque content ended before its declared byte count",
                )
            os.fsync(fd)
        except OSError as exc:
            self._raise_post_admission_io(exc)
        finally:
            os.close(fd)
        return digest.hexdigest()

    @staticmethod
    def _raise_post_admission_io(exc: OSError) -> None:
        if exc.errno in _CAPACITY_ERRORS:
            raise StorageAdapterRejection(
                "provider_unavailable",
                "filesystem could not honor an admitted write session",
            ) from exc
        raise exc

    def _require_write_state(self, state_path: Path, session: WriteSession) -> _WriteState:
        if not state_path.exists():
            raise StorageAdapterRejection("not_found", "filesystem write session was not found")
        state = self._read_write_state(state_path, session.object_path)
        if state.token != session.write_token or state.expected_bytes != session.expected_bytes:
            raise StorageAdapterRejection(
                "identity_conflict",
                "filesystem write session differs from persisted state",
            )
        return state

    def _read_write_state(self, state_path: Path, object_path: str) -> _WriteState:
        state = _WriteState.from_json(self._read_json(state_path))
        if state.object_path != object_path:
            raise RuntimeError("filesystem write state path differs from its storage identity")
        return state

    def _verify_active_payload(self, write_dir: Path, state: _WriteState) -> None:
        payload = write_dir / "payload.data"
        try:
            details = payload.stat(follow_symlinks=False)
        except FileNotFoundError as exc:
            raise RuntimeError("filesystem write payload is missing") from exc
        if not stat.S_ISREG(details.st_mode) or details.st_size != state.expected_bytes:
            raise RuntimeError("filesystem write payload differs from its admitted size")

    def _verify_replayed_segment(
        self,
        payload_path: Path,
        segment: _SegmentRecord,
        content: BinaryContent,
    ) -> None:
        fd = os.open(payload_path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
        digest = hashlib.sha256()
        observed = 0
        try:
            for chunk in _content_chunks(content):
                observed += len(chunk)
                if observed > segment.stored_bytes:
                    raise StorageAdapterRejection(
                        "identity_conflict",
                        "replayed segment exceeds the accepted byte count",
                    )
                persisted = _pread_exact(
                    fd,
                    len(chunk),
                    segment.offset + observed - len(chunk),
                )
                if persisted != chunk:
                    raise StorageAdapterRejection(
                        "identity_conflict",
                        "replayed segment differs from the accepted bytes",
                    )
                digest.update(chunk)
            if observed != segment.stored_bytes or digest.hexdigest() != segment.stored_sha256:
                raise StorageAdapterRejection(
                    "identity_conflict",
                    "replayed segment differs from the accepted identity",
                )
        finally:
            os.close(fd)

    def _validate_complete_segments(self, state: _WriteState) -> None:
        segments = [state.segments[number] for number in sorted(state.segments)]
        if [segment.number for segment in segments] != list(range(1, len(segments) + 1)):
            raise StorageAdapterRejection(
                "invalid_request",
                "filesystem write segments are not contiguous",
            )
        if sum(segment.stored_bytes for segment in segments) != state.expected_bytes:
            raise StorageAdapterRejection(
                "invalid_request",
                "filesystem write segments do not fill the admitted object bytes",
            )
        if any(segment.stored_bytes < self._config.segment_bytes for segment in segments[:-1]):
            raise StorageAdapterRejection(
                "invalid_request",
                "filesystem completion contains an undersized non-final segment",
            )

    def _install_write_as_revision(
        self,
        object_key: str,
        write_dir: Path,
        record: _ObjectRecord,
    ) -> None:
        object_dir = self._ensure_object_dir(object_key, record.object_path)
        revisions_dir = object_dir / "revisions"
        revision_dir = self._revision_dir(object_key, record.revision)
        source = write_dir / "payload.data"
        if revision_dir.exists():
            persisted = self._load_object(record.object_path, revision=record.revision)
            if persisted != record:
                raise RuntimeError("filesystem revision conflicts with its write session")
            if not os.path.samefile(source, revision_dir / "payload.data"):
                raise RuntimeError("filesystem revision payload conflicts with its write session")
        else:
            temporary = revisions_dir / f".{record.revision}.{uuid.uuid4().hex}.tmp"
            temporary.mkdir(mode=_INTERNAL_MODE)
            try:
                destination = temporary / "payload.data"
                os.link(source, destination, follow_symlinks=False)
                os.chmod(destination, _FILE_MODE)
                self._write_json_atomic(temporary / "metadata.json", record.as_json())
                self._fsync_dir(temporary)
                os.rename(temporary, revision_dir)
                self._fsync_dir(revisions_dir)
            finally:
                if temporary.exists():
                    shutil.rmtree(temporary)
        self._write_text_atomic(object_dir / "current", record.revision)
        self._fsync_dir(object_dir)
        self._remove_write_dir(object_key)

    def _install_staged_revision(
        self,
        object_key: str,
        staging_path: Path,
        record: _ObjectRecord,
    ) -> None:
        object_dir = self._ensure_object_dir(object_key, record.object_path)
        revisions_dir = object_dir / "revisions"
        revision_dir = self._revision_dir(object_key, record.revision)
        if revision_dir.exists():
            persisted = self._load_object(record.object_path, revision=record.revision)
            if persisted != record:
                raise RuntimeError("filesystem deterministic small-object revision conflicts")
            self._verify_object_payload(revision_dir, persisted)
            staging_path.unlink(missing_ok=True)
        else:
            temporary = revisions_dir / f".{record.revision}.{uuid.uuid4().hex}.tmp"
            temporary.mkdir(mode=_INTERNAL_MODE)
            try:
                destination = temporary / "payload.data"
                os.replace(staging_path, destination)
                os.chmod(destination, _FILE_MODE)
                self._write_json_atomic(temporary / "metadata.json", record.as_json())
                self._fsync_dir(temporary)
                os.rename(temporary, revision_dir)
                self._fsync_dir(revisions_dir)
            finally:
                if temporary.exists():
                    shutil.rmtree(temporary)
        self._write_text_atomic(object_dir / "current", record.revision)
        self._fsync_dir(object_dir)

    def _ensure_object_dir(self, object_key: str, object_path: str) -> Path:
        object_dir = self._object_dir_by_key(object_key)
        object_dir.mkdir(parents=True, mode=_INTERNAL_MODE, exist_ok=True)
        path_file = object_dir / "path"
        if path_file.exists():
            self._verify_object_path_file(object_dir, object_path)
        else:
            self._write_text_atomic(path_file, object_path)
        (object_dir / "revisions").mkdir(mode=_INTERNAL_MODE, exist_ok=True)
        return object_dir

    @staticmethod
    def _verify_object_path_file(object_dir: Path, expected: str) -> None:
        try:
            actual = (object_dir / "path").read_text(encoding="utf-8")
        except FileNotFoundError as exc:
            raise RuntimeError("filesystem object identity file is missing") from exc
        if actual != expected:
            raise RuntimeError("filesystem object hash collision or identity corruption")

    def _load_object(self, object_path: str, *, revision: str | None) -> _ObjectRecord | None:
        object_key = self._object_key(object_path)
        object_dir = self._object_dir_by_key(object_key)
        if not object_dir.exists():
            return None
        self._verify_object_path_file(object_dir, object_path)
        selected = revision or self._read_current_revision(object_dir)
        if selected is None:
            return None
        metadata_path = self._revision_dir(object_key, selected) / "metadata.json"
        if not metadata_path.exists():
            return None
        record = _ObjectRecord.from_json(self._read_json(metadata_path))
        if record.object_path != object_path or record.revision != selected:
            raise RuntimeError("filesystem object metadata differs from its path identity")
        return record

    @staticmethod
    def _read_current_revision(object_dir: Path) -> str | None:
        try:
            revision = (object_dir / "current").read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        if not revision:
            raise RuntimeError("filesystem current revision pointer is empty")
        return revision

    def _verify_object_payload(self, revision_dir: Path, record: _ObjectRecord) -> None:
        payload = revision_dir / "payload.data"
        try:
            details = payload.stat(follow_symlinks=False)
        except FileNotFoundError as exc:
            raise StorageAdapterRejection(
                "integrity_failure",
                "filesystem object payload is missing",
            ) from exc
        if not stat.S_ISREG(details.st_mode) or details.st_size != record.stored_bytes:
            raise StorageAdapterRejection(
                "integrity_failure",
                "filesystem object payload differs from its metadata",
            )

    @staticmethod
    def _small_revision(request: SmallObjectWriteRequest) -> str:
        identity = json.dumps(
            {
                "schema": "riverhog-filesystem-small-object-revision/v1",
                "object_path": request.object_path,
                "stored_bytes": request.stored_bytes,
                "stored_sha256": request.stored_sha256,
                "content_type": request.content_type,
                "required_identity_assertions": request.required_identity_assertions,
                "placement": request.placement,
            },
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        return hashlib.sha256(identity).hexdigest()

    @staticmethod
    def _remove_atomic_temps(root: Path) -> None:
        for path in tuple(root.rglob(".*.tmp")):
            if path.is_dir():
                shutil.rmtree(path)
            else:
                path.unlink(missing_ok=True)

    @staticmethod
    def _small_object_matches(
        record: _ObjectRecord,
        request: SmallObjectWriteRequest,
    ) -> bool:
        return (
            record.stored_bytes == request.stored_bytes
            and record.stored_sha256 == request.stored_sha256
            and record.content_type == request.content_type
            and record.required_identity_assertions == request.required_identity_assertions
            and record.placement == request.placement
        )

    @staticmethod
    def _require_completed_match(
        record: _ObjectRecord,
        *,
        expected_bytes: int,
        expected_content_type: str,
        required_identity_assertions: dict[str, str],
        expected_placement: ObjectPlacement,
    ) -> None:
        if (
            record.stored_bytes != expected_bytes
            or record.content_type != expected_content_type
            or record.required_identity_assertions != required_identity_assertions
            or record.placement != expected_placement
        ):
            raise StorageAdapterRejection(
                "identity_conflict",
                "completed filesystem object differs from the requested identity",
            )

    @staticmethod
    def _require_placement(record: _ObjectRecord, expected: ObjectPlacement) -> None:
        if record.placement != expected:
            raise StorageAdapterRejection(
                "identity_conflict",
                "filesystem object placement differs from its request",
            )

    @staticmethod
    def _completed_receipt(record: _ObjectRecord) -> CompletedObjectReceipt:
        return CompletedObjectReceipt(
            object_path=record.object_path,
            revision=record.revision,
            entity_token=record.entity_token,
            stored_bytes=record.stored_bytes,
            verified_content_type=record.content_type,
            verified_identity_assertions=record.required_identity_assertions,
            verified_placement=record.placement,
            completed_at=record.completed_at,
        )

    @staticmethod
    def _immutable_receipt(record: _ObjectRecord) -> ImmutableObjectReceipt:
        if record.stored_sha256 is None:
            raise RuntimeError("filesystem immutable object is missing its stored digest")
        return ImmutableObjectReceipt(
            object_path=record.object_path,
            revision=record.revision,
            entity_token=record.entity_token,
            stored_bytes=record.stored_bytes,
            stored_sha256=record.stored_sha256,
            verified_content_type=record.content_type,
            verified_identity_assertions=record.required_identity_assertions,
            verified_placement=record.placement,
            completed_at=record.completed_at,
        )

    def _remove_write_dir(self, object_key: str, *, missing_ok: bool = False) -> None:
        write_dir = self._write_dir_by_key(object_key)
        if not write_dir.exists():
            if missing_ok:
                return
            raise RuntimeError("filesystem write directory is missing")
        shutil.rmtree(write_dir)
        self._prune_empty_parents(write_dir.parent, stop=self._root / "writes")

    def _iter_object_dirs(self) -> Iterator[Path]:
        for path in (self._root / "objects").glob("*/*/*"):
            if path.is_dir():
                yield path

    @staticmethod
    def _prune_empty_parents(path: Path, *, stop: Path) -> None:
        current = path
        while current != stop:
            try:
                current.rmdir()
            except OSError:
                return
            current = current.parent

    @staticmethod
    def _prune_empty_tree(root: Path) -> None:
        for first in tuple(root.iterdir()):
            if not first.is_dir():
                continue
            for second in tuple(first.iterdir()):
                if second.is_dir():
                    try:
                        second.rmdir()
                    except OSError:
                        pass
            try:
                first.rmdir()
            except OSError:
                pass

    @staticmethod
    def _read_json(path: Path) -> dict[str, Any]:
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError("filesystem adapter metadata is unreadable") from exc
        if not isinstance(raw, dict):
            raise RuntimeError("filesystem adapter metadata root is invalid")
        return cast(dict[str, Any], raw)

    def _write_json_atomic(self, path: Path, value: dict[str, Any]) -> None:
        payload = json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        self._write_bytes_atomic(path, payload)

    def _write_text_atomic(self, path: Path, value: str) -> None:
        self._write_bytes_atomic(path, value.encode("utf-8"))

    def _write_bytes_atomic(self, path: Path, payload: bytes) -> None:
        path.parent.mkdir(parents=True, mode=_INTERNAL_MODE, exist_ok=True)
        temporary = path.parent / f".{path.name}.{uuid.uuid4().hex}.tmp"
        fd = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
            _FILE_MODE,
        )
        try:
            _write_all(fd, payload)
            os.fsync(fd)
        except BaseException:
            os.close(fd)
            temporary.unlink(missing_ok=True)
            raise
        os.close(fd)
        try:
            os.replace(temporary, path)
            self._fsync_dir(path.parent)
        finally:
            temporary.unlink(missing_ok=True)

    @staticmethod
    def _fsync_dir(path: Path) -> None:
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def _content_chunks(content: BinaryContent) -> Iterator[bytes]:
    chunks: Iterable[bytes] = (content,) if isinstance(content, bytes) else content
    for chunk in chunks:
        if not isinstance(chunk, bytes):
            raise StorageAdapterRejection(
                "integrity_failure",
                "opaque content contains a non-byte chunk",
            )
        if chunk:
            yield chunk


def _drain_and_verify_content(
    content: BinaryContent,
    *,
    expected_bytes: int,
    expected_sha256: str,
) -> None:
    digest = hashlib.sha256()
    observed = 0
    for chunk in _content_chunks(content):
        observed += len(chunk)
        if observed > expected_bytes:
            raise StorageAdapterRejection(
                "integrity_failure",
                "opaque content exceeds its declared byte count",
            )
        digest.update(chunk)
    if observed != expected_bytes or digest.hexdigest() != expected_sha256:
        raise StorageAdapterRejection(
            "integrity_failure",
            "opaque content differs from its declared identity",
        )


def _write_all(fd: int, payload: bytes) -> None:
    offset = 0
    while offset < len(payload):
        written = os.write(fd, payload[offset:])
        if written <= 0:
            raise OSError("filesystem write made no progress")
        offset += written


def _pwrite_all(fd: int, payload: bytes, offset: int) -> None:
    written = 0
    while written < len(payload):
        count = os.pwrite(fd, payload[written:], offset + written)
        if count <= 0:
            raise OSError("filesystem positioned write made no progress")
        written += count


def _pread_exact(fd: int, size: int, offset: int) -> bytes:
    result = bytearray()
    while len(result) < size:
        chunk = os.pread(fd, size - len(result), offset + len(result))
        if not chunk:
            break
        result.extend(chunk)
    return bytes(result)


def _validate_extents(
    extents: Iterable[tuple[int, int]],
    *,
    maximum: int,
    label: str,
) -> None:
    ordered = sorted(extents)
    end = 0
    for offset, size in ordered:
        if offset < end or offset + size > maximum:
            raise RuntimeError(f"{label} extents overlap or exceed their payload")
        end = offset + size


def _required_dict(value: object, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise RuntimeError(f"{label} metadata is invalid")
    return cast(dict[str, Any], value)


def _required_int(raw: dict[str, Any], key: str, *, minimum: int) -> int:
    value = raw[key]
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise RuntimeError(f"filesystem metadata field {key} is invalid")
    return value


def _required_string(raw: dict[str, Any], key: str) -> str:
    value = raw[key]
    if not isinstance(value, str) or not value:
        raise RuntimeError(f"filesystem metadata field {key} is invalid")
    return value


def _required_sha256(raw: dict[str, Any], key: str) -> str:
    value = raw[key]
    if not _is_sha256(value):
        raise RuntimeError(f"filesystem metadata field {key} is not a SHA-256 digest")
    return cast(str, value)


def _optional_sha256(raw: dict[str, Any], key: str) -> str | None:
    value = raw[key]
    if value is None:
        return None
    if not _is_sha256(value):
        raise RuntimeError(f"filesystem metadata field {key} is not a SHA-256 digest")
    return cast(str, value)


def _is_string_mapping(value: object) -> bool:
    return isinstance(value, dict) and all(
        isinstance(key, str) and isinstance(item, str) for key, item in value.items()
    )


def _is_sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


__all__ = ["FilesystemStorageAdapter", "FilesystemStorageAdapterConfig"]
