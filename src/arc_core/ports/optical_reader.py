from __future__ import annotations

from typing import Protocol


class OpticalReader(Protocol):
    def read(self, disc_path: str, *, device: str) -> bytes: ...
