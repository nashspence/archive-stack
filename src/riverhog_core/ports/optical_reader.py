from __future__ import annotations

from collections.abc import Iterator
from typing import Protocol


class OpticalReader(Protocol):
    def read_iter(self, disc_path: str, *, device: str) -> Iterator[bytes]: ...
