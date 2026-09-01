from __future__ import annotations

from typing import Any

from sqlalchemy import String
from sqlalchemy.engine.interfaces import Dialect
from sqlalchemy.types import TypeDecorator


class CanonicalUnsignedInteger(TypeDecorator[int]):
    """Persist a bounded unsigned integer as fixed-width lowercase hexadecimal.

    Fixed-width text preserves numeric ordering on SQLite and PostgreSQL without
    narrowing Python's integer semantics to either database's native integer
    range. The representation is a private catalog detail.
    """

    impl = String
    cache_ok = True

    def __init__(self, *, bits: int) -> None:
        if bits < 1:
            raise ValueError("canonical integer width must be positive")
        self.bits = bits
        self.hex_width = (bits + 3) // 4
        super().__init__(length=self.hex_width)

    @property
    def python_type(self) -> type[int]:
        return int

    def process_bind_param(self, value: int | None, dialect: Dialect) -> str | None:
        del dialect
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError("canonical integer value must be an integer")
        if value < 0 or value >= 1 << self.bits:
            raise ValueError(f"canonical integer exceeds its {self.bits}-bit representation")
        return f"{value:0{self.hex_width}x}"

    def process_result_value(self, value: Any, dialect: Dialect) -> int | None:
        del dialect
        if value is None:
            return None
        text = str(value)
        if (
            len(text) != self.hex_width
            or text != text.lower()
            or any(character not in "0123456789abcdef" for character in text)
        ):
            raise ValueError("catalog contains a non-canonical unsigned integer")
        parsed = int(text, 16)
        if parsed >= 1 << self.bits:
            raise ValueError("catalog integer exceeds its configured representation")
        return parsed


def archive_sequence_type() -> CanonicalUnsignedInteger:
    """Return the private database encoding for one v1 archive sequence."""

    return CanonicalUnsignedInteger(bits=256)


def archive_object_order_type() -> CanonicalUnsignedInteger:
    """Return the private ordering encoding for all objects in one v1 archive."""

    # The final recovery descriptor can occupy order 2^258 when both independent
    # sequence domains are exhausted. The fixed 65-hex-digit private encoding
    # therefore uses its complete 260-bit storage domain.
    return CanonicalUnsignedInteger(bits=260)
