from __future__ import annotations

import hashlib
import time
from collections.abc import Callable, Iterator, Sequence
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, replace

from riverhog_age import ResumableAgeScryptSession, UploadState
from riverhog_protocol.paths import normalize_relpath

from riverhog_core.age_range import (
    iter_decrypt_age_plaintext_range,
    plan_age_plaintext_range,
)
from riverhog_core.domain.archive import StoredArchivePart
from riverhog_core.ports.archive_objects import ArchiveObjectRangeStore
from riverhog_core.streaming_age import ResumableAgeSessionCache
from riverhog_core.throughput import (
    DEFAULT_AGE_SESSION_CACHE_ENTRIES,
    DEFAULT_RETRIEVAL_MAX_INFLIGHT_BYTES,
    DEFAULT_RETRIEVAL_READ_CHUNK_BYTES,
    DEFAULT_RETRIEVAL_REQUEST_CONCURRENCY,
    ArchiveTransferResources,
    TransferConcurrencyGate,
    TransferTiming,
    WeightedByteSemaphore,
)

TransferTimingObserver = Callable[[TransferTiming], None]


@dataclass(frozen=True, slots=True)
class RawVolumeRetrievalSource:
    volume_id: str
    object_path: str
    revision: str | None
    source_path: str
    file_offset: int
    plaintext_bytes: int
    file_bytes: int
    file_sha256: str
    age_state_json: str
    parts: tuple[StoredArchivePart, ...]

    def __post_init__(self) -> None:
        if not self.volume_id.startswith("segment-") or not self.object_path:
            raise ValueError("raw retrieval volume identity is invalid")
        if normalize_relpath(self.source_path) != self.source_path:
            raise ValueError("raw retrieval source path is not canonical")
        if self.file_offset < 0 or self.plaintext_bytes < 0 or self.file_bytes < 0:
            raise ValueError("raw retrieval byte range is invalid")
        if self.file_offset + self.plaintext_bytes > self.file_bytes:
            raise ValueError("raw retrieval volume exceeds its source file")
        state = UploadState.from_json_bytes(self.age_state_json)
        if state.plaintext_size != self.plaintext_bytes:
            raise ValueError("raw retrieval age state plaintext size mismatch")
        expected_start = 0
        for expected_number, part in enumerate(self.parts, start=1):
            if (
                part.number != expected_number
                or part.plaintext_start != expected_start
                or part.plaintext_bytes < 0
                or part.stored_bytes <= 0
            ):
                raise ValueError("raw retrieval part layout is invalid")
            expected_start += part.plaintext_bytes
        if expected_start != self.plaintext_bytes:
            raise ValueError("raw retrieval parts do not cover the volume")


@dataclass(frozen=True, slots=True)
class _RetrievedRawPart:
    number: int
    content: bytes
    timing: TransferTiming


class RawVolumeRangeReader:
    """Parallel, ordered retrieval of independently authenticated age archive-part ranges."""

    def __init__(
        self,
        store: ArchiveObjectRangeStore,
        *,
        passphrase: str | bytes,
        request_concurrency: int = DEFAULT_RETRIEVAL_REQUEST_CONCURRENCY,
        max_inflight_bytes: int = DEFAULT_RETRIEVAL_MAX_INFLIGHT_BYTES,
        read_working_bytes: int = DEFAULT_RETRIEVAL_READ_CHUNK_BYTES,
        byte_budget: WeightedByteSemaphore | None = None,
        request_gate: TransferConcurrencyGate | None = None,
        resources: ArchiveTransferResources | None = None,
        timing_observer: TransferTimingObserver | None = None,
        session_cache: ResumableAgeSessionCache | None = None,
        session_cache_entries: int = DEFAULT_AGE_SESSION_CACHE_ENTRIES,
    ) -> None:
        if request_concurrency < 1:
            raise ValueError("raw retrieval request concurrency must be positive")
        if read_working_bytes < 64 * 1024:
            raise ValueError("raw retrieval working buffer must be at least 64 KiB")
        self._store = store
        self._session_cache = session_cache or ResumableAgeSessionCache(
            passphrase,
            max_entries=session_cache_entries,
            derivation_gate=(resources.age_derivations if resources is not None else None),
        )
        self._request_concurrency = request_concurrency
        self._read_working_bytes = read_working_bytes
        if resources is not None and (byte_budget is not None or request_gate is not None):
            raise ValueError(
                "shared transfer resources cannot be combined with explicit retrieval limits"
            )
        self._byte_budget = (
            resources.retrieval_bytes
            if resources is not None
            else byte_budget or WeightedByteSemaphore(max_inflight_bytes)
        )
        self._request_gate = (
            resources.retrieval_requests
            if resources is not None
            else request_gate or TransferConcurrencyGate(request_concurrency)
        )
        self._timing_observer = timing_observer

    def iter_volume(self, source: RawVolumeRetrievalSource) -> Iterator[bytes]:
        yield from self.iter_volume_range(
            source,
            offset=0,
            size=source.plaintext_bytes,
        )

    def iter_volume_range(
        self,
        source: RawVolumeRetrievalSource,
        *,
        offset: int,
        size: int,
    ) -> Iterator[bytes]:
        """Read only authenticated archive parts overlapping one logical range."""

        if offset < 0 or size < 0 or offset + size > source.plaintext_bytes:
            raise ValueError("raw volume requested range is invalid")
        if size == 0:
            return
        state = UploadState.from_json_bytes(source.age_state_json)
        session = self._session_cache.get(state)
        part_offsets: dict[int, int] = {}
        cursor = 0
        for part in source.parts:
            part_offsets[part.number] = cursor
            cursor += part.stored_bytes
        requested_end = offset + size
        selected = tuple(
            part
            for part in source.parts
            if part.plaintext_start + part.plaintext_bytes > offset
            and part.plaintext_start < requested_end
        )
        if not selected:
            raise RuntimeError("raw retrieval range has no archive parts")
        pending: dict[int, Future[_RetrievedRawPart]] = {}
        reservations: dict[int, int] = {}
        next_submit = 0
        emitted = 0

        def reserve_bytes(index: int) -> int:
            part = selected[index]
            return max(
                1,
                part.plaintext_bytes + min(part.stored_bytes, self._read_working_bytes),
            )

        def submit(
            executor: ThreadPoolExecutor,
            index: int,
            *,
            block: bool,
        ) -> bool:
            amount = reserve_bytes(index)
            if block:
                queue_wait_seconds = self._byte_budget.acquire(amount)
            elif self._byte_budget.try_acquire(amount):
                queue_wait_seconds = 0.0
            else:
                return False
            try:
                part = selected[index]
                pending[index] = executor.submit(
                    self._read_part,
                    source=source,
                    state=state,
                    session=session,
                    part=part,
                    stored_offset=part_offsets[part.number],
                    queue_wait_seconds=queue_wait_seconds,
                )
                reservations[index] = amount
            except BaseException:
                self._byte_budget.release(amount)
                raise
            return True

        def fill(executor: ThreadPoolExecutor) -> None:
            nonlocal next_submit
            while next_submit < len(selected) and len(pending) < self._request_concurrency:
                if not submit(executor, next_submit, block=False):
                    return
                next_submit += 1

        with ThreadPoolExecutor(
            max_workers=min(self._request_concurrency, len(selected)),
            thread_name_prefix="riverhog-raw-read",
        ) as executor:
            submit(executor, next_submit, block=True)
            next_submit += 1
            fill(executor)
            try:
                for expected_index, part in enumerate(selected):
                    if expected_index not in pending:
                        submit(executor, expected_index, block=True)
                        if next_submit == expected_index:
                            next_submit += 1
                    retrieved = pending.pop(expected_index).result()
                    if retrieved.number != part.number:
                        raise RuntimeError("raw retrieval returned an unexpected part number")
                    local_start = max(offset, part.plaintext_start) - part.plaintext_start
                    local_end = (
                        min(requested_end, part.plaintext_start + part.plaintext_bytes)
                        - part.plaintext_start
                    )
                    downstream_seconds = 0.0
                    try:
                        for chunk_offset in range(
                            local_start,
                            local_end,
                            self._read_working_bytes,
                        ):
                            chunk = retrieved.content[
                                chunk_offset : min(
                                    local_end, chunk_offset + self._read_working_bytes
                                )
                            ]
                            emitted += len(chunk)
                            downstream_started = time.perf_counter()
                            yield chunk
                            downstream_seconds += time.perf_counter() - downstream_started
                    finally:
                        self._byte_budget.release(reservations.pop(expected_index))
                    if self._timing_observer is not None:
                        self._timing_observer(
                            replace(
                                retrieved.timing,
                                elapsed_seconds=(
                                    retrieved.timing.elapsed_seconds + downstream_seconds
                                ),
                                downstream_seconds=downstream_seconds,
                            )
                        )
                    fill(executor)
            except BaseException:
                for future in pending.values():
                    future.cancel()
                raise
            finally:
                for amount in reservations.values():
                    self._byte_budget.release(amount)
                reservations.clear()
        if emitted != size:
            raise RuntimeError("raw retrieval emitted an unexpected byte count")

    def _read_part(
        self,
        *,
        source: RawVolumeRetrievalSource,
        state: UploadState,
        session: ResumableAgeScryptSession,
        part: StoredArchivePart,
        stored_offset: int,
        queue_wait_seconds: float,
    ) -> _RetrievedRawPart:
        started = time.perf_counter()
        request_wait_seconds = 0.0
        raw_chunks: Iterator[bytes]

        def gated_raw_chunks() -> Iterator[bytes]:
            nonlocal request_wait_seconds
            with self._request_gate.reserve() as waited:
                request_wait_seconds = waited
                yield from self._store.iter_object_range(
                    object_path=source.object_path,
                    revision=source.revision,
                    expected_bytes=sum(part.stored_bytes for part in source.parts),
                    offset=stored_offset,
                    size=part.stored_bytes,
                )

        raw_chunks = gated_raw_chunks()
        stored_hasher = hashlib.sha256()
        stored_count = 0
        remote_seconds = 0.0
        prefix_bytes = len(state.header) + len(state.payload_nonce) if part.number == 1 else 0

        def payload_chunks() -> Iterator[bytes]:
            nonlocal stored_count, prefix_bytes, remote_seconds
            skip = prefix_bytes
            source_chunks = iter(raw_chunks)
            while True:
                fetch_started = time.perf_counter()
                try:
                    data = bytes(next(source_chunks))
                except StopIteration:
                    remote_seconds += time.perf_counter() - fetch_started
                    break
                remote_seconds += time.perf_counter() - fetch_started
                if not data:
                    continue
                stored_count += len(data)
                stored_hasher.update(data)
                if skip >= len(data):
                    skip -= len(data)
                    continue
                if skip:
                    data = data[skip:]
                    skip = 0
                if data:
                    yield data
            if skip:
                raise ValueError("raw retrieval first part ended inside the age prefix")

        age_plan = plan_age_plaintext_range(
            age_state=state,
            total_plaintext_bytes=source.plaintext_bytes,
            plaintext_offset=part.plaintext_start,
            plaintext_bytes=part.plaintext_bytes,
        )
        expected_offset = age_plan.ciphertext_offset - (
            len(state.header) + len(state.payload_nonce) if part.number == 1 else 0
        )
        if expected_offset != stored_offset:
            raise ValueError("raw retrieval stored part offset does not match its age range")
        decrypt_started = time.perf_counter()
        content = b"".join(
            iter_decrypt_age_plaintext_range(
                age_state=state,
                plan=age_plan,
                ciphertext_chunks=payload_chunks(),
                session=session,
            )
        )
        decrypt_elapsed = time.perf_counter() - decrypt_started
        crypto_seconds = max(0.0, decrypt_elapsed - remote_seconds)
        if stored_count != part.stored_bytes:
            raise ValueError("raw retrieval stored part byte count mismatch")
        if stored_hasher.hexdigest() != part.stored_sha256:
            raise ValueError("raw retrieval stored part sha256 mismatch")
        if len(content) != part.plaintext_bytes:
            raise ValueError("raw retrieval plaintext part byte count mismatch")
        if hashlib.sha256(content).hexdigest() != part.plaintext_sha256:
            raise ValueError("raw retrieval plaintext part sha256 mismatch")
        return _RetrievedRawPart(
            number=part.number,
            content=content,
            timing=TransferTiming(
                operation="raw_retrieval_part",
                identity=f"{source.volume_id}:{part.number}",
                plaintext_bytes=part.plaintext_bytes,
                stored_bytes=part.stored_bytes,
                queue_wait_seconds=queue_wait_seconds + request_wait_seconds,
                source_seconds=0.0,
                crypto_seconds=crypto_seconds,
                remote_seconds=remote_seconds,
                checkpoint_seconds=0.0,
                elapsed_seconds=time.perf_counter() - started,
            ),
        )


class RawFileRangeReader:
    """Reassemble ordered raw volumes and verify the logical file at delivery."""

    def __init__(self, volume_reader: RawVolumeRangeReader) -> None:
        self._volume_reader = volume_reader

    def iter_file(self, volumes: Sequence[RawVolumeRetrievalSource]) -> Iterator[bytes]:
        yield from self.iter_file_range(volumes, offset=0, size=None)

    def iter_file_range(
        self,
        volumes: Sequence[RawVolumeRetrievalSource],
        *,
        offset: int,
        size: int | None,
    ) -> Iterator[bytes]:
        if not volumes:
            raise ValueError("raw file retrieval requires at least one volume")
        ordered = tuple(sorted(volumes, key=lambda current: current.file_offset))
        path = ordered[0].source_path
        file_bytes = ordered[0].file_bytes
        file_sha256 = ordered[0].file_sha256
        expected_offset = 0
        for volume in ordered:
            if (
                volume.source_path != path
                or volume.file_bytes != file_bytes
                or volume.file_sha256 != file_sha256
                or volume.file_offset != expected_offset
            ):
                raise ValueError("raw file retrieval volumes are not one contiguous file")
            expected_offset += volume.plaintext_bytes
        if expected_offset != file_bytes:
            raise ValueError("raw file retrieval volumes do not cover the file")
        resolved_size = file_bytes - offset if size is None else size
        if offset < 0 or resolved_size < 0 or offset + resolved_size > file_bytes:
            raise ValueError("raw file requested range is invalid")

        requested_end = offset + resolved_size
        digest = hashlib.sha256() if offset == 0 and resolved_size == file_bytes else None
        emitted = 0
        for volume in ordered:
            volume_start = volume.file_offset
            volume_end = volume_start + volume.plaintext_bytes
            if volume_end <= offset or volume_start >= requested_end:
                continue
            local_start = max(offset, volume_start) - volume_start
            local_end = min(requested_end, volume_end) - volume_start
            volume_size = local_end - local_start
            volume_emitted = 0
            for current in self._volume_reader.iter_volume_range(
                volume,
                offset=local_start,
                size=volume_size,
            ):
                emitted += len(current)
                volume_emitted += len(current)
                if digest is not None:
                    digest.update(current)
                yield current
            if volume_emitted != volume_size:
                raise ValueError("raw volume ended before the requested range")
        if emitted != resolved_size:
            raise ValueError("raw file retrieval emitted an unexpected byte count")
        if digest is not None and digest.hexdigest() != file_sha256:
            raise ValueError("raw file retrieval verification failed")
