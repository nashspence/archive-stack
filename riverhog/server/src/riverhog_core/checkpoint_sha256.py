"""Private serializable SHA-256 state for bounded restartable server work.

The checkpoint encoding is operational state, not a Riverhog wire or archive contract.
Completed identities are ordinary SHA-256 digests and remain implementation independent.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Self

_MASK = 0xFFFFFFFF
_K = (
    0x428A2F98,
    0x71374491,
    0xB5C0FBCF,
    0xE9B5DBA5,
    0x3956C25B,
    0x59F111F1,
    0x923F82A4,
    0xAB1C5ED5,
    0xD807AA98,
    0x12835B01,
    0x243185BE,
    0x550C7DC3,
    0x72BE5D74,
    0x80DEB1FE,
    0x9BDC06A7,
    0xC19BF174,
    0xE49B69C1,
    0xEFBE4786,
    0x0FC19DC6,
    0x240CA1CC,
    0x2DE92C6F,
    0x4A7484AA,
    0x5CB0A9DC,
    0x76F988DA,
    0x983E5152,
    0xA831C66D,
    0xB00327C8,
    0xBF597FC7,
    0xC6E00BF3,
    0xD5A79147,
    0x06CA6351,
    0x14292967,
    0x27B70A85,
    0x2E1B2138,
    0x4D2C6DFC,
    0x53380D13,
    0x650A7354,
    0x766A0ABB,
    0x81C2C92E,
    0x92722C85,
    0xA2BFE8A1,
    0xA81A664B,
    0xC24B8B70,
    0xC76C51A3,
    0xD192E819,
    0xD6990624,
    0xF40E3585,
    0x106AA070,
    0x19A4C116,
    0x1E376C08,
    0x2748774C,
    0x34B0BCB5,
    0x391C0CB3,
    0x4ED8AA4A,
    0x5B9CCA4F,
    0x682E6FF3,
    0x748F82EE,
    0x78A5636F,
    0x84C87814,
    0x8CC70208,
    0x90BEFFFA,
    0xA4506CEB,
    0xBEF9A3F7,
    0xC67178F2,
)
_INITIAL = (
    0x6A09E667,
    0xBB67AE85,
    0x3C6EF372,
    0xA54FF53A,
    0x510E527F,
    0x9B05688C,
    0x1F83D9AB,
    0x5BE0CD19,
)


def _rotr(value: int, count: int) -> int:
    return ((value >> count) | (value << (32 - count))) & _MASK


@dataclass(slots=True)
class CheckpointSHA256:
    _h: list[int]
    _buffer: bytearray
    _total_bytes: int

    def __init__(self, data: bytes = b"") -> None:
        self._h = list(_INITIAL)
        self._buffer = bytearray()
        self._total_bytes = 0
        self.update(data)

    def copy(self) -> Self:
        other = type(self)()
        other._h = list(self._h)
        other._buffer = bytearray(self._buffer)
        other._total_bytes = self._total_bytes
        return other

    def update(self, data: bytes | bytearray | memoryview) -> Self:
        raw = bytes(data)
        self._total_bytes += len(raw)
        self._buffer.extend(raw)
        while len(self._buffer) >= 64:
            block = bytes(self._buffer[:64])
            del self._buffer[:64]
            self._compress(block)
        return self

    def _compress(self, block: bytes) -> None:
        if len(block) != 64:
            raise ValueError("SHA-256 compression block must be 64 bytes")
        schedule = [0] * 64
        for index in range(16):
            start = index * 4
            schedule[index] = int.from_bytes(block[start : start + 4], "big")
        for index in range(16, 64):
            x = schedule[index - 15]
            y = schedule[index - 2]
            s0 = _rotr(x, 7) ^ _rotr(x, 18) ^ (x >> 3)
            s1 = _rotr(y, 17) ^ _rotr(y, 19) ^ (y >> 10)
            schedule[index] = (schedule[index - 16] + s0 + schedule[index - 7] + s1) & _MASK
        a, b, c, d, e, f, g, h = self._h
        for index in range(64):
            s1 = _rotr(e, 6) ^ _rotr(e, 11) ^ _rotr(e, 25)
            choice = (e & f) ^ ((~e) & g)
            temp1 = (h + s1 + choice + _K[index] + schedule[index]) & _MASK
            s0 = _rotr(a, 2) ^ _rotr(a, 13) ^ _rotr(a, 22)
            majority = (a & b) ^ (a & c) ^ (b & c)
            temp2 = (s0 + majority) & _MASK
            h, g, f, e, d, c, b, a = g, f, e, (d + temp1) & _MASK, c, b, a, (temp1 + temp2) & _MASK
        self._h = [
            (self._h[0] + a) & _MASK,
            (self._h[1] + b) & _MASK,
            (self._h[2] + c) & _MASK,
            (self._h[3] + d) & _MASK,
            (self._h[4] + e) & _MASK,
            (self._h[5] + f) & _MASK,
            (self._h[6] + g) & _MASK,
            (self._h[7] + h) & _MASK,
        ]

    def digest(self) -> bytes:
        clone = self.copy()
        bit_length = clone._total_bytes * 8
        clone._buffer.append(0x80)
        while len(clone._buffer) % 64 != 56:
            clone._buffer.append(0)
        clone._buffer.extend(bit_length.to_bytes(8, "big"))
        while clone._buffer:
            block = bytes(clone._buffer[:64])
            del clone._buffer[:64]
            clone._compress(block)
        return b"".join(value.to_bytes(4, "big") for value in clone._h)

    def hexdigest(self) -> str:
        return self.digest().hex()

    def export_state(self) -> str:
        return json.dumps(
            {
                "format": "riverhog-private-sha256-checkpoint/v1",
                "h": self._h,
                "buffer_hex": bytes(self._buffer).hex(),
                "total_bytes": self._total_bytes,
            },
            sort_keys=True,
            separators=(",", ":"),
        )

    @classmethod
    def from_state(cls, state: str) -> Self:
        try:
            payload = json.loads(state)
            if payload.get("format") != "riverhog-private-sha256-checkpoint/v1":
                raise ValueError("unsupported SHA-256 checkpoint format")
            h = payload["h"]
            buffer = bytes.fromhex(payload["buffer_hex"])
            total_bytes = payload["total_bytes"]
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ValueError("invalid SHA-256 checkpoint") from exc
        if (
            not isinstance(h, list)
            or len(h) != 8
            or any(not isinstance(value, int) or not 0 <= value <= _MASK for value in h)
            or not isinstance(total_bytes, int)
            or total_bytes < 0
            or len(buffer) >= 64
            or total_bytes % 64 != len(buffer)
        ):
            raise ValueError("invalid SHA-256 checkpoint")
        instance = cls()
        instance._h = list(h)
        instance._buffer = bytearray(buffer)
        instance._total_bytes = total_bytes
        return instance
