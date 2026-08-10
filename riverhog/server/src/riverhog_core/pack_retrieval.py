from __future__ import annotations

import hashlib
import re
import time
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from dataclasses import dataclass

from riverhog_age import AEAD_TAG_SIZE, CHUNK_SIZE, ResumableAgeScryptSession, UploadState
from riverhog_protocol.paths import normalize_relpath

from riverhog_core.age_range import (
    iter_decrypt_age_plaintext_range,
    plan_age_plaintext_range,
)
from riverhog_core.pack_volume import RESERVED_ARCHIVE_PREFIX
from riverhog_core.ports.archive_range_store import ArchiveObjectRangeStore
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

BILLING_MODE_RETURNED_BYTES = "returned_bytes"
BILLING_MODE_WHOLE_OBJECT = "whole_object"
DEFAULT_MAX_RANGE_REQUEST_BYTES = 64 * 1024 * 1024
_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_VOLUME_ID_RE = re.compile(r"pack-[0-9]{12}")


@dataclass(frozen=True, slots=True)
class PackVolumeRetrievalSource:
    volume_id: str
    object_path: str
    version_id: str | None
    plaintext_bytes: int
    stored_bytes: int
    age_state_json: str

    def __post_init__(self) -> None:
        if _VOLUME_ID_RE.fullmatch(self.volume_id) is None:
            raise ValueError("pack retrieval volume id is invalid")
        if not self.object_path or self.object_path.startswith("/"):
            raise ValueError("pack retrieval object path is invalid")
        if self.plaintext_bytes <= 0 or self.stored_bytes <= 0:
            raise ValueError("pack retrieval volume sizes must be positive")
        state = UploadState.from_json_bytes(self.age_state_json)
        if state.plaintext_size != self.plaintext_bytes:
            raise ValueError("pack retrieval age state plaintext size mismatch")
        if state.to_json_bytes().decode("utf-8") != self.age_state_json:
            raise ValueError("pack retrieval age state JSON is not canonical")


@dataclass(frozen=True, slots=True)
class PackMemberRetrievalSource:
    path: str
    bytes: int
    sha256: str
    data_offset: int

    def __post_init__(self) -> None:
        normalized = normalize_relpath(self.path)
        if normalized != self.path or normalized.startswith(RESERVED_ARCHIVE_PREFIX):
            raise ValueError("pack retrieval member path is invalid")
        if self.bytes < 0 or self.data_offset < 0:
            raise ValueError("pack retrieval member range is invalid")
        if _SHA256_RE.fullmatch(self.sha256) is None:
            raise ValueError("pack retrieval member sha256 is invalid")


@dataclass(frozen=True, slots=True)
class PackMemberRange:
    path: str
    bytes: int
    sha256: str
    data_offset: int
    ciphertext_offset: int
    ciphertext_bytes: int
    first_chunk: int
    chunk_count: int

    @property
    def ciphertext_end(self) -> int:
        return self.ciphertext_offset + self.ciphertext_bytes


@dataclass(frozen=True, slots=True)
class PackCiphertextRequest:
    number: int
    ciphertext_offset: int
    ciphertext_bytes: int
    first_chunk: int
    last_chunk: int
    member_paths: tuple[str, ...]

    @property
    def ciphertext_end(self) -> int:
        return self.ciphertext_offset + self.ciphertext_bytes


@dataclass(frozen=True, slots=True)
class PackRangeRetrievalPolicy:
    merge_gap_ciphertext_bytes: int = 0
    max_request_ciphertext_bytes: int = DEFAULT_MAX_RANGE_REQUEST_BYTES
    billing_mode: str = BILLING_MODE_RETURNED_BYTES

    def __post_init__(self) -> None:
        if self.merge_gap_ciphertext_bytes < 0:
            raise ValueError("pack range merge gap must be non-negative")
        if self.max_request_ciphertext_bytes < CHUNK_SIZE + AEAD_TAG_SIZE:
            raise ValueError("pack range request limit is below one age chunk")
        if self.billing_mode not in {
            BILLING_MODE_RETURNED_BYTES,
            BILLING_MODE_WHOLE_OBJECT,
        }:
            raise ValueError("pack range billing mode is invalid")

    @classmethod
    def from_env(
        cls,
        values: Mapping[str, str],
        *,
        store_name: str | None = None,
    ) -> PackRangeRetrievalPolicy:
        return cls(
            merge_gap_ciphertext_bytes=_scoped_env_bytes(
                values,
                "RIVERHOG_RETRIEVAL_RANGE_MERGE_GAP_BYTES",
                0,
                store_name=store_name,
            ),
            max_request_ciphertext_bytes=_scoped_env_bytes(
                values,
                "RIVERHOG_RETRIEVAL_MAX_RANGE_BYTES",
                DEFAULT_MAX_RANGE_REQUEST_BYTES,
                store_name=store_name,
            ),
            billing_mode=_scoped_env_value(
                values,
                "RIVERHOG_RETRIEVAL_RANGE_BILLING_MODE",
                BILLING_MODE_RETURNED_BYTES,
                store_name=store_name,
            )
            .strip()
            .casefold(),
        )


@dataclass(frozen=True, slots=True)
class PackRangeRetrievalPlan:
    source: PackVolumeRetrievalSource
    policy: PackRangeRetrievalPolicy
    members: tuple[PackMemberRange, ...]
    requests: tuple[PackCiphertextRequest, ...]
    logical_bytes: int
    remote_bytes: int
    accounted_remote_bytes: int

    @property
    def request_count(self) -> int:
        return len(self.requests)

    @property
    def remote_overfetch_bytes(self) -> int:
        return max(0, self.remote_bytes - self.logical_bytes)


def plan_pack_range_retrieval(
    source: PackVolumeRetrievalSource,
    members: Sequence[PackMemberRetrievalSource],
    *,
    policy: PackRangeRetrievalPolicy | None = None,
) -> PackRangeRetrievalPlan:
    """Plan bounded authenticated range reads for selected members of one pack."""

    effective_policy = policy or PackRangeRetrievalPolicy()
    seen: set[str] = set()
    planned_members: list[PackMemberRange] = []
    for member in sorted(members, key=lambda current: (current.data_offset, current.path)):
        if member.path in seen:
            raise ValueError(f"pack range retrieval repeats member path: {member.path}")
        if member.data_offset + member.bytes > source.plaintext_bytes:
            raise ValueError(f"pack member range exceeds its volume: {member.path}")
        seen.add(member.path)
        range_plan = plan_age_plaintext_range(
            age_state=source.age_state_json,
            total_plaintext_bytes=source.plaintext_bytes,
            plaintext_offset=member.data_offset,
            plaintext_bytes=member.bytes,
        )
        planned_members.append(
            PackMemberRange(
                path=member.path,
                bytes=member.bytes,
                sha256=member.sha256,
                data_offset=member.data_offset,
                ciphertext_offset=range_plan.ciphertext_offset,
                ciphertext_bytes=range_plan.ciphertext_bytes,
                first_chunk=range_plan.first_chunk,
                chunk_count=range_plan.chunk_count,
            )
        )

    requests = _coalesced_requests(planned_members, policy=effective_policy)
    logical_bytes = sum(current.bytes for current in planned_members)
    remote_bytes = sum(current.ciphertext_bytes for current in requests)
    accounted_remote_bytes = (
        source.stored_bytes
        if requests and effective_policy.billing_mode == BILLING_MODE_WHOLE_OBJECT
        else remote_bytes
    )
    return PackRangeRetrievalPlan(
        source=source,
        policy=effective_policy,
        members=tuple(planned_members),
        requests=requests,
        logical_bytes=logical_bytes,
        remote_bytes=remote_bytes,
        accounted_remote_bytes=accounted_remote_bytes,
    )


class PackMemberRangeReader:
    """Stream one packed file from exact age-chunk-aligned object ranges."""

    def __init__(
        self,
        store: ArchiveObjectRangeStore,
        *,
        passphrase: str | bytes,
        max_inflight_bytes: int = DEFAULT_RETRIEVAL_MAX_INFLIGHT_BYTES,
        read_working_bytes: int = DEFAULT_RETRIEVAL_READ_CHUNK_BYTES,
        byte_budget: WeightedByteSemaphore | None = None,
        request_gate: TransferConcurrencyGate | None = None,
        resources: ArchiveTransferResources | None = None,
        timing_observer: Callable[[TransferTiming], None] | None = None,
        session_cache: ResumableAgeSessionCache | None = None,
        session_cache_entries: int = DEFAULT_AGE_SESSION_CACHE_ENTRIES,
        policy: PackRangeRetrievalPolicy | None = None,
    ) -> None:
        if read_working_bytes < 64 * 1024:
            raise ValueError("pack retrieval working buffer must be at least 64 KiB")
        if resources is not None and (byte_budget is not None or request_gate is not None):
            raise ValueError(
                "shared transfer resources cannot be combined with explicit retrieval limits"
            )
        self._store = store
        self._session_cache = session_cache or ResumableAgeSessionCache(
            passphrase,
            max_entries=session_cache_entries,
            derivation_gate=(resources.age_derivations if resources is not None else None),
        )
        self._read_working_bytes = read_working_bytes
        self._byte_budget = (
            resources.retrieval_bytes
            if resources is not None
            else byte_budget or WeightedByteSemaphore(max_inflight_bytes)
        )
        self._request_gate = (
            resources.retrieval_requests
            if resources is not None
            else request_gate or TransferConcurrencyGate(1)
        )
        self._timing_observer = timing_observer
        self._policy = policy or PackRangeRetrievalPolicy()

    def iter_member(
        self,
        source: PackVolumeRetrievalSource,
        member: PackMemberRetrievalSource,
    ) -> Iterator[bytes]:
        started = time.perf_counter()
        plan = plan_pack_range_retrieval(source, (member,), policy=self._policy)
        if not plan.members:
            return
        current = plan.members[0]
        if current.bytes == 0:
            _require_verified_member(current, b"")
            if self._timing_observer is not None:
                self._timing_observer(
                    TransferTiming(
                        operation="pack_retrieval_member",
                        identity=f"{source.volume_id}:{current.path}",
                        plaintext_bytes=0,
                        stored_bytes=0,
                        queue_wait_seconds=0.0,
                        source_seconds=0.0,
                        crypto_seconds=0.0,
                        remote_seconds=0.0,
                        checkpoint_seconds=0.0,
                        elapsed_seconds=time.perf_counter() - started,
                    )
                )
            return
        age_plan = plan_age_plaintext_range(
            age_state=source.age_state_json,
            total_plaintext_bytes=source.plaintext_bytes,
            plaintext_offset=current.data_offset,
            plaintext_bytes=current.bytes,
        )
        reserve_bytes = (
            min(
                age_plan.ciphertext_bytes,
                self._read_working_bytes,
            )
            + CHUNK_SIZE
        )
        remote_seconds = 0.0
        crypto_seconds = 0.0
        downstream_seconds = 0.0
        emitted = 0
        digest = hashlib.sha256()
        with self._byte_budget.reserve(max(1, reserve_bytes)) as byte_wait_seconds:
            with self._request_gate.reserve() as request_wait_seconds:
                raw = self._store.iter_object_range(
                    object_path=source.object_path,
                    version_id=source.version_id,
                    offset=age_plan.ciphertext_offset,
                    size=age_plan.ciphertext_bytes,
                )

                def timed_ciphertext() -> Iterator[bytes]:
                    nonlocal remote_seconds
                    source_chunks = iter(raw)
                    while True:
                        fetch_started = time.perf_counter()
                        try:
                            data = bytes(next(source_chunks))
                        except StopIteration:
                            remote_seconds += time.perf_counter() - fetch_started
                            return
                        remote_seconds += time.perf_counter() - fetch_started
                        if data:
                            yield data

                decrypted = iter(
                    iter_decrypt_age_plaintext_range(
                        session=self._session_cache.get(source.age_state_json),
                        age_state=source.age_state_json,
                        plan=age_plan,
                        ciphertext_chunks=timed_ciphertext(),
                    )
                )
                while True:
                    remote_before = remote_seconds
                    decrypt_started = time.perf_counter()
                    try:
                        chunk = bytes(next(decrypted))
                    except StopIteration:
                        crypto_seconds += max(
                            0.0,
                            time.perf_counter()
                            - decrypt_started
                            - (remote_seconds - remote_before),
                        )
                        break
                    crypto_seconds += max(
                        0.0,
                        time.perf_counter() - decrypt_started - (remote_seconds - remote_before),
                    )
                    emitted += len(chunk)
                    if emitted > current.bytes:
                        raise ValueError(
                            f"pack member range is longer than declared: {current.path}"
                        )
                    digest.update(chunk)
                    downstream_started = time.perf_counter()
                    yield chunk
                    downstream_seconds += time.perf_counter() - downstream_started
        if emitted != current.bytes or digest.hexdigest() != current.sha256:
            raise ValueError(f"pack member range verification failed: {current.path}")
        if self._timing_observer is not None:
            self._timing_observer(
                TransferTiming(
                    operation="pack_retrieval_member",
                    identity=f"{source.volume_id}:{current.path}",
                    plaintext_bytes=current.bytes,
                    stored_bytes=age_plan.ciphertext_bytes,
                    queue_wait_seconds=byte_wait_seconds + request_wait_seconds,
                    source_seconds=0.0,
                    crypto_seconds=crypto_seconds,
                    remote_seconds=remote_seconds,
                    checkpoint_seconds=0.0,
                    elapsed_seconds=time.perf_counter() - started,
                    downstream_seconds=downstream_seconds,
                )
            )


@dataclass(frozen=True, slots=True)
class _PackRequestResult:
    members: dict[str, bytes]
    timing: TransferTiming


class PackRangeBatchReader:
    """Read several members with bounded request coalescing and parallel range I/O."""

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
        timing_observer: Callable[[TransferTiming], None] | None = None,
        session_cache: ResumableAgeSessionCache | None = None,
        session_cache_entries: int = DEFAULT_AGE_SESSION_CACHE_ENTRIES,
    ) -> None:
        if request_concurrency < 1:
            raise ValueError("pack retrieval request concurrency must be positive")
        if read_working_bytes < 64 * 1024:
            raise ValueError("pack retrieval working buffer must be at least 64 KiB")
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

    def read_members(
        self,
        plan: PackRangeRetrievalPlan,
    ) -> dict[str, bytes]:
        expected = plan_pack_range_retrieval(
            plan.source,
            tuple(
                PackMemberRetrievalSource(
                    path=current.path,
                    bytes=current.bytes,
                    sha256=current.sha256,
                    data_offset=current.data_offset,
                )
                for current in plan.members
            ),
            policy=plan.policy,
        )
        if expected != plan:
            raise ValueError("pack range retrieval plan is inconsistent")
        if not plan.members:
            return {}

        members_by_path = {current.path: current for current in plan.members}
        out: dict[str, bytes] = {}
        state = UploadState.from_json_bytes(plan.source.age_state_json)
        session = self._session_cache.get(state)
        prefix_bytes = len(state.header) + len(state.payload_nonce)
        for member in plan.members:
            if member.bytes == 0:
                _require_verified_member(member, b"")
                out[member.path] = b""

        futures: list[Future[_PackRequestResult]] = []
        with ThreadPoolExecutor(
            max_workers=min(self._request_concurrency, max(1, len(plan.requests))),
            thread_name_prefix="riverhog-pack-read",
        ) as executor:
            for request in plan.requests:
                futures.append(
                    executor.submit(
                        self._read_request,
                        plan=plan,
                        request=request,
                        members_by_path=members_by_path,
                        state=state,
                        session=session,
                        prefix_bytes=prefix_bytes,
                    )
                )
            try:
                for future in as_completed(futures):
                    result = future.result()
                    overlap = set(out).intersection(result.members)
                    if overlap:
                        raise RuntimeError(
                            "pack range retrieval returned duplicate members: "
                            + ", ".join(sorted(overlap))
                        )
                    out.update(result.members)
                    if self._timing_observer is not None:
                        self._timing_observer(result.timing)
            except BaseException:
                for future in futures:
                    future.cancel()
                raise

        if set(out) != set(members_by_path):
            raise RuntimeError("pack range retrieval did not return every requested member")
        return out

    def _read_request(
        self,
        *,
        plan: PackRangeRetrievalPlan,
        request: PackCiphertextRequest,
        members_by_path: dict[str, PackMemberRange],
        state: UploadState,
        session: ResumableAgeScryptSession,
        prefix_bytes: int,
    ) -> _PackRequestResult:
        started = time.perf_counter()
        plaintext_start = request.first_chunk * CHUNK_SIZE
        plaintext_end = min(
            (request.last_chunk + 1) * CHUNK_SIZE,
            plan.source.plaintext_bytes,
        )
        plaintext_bytes = plaintext_end - plaintext_start
        reserve_bytes = plaintext_bytes + min(
            request.ciphertext_bytes,
            self._read_working_bytes,
        )
        with self._byte_budget.reserve(max(1, reserve_bytes)) as byte_wait_seconds:
            broad_plan = plan_age_plaintext_range(
                age_state=state,
                total_plaintext_bytes=plan.source.plaintext_bytes,
                plaintext_offset=plaintext_start,
                plaintext_bytes=plaintext_bytes,
            )
            if (
                broad_plan.ciphertext_offset != request.ciphertext_offset
                or broad_plan.ciphertext_bytes != request.ciphertext_bytes
                or broad_plan.ciphertext_offset
                != prefix_bytes + request.first_chunk * (CHUNK_SIZE + AEAD_TAG_SIZE)
            ):
                raise ValueError("pack coalesced request is not age-chunk aligned")
            with self._request_gate.reserve() as request_wait_seconds:
                raw = self._store.iter_object_range(
                    object_path=plan.source.object_path,
                    version_id=plan.source.version_id,
                    offset=request.ciphertext_offset,
                    size=request.ciphertext_bytes,
                )
                remote_seconds = 0.0
                remote_bytes = 0

                def timed_chunks() -> Iterator[bytes]:
                    nonlocal remote_seconds, remote_bytes
                    source = iter(raw)
                    while True:
                        fetch_started = time.perf_counter()
                        try:
                            chunk = bytes(next(source))
                        except StopIteration:
                            remote_seconds += time.perf_counter() - fetch_started
                            return
                        remote_seconds += time.perf_counter() - fetch_started
                        remote_bytes += len(chunk)
                        yield chunk

                decrypt_started = time.perf_counter()
                plaintext = b"".join(
                    iter_decrypt_age_plaintext_range(
                        age_state=state,
                        plan=broad_plan,
                        ciphertext_chunks=timed_chunks(),
                        session=session,
                    )
                )
                decrypt_elapsed = time.perf_counter() - decrypt_started
        queue_wait_seconds = byte_wait_seconds + request_wait_seconds
        if remote_bytes != request.ciphertext_bytes:
            raise ValueError("pack range response byte count mismatch")
        selected: dict[str, bytes] = {}
        for path in request.member_paths:
            member = members_by_path[path]
            relative_start = member.data_offset - plaintext_start
            relative_end = relative_start + member.bytes
            if relative_start < 0 or relative_end > len(plaintext):
                raise RuntimeError("pack member is outside its coalesced plaintext range")
            content = plaintext[relative_start:relative_end]
            _require_verified_member(member, content)
            selected[path] = content
        return _PackRequestResult(
            members=selected,
            timing=TransferTiming(
                operation="pack_retrieval_range",
                identity=f"{plan.source.volume_id}:{request.number}",
                plaintext_bytes=plaintext_bytes,
                stored_bytes=request.ciphertext_bytes,
                queue_wait_seconds=queue_wait_seconds,
                source_seconds=0.0,
                crypto_seconds=max(0.0, decrypt_elapsed - remote_seconds),
                remote_seconds=remote_seconds,
                checkpoint_seconds=0.0,
                elapsed_seconds=time.perf_counter() - started,
            ),
        )


def _coalesced_requests(
    members: Sequence[PackMemberRange],
    *,
    policy: PackRangeRetrievalPolicy,
) -> tuple[PackCiphertextRequest, ...]:
    nonempty = [current for current in members if current.chunk_count > 0]
    if not nonempty:
        return ()
    oversized = [
        current.path
        for current in nonempty
        if current.ciphertext_bytes > policy.max_request_ciphertext_bytes
    ]
    if oversized:
        raise ValueError(
            "pack member range exceeds the configured request bound: " + ", ".join(oversized)
        )

    requests: list[PackCiphertextRequest] = []
    first = nonempty[0]
    start = first.ciphertext_offset
    end = first.ciphertext_end
    first_chunk = first.first_chunk
    last_chunk = _required_last_chunk(first)
    paths = [first.path]

    for current in nonempty[1:]:
        current_end = current.ciphertext_end
        gap = max(0, current.ciphertext_offset - end)
        merged_end = max(end, current_end)
        merged_bytes = merged_end - start
        overlaps = current.ciphertext_offset <= end
        merge = (
            overlaps or gap <= policy.merge_gap_ciphertext_bytes
        ) and merged_bytes <= policy.max_request_ciphertext_bytes
        if merge:
            end = merged_end
            last_chunk = max(last_chunk, _required_last_chunk(current))
            paths.append(current.path)
            continue
        requests.append(
            PackCiphertextRequest(
                number=len(requests) + 1,
                ciphertext_offset=start,
                ciphertext_bytes=end - start,
                first_chunk=first_chunk,
                last_chunk=last_chunk,
                member_paths=tuple(paths),
            )
        )
        start = current.ciphertext_offset
        end = current_end
        first_chunk = current.first_chunk
        last_chunk = _required_last_chunk(current)
        paths = [current.path]

    requests.append(
        PackCiphertextRequest(
            number=len(requests) + 1,
            ciphertext_offset=start,
            ciphertext_bytes=end - start,
            first_chunk=first_chunk,
            last_chunk=last_chunk,
            member_paths=tuple(paths),
        )
    )
    return tuple(requests)


def _required_last_chunk(member: PackMemberRange) -> int:
    if member.chunk_count <= 0:
        raise ValueError("empty pack member has no required age chunk")
    return member.first_chunk + member.chunk_count - 1


def _verify_member_bytes(
    member: PackMemberRange,
    chunks: Iterable[bytes],
) -> Iterator[bytes]:
    digest = hashlib.sha256()
    byte_count = 0
    for chunk in chunks:
        data = bytes(chunk)
        if not data:
            continue
        byte_count += len(data)
        if byte_count > member.bytes:
            raise ValueError(f"pack member range is longer than declared: {member.path}")
        digest.update(data)
        yield data
    if byte_count != member.bytes or digest.hexdigest() != member.sha256:
        raise ValueError(f"pack member range verification failed: {member.path}")


def _require_verified_member(member: PackMemberRange, content: bytes) -> None:
    if len(content) != member.bytes or hashlib.sha256(content).hexdigest() != member.sha256:
        raise ValueError(f"pack member range verification failed: {member.path}")


def _read_exact(chunks: Iterable[bytes], *, expected_bytes: int, label: str) -> bytes:
    out = bytearray()
    for chunk in chunks:
        data = bytes(chunk)
        if not data:
            continue
        out.extend(data)
        if len(out) > expected_bytes:
            raise ValueError(f"{label} is longer than declared")
    if len(out) != expected_bytes:
        raise ValueError(f"{label} byte count mismatch")
    return bytes(out)


def _scoped_env_value(
    values: Mapping[str, str],
    global_name: str,
    default: str,
    *,
    store_name: str | None,
) -> str:
    if store_name is not None:
        normalized = store_name.strip().casefold()
        if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", normalized):
            raise ValueError("archive store name is invalid for retrieval tuning")
        suffix = normalized.upper().replace("-", "_")
        setting = global_name.removeprefix("RIVERHOG_")
        scoped_name = f"RIVERHOG_ARCHIVE_STORE_{suffix}_{setting}"
        scoped = values.get(scoped_name)
        if scoped is not None and scoped.strip():
            return scoped
    value = values.get(global_name)
    return value if value is not None and value.strip() else default


def _scoped_env_bytes(
    values: Mapping[str, str],
    global_name: str,
    default: int,
    *,
    store_name: str | None,
) -> int:
    raw = _scoped_env_value(
        values,
        global_name,
        str(default),
        store_name=store_name,
    )
    return _parse_bytes(raw, global_name)


def _parse_bytes(raw: str, name: str) -> int:
    candidate = raw.strip().casefold().replace(" ", "")
    units = {
        "b": 1,
        "kib": 1024,
        "mib": 1024**2,
        "gib": 1024**3,
        "kb": 1000,
        "mb": 1000**2,
        "gb": 1000**3,
    }
    for suffix in sorted(units, key=len, reverse=True):
        if candidate.endswith(suffix):
            number = candidate[: -len(suffix)]
            break
    else:
        suffix = "b"
        number = candidate
    try:
        value = int(number) * units[suffix]
    except (KeyError, ValueError) as exc:
        raise ValueError(f"{name} must be a byte size such as 64MiB") from exc
    if value < 0:
        raise ValueError(f"{name} must be non-negative")
    return value


def _env_bytes(values: Mapping[str, str], name: str, default: int) -> int:
    raw = values.get(name)
    if raw is None or not raw.strip():
        return default
    return _parse_bytes(raw, name)
