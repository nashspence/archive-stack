from __future__ import annotations

from typing import Protocol


class CryptoPort(Protocol):
    def decrypt_entry(self, encrypted: bytes, enc: dict) -> bytes: ...
