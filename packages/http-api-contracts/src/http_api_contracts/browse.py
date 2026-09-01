"""Opaque, bounded mutable-browse continuation tokens."""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import time
import unicodedata
from collections.abc import Callable, Mapping, Sequence
from typing import Annotated

from pydantic import AfterValidator, StringConstraints

type BrowseScalar = str | int | bool | bytes | None

_DOMAIN = b"riverhog-mutable-browse-token/v1\x00"
MAX_BROWSE_QUERY_CHARACTERS = 4096
MAX_BROWSE_TOKEN_BYTES = 8192
_MAX_POSITION_ITEMS = 8
_MAX_POSITION_STRING_BYTES = 4096


def validate_browse_query(value: str) -> str:
    """Accept one bounded canonical visible query without changing its meaning."""

    if value != unicodedata.normalize("NFC", value):
        raise ValueError("browse query must use NFC normalization")
    return value


type BrowseQuery = Annotated[
    str,
    StringConstraints(
        min_length=1,
        max_length=MAX_BROWSE_QUERY_CHARACTERS,
        pattern=r"^\S(?:[\s\S]*\S)?$",
    ),
    AfterValidator(validate_browse_query),
]
type BrowsePageToken = Annotated[
    str,
    StringConstraints(min_length=1, max_length=MAX_BROWSE_TOKEN_BYTES),
]


class BrowseTokenError(ValueError):
    """The supplied continuation is invalid for this browse request."""


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _b64encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _b64decode(value: str) -> bytes:
    try:
        return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    except (binascii.Error, ValueError, UnicodeEncodeError) as exc:
        raise BrowseTokenError("page token encoding is invalid") from exc


def _identity(value: object) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _position(value: object) -> tuple[BrowseScalar, ...]:
    if not isinstance(value, list) or len(value) > _MAX_POSITION_ITEMS:
        raise BrowseTokenError("page token position is invalid")
    parsed: list[BrowseScalar] = []
    for item in value:
        if item is None or isinstance(item, bool):
            parsed.append(item)
            continue
        if isinstance(item, int) and -(2**63) <= item < 2**63:
            parsed.append(item)
            continue
        if isinstance(item, str) and len(item.encode("utf-8")) <= _MAX_POSITION_STRING_BYTES:
            parsed.append(item)
            continue
        if isinstance(item, bytes) and len(item) <= _MAX_POSITION_STRING_BYTES:
            parsed.append(item)
            continue
        if isinstance(item, dict) and set(item) == {"bytes"} and isinstance(item["bytes"], str):
            decoded = _b64decode(item["bytes"])
            if len(decoded) <= _MAX_POSITION_STRING_BYTES:
                parsed.append(decoded)
                continue
        raise BrowseTokenError("page token position contains an invalid value")
    return tuple(parsed)


def _position_payload(value: Sequence[BrowseScalar]) -> list[object]:
    return [{"bytes": _b64encode(item)} if isinstance(item, bytes) else item for item in value]


class BrowseTokenCodec:
    """Issue and verify one application's opaque mutable-browse tokens."""

    def __init__(
        self,
        signing_key: str | bytes,
        *,
        lifetime_seconds: int,
        clock: Callable[[], float] = time.time,
    ) -> None:
        key = signing_key.encode("utf-8") if isinstance(signing_key, str) else signing_key
        if len(key) < 32:
            raise ValueError("browse token signing key must contain at least 32 bytes")
        if lifetime_seconds < 1:
            raise ValueError("browse token lifetime must be positive")
        self._key = bytes(key)
        self._lifetime_seconds = lifetime_seconds
        self._clock = clock

    def issue(
        self,
        *,
        operation: str,
        principal: object,
        selectors: Mapping[str, object],
        position: Sequence[BrowseScalar],
    ) -> str:
        if not operation or len(operation.encode("utf-8")) > 160:
            raise ValueError("browse operation identity is invalid")
        canonical_position = _position(list(position))
        body = _canonical_bytes(
            {
                "expires_at": int(self._clock()) + self._lifetime_seconds,
                "operation": operation,
                "position": _position_payload(canonical_position),
                "principal_sha256": _identity(principal),
                "selectors_sha256": _identity(dict(selectors)),
                "version": 1,
            }
        )
        encoded = _b64encode(body)
        signature = _b64encode(hmac.digest(self._key, _DOMAIN + encoded.encode("ascii"), "sha256"))
        token = f"{encoded}.{signature}"
        if len(token.encode("ascii")) > MAX_BROWSE_TOKEN_BYTES:
            raise ValueError("browse token exceeds its bounded representation")
        return token

    def verify(
        self,
        token: str | None,
        *,
        operation: str,
        principal: object,
        selectors: Mapping[str, object],
    ) -> tuple[BrowseScalar, ...] | None:
        if token is None:
            return None
        if not token or len(token.encode("utf-8")) > MAX_BROWSE_TOKEN_BYTES:
            raise BrowseTokenError("page token is invalid")
        encoded, separator, supplied_signature = token.partition(".")
        if separator != "." or not encoded or not supplied_signature:
            raise BrowseTokenError("page token is invalid")
        try:
            encoded_bytes = encoded.encode("ascii")
        except UnicodeEncodeError as exc:
            raise BrowseTokenError("page token encoding is invalid") from exc
        expected_signature = _b64encode(hmac.digest(self._key, _DOMAIN + encoded_bytes, "sha256"))
        if not hmac.compare_digest(supplied_signature, expected_signature):
            raise BrowseTokenError("page token integrity check failed")
        try:
            payload = json.loads(_b64decode(encoded))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise BrowseTokenError("page token payload is invalid") from exc
        if not isinstance(payload, dict) or set(payload) != {
            "expires_at",
            "operation",
            "position",
            "principal_sha256",
            "selectors_sha256",
            "version",
        }:
            raise BrowseTokenError("page token payload is invalid")
        if payload["version"] != 1 or payload["operation"] != operation:
            raise BrowseTokenError("page token belongs to another operation")
        expires_at = payload["expires_at"]
        if isinstance(expires_at, bool) or not isinstance(expires_at, int):
            raise BrowseTokenError("page token expiry is invalid")
        if expires_at < int(self._clock()):
            raise BrowseTokenError("page token has expired")
        if payload["principal_sha256"] != _identity(principal):
            raise BrowseTokenError("page token belongs to another principal")
        if payload["selectors_sha256"] != _identity(dict(selectors)):
            raise BrowseTokenError("page token selectors differ from this request")
        return _position(payload["position"])


__all__ = [
    "MAX_BROWSE_QUERY_CHARACTERS",
    "MAX_BROWSE_TOKEN_BYTES",
    "BrowsePageToken",
    "BrowseQuery",
    "BrowseScalar",
    "BrowseTokenCodec",
    "BrowseTokenError",
    "validate_browse_query",
]
