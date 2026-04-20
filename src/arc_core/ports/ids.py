from __future__ import annotations

from typing import Protocol


class Ids(Protocol):
    def fetch_id(self) -> str: ...
    def entry_id(self) -> str: ...
