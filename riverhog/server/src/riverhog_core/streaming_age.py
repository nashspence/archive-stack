from __future__ import annotations

import hashlib
import threading
import time
from collections import OrderedDict
from collections.abc import Iterable
from dataclasses import dataclass

from riverhog_age import CHUNK_SIZE, ResumableAgeScryptSession, S3PartPlan, UploadState

from riverhog_core.throughput import (
    DEFAULT_AGE_DERIVATION_CONCURRENCY,
    TransferConcurrencyGate,
)


@dataclass(frozen=True, slots=True)
class PreparedAgePart:
    part_number: int
    plaintext_bytes: int
    plaintext_sha256: str
    ciphertext: bytes
    stored_sha256: str
    source_seconds: float
    crypto_seconds: float

    @property
    def stored_bytes(self) -> int:
        return len(self.ciphertext)


@dataclass(slots=True)
class _PendingSession:
    ready: threading.Event
    session: ResumableAgeScryptSession | None = None
    error: BaseException | None = None


class ResumableAgeSessionCache:
    """Bounded single-flight cache for the expensive scrypt-derived age session.

    Standard age passphrase files intentionally perform scrypt once per object. Multipart
    workers must not repeat that derivation for every part. This cache permits independent
    objects to derive concurrently while ensuring that concurrent workers for one object
    share one immutable session. The cached object contains only derived encryption state;
    no plaintext payload is retained.
    """

    def __init__(
        self,
        passphrase: str | bytes,
        *,
        max_entries: int = 128,
        derivation_concurrency: int = DEFAULT_AGE_DERIVATION_CONCURRENCY,
        derivation_gate: TransferConcurrencyGate | None = None,
    ) -> None:
        if not passphrase:
            raise ValueError("age session cache passphrase must not be empty")
        if max_entries < 0 or max_entries > 4096:
            raise ValueError("age session cache entries must be between 0 and 4096")
        self._passphrase = passphrase
        self._max_entries = max_entries
        self._lock = threading.Lock()
        self._sessions: OrderedDict[str, ResumableAgeScryptSession] = OrderedDict()
        self._pending: dict[str, _PendingSession] = {}
        self._derivation_gate = derivation_gate or TransferConcurrencyGate(derivation_concurrency)

    def get(
        self,
        state: UploadState | bytes | str,
    ) -> ResumableAgeScryptSession:
        upload_state = _upload_state(state)
        key = hashlib.sha256(upload_state.to_json_bytes()).hexdigest()
        leader = False
        with self._lock:
            cached = self._sessions.get(key)
            if cached is not None:
                self._sessions.move_to_end(key)
                return cached
            pending = self._pending.get(key)
            if pending is None:
                pending = _PendingSession(ready=threading.Event())
                self._pending[key] = pending
                leader = True

        if not leader:
            pending.ready.wait()
            if pending.error is not None:
                raise RuntimeError("age session derivation failed") from pending.error
            if pending.session is None:
                raise RuntimeError("age session derivation produced no session")
            return pending.session

        try:
            with self._derivation_gate.reserve():
                session = ResumableAgeScryptSession.from_state(
                    self._passphrase,
                    upload_state,
                )
        except BaseException as exc:
            with self._lock:
                pending.error = exc
                self._pending.pop(key, None)
                pending.ready.set()
            raise

        with self._lock:
            pending.session = session
            if self._max_entries:
                self._sessions[key] = session
                self._sessions.move_to_end(key)
                while len(self._sessions) > self._max_entries:
                    self._sessions.popitem(last=False)
            self._pending.pop(key, None)
            pending.ready.set()
        return session

    def remember(
        self,
        state: UploadState | bytes | str,
        session: ResumableAgeScryptSession,
    ) -> None:
        upload_state = _upload_state(state)
        if session.age_prefix != upload_state.header + upload_state.payload_nonce:
            raise ValueError("age session does not match the state being cached")
        if not self._max_entries:
            return
        key = hashlib.sha256(upload_state.to_json_bytes()).hexdigest()
        with self._lock:
            self._sessions[key] = session
            self._sessions.move_to_end(key)
            while len(self._sessions) > self._max_entries:
                self._sessions.popitem(last=False)

    def clear(self) -> None:
        with self._lock:
            self._sessions.clear()


def prepare_age_part(
    *,
    session: ResumableAgeScryptSession,
    plan: S3PartPlan,
    total_plaintext_bytes: int,
    plaintext_chunks: Iterable[bytes],
) -> PreparedAgePart:
    """Encrypt one age/S3 part without materializing its complete plaintext.

    Only the final ciphertext body is buffered because botocore's portable UploadPart API
    needs a replayable body. Plaintext is consumed, hashed, and encrypted in 64 KiB age
    chunks. The source iterator may contribute one configured read chunk of additional
    transient plaintext buffering, but a complete multipart part is never retained in
    plaintext.
    """

    if total_plaintext_bytes < 0:
        raise ValueError("total plaintext bytes must be non-negative")
    expected_plaintext_bytes = plan.plaintext_len
    source = iter(plaintext_chunks)
    buffer = bytearray()
    buffer_offset = 0
    prefix = session.age_prefix if plan.includes_age_prefix else b""
    ciphertext = bytearray(prefix)
    plaintext_hasher = hashlib.sha256()
    stored_hasher = hashlib.sha256()
    if prefix:
        stored_hasher.update(prefix)
    source_seconds = 0.0
    crypto_seconds = 0.0
    consumed = 0

    def buffered_bytes() -> int:
        return len(buffer) - buffer_offset

    def append_source_chunk() -> None:
        nonlocal buffer_offset, source_seconds
        if buffer_offset:
            if buffer_offset == len(buffer):
                buffer.clear()
            else:
                del buffer[:buffer_offset]
            buffer_offset = 0
        started = time.perf_counter()
        try:
            incoming = bytes(next(source))
        except StopIteration as exc:
            raise ValueError("plaintext stream ended before the age part was complete") from exc
        finally:
            source_seconds += time.perf_counter() - started
        if incoming:
            buffer.extend(incoming)

    for chunk_index in range(plan.first_chunk, plan.first_chunk + plan.chunk_count):
        absolute_start = chunk_index * CHUNK_SIZE
        absolute_end = min(absolute_start + CHUNK_SIZE, total_plaintext_bytes)
        if total_plaintext_bytes == 0:
            absolute_start = absolute_end = 0
        needed = absolute_end - absolute_start
        while buffered_bytes() < needed:
            append_source_chunk()
        end = buffer_offset + needed
        plaintext = bytes(buffer[buffer_offset:end])
        buffer_offset = end
        if buffer_offset == len(buffer):
            buffer.clear()
            buffer_offset = 0
        plaintext_hasher.update(plaintext)
        consumed += len(plaintext)
        started = time.perf_counter()
        encrypted = session.encrypt_chunk(
            chunk_index,
            plaintext,
            final=absolute_end == total_plaintext_bytes,
        )
        ciphertext.extend(encrypted)
        stored_hasher.update(encrypted)
        crypto_seconds += time.perf_counter() - started

    if buffered_bytes():
        raise ValueError("plaintext stream is longer than the planned age part")
    source_iterator = iter(source)
    while True:
        started = time.perf_counter()
        try:
            data = bytes(next(source_iterator))
        except StopIteration:
            source_seconds += time.perf_counter() - started
            break
        source_seconds += time.perf_counter() - started
        if data:
            raise ValueError("plaintext stream is longer than the planned age part")
    if consumed != expected_plaintext_bytes:
        raise RuntimeError("prepared age part plaintext byte count mismatch")
    if len(ciphertext) != plan.ciphertext_len:
        raise RuntimeError("prepared age part ciphertext byte count mismatch")
    result = bytes(ciphertext)
    return PreparedAgePart(
        part_number=plan.part_number,
        plaintext_bytes=consumed,
        plaintext_sha256=plaintext_hasher.hexdigest(),
        ciphertext=result,
        stored_sha256=stored_hasher.hexdigest(),
        source_seconds=source_seconds,
        crypto_seconds=crypto_seconds,
    )


def _upload_state(value: UploadState | bytes | str) -> UploadState:
    if isinstance(value, UploadState):
        return value
    return UploadState.from_json_bytes(value)


def iter_rechunk(chunks: Iterable[bytes], *, chunk_bytes: int) -> Iterable[bytes]:
    """Normalize request-body fragments to one operator-selected processing size."""

    if chunk_bytes < 64 * 1024:
        raise ValueError("source read chunk must be at least 64 KiB")
    pending = bytearray()
    for chunk in chunks:
        data = memoryview(bytes(chunk))
        cursor = 0
        if pending:
            take = min(chunk_bytes - len(pending), len(data))
            pending.extend(data[:take])
            cursor += take
            if len(pending) == chunk_bytes:
                yield bytes(pending)
                pending.clear()
        while len(data) - cursor >= chunk_bytes:
            yield bytes(data[cursor : cursor + chunk_bytes])
            cursor += chunk_bytes
        if cursor < len(data):
            pending.extend(data[cursor:])
    if pending:
        yield bytes(pending)
