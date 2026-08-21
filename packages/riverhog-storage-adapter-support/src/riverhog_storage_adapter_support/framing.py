"""Bounded request framing for a JSON declaration followed by opaque bytes."""

from __future__ import annotations

import struct
from collections.abc import Iterable

from pydantic import BaseModel

_HEADER_LENGTH_BYTES = 4
DEFAULT_MAXIMUM_HEADER_BYTES = 32 * 1024


def framed_request(model: BaseModel, content: bytes) -> Iterable[bytes]:
    """Yield one declaration header and the unchanged content without joining them."""

    stored_bytes = getattr(model, "stored_bytes", None)
    if stored_bytes != len(content):
        raise ValueError("framed content length differs from its declaration")
    header = model.model_dump_json(exclude_none=True).encode("utf-8")
    if len(header) > DEFAULT_MAXIMUM_HEADER_BYTES:
        raise ValueError("framed request declaration exceeds its size bound")
    yield struct.pack(">I", len(header))
    yield header
    if content:
        yield content


def parse_framed_request[ModelT: BaseModel](
    body: bytes,
    model: type[ModelT],
    *,
    maximum_header_bytes: int = DEFAULT_MAXIMUM_HEADER_BYTES,
) -> tuple[ModelT, bytes]:
    """Parse one already-bounded request body into its declaration and content."""

    if maximum_header_bytes < 1:
        raise ValueError("framed request header limit must be positive")
    if len(body) < _HEADER_LENGTH_BYTES:
        raise ValueError("framed request is missing its declaration length")
    header_bytes = struct.unpack(">I", body[:_HEADER_LENGTH_BYTES])[0]
    if header_bytes < 2 or header_bytes > maximum_header_bytes:
        raise ValueError("framed request declaration length is invalid")
    content_offset = _HEADER_LENGTH_BYTES + header_bytes
    if content_offset > len(body):
        raise ValueError("framed request declaration is truncated")
    declaration = model.model_validate_json(body[_HEADER_LENGTH_BYTES:content_offset])
    content = body[content_offset:]
    if getattr(declaration, "stored_bytes", None) != len(content):
        raise ValueError("framed content length differs from its declaration")
    return declaration, content


__all__ = [
    "DEFAULT_MAXIMUM_HEADER_BYTES",
    "framed_request",
    "parse_framed_request",
]
