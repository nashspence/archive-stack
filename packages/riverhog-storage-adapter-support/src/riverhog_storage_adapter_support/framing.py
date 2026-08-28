"""Named bounded framing for a JSON declaration followed by opaque bytes."""

from __future__ import annotations

import struct
from collections.abc import Iterable, Iterator

from http_api_contracts import (
    FRAMED_BODY_DECLARATION_LENGTH_BYTES,
    FRAMED_BODY_FORMAT,
    FRAMED_BODY_MAXIMUM_DECLARATION_BYTES,
    FRAMED_BODY_MEDIA_TYPE,
)
from pydantic import BaseModel
from riverhog_storage_adapter_protocol import BinaryContent

DEFAULT_MAXIMUM_HEADER_BYTES = FRAMED_BODY_MAXIMUM_DECLARATION_BYTES


class FramedBodyError(ValueError):
    """The peer's named declaration/payload framing is malformed."""


def framed_declaration_bytes(model: BaseModel) -> bytes:
    """Return the bounded UTF-8 JSON declaration used on the wire."""

    header = model.model_dump_json(exclude_none=True).encode("utf-8")
    if len(header) < 2 or len(header) > DEFAULT_MAXIMUM_HEADER_BYTES:
        raise FramedBodyError("framed body declaration exceeds its size bound")
    return header


def framed_body(model: BaseModel, content: BinaryContent) -> Iterator[bytes]:
    """Yield one declaration and its exact opaque payload without joining them."""

    expected = _declared_content_bytes(model)
    header = framed_declaration_bytes(model)
    yield struct.pack(">I", len(header))
    yield header
    emitted = 0
    for chunk in _content_chunks(content):
        emitted += len(chunk)
        if emitted > expected:
            raise FramedBodyError("framed content exceeds its declared length")
        yield chunk
    if emitted != expected:
        raise FramedBodyError("framed content ended before its declared length")


def framed_body_length(model: BaseModel) -> int:
    """Return the exact framed HTTP body length from its sealed declaration."""

    return (
        FRAMED_BODY_DECLARATION_LENGTH_BYTES
        + len(framed_declaration_bytes(model))
        + _declared_content_bytes(model)
    )


class FramedContent(Iterator[bytes]):
    """Single-pass opaque payload whose consumption is length checked."""

    def __init__(self, chunks: Iterator[bytes], expected_bytes: int) -> None:
        self._chunks = chunks
        self.expected_bytes = expected_bytes
        self.emitted_bytes = 0

    def __iter__(self) -> FramedContent:
        return self

    def __next__(self) -> bytes:
        while True:
            try:
                chunk = next(self._chunks)
            except StopIteration:
                if self.emitted_bytes != self.expected_bytes:
                    raise FramedBodyError(
                        "framed content ended before its declared length"
                    ) from None
                raise
            if not chunk:
                continue
            if not isinstance(chunk, bytes):
                raise TypeError("framed body chunks must be bytes")
            self.emitted_bytes += len(chunk)
            if self.emitted_bytes > self.expected_bytes:
                raise FramedBodyError("framed content exceeds its declared length")
            return chunk

    def require_consumed(self) -> None:
        """Require the capability implementation to have consumed exact custody."""

        if self.emitted_bytes != self.expected_bytes:
            raise ValueError("adapter did not consume the complete framed content")
        try:
            next(self)
        except StopIteration:
            return
        raise FramedBodyError("framed content contains trailing bytes")


def parse_framed_stream[ModelT: BaseModel](
    chunks: Iterable[bytes],
    model: type[ModelT],
    *,
    content_length: int,
    maximum_header_bytes: int = DEFAULT_MAXIMUM_HEADER_BYTES,
) -> tuple[ModelT, FramedContent]:
    """Parse the bounded declaration while leaving opaque bytes single-pass."""

    if maximum_header_bytes < 1:
        raise FramedBodyError("framed body header limit must be positive")
    if content_length < FRAMED_BODY_DECLARATION_LENGTH_BYTES:
        raise FramedBodyError("framed body is missing its declaration length")
    cursor = _ChunkCursor(chunks)
    raw_length = cursor.read_exact(FRAMED_BODY_DECLARATION_LENGTH_BYTES)
    header_bytes = struct.unpack(">I", raw_length)[0]
    if header_bytes < 2 or header_bytes > maximum_header_bytes:
        raise FramedBodyError("framed body declaration length is invalid")
    declaration = model.model_validate_json(cursor.read_exact(header_bytes))
    expected_content = _declared_content_bytes(declaration)
    expected_total = FRAMED_BODY_DECLARATION_LENGTH_BYTES + header_bytes + expected_content
    if content_length != expected_total:
        raise FramedBodyError("framed HTTP content length differs from its declaration")
    return declaration, FramedContent(cursor.remaining(), expected_content)


class _ChunkCursor:
    def __init__(self, chunks: Iterable[bytes]) -> None:
        self._chunks = iter(chunks)
        self._pending = memoryview(b"")

    def read_exact(self, size: int) -> bytes:
        result = bytearray()
        while len(result) < size:
            if not self._pending:
                self._pending = memoryview(self._next_chunk())
            count = min(size - len(result), len(self._pending))
            result.extend(self._pending[:count])
            self._pending = self._pending[count:]
        return bytes(result)

    def remaining(self) -> Iterator[bytes]:
        if self._pending:
            yield bytes(self._pending)
            self._pending = memoryview(b"")
        for chunk in self._chunks:
            if not isinstance(chunk, bytes):
                raise TypeError("framed body chunks must be bytes")
            if chunk:
                yield chunk

    def _next_chunk(self) -> bytes:
        while True:
            try:
                chunk = next(self._chunks)
            except StopIteration:
                raise FramedBodyError("framed body declaration is truncated") from None
            if not isinstance(chunk, bytes):
                raise TypeError("framed body chunks must be bytes")
            if chunk:
                return chunk


def _declared_content_bytes(model: BaseModel) -> int:
    declared_bytes = getattr(model, "stored_bytes", None)
    if declared_bytes is None:
        declared_bytes = getattr(model, "read_bytes", None)
    if isinstance(declared_bytes, bool) or not isinstance(declared_bytes, int):
        raise FramedBodyError("framed body declaration has no byte count")
    if declared_bytes < 0:
        raise FramedBodyError("framed body declaration has a negative byte count")
    return declared_bytes


def _content_chunks(content: BinaryContent) -> Iterator[bytes]:
    chunks: Iterable[bytes] = (content,) if isinstance(content, bytes) else content
    for chunk in chunks:
        if not isinstance(chunk, bytes):
            raise TypeError("opaque content chunks must be bytes")
        if chunk:
            yield chunk


__all__ = [
    "DEFAULT_MAXIMUM_HEADER_BYTES",
    "FRAMED_BODY_FORMAT",
    "FRAMED_BODY_MEDIA_TYPE",
    "FramedContent",
    "FramedBodyError",
    "framed_declaration_bytes",
    "framed_body",
    "framed_body_length",
    "parse_framed_stream",
]
