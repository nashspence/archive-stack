from __future__ import annotations

from collections.abc import Iterable, Iterator
from dataclasses import dataclass

from riverhog_age import (
    AEAD_TAG_SIZE,
    CHUNK_SIZE,
    ResumableAgeScryptSession,
    UploadState,
    age_chunk_count_for_plaintext_len,
)


@dataclass(frozen=True, slots=True)
class AgePlaintextRange:
    """One exact plaintext range and the authenticated age chunks needed for it."""

    total_plaintext_bytes: int
    plaintext_offset: int
    plaintext_bytes: int
    first_chunk: int
    chunk_count: int
    chunk_plaintext_start: int
    chunk_plaintext_bytes: int
    ciphertext_offset: int
    ciphertext_bytes: int
    trim_start: int
    trim_end: int

    @property
    def last_chunk(self) -> int | None:
        if self.chunk_count == 0:
            return None
        return self.first_chunk + self.chunk_count - 1

    @property
    def plaintext_end(self) -> int:
        return self.plaintext_offset + self.plaintext_bytes

    @property
    def ciphertext_end(self) -> int:
        return self.ciphertext_offset + self.ciphertext_bytes

    @property
    def remote_overfetch_bytes(self) -> int:
        return max(0, self.ciphertext_bytes - self.plaintext_bytes)


def plan_age_plaintext_range(
    *,
    age_state: UploadState | bytes | str,
    total_plaintext_bytes: int,
    plaintext_offset: int,
    plaintext_bytes: int,
) -> AgePlaintextRange:
    """Map a plaintext range to one exact age ciphertext range.

    Standard age v1 payload chunks are independently authenticated. Riverhog can therefore
    fetch only the chunks covering a tar member, decrypt them using the persisted public age
    header and payload nonce, and trim the neighboring plaintext bytes. No plaintext before
    the requested range needs to be downloaded or decrypted.
    """

    state = _upload_state(age_state)
    if total_plaintext_bytes < 0:
        raise ValueError("age range total plaintext bytes must be non-negative")
    if state.plaintext_size != total_plaintext_bytes:
        raise ValueError("age range state plaintext size mismatch")
    if plaintext_offset < 0 or plaintext_bytes < 0:
        raise ValueError("age plaintext range must be non-negative")
    if plaintext_offset + plaintext_bytes > total_plaintext_bytes:
        raise ValueError("age plaintext range exceeds the object")
    if plaintext_bytes == 0:
        return AgePlaintextRange(
            total_plaintext_bytes=total_plaintext_bytes,
            plaintext_offset=plaintext_offset,
            plaintext_bytes=0,
            first_chunk=0,
            chunk_count=0,
            chunk_plaintext_start=plaintext_offset,
            chunk_plaintext_bytes=0,
            ciphertext_offset=0,
            ciphertext_bytes=0,
            trim_start=0,
            trim_end=0,
        )

    final_plaintext_byte = plaintext_offset + plaintext_bytes
    first_chunk = plaintext_offset // CHUNK_SIZE
    last_chunk = (final_plaintext_byte - 1) // CHUNK_SIZE
    chunk_plaintext_start = first_chunk * CHUNK_SIZE
    chunk_plaintext_end = min((last_chunk + 1) * CHUNK_SIZE, total_plaintext_bytes)
    prefix_bytes = len(state.header) + len(state.payload_nonce)
    ciphertext_offset = prefix_bytes + first_chunk * (CHUNK_SIZE + AEAD_TAG_SIZE)
    final_chunk_plaintext_bytes = _chunk_plaintext_bytes(
        total_plaintext_bytes=total_plaintext_bytes,
        chunk_index=last_chunk,
    )
    ciphertext_end = (
        prefix_bytes
        + last_chunk * (CHUNK_SIZE + AEAD_TAG_SIZE)
        + final_chunk_plaintext_bytes
        + AEAD_TAG_SIZE
    )
    return AgePlaintextRange(
        total_plaintext_bytes=total_plaintext_bytes,
        plaintext_offset=plaintext_offset,
        plaintext_bytes=plaintext_bytes,
        first_chunk=first_chunk,
        chunk_count=last_chunk - first_chunk + 1,
        chunk_plaintext_start=chunk_plaintext_start,
        chunk_plaintext_bytes=chunk_plaintext_end - chunk_plaintext_start,
        ciphertext_offset=ciphertext_offset,
        ciphertext_bytes=ciphertext_end - ciphertext_offset,
        trim_start=plaintext_offset - chunk_plaintext_start,
        trim_end=chunk_plaintext_end - final_plaintext_byte,
    )


def iter_decrypt_age_plaintext_range(
    *,
    age_state: UploadState | bytes | str,
    plan: AgePlaintextRange,
    ciphertext_chunks: Iterable[bytes],
    passphrase: str | bytes | None = None,
    session: ResumableAgeScryptSession | None = None,
) -> Iterator[bytes]:
    """Decrypt an exact ciphertext range and yield only the requested plaintext."""

    state = _upload_state(age_state)
    expected = plan_age_plaintext_range(
        age_state=state,
        total_plaintext_bytes=plan.total_plaintext_bytes,
        plaintext_offset=plan.plaintext_offset,
        plaintext_bytes=plan.plaintext_bytes,
    )
    if expected != plan:
        raise ValueError("age plaintext range plan is inconsistent")
    if plan.plaintext_bytes == 0:
        for chunk in ciphertext_chunks:
            if chunk:
                raise ValueError("empty age range received ciphertext")
        return

    if session is None:
        if passphrase is None:
            raise ValueError("age range decryption requires a passphrase or session")
        session = ResumableAgeScryptSession.from_state(passphrase, state)
    elif session.age_prefix != state.header + state.payload_nonce:
        raise ValueError("age range session does not match the persisted state")
    total_chunks = age_chunk_count_for_plaintext_len(plan.total_plaintext_bytes)
    source = iter(ciphertext_chunks)
    buffer = bytearray()
    buffer_offset = 0
    emitted = 0
    requested_end = plan.plaintext_end

    def buffered_bytes() -> int:
        return len(buffer) - buffer_offset

    def append_source_chunk() -> None:
        nonlocal buffer_offset
        if buffer_offset:
            if buffer_offset == len(buffer):
                buffer.clear()
            else:
                del buffer[:buffer_offset]
            buffer_offset = 0
        try:
            incoming = bytes(next(source))
        except StopIteration as exc:
            raise ValueError("age ciphertext range ended before a complete chunk") from exc
        if incoming:
            buffer.extend(incoming)

    for chunk_index in range(plan.first_chunk, plan.first_chunk + plan.chunk_count):
        plaintext_start = chunk_index * CHUNK_SIZE
        plaintext_size = _chunk_plaintext_bytes(
            total_plaintext_bytes=plan.total_plaintext_bytes,
            chunk_index=chunk_index,
        )
        ciphertext_size = plaintext_size + AEAD_TAG_SIZE
        while buffered_bytes() < ciphertext_size:
            append_source_chunk()
        end = buffer_offset + ciphertext_size
        ciphertext = bytes(buffer[buffer_offset:end])
        buffer_offset = end
        if buffer_offset == len(buffer):
            buffer.clear()
            buffer_offset = 0
        plaintext = session.decrypt_chunk(
            chunk_index,
            ciphertext,
            final=chunk_index == total_chunks - 1,
        )
        emit_start = max(plan.plaintext_offset - plaintext_start, 0)
        emit_end = min(requested_end - plaintext_start, len(plaintext))
        if emit_start < emit_end:
            selected = plaintext[emit_start:emit_end]
            emitted += len(selected)
            yield selected

    if buffered_bytes():
        raise ValueError("age ciphertext range contains trailing bytes")
    for chunk in source:
        if chunk:
            raise ValueError("age ciphertext range contains trailing bytes")
    if emitted != plan.plaintext_bytes:
        raise RuntimeError("age plaintext range emitted an unexpected byte count")


def _upload_state(value: UploadState | bytes | str) -> UploadState:
    if isinstance(value, UploadState):
        return value
    return UploadState.from_json_bytes(value)


def _chunk_plaintext_bytes(*, total_plaintext_bytes: int, chunk_index: int) -> int:
    if chunk_index < 0:
        raise ValueError("age chunk index must be non-negative")
    total_chunks = age_chunk_count_for_plaintext_len(total_plaintext_bytes)
    if chunk_index >= total_chunks:
        raise ValueError("age chunk index exceeds the object")
    if total_plaintext_bytes == 0:
        return 0
    start = chunk_index * CHUNK_SIZE
    return min(CHUNK_SIZE, total_plaintext_bytes - start)
