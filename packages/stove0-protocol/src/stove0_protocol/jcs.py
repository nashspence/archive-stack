"""Small RFC 8785 JSON Canonicalization Scheme encoder.

The implementation lives in the released stove0 protocol package so content
observers, transform targets, and controllers share one cross-language identity
rule. It accepts the I-JSON value subset:
string-keyed objects, finite IEEE-754 numbers, and integers in the interoperable
53-bit range.
"""

from __future__ import annotations

import hashlib

import rfc8785


def canonical_json_bytes(value: object) -> bytes:
    try:
        # The protocol models validate the I-JSON shape before reaching this
        # boundary. rfc8785's private recursive value alias is intentionally
        # narrower than Python's public ``object`` type.
        return rfc8785.dumps(value)  # type: ignore[arg-type]
    except (rfc8785.CanonicalizationError, UnicodeEncodeError) as exc:
        raise ValueError(f"value is not canonical I-JSON: {exc}") from exc


def canonical_json_sha256(value: object) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


__all__ = ["canonical_json_bytes", "canonical_json_sha256"]
